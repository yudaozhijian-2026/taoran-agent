import asyncio
import json
from concurrent.futures import Future
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import HTTPException

from taoran_agent import api
from taoran_agent.config import Settings
from taoran_agent.experimental_semantic_streaming import _validate
from taoran_agent.experimental_semantic_streaming_v21 import build_evidence_from_hints
from taoran_agent.experimental_semantic_streaming_v22 import (
    _interactive_preview_safe,
    detect_unsupported_specific_facts,
)


class _ConnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


def _experimental_settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="experimental",
        experimental_streaming_poc_enabled=True,
    )


def test_experimental_streaming_is_disabled_outside_experimental() -> None:
    with pytest.raises(HTTPException) as exc:
        api._require_experimental_streaming(Settings(_env_file=None))
    assert exc.value.status_code == 404


def test_experimental_sse_emits_stages_then_unchanged_final_feedback(monkeypatch) -> None:
    settings = _experimental_settings()
    canonical = SimpleNamespace(
        context=SimpleNamespace(tenant_id="tenant_demo"),
    )
    api._experimental_streaming_tasks.clear()
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    monkeypatch.setattr(
        api,
        "_canonicalize_button_request",
        lambda request, tenant_id, api_key: (canonical, settings),
    )
    monkeypatch.setattr(
        api,
        "_experimental_run_quick_check",
        lambda request, configured_settings, started_at: {
            "status": "completed",
            "feedback_text": "【AI反馈意见】\n真实且已校验的最终文本。",
            "full_feedback_ms": 1200,
            "provider_first_byte_ms": 680,
            "provider_complete_ms": 1180,
        },
    )

    created = api.create_experimental_quick_check({"tenant_id": "tenant_demo"})
    assert created["experimental"] is True
    assert created["check_id"].startswith("exp_")
    assert len(created["stream_token"]) >= 32

    task = api._experimental_task_for_events(
        created["check_id"],
        created["stream_token"],
    )

    async def collect() -> list[str]:
        return [
            message
            async for message in api._experimental_event_stream(
                _ConnectedRequest(), created["check_id"], task
            )
            if not message.startswith(":")
        ]

    messages = asyncio.run(collect())
    assert messages[0].startswith("event: started")
    assert messages[1].startswith("event: delta")
    assert messages[2].startswith("event: delta")
    assert messages[-1].startswith("event: completed")
    assert "真实且已校验的最终文本" in messages[-1]
    assert '"raw_streaming_exposed": false' in messages[-1]


def test_experimental_current_record_reads_only_configured_test_form(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        environment="experimental",
        experimental_streaming_poc_enabled=True,
        experimental_streaming_poc_launch_token="x" * 32,
        tenant_keys_json='{"tenant_demo":"test-key"}',
    )
    canonical = SimpleNamespace(context=SimpleNamespace(tenant_id="tenant_demo"))
    record = {"_id": "current-data-id", "field_a": "only-from-jiandaoyun"}
    captured: dict[str, object] = {}
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    monkeypatch.setattr(
        api,
        "tenant_mapping",
        lambda configured_settings, tenant_id: {
            "source_application_id": "test-app",
            "source_entry_id": "test-form",
        },
    )
    monkeypatch.setattr(
        api,
        "get_jiandaoyun_record",
        lambda configured_settings, tenant_id, app_id, entry_id, data_id: (
            captured.update({"tenant_id": tenant_id, "app_id": app_id, "entry_id": entry_id, "data_id": data_id})
            or record
        ),
    )
    monkeypatch.setattr(
        api,
        "_canonicalize_button_request",
        lambda request, tenant_id, api_key: (captured.update({"request": request}) or (canonical, settings)),
    )
    monkeypatch.setattr(
        api,
        "_experimental_enqueue_quick_check",
        lambda canonical_request, configured_settings: {"experimental": True, "check_id": "exp_current"},
    )

    created = api.create_experimental_current_record_quick_check(
        "current-data-id", "x" * 32
    )

    assert created == {"experimental": True, "check_id": "exp_current"}
    assert captured["tenant_id"] == "tenant_demo"
    assert captured["app_id"] == "test-app"
    assert captured["entry_id"] == "test-form"
    assert captured["data_id"] == "current-data-id"
    request = captured["request"]
    assert isinstance(request, dict)
    assert request["form_data"] == record
    assert request["context"]["source"] == "test"


def test_interactive_current_record_uses_mapped_visit_record_code(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        quick_check_interactive_enabled=True,
        environment="experimental",
        experimental_streaming_poc_enabled=True,
        tenant_keys_json='{"tenant_demo":"test-key"}',
    )
    captured: dict[str, object] = {}
    record = {"_id": "current-data-id", "field_a": "saved-record"}
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    monkeypatch.setattr(
        api,
        "tenant_mapping",
        lambda configured_settings, tenant_id: {
            "source_application_id": "test-app",
            "source_entry_id": "test-form",
            "record_fields": {
                "visit_record_code": {"widget_id": "visit-code", "widget_type": "sn"}
            },
        },
    )
    monkeypatch.setattr(
        api,
        "find_jiandaoyun_record_by_field",
        lambda *args: captured.update({"lookup": args}) or record,
    )
    monkeypatch.setattr(
        api,
        "create_interactive_quick_check_task",
        lambda request, **kwargs: captured.update({"request": request}) or {"check_id": "exp-1"},
    )

    created = api.create_experimental_interactive_quick_check_current_record_task(
        {"visit_record_code": "VISIT-001", "user_id": "user-1"},
        x_tenant_id="tenant_demo",
        x_api_key="test-key",
    )

    assert captured["lookup"][4:] == ("visit-code", "sn", "VISIT-001")
    assert captured["request"] == {
        "record_code": "VISIT-001",
        "data_id": "current-data-id",
        "user_id": "user-1",
        "form_snapshot": record,
    }
    assert created == {"check_id": "exp-1", "experimental": True, "source": "saved_current_record"}


def test_jdy_record_launch_is_origin_restricted_and_record_scoped(monkeypatch) -> None:
    settings = _experimental_settings()
    api._experimental_jdy_record_launches.clear()
    monkeypatch.setattr(api, "get_settings", lambda: settings)

    response = api.create_experimental_jdy_current_record_launch(
        data_id="current-data-id",
        origin=api._EXPERIMENTAL_JDY_PLUGIN_ORIGIN,
    )

    assert response.status_code == 200
    payload = response.body.decode("utf-8")
    assert "current-data-id" in payload
    assert response.headers["access-control-allow-origin"] == api._EXPERIMENTAL_JDY_PLUGIN_ORIGIN
    query = parse_qs(urlsplit(json.loads(response.body)["launch_url"]).query)
    record_launch_token = query["record_launch_token"][0]
    api._require_experimental_jdy_record_launch(
        "current-data-id",
        record_launch_token,
        consume=False,
    )
    with pytest.raises(HTTPException) as exc:
        api._require_experimental_jdy_record_launch(
            "different-data-id",
            record_launch_token,
            consume=False,
        )
    assert exc.value.status_code == 404


def test_jdy_record_launch_can_resolve_current_record_by_visit_code(monkeypatch) -> None:
    settings = _experimental_settings()
    api._experimental_jdy_record_launches.clear()
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    monkeypatch.setattr(
        api,
        "tenant_mapping",
        lambda configured_settings, tenant_id: {
            "source_application_id": "test-app",
            "source_entry_id": "test-form",
            "record_fields": {
                "visit_record_code": {
                    "widget_id": "visit-code-widget",
                    "widget_type": "sn",
                }
            },
        },
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        api,
        "find_jiandaoyun_record_by_field",
        lambda *args: captured.update({"args": args}) or {"_id": "resolved-data-id"},
    )

    response = api.create_experimental_jdy_current_record_launch(
        visit_record_code="VISIT-001",
        origin=api._EXPERIMENTAL_JDY_PLUGIN_ORIGIN,
    )

    assert response.status_code == 200
    assert captured["args"][4:] == ("visit-code-widget", "sn", "VISIT-001")
    query = parse_qs(urlsplit(json.loads(response.body)["launch_url"]).query)
    assert query["data_id"] == ["resolved-data-id"]


def test_v22_review_launch_binds_one_current_record(monkeypatch) -> None:
    settings = _experimental_settings()
    api._experimental_semantic_v22_review_launches.clear()
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    monkeypatch.setattr(
        api,
        "tenant_mapping",
        lambda configured_settings, tenant_id: {
            "source_application_id": "test-app",
            "source_entry_id": "test-form",
            "record_fields": {
                "visit_record_code": {"widget_id": "visit-code", "widget_type": "sn"}
            },
        },
    )
    monkeypatch.setattr(
        api, "find_jiandaoyun_record_by_field", lambda *args: {"_id": "record-42"}
    )

    response = api.create_experimental_semantic_v22_jdy_current_record_review_launch(
        visit_record_code="VISIT-042", origin=api._EXPERIMENTAL_JDY_PLUGIN_ORIGIN,
    )

    query = parse_qs(urlsplit(json.loads(response.body)["launch_url"]).query)
    token = query["review_launch_token"][0]
    assert query["mode"] == ["current-record"]
    assert api._require_experimental_semantic_v22_review_launch(token)["data_id"] == "record-42"


def test_v22_current_record_launch_can_redirect_from_jdy_navigation(monkeypatch) -> None:
    settings = _experimental_settings()
    api._experimental_semantic_v22_review_launches.clear()
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    monkeypatch.setattr(
        api,
        "tenant_mapping",
        lambda configured_settings, tenant_id: {
            "source_application_id": "test-app",
            "source_entry_id": "test-form",
            "record_fields": {
                "visit_record_code": {"widget_id": "visit-code", "widget_type": "sn"}
            },
        },
    )
    monkeypatch.setattr(
        api, "find_jiandaoyun_record_by_field", lambda *args: {"_id": "record-42"}
    )

    response = api.create_experimental_semantic_v22_jdy_current_record_review_launch(
        visit_record_code="VISIT-042",
        open_page=True,
        referer="https://www.jiandaoyun.com/dashboard#/app/test",
    )

    assert response.status_code == 303
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["mode"] == ["current-record"]
    assert api._require_experimental_semantic_v22_review_launch(
        query["review_launch_token"][0]
    )["data_id"] == "record-42"


def test_v22_expansion_review_only_accepts_the_selected_15_cases(monkeypatch) -> None:
    settings = _experimental_settings()
    captured: dict[str, object] = {}
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    monkeypatch.setattr(api, "_require_experimental_semantic_v22_review_access", lambda *args: None)
    monkeypatch.setattr(
        api,
        "_experimental_semantic_v22_start_review_case",
        lambda case_id, record, configured_settings: captured.update(
            {"case_id": case_id, "record": record, "settings": configured_settings}
        ) or {"check_id": "expansion-check"},
    )

    result = api.create_experimental_semantic_v22_review_expansion(
        "expansion_01", launch_token="x" * 32,
    )

    assert result == {"check_id": "expansion-check"}
    assert captured["case_id"] == "expansion_01"
    assert captured["record"] == ("BFJL2026081900001", "6a851bd917936e24c254f9f5")
    with pytest.raises(HTTPException) as exc:
        api.create_experimental_semantic_v22_review_expansion(
            "unapproved-case", launch_token="x" * 32,
        )
    assert exc.value.status_code == 404


def test_v22_current_record_review_writes_only_approved_final_feedback(monkeypatch, tmp_path) -> None:
    settings = _experimental_settings()
    api._experimental_semantic_v22_tasks.clear()
    future: Future[dict[str, object]] = Future()
    future.set_result({
        "authoritative": {"status": "completed", "feedback_text": "真实最终AI反馈", "full_feedback_ms": 1200},
        "preview": {"status": "completed", "feedback_hash": "hash", "feedback_length": 24},
    })
    api._experimental_semantic_v22_tasks["sem_v22_test"] = {
        "stream_token": "x" * 32,
        "expires_at": 9_999_999_999.0,
        "future": future,
        "review_data_id": "record-42",
    }
    captured: dict[str, object] = {}
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    monkeypatch.setattr(api, "_experimental_semantic_v22_review_path", lambda _: tmp_path / "review.jsonl")
    monkeypatch.setattr(
        api,
        "_experimental_semantic_v22_writeback_feedback",
        lambda configured_settings, **kwargs: captured.update(kwargs) or {"status": "succeeded"},
    )

    result = api.save_experimental_semantic_v22_review(
        "sem_v22_test",
        {
            "relevant": "PASS", "specific": "PASS", "actionable": "PASS",
            "consistent_with_record": "PASS", "reviewer_note": "可以回写",
        },
        "x" * 32,
    )

    assert result["writeback"]["status"] == "succeeded"
    assert captured["data_id"] == "record-42"
    assert captured["feedback_text"] == "真实最终AI反馈"


def test_v22_review_submission_is_immutable(monkeypatch, tmp_path) -> None:
    settings = _experimental_settings()
    api._experimental_semantic_v22_tasks.clear()
    future: Future[dict[str, object]] = Future()
    future.set_result({
        "authoritative": {"status": "completed", "feedback_text": "最终反馈", "full_feedback_ms": 1200},
        "preview": {"status": "completed", "feedback_hash": "hash", "feedback_length": 24},
    })
    api._experimental_semantic_v22_tasks["sem_v22_locked"] = {
        "stream_token": "x" * 32,
        "expires_at": 9_999_999_999.0,
        "future": future,
        "review_data_id": "record-42",
    }
    review_path = tmp_path / "review.jsonl"
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    monkeypatch.setattr(api, "_experimental_semantic_v22_review_path", lambda _: review_path)

    first = api.save_experimental_semantic_v22_review(
        "sem_v22_locked",
        {
            "relevant": "FAIL", "specific": "FAIL", "actionable": "FAIL",
            "consistent_with_record": "FAIL", "reviewer_type": "ai_sales_expert",
        },
        "x" * 32,
    )

    assert first["writeback"] == {"status": "skipped", "reason": "review_not_approved"}
    with pytest.raises(HTTPException) as exc:
        api.save_experimental_semantic_v22_review(
            "sem_v22_locked",
            {
                "relevant": "PASS", "specific": "PASS", "actionable": "PASS",
                "consistent_with_record": "PASS",
            },
            "x" * 32,
        )
    assert exc.value.status_code == 409
    entries = [json.loads(line) for line in review_path.read_text(encoding="utf-8").splitlines()]
    assert len(entries) == 1
    assert entries[0]["relevant"] == "FAIL"
    assert entries[0]["reviewer_type"] == "ai_sales_expert"


def test_semantic_v2_keeps_authoritative_final_and_marks_preview_provisional(monkeypatch) -> None:
    settings = _experimental_settings()
    canonical = SimpleNamespace(context=SimpleNamespace(tenant_id="tenant_demo"))
    api._experimental_semantic_streaming_tasks.clear()
    monkeypatch.setattr(
        api,
        "_experimental_run_semantic_quick_check",
        lambda request, configured_settings, started_at, deltas: {
            "authoritative": {
                "status": "completed",
                "feedback_text": "【AI反馈意见】\n经过正式校验的最终文本。",
                "full_feedback_ms": 1200,
                "provider_first_byte_ms": 700,
                "provider_complete_ms": 1100,
            },
            "semantic": {
                "status": "completed",
                "first_real_ai_text_ms": 360,
                "semantic_complete_ms": 900,
                "evidence_count": 1,
            },
        },
    )
    created = api._experimental_enqueue_semantic_quick_check(canonical, settings)
    assert created["semantic_streaming_v2"] is True
    assert created["check_id"].startswith("sem_v2_")
    task = api._experimental_semantic_task_for_events(
        created["check_id"], created["stream_token"]
    )

    async def collect() -> list[str]:
        return [message async for message in api._experimental_semantic_event_stream(
            _ConnectedRequest(), created["check_id"], task
        ) if not message.startswith(":")]

    messages = asyncio.run(collect())
    assert messages[0].startswith("event: started")
    assert messages[1].startswith("event: stage")
    assert messages[-1].startswith("event: completed")
    assert "经过正式校验的最终文本" in messages[-1]
    assert '"preview_validated": true' in messages[-1]


def test_semantic_v22_exposes_final_cache_and_provider_telemetry() -> None:
    task = {
        "created_at": 0.0,
        "future": Future(),
        "deltas": __import__("queue").Queue(),
    }
    task["future"].set_result({
        "authoritative": {
            "status": "completed",
            "feedback_text": "【AI反馈意见】\n正式文本。",
            "full_feedback_ms": 1200,
            "provider_first_byte_ms": 300,
            "provider_complete_ms": 1100,
            "knowledge_cache_hit": False,
        },
        "preview": {
            "status": "completed",
            "first_real_ai_text_ms": 200,
            "semantic_complete_ms": 800,
        },
    })

    async def collect() -> list[str]:
        return [message async for message in api._experimental_semantic_v22_events(
            _ConnectedRequest(), "sem_v22_telemetry", task
        ) if not message.startswith(":")]

    messages = asyncio.run(collect())
    completed = messages[-1]
    assert '"provider_first_byte_ms": 300' in completed
    assert '"provider_complete_ms": 1100' in completed
    assert '"knowledge_cache_hit": false' in completed
    assert '"final_validation_complete_ms": 1200' in completed


def test_semantic_v2_requires_machine_evidence_to_match_visible_quote() -> None:
    snapshot = {"process_description": "客户确认下周评审方案。"}
    raw = (
        "<USER_FEEDBACK>本次客户已明确表示“客户确认下周评审方案。”，下一步应围绕该计划继续推进。</USER_FEEDBACK>"
        "<MACHINE_RESULT>{\"evidence\":[{\"field\":\"process_description\",\"quote\":\"客户确认下周评审方案。\"}]}</MACHINE_RESULT>"
    )
    feedback, count = _validate(raw, snapshot)
    assert "客户确认下周评审方案" in feedback
    assert count == 1


def test_semantic_v21_builds_program_owned_evidence_from_hint() -> None:
    evidence, telemetry = build_evidence_from_hints(
        [{"topic": "客户状态", "keywords": ["内部评估"]}],
        [{"field": "process_description", "quote": "客户表示先内部评估，后续再确认。"}],
    )
    assert evidence == [{"field": "process_description", "quote": "客户表示先内部评估，后续再确认。"}]
    assert telemetry["exact_match_count"] == 1
    assert telemetry["matched_fields"] == ["process_description"]


def test_semantic_v21_rejects_ambiguous_program_evidence() -> None:
    with pytest.raises(ValueError, match="multiple_ambiguous_matches"):
        build_evidence_from_hints(
            [{"topic": "确认", "keywords": ["确认"]}],
            [
                {"field": "process_description", "quote": "客户确认评审。"},
                {"field": "customer_feedback", "quote": "客户确认预算。"},
            ],
        )


def test_semantic_v21_keeps_matched_evidence_when_an_extra_hint_is_unmatched() -> None:
    evidence, telemetry = build_evidence_from_hints(
        [{"topic": "客户状态", "keywords": ["内部评估"]}, {"topic": "其他", "keywords": ["不存在"]}],
        [{"field": "process_description", "quote": "客户表示先内部评估，后续再确认。"}],
    )
    assert evidence[0]["field"] == "process_description"
    assert telemetry["unmatched_hint_count"] == 1


def test_semantic_v22_blocks_fabricated_specific_amount() -> None:
    outcome = detect_unsupported_specific_facts(
        "客户预算为100万元，建议下周推进。",
        {"process_description": "客户正在内部评估方案。"},
    )
    assert outcome["specific_fact_claim_count"] == 1
    assert outcome["failure_category"] == "fabricated_specific_fact"


def test_semantic_v22_allows_general_assistive_analysis_without_unique_evidence() -> None:
    outcome = detect_unsupported_specific_facts(
        "当前记录对下一步行动描述还不够具体，建议明确希望客户确认的事项。",
        {"process_description": "客户正在内部评估。", "next_action_purpose": "继续跟进"},
    )
    assert outcome["specific_fact_claim_count"] == 0
    assert outcome["failure_category"] is None


@pytest.mark.parametrize("text,safe", [
    ("尚未体现客户明确同意让己方参与研发。", True),
    ("记录无法证明客户已经确认采购。", True),
    ("没有记录客户同意试用。", True),
    ("尚不足以证明客户已确认合同文本最终版本并承诺签署。", True),
    ("尚未体现参与权的证据，如客户明确同意参与研发。", True),
    ("尚未记录“客户明确同意参与研发”的表态。", True),
    ("客户明确同意试用。", False),
    ("尚未体现客户同意试用，客户已经确认采购。", False),
    ("并非没有记录客户同意试用。", False),
    ("尚不足以证明客户已确认试用，客户已经确认采购。", False),
    ("例如客户明确同意试用，客户已经确认采购。", False),
    ("正如客户已经确认采购的情况。", False),
    ("客户已经确认采购，尚未体现客户同意试用。", False),
])
def test_candidate_preview_distinguishes_uncertainty_from_affirmative_claims(text, safe):
    snapshot = {"process_description": "客户表示等待进一步评估。"}
    assert _interactive_preview_safe(text, snapshot) is safe
    result = detect_unsupported_specific_facts(text, snapshot, interactive=True)
    assert (result["failure_category"] is None) is safe


def test_candidate_negation_fix_preserves_legacy_preview_guard():
    text = "尚未体现客户明确同意参与研发。"
    snapshot = {"process_description": "客户表示等待进一步评估。"}
    assert detect_unsupported_specific_facts(text, snapshot)["failure_category"] == "unsupported_customer_commitment"
    assert detect_unsupported_specific_facts(text, snapshot, interactive=True)["failure_category"] is None


def test_preview_future_advice_does_not_raise_current_attainment_standard():
    assert _interactive_preview_safe("已记录采购沟通，建议下次充分沟通审批流程。", {
        "expected_key_result": "沟通采购", "process_description": "已沟通采购安排"})


@pytest.mark.parametrize("claim,safe", [
    ("当前关键结果仅填写数字1，因此无法判断目标达成程度。", True),
    ("关键结果不清楚，不能把目的当作获得参与这一目标。", True),
    ("尚不足以证明获得参与这一目标已实现。", False),
    ("不能把目的当作获得参与这一目标。但尚不足以证明获得参与这一目标已实现。", False),
])
def test_candidate_preview_observes_goal_guard_without_blocking(monkeypatch, claim, safe):
    from taoran_agent import experimental_semantic_streaming_v22 as preview

    claim = "当前记录显示，" + claim
    client = httpx.Client
    payload = {"choices": [{"delta": {"content": "<USER_FEEDBACK>" + claim + "</USER_FEEDBACK>"}}]}
    transport = httpx.MockTransport(lambda request: httpx.Response(200,
        text="data: " + json.dumps(payload) + "\n\ndata: [DONE]\n\n"))
    monkeypatch.setattr(preview.httpx, "Client", lambda **kwargs: client(transport=transport))
    monkeypatch.setattr(preview, "_interactive_snapshot", lambda visit: {
        "expected_key_result": "1", "purpose_code": "获得参与", "process_description": "催款"})
    emitted = []
    result = preview.stream_semantic_preview_v22(Settings(_env_file=None,
        llm_enabled=True, llm_model="glm-test", llm_api_url="https://example.test/chat",
        llm_api_key="test"), None, emitted.append, interactive=True)
    assert result["status"] == "completed"
    assert result["semantic_diagnostics"]["existing_checks_passed"] is safe
    assert emitted == [claim]


@pytest.mark.parametrize("claim,expected", [
    ("尚未体现客户明确同意参与研发，记录尚不足以证明参与权已取得。", "completed"),
    ("尚未体现客户同意试用，客户已经确认采购并承诺马上签约。", "failed"),
])
def test_candidate_stream_records_uncertainty_without_blocking(monkeypatch, claim, expected):
    from taoran_agent import experimental_semantic_streaming_v22 as preview

    client = httpx.Client
    payload = {"choices": [{"delta": {"content": "<USER_FEEDBACK>" + claim + "</USER_FEEDBACK>"}}]}
    transport = httpx.MockTransport(lambda request: httpx.Response(200,
        text="data: " + json.dumps(payload) + "\n\ndata: [DONE]\n\n"))
    monkeypatch.setattr(preview.httpx, "Client", lambda **kwargs: client(transport=transport))
    monkeypatch.setattr(preview, "_interactive_snapshot", lambda visit: {
        "process_description": "客户表示等待进一步评估。"})
    emitted = []
    result = preview.stream_semantic_preview_v22(Settings(_env_file=None,
        llm_enabled=True, llm_model="glm-test", llm_api_url="https://example.test/chat",
        llm_api_key="test"), None, emitted.append, interactive=True)
    assert result["status"] == "completed"
    assert result["semantic_diagnostics"]["existing_checks_passed"] is (expected == "completed")
    assert result["failure_category"] is None
    assert emitted == [claim]
