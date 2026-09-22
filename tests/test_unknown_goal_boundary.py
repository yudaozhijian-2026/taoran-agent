import json
from copy import deepcopy

import httpx
import pytest

from taoran_agent.goal_normalization import normalize_expected_key_result
from taoran_agent.models import VisitDraftInput


def visit(**updates):
    values = {
        "visit_date": "2026-09-21",
        "employee_id": "goal-boundary",
        "customer_type_ii": "potential",
        "visit_method": "face_to_face",
        "is_appointment": False,
        "purpose_code": "收集信息",
        "expected_key_result": "-",
        "process_description": "客户提供招标价格及竞争信息。",
        "self_assessment": "achieved",
        "next_action_purpose": "收集信息",
        "next_action_expected_result": "确认后续安排",
    }
    values.update(updates)
    return VisitDraftInput.model_validate(values)


@pytest.mark.parametrize("raw", [None, "", "  ", "-", "—", "/", "1", "3", "N/A", "无"])
def test_placeholder_goal_is_not_assessable(raw):
    value = normalize_expected_key_result(raw)
    assert value.goal_state == "missing_placeholder"
    assert value.goal_assessable is False
    assert value.goal_source == "key_result"
    assert value.achievement == "unresolved"


@pytest.mark.parametrize("raw", ["项目顺利实施", "收集信息", "保持关系", "推进项目"])
def test_broad_goal_is_distinct_from_missing_goal(raw):
    value = normalize_expected_key_result(raw)
    assert value.goal_state == "broad"
    assert value.goal_assessable is False
    assert value.achievement == "unresolved"


def test_hyphen_inside_real_goal_is_not_placeholder():
    value = normalize_expected_key_result("确认A-B项目审批情况")
    assert value.goal_state == "specific"
    assert value.goal_assessable is True
    assert value.goal_normalized == "确认A-B项目审批情况"


def test_record_state_exposes_goal_boundary_for_front_and_formal_analysis():
    from taoran_agent.experimental_record_state import build as build_formal
    from taoran_agent.front_v46.experimental_record_state import build as build_front

    context = visit().model_dump(mode="json")
    for build in (build_front, build_formal):
        goal = build(context)["goal"]
        assert goal["goal_state"] == "missing_placeholder"
        assert goal["goal_assessable"] is False
        assert goal["goal_source"] == "key_result"
        assert goal["achievement"] == "unresolved"


def test_business_state_uses_goal_boundary_without_touching_scoring_inputs():
    from taoran_agent.experimental_business_semantic_state import (
        build_business_state as build_formal,
    )
    from taoran_agent.front_v46.experimental_business_semantic_state import (
        build_business_state as build_front,
    )

    for build in (build_front, build_formal):
        placeholder = build(visit().model_dump(mode="json"))
        assert placeholder["field_states"]["expected_key_result"] == "placeholder"
        assert placeholder["goal_items"] == []

        broad = build(visit(expected_key_result="收集信息").model_dump(mode="json"))
        assert broad["field_states"]["expected_key_result"] == "vague"
        assert broad["goal_items"][0]["assessability"] == "vague"
        assert broad["self_assessment_alignment"]["computed_goal_summary"] == "not_assessable"


def test_dash_goal_does_not_fallback_to_visit_purpose():
    from taoran_agent.front_v46.experimental_record_state import boundary_issues

    issues = boundary_issues("本次目标为收集信息，收集信息目标已达成。", visit().model_dump(mode="json"))
    assert any(item["error_type"] == "unknown_goal_assessed" for item in issues)


def test_empty_goal_does_not_fallback_to_visit_purpose():
    from taoran_agent.front_v46.experimental_record_state import boundary_issues

    context = visit(expected_key_result=None, purpose_code="保持关系").model_dump(mode="json")
    issues = boundary_issues("本次目标为保持关系，保持关系目标已达成。", context)
    assert any(item["error_type"] == "unknown_goal_assessed" for item in issues)


def test_broad_goal_forces_unresolved_judgment():
    from taoran_agent.front_v46.experimental_record_state import boundary_issues

    issues = boundary_issues("本次目标已经达成。", visit(expected_key_result="项目顺利实施").model_dump(mode="json"))
    assert any(item["error_type"] == "broad_goal_assessed" for item in issues)


def test_unknown_goal_deterministic_repair_preserves_actual_progress():
    from taoran_agent.front_v46.experimental_semantic_streaming_v22 import (
        _interactive_preview_violations,
        _interactive_snapshot,
        deterministic_goal_repair,
    )

    snapshot = _interactive_snapshot(visit())
    candidate = "本次目标为收集信息，已取得招标价格及竞争信息，因此收集信息目标已达成。"
    assert "unknown_goal_assessed" in _interactive_preview_violations(candidate, snapshot)
    repair = deterministic_goal_repair(snapshot)
    assert repair is not None
    assert repair["goal_repair_applied"] is True
    assert repair["goal_state"] == "missing_placeholder"
    assert repair["goal_assessable"] is False
    assert repair["goal_source"] == "key_result"
    assert "无法据此判断本次目标是否达成" in repair["feedback_text"]
    assert "客户提供招标价格及竞争信息" in repair["feedback_text"]
    assert "收集信息目标已达成" not in repair["feedback_text"]
    assert not _interactive_preview_violations(repair["feedback_text"], snapshot)


def test_unknown_goal_repair_does_not_trigger_second_model_call(monkeypatch):
    from taoran_agent.front_v46 import experimental_semantic_streaming_v22 as stream

    calls = []

    def first_candidate(*args, **kwargs):
        calls.append(kwargs)
        return {
            "status": "failed",
            "failure_category": "preview_business_boundary_conflict",
            "failure_reason": "preview_business_boundary_conflict",
            "validation_errors": ["unknown_goal_assessed"],
            "feedback_text": "本次目标为收集信息，收集信息目标已达成。",
        }

    monkeypatch.setattr(stream, "_stream_semantic_preview_once", first_candidate)
    result = stream.stream_semantic_preview_v22(object(), visit(), lambda _text: None, interactive=True)
    assert len(calls) == 1
    assert result["status"] == "completed"
    assert result["attempt_count"] == 1
    assert result["analysis_model_call_count"] == 1
    assert result["analysis_second_call_due_to_unknown_goal_count"] == 0
    assert result["goal_repair_applied"] is True
    assert result["initial_validation_errors"] == ["unknown_goal_assessed"]


def test_missing_goal_fact_boundary_uses_deterministic_repair_without_retry(monkeypatch):
    from taoran_agent.front_v46 import experimental_semantic_streaming_v22 as stream

    calls = []

    def first_candidate(*args, **kwargs):
        calls.append(kwargs)
        return {
            "status": "failed",
            "failure_category": "preview_business_boundary_conflict",
            "failure_reason": "preview_business_boundary_conflict",
            "validation_errors": ["fabricated_specific_fact"],
            "feedback_text": "本次记录包含招标价格和竞争信息。",
        }

    monkeypatch.setattr(stream, "_stream_semantic_preview_once", first_candidate)
    result = stream.stream_semantic_preview_v22(
        object(),
        visit(process_description="客户提供招标价格及竞争信息。"),
        lambda _text: None,
        interactive=True,
    )
    assert len(calls) == 1
    assert result["status"] == "completed"
    assert result["analysis_model_call_count"] == 1
    assert result["goal_repair_applied"] is True
    assert result["initial_validation_errors"] == ["fabricated_specific_fact"]
    assert "客户提供招标价格及竞争信息" in result["feedback_text"]


def test_unknown_goal_repair_uses_one_real_stream_request(tmp_path, monkeypatch):
    from taoran_agent.config import Settings
    from taoran_agent.front_v46 import experimental_semantic_streaming_v22 as stream

    calls = []

    def provider(request):
        calls.append(json.loads(request.content))
        events = [
            {"choices": [{"delta": {"content": "本次目标为收集信息，收集信息目标已达成。"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        body = "".join("data: " + json.dumps(item, ensure_ascii=False) + "\n\n" for item in events)
        return httpx.Response(200, content=body)

    real_client = httpx.Client
    monkeypatch.setattr(stream.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(provider)
    ))
    settings = Settings(
        _env_file=None,
        database_path=str(tmp_path / "db"),
        llm_enabled=True,
        llm_model="test",
        llm_api_key="test",
        llm_api_url="https://example.test/chat",
    )
    result = stream.stream_semantic_preview_v22(settings, visit(), lambda _text: None, interactive=True)
    assert len(calls) == 1
    model_snapshot = json.loads(calls[0]["messages"][1]["content"])["untrusted_visit_data"]
    assert model_snapshot["_goal_boundary"] == {
        "goal_raw": "-",
        "goal_normalized": None,
        "goal_state": "missing_placeholder",
        "goal_assessable": False,
        "goal_source": "key_result",
        "achievement": "unresolved",
        "legacy_status": "placeholder",
    }
    assert result["status"] == "completed"
    assert result["analysis_model_call_count"] == 1
    assert result["analysis_second_call_due_to_unknown_goal_count"] == 0
    assert result["goal_repair_applied"] is True


def test_goal_repair_is_presentation_only_and_leaves_scores_and_artifact_untouched():
    from taoran_agent.front_v46.experimental_semantic_streaming_v22 import deterministic_goal_repair

    snapshot = {
        "expected_key_result": "-",
        "process_description": "客户提供招标价格及竞争信息。",
        "q33": 50,
        "q34": 50,
        "total": 100,
        "front_artifact": {"id": "unchanged"},
    }
    original = deepcopy(snapshot)
    repair = deterministic_goal_repair(snapshot)
    assert repair is not None
    assert snapshot == original
    assert (snapshot["q33"], snapshot["q34"], snapshot["total"]) == (50, 50, 100)
    assert snapshot["front_artifact"] == {"id": "unchanged"}
