import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from taoran_agent import api
from taoran_agent.config import Settings
from taoran_agent.experimental_assessment import GOAL_SCOPE_GUIDANCE, contradicts_approved_goal
from taoran_agent.experimental_final_consistency import process_fully_covered_in_advice
from taoran_agent.llm import SECTION_FIELDS, ChatModelReviewer, ModelCallError
from taoran_agent.numeric_evidence import missing_numeric_tokens


@pytest.mark.parametrize("text,field,quote,missing", [
    ("下次联系9月4日", "next_contact_at", "2026-09-03T16:00:00Z", False),
    ("下次联系9月5日", "next_contact_at", "2026-09-03T16:00:00Z", True),
    ("下次联系9月3日", "next_contact_at", "2026-09-03T16:00:00Z", True),
    ("预算3000元", "process_description", "工程预算三千", False),
    ("预算3元", "process_description", "预算3000元", True),
    ("预算3万元", "process_description", "预算3元", True),
    ("预算3000.00元", "process_description", "预算3千元", False),
])
def test_numeric_display_equivalence_does_not_allow_substring_invention(text, field, quote, missing):
    assert bool(missing_numeric_tokens(text, [SimpleNamespace(field=field, quote=quote)])) is missing


def test_gap_type_alone_never_rejects_approved_goal(monkeypatch):
    monkeypatch.setattr("taoran_agent.experimental_assessment.approved_procurement_goal", lambda _: True)
    assert not contradicts_approved_goal("assessment_gap", "采购沟通已完成，合作意愿中立。", {})
    assert "目标若要求签约或首次合作" in GOAL_SCOPE_GUIDANCE


def test_stage_code_uses_canonical_stage_not_incidental_process_digit():
    proofs = [SimpleNamespace(field="process_description", quote="前面订单发票报账资料核对，共5项")]
    context = {"opportunity_stage": "P5"}
    assert not missing_numeric_tokens("本次面访商机P5阶段客户，核对订单资料。", proofs, context)
    assert missing_numeric_tokens("本次面访P4阶段客户。", proofs, context) == ["P4"]
    assert missing_numeric_tokens("本次面访P5阶段客户。", proofs, {}) == ["P5"]
    assert missing_numeric_tokens("本次面访P5客户，新增采购9件。", proofs, context) == ["9"]
    assert not missing_numeric_tokens("本次P2商机面访。", proofs, {"opportunity_stages": ["P1", "P2"]})


def test_process_advice_must_cover_entire_original_and_include_grounded_quote():
    item = SimpleNamespace(code="R", suggestion="过程仅记录催款，尚未记录客户回应。",
        evidence=[SimpleNamespace(field="process_description", quote="催款")])
    assert process_fully_covered_in_advice([item], {"process_description": "催款"})
    assert not process_fully_covered_in_advice([item], {"process_description": "催款，客户答应明天付款"})
    item.evidence = []
    assert not process_fully_covered_in_advice([item], {"process_description": "催款"})


@pytest.mark.parametrize("audit_coverage_only", [False, True])
def test_actual_short_process_candidate_does_not_lose_existing_valid_feedback(tmp_path, monkeypatch, audit_coverage_only):
    fixture = json.loads((Path(__file__).parent / "fixtures/short_process_advice.json").read_text())
    context, candidate = fixture["context"], fixture["candidate"]
    def provider(request):
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps(candidate)}}]})
    settings = Settings(_env_file=None, database_path=str(tmp_path / "db"),
        llm_model="glm-test", llm_api_url="https://example.test/chat", llm_api_key="test")
    reviewer = ChatModelReviewer(settings, None, transport=httpx.MockTransport(provider))
    def audit(context, analysis, suggestions, timeout, audit, **kwargs):
        if audit_coverage_only:
            audit.update(status="rejected", failed_checks=["coverage"])
            raise ModelCallError("wording_experimental_audit_coverage")
    monkeypatch.setattr(reviewer, "_experimental_audit_wording", audit)
    try:
        result = reviewer.verbalize_knowledge_issues(
            [{"code": code, "source_fields": context} for code in ["O_KR", "R", "N"]], 5,
            taoran_snapshot={"visit_analysis_context": context}, experimental=True)
        assert result.status == "completed", result.failure_reason
        assert any("催款" in item.suggestion for item in result.items)
        assert "已付款" not in result.visit_analysis
        assert result.model_attempts[0]["diagnostic_evidence_id"]
    finally:
        reviewer.close()


@pytest.mark.parametrize("always_invalid", [False, True])
def test_backend_invalid_field_retries_once_and_persists_private_evidence(tmp_path, monkeypatch, always_invalid):
    reviewer = ChatModelReviewer(Settings(_env_file=None, database_path=str(tmp_path / "db"),
        llm_format_retries=1), None)
    data = {field: "" for fields in SECTION_FIELDS.values() for field in fields}
    valid = {"sections": [{"code": code, "verdict": "needs_revision", "field_paths": [min(fields)],
        "reason": "字段内容缺失。", "suggestion": "补充实际记录。", "evidence": []}
        for code, fields in SECTION_FIELDS.items()], "facts": {
        "key_result_quality_ok": False, "process_fact_based": False,
        "purpose_achievement": "not_achieved", "next_action_logic_ok": False,
        "customer_consensus_met": False, "reason": "缺少实际记录。"}}
    invalid = deepcopy(valid)
    invalid["sections"][0]["field_paths"].append("invented_field")
    monkeypatch.setattr(reviewer, "_input", lambda *a, **k: data)
    monkeypatch.setattr(reviewer, "_messages", lambda *a, **k: [{"role": "user", "content": "original"}])
    calls = []
    def request(messages, precheck, timeout, lease):
        calls.append(messages)
        lease.release()
        return (invalid if always_invalid or len(calls) == 1 else valid), {}
    monkeypatch.setattr(reviewer, "_request", request)
    attempts = []
    try:
        if always_invalid:
            with pytest.raises(ModelCallError, match="invalid_field_reference"):
                reviewer._analyze(None, False, attempts)
        else:
            reviewer._analyze(None, False, attempts)
        assert len(calls) == 2
        assert attempts[0].validation_errors[0].location.startswith("sections.T.field_paths.")
        assert "invented_field" not in json.dumps(attempts[0].model_dump())
        path = tmp_path / "model-failure-evidence" / (attempts[0].diagnostic_evidence_id + ".json")
        evidence = json.loads(path.read_text())
        assert evidence["details"]["field_error"]["invalid_fields"] == ["invented_field"]
        assert path.stat().st_mode & 0o777 == 0o600
        assert "各项只能列出本次实际传入" in calls[1][-1]["content"]
    finally:
        reviewer.close()


@pytest.mark.parametrize("optional_still_bad", [False, True])
def test_numeric_failure_retries_only_affected_point_and_keeps_raw_diagnostic(tmp_path, monkeypatch, optional_still_bad):
    from test_experimental_semantic_audit import PIPELINE_CONTEXT, pipeline_provider
    calls, requests = [], []
    ordinary = pipeline_provider(["pass"], calls)
    def provider(request):
        body = json.loads(request.content)
        response = ordinary(request)
        if "candidate" in json.loads(body["messages"][1]["content"]):
            return response
        requests.append(body)
        wire = response.json()
        payload = json.loads(wire["choices"][0]["message"]["content"])
        if optional_still_bad:
            payload["analysis_points"].append({"contract_id": "C_NEXT", "claim_type": "planned",
                "kind": "next_step", "text": "下次联系安排在2027年。",
                "proofs": [{"field": "next_contact_at", "quote": "2026-08-24"}]})
        elif len(requests) == 1:
            payload["analysis_points"][0]["text"] = "客户表示审批暂缓30天。"
        wire["choices"][0]["message"]["content"] = json.dumps(payload)
        return httpx.Response(200, json=wire)
    settings = Settings(_env_file=None, database_path=str(tmp_path / "db"),
        llm_model="glm-test", llm_api_url="https://example.test/chat", llm_api_key="test")
    reviewer = ChatModelReviewer(settings, None, transport=httpx.MockTransport(provider))
    monkeypatch.setattr(api, "get_store", lambda _: SimpleNamespace(get_feedback_artifact=lambda *a: None, save_feedback_artifact=lambda *a: a[-1]))
    monkeypatch.setattr(api, "_front_specificity_items", lambda *a: [{"code": "R", "source_fields": PIPELINE_CONTEXT}])
    monkeypatch.setattr(api, "_cached_knowledge_wording", lambda _: None)
    monkeypatch.setattr(api, "_apply_knowledge_wording", lambda _, wording, *a, **k: wording)
    response = SimpleNamespace(knowledge_snapshot_hash="k", knowledge_references=["ref"], issues=[], tenant_id="t", input_snapshot_hash="i")
    try:
        result = api._enhance_front_suggestions(response, reviewer, settings, 5,
            {"visit_snapshot": {**PIPELINE_CONTEXT, "next_contact_at": "2026-08-24"}}, experimental=True)
        assert result.status == "completed"
        assert result.recovered_after_retry and len(requests) == 2
        assert "30" not in result.visit_analysis
        assert "2027" not in result.visit_analysis
        diagnostic = json.loads((tmp_path / "model-failure-evidence" / (result.model_attempts[0]["diagnostic_evidence_id"] + ".json")).read_text())
        assert diagnostic["details"]["rejection"]["numeric_errors"][0]["point_index"] == (1 if optional_still_bad else 0)
        if optional_still_bad:
            assert result.model_attempts[1]["diagnostic_evidence_id"]
        else:
            assert diagnostic["candidate"]["analysis_points"][0]["text"] == "客户表示审批暂缓30天。"
    finally:
        reviewer.close()
