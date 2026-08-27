"""Internal probes: synthetic inputs only; never write to the real Jiandaoyun API."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from unittest.mock import patch
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from taoran_agent import api
from taoran_agent.cli import _synthetic_visit
from taoran_agent.config import get_settings
from taoran_agent.connector import load_jiandaoyun_mapping
from taoran_agent.models import PostEvaluationRequest


def synthetic_visit():
    visit = _synthetic_visit(0).model_dump(mode="json", exclude_none=True)
    visit.update({
        "opportunities": [{"opportunity_id": "INTERNAL-OPP", "current_stage": "P3"}],
        "participants": [{"contact_id": "INTERNAL-CONTACT"}],
        "actual_start_at": "2026-08-25T09:00:00+08:00",
        "actual_end_at": "2026-08-25T10:00:00+08:00",
        "submitted_at": "2026-08-25T11:00:00+08:00",
        "evidence_ids": [],
    })
    return visit


def live_buttons(base):
    settings = get_settings()
    headers = {"X-API-Key": settings.tenant_keys["tenant_demo"],
               "bypass-tunnel-reminder": "true"}
    endpoint = base.rstrip("/") + "/api/v1/connectors/jiandaoyun/visit/button-check"
    cases = [
        ("完整输入", {}, (), 200, "R｜过程事实与结果：达标"),
        ("相同表单重复检测", {}, (), 200, "R｜过程事实与结果：达标"),
        ("日期早于", {"next_contact_at": "2026-08-24T10:00:00+08:00"}, (), 200, "时间安排：异常"),
        ("日期同日", {"next_contact_at": "2026-08-25T10:00:00+08:00"}, (), 200, "时间安排：异常"),
        ("修改日期后重检", {"next_contact_at": "2026-08-28T10:00:00+08:00"}, (), 200, "N｜下一步客户行动：达标"),
        ("过程清空", {"process_description": ""}, (), 200, "R｜过程事实与结果：未达标"),
        ("过程漏传", {}, ("process_description",), 200, "接口未获取：“过程详细描述”"),
        ("子表格式错误", {"opportunities": "not json"}, (), 422, ""),
    ]
    results, ids = [], set()
    with httpx.Client(timeout=15, follow_redirects=False) as client:
        denied = client.post(endpoint, json={"tenant_id": "tenant_demo"},
                             headers={"bypass-tunnel-reminder": "true"})
        assert denied.status_code == 401
        for name, changes, omitted, code, expected in cases:
            payload = {"tenant_id": "tenant_demo", **synthetic_visit(), **changes}
            for field in omitted:
                payload.pop(field, None)
            start = monotonic()
            response = client.post(endpoint, json=payload, headers=headers)
            elapsed = round((monotonic() - start)*1000, 2)
            assert response.status_code == code, (name, response.status_code)
            result = response.json()
            if code == 200:
                assert result["official_score_generated"] is False
                assert result["can_submit"] and not result["submission_blocked"]
                assert not {"q33_score", "q34_score", "total_score"} & result.keys()
                feedback = result["feedback_text"]
                assert expected in feedback, name
                assert feedback.count("检查标准：") == 6, name
                assert not any(t in feedback for t in ["知识依据", "分析方式", "本检查不会阻断"])
                assert result["check_id"] not in ids
                ids.add(result["check_id"])
            results.append({"case": name, "http": code, "elapsed_ms": elapsed,
                            "status": result.get("status", "invalid_input")})
    return {"base": base, "unauthorized_http": 401, "cases": results}


def live_async(base):
    """One real model call through the running HTTP service, without a writeback target."""
    settings = get_settings()
    run_id = "internal-" + uuid4().hex
    req = PostEvaluationRequest.model_validate({
        "context": {"tenant_id": "tenant_demo", "user_id": "internal-test",
                    "request_id": run_id, "source": "test"},
        "visit_record_code": run_id, "visit": synthetic_visit(),
    })
    headers = {"X-API-Key": settings.tenant_keys["tenant_demo"]}
    with httpx.Client(base_url=base, headers=headers, timeout=12, follow_redirects=False) as client:
        start = monotonic()
        accepted = client.post("/api/v1/visit/evaluations", json=req.model_dump(mode="json"))
        ack_ms = round((monotonic()-start)*1000, 2)
        assert accepted.status_code == 202
        job = accepted.json()["job_id"]
        while monotonic()-start < 60:
            record = client.get(f"/api/v1/visit/evaluations/{job}",
                                params={"tenant_id": "tenant_demo"}).json()
            if record["status"] in {"completed", "failed"}:
                break
            sleep(1)
        assert record["status"] == "completed"
        result = record["response"]
        assert result["semantic_facts"]["status"] == "completed"
        assert result["total_max_score"] == 100
        assert result["writeback"]["status"] == "skipped"
        assert len(result["semantic_facts"]["sections"]) == 6
        return {"accepted_ms": ack_ms, "completed_ms": round((monotonic()-start)*1000, 2),
                "status": result["status"], "model_status": result["semantic_facts"]["status"],
                "q33": result["q33_score"], "q34": result["q34_score"],
                "total": result["total_score"], "writeback": "skipped", "job_id": job}


def signed_pipeline():
    """Real GLM, actual webhook/score/writeback code, isolated DB and intercepted API."""
    original = get_settings()
    mapping = load_jiandaoyun_mapping(original.jiandaoyun_mapping_path)
    record_id = "INTERNAL-NEVER-REAL-" + uuid4().hex
    body = json.dumps({"op": "data_create", "data": {
        "_id": record_id, "appId": mapping["source_application_id"],
        "entryId": mapping["source_entry_id"], "createTime": "2026-08-25T03:00:00Z",
        **synthetic_visit(),
    }}, ensure_ascii=False).encode()
    nonce, timestamp, secret = "internal-nonce", "1787731200", "synthetic-webhook-secret"
    signature = hashlib.sha1(b":".join([nonce.encode(), body, secret.encode(), timestamp.encode()])).hexdigest()
    writes = []

    def capture_writeback(url, **kwargs):
        payload = kwargs["json"]
        assert url.endswith("/v5/app/entry/data/update")
        assert payload["data_id"] == record_id
        assert payload["app_id"] == mapping["source_application_id"]
        assert payload["entry_id"] == mapping["source_entry_id"]
        assert set(payload["data"]) == {"_widget_1787037882562", "_widget_1787037882560"}
        assert 0 <= float(payload["data"]["_widget_1787037882562"]["value"]) <= 100
        assert payload["is_start_trigger"] is False
        writes.append(sorted(payload["data"]))
        return httpx.Response(200, json={"data": {"_id": record_id}}, request=httpx.Request("POST", url))

    with TemporaryDirectory(prefix="taoran-pipeline-") as directory:
        settings = original.model_copy(update={
            "database_path": str(Path(directory)/"probe.db"),
            "jiandaoyun_webhook_secret": secret,
        })
        with patch.object(api, "get_settings", return_value=settings), \
                patch("taoran_agent.writeback.httpx.post", side_effect=capture_writeback), \
                patch.object(api, "get_jiandaoyun_record", side_effect=AssertionError("Unexpected business read")), \
                TestClient(api.app) as client:
            url = f"/api/v1/connectors/jiandaoyun/visit/webhook?tenant_id=tenant_demo&nonce={nonce}&timestamp={timestamp}"
            start = monotonic()
            headers = {"X-JDY-Signature": signature, "Content-Type": "application/json"}
            accepted = client.post(url, content=body, headers=headers)
            assert accepted.status_code == 202
            result = api.get_store(settings).get_evaluation("tenant_demo", accepted.json()["job_id"])["response"]
            model_complete = result["semantic_facts"]["status"] == "completed"
            if model_complete:
                assert result["writeback"]["status"] == "succeeded"
                assert len(writes) == 1
            else:
                # Preserve failure evidence; never retry until success or bypass validation.
                assert result["writeback"]["status"] == "failed"
                assert len(writes) == 0, "Incomplete model analysis must not write"
            assert client.post(url, content=body, headers=headers).status_code == 202
            assert len(writes) == int(model_complete), "Duplicate webhook must not write again"
            assert client.post(url, content=body, headers={"X-JDY-Signature": "bad"}).status_code == 401
            return {"synthetic_only": True, "real_model": True, "real_jiandaoyun_write": False,
                    "elapsed_ms": round((monotonic()-start)*1000, 2),
                    "passed": model_complete,
                    "model_status": result["semantic_facts"]["status"],
                    "failure_reason": result["semantic_facts"].get("failure_reason"),
                    "model_attempts": result["semantic_facts"].get("model_attempts", []),
                    "writeback_simulated": result["writeback"]["status"],
                    "written_fields": writes[0] if writes else [],
                    "q33": result["q33_score"], "q34": result["q34_score"],
                    "total": result["total_score"], "duplicate_write_count": len(writes)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["buttons", "async", "pipeline"])
    parser.add_argument("--base", default="http://127.0.0.1:8030")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = (signed_pipeline() if args.mode == "pipeline" else
              live_buttons(args.base) if args.mode == "buttons" else live_async(args.base))
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text+"\n", encoding="utf-8")
    print(text)
    if result.get("passed") is False:
        raise SystemExit(1)
