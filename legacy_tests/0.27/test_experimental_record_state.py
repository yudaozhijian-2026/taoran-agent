import json

import httpx
import pytest

from taoran_agent.config import Settings
from taoran_agent.experimental_record_state import boundary_issues, build
from taoran_agent.experimental_semantic_audit import CHECKS, validate_review
from taoran_agent.experimental_semantic_streaming_v22 import _interactive_preview_safe
from taoran_agent.llm import ChatModelReviewer, ModelCallError


def test_registry_preserves_offsets_and_does_not_infer_attainment():
    context = {"expected_key_result": "客户确认需求，并同意下次反馈条件",
        "process_description": "客户反馈暂无采购计划。李部长说先将资料转告QA。",
        "self_assessment": "partially_achieved", "next_contact_at": None}
    state = build(context)
    assert len(state["goal"]["parts"]) == 2
    assert all(p["attainment"] == "unassessed" for p in state["goal"]["parts"])
    assert state["field_states"]["next_contact_at"] == "not_recorded"
    actual = [s for s in state["sources"] if s["field"] == "process_description"]
    assert actual[0]["actor_hint"] == "customer"
    assert actual[0]["negative_statement"] is True
    assert actual[1]["modality_hint"] == "mixed_or_planned"
    for span in actual:
        assert context[span["field"]][span["start"]:span["end"]] == span["quote"]
    assert build({"expected_key_result": "1"})["goal"]["parts"] == []


@pytest.mark.parametrize("text,blocked", [
    ("已约定再访，但记录未体现具体联系日期。", False),
    ("已约定再访，但未约定具体联系时间。", True),
    ("关键结果为占位内容，无法判断目标达成。", False),
    ("目标无法判断，但尚不足以证明催款目标已实现。", True),
    ("不能把实际过程当作催款目标。", False),
    ("不能把实际过程当作催款目标，但催款目标已经达成。", True),
    ("尚不足以证明获得参与这一目标已实现。", True),
])
def test_shared_boundaries_keep_unknown_separate_from_absence(text, blocked):
    context = {"expected_key_result": "1", "purpose_code": "获得参与", "process_description": "催款"}
    assert bool(boundary_issues(text, context)) is blocked
    assert _interactive_preview_safe(text, context) is (not blocked)


def false_denial(context, output):
    source = next(s for s in build(context)["sources"] if s["field"] == "process_description")
    return {"checks": {k: k != "consistency" for k in CHECKS}, "issues": [{
        "check": "consistency", "error_type": "fact_denial", "target": "analysis:0",
        "output_quote": output, "source_ids": [source["id"]], "reason": "测试一个错误复核理由。",
        "comparison": {"source_actor": "sales", "candidate_actor": "customer",
            "source_state": "reported", "candidate_state": "not_attained",
            "relation": "denial", "examined_targets": ["analysis:0"]}}]}


def test_cross_actor_rejection_is_invalid_even_with_exact_source_id():
    context = {"process_description": "向客户介绍产品"}
    output = "没有记录客户的回应。"
    value = false_denial(context, output)
    with pytest.raises(ValueError, match="comparison"):
        validate_review(value, context, output, [])
    value["issues"][0]["comparison"]["source_actor"] = "customer"
    with pytest.raises(ValueError, match="comparison"):
        validate_review(value, context, output, [])
    value["issues"][0]["comparison"].update(source_actor="sales", candidate_actor="sales")
    with pytest.raises(ValueError, match="comparison"):
        validate_review(value, context, output, [])


@pytest.mark.parametrize("repair_ok", [True, False])
def test_review_contract_repair_keeps_identical_candidate_and_accounts_cost(repair_ok):
    context = {"process_description": "向客户介绍产品"}
    output = "没有记录客户的回应。"
    calls = []

    def provider(request):
        body = json.loads(request.content)
        calls.append(body)
        data = json.loads(body["messages"][1]["content"])
        assert data["candidate"]["units"][0]["text"] == output
        payload = false_denial(context, output) if len(calls) == 1 or not repair_ok else {
            "checks": dict.fromkeys(CHECKS, True), "issues": []}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps(payload)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

    reviewer = ChatModelReviewer(Settings(_env_file=None, llm_model="glm-test",
        llm_api_url="https://example.test/chat", llm_api_key="test"), None,
        transport=httpx.MockTransport(provider))
    audit = {}
    try:
        if repair_ok:
            reviewer._experimental_audit_wording(context, output, [], 2, audit)
            assert audit["status"] == "passed"
        else:
            with pytest.raises(ModelCallError, match="comparison"):
                reviewer._experimental_audit_wording(context, output, [], 2, audit)
            assert audit["status"] == "unavailable"
        assert len(calls) == 2
        assert calls[0]["messages"][:2] == calls[1]["messages"][:2]
        assert "review_repair" in json.loads(calls[1]["messages"][-1]["content"])
        assert audit["total_tokens"] == 30
        assert reviewer.model_capacity.snapshot()["active_frontend"] == 0
    finally:
        reviewer.close()


@pytest.mark.parametrize("text,day,blocked", [
    ("记录未填写联系时间。", None, False),
    ("记录未填写联系时间。", "2026-08-24", True),
    ("未提供具体联系日期。", "2026-08-24", True),
    ("联系时间已填写。", "2026-08-24", False),
])
def test_missing_wording_tracks_actual_field_state(text, day, blocked):
    assert bool(boundary_issues(text, {"next_contact_at": day})) is blocked


@pytest.mark.parametrize("text,source,blocked", [
    ("联系时间为8月24日。", "2026-08-24", False),
    ("联系时间为2026年8月24日。", "2026-08-24", False),
    ("联系时间为8月25日。", "2026-08-24", True),
    ("联系时间为2027年8月24日。", "2026-08-24", True),
    ("联系时间为2月30日。", "2026-02-30", True),
])
def test_calendar_normalization_preserves_actual_date(text, source, blocked):
    from taoran_agent.experimental_semantic_streaming_v22 import detect_unsupported_specific_facts
    result = detect_unsupported_specific_facts(text, {"next_contact_at": source}, interactive=True)
    assert bool(result["unsupported_specific_fact_count"]) is blocked
    if not blocked:
        legacy = detect_unsupported_specific_facts(text, {"next_contact_at": source}, interactive=False)
        assert legacy["unsupported_specific_fact_count"]


@pytest.mark.parametrize("output,allowed", [
    ("没有客户互动，也没有约定后续拜访。", True),
    ("没有记录客户的回应。", False),
])
def test_joint_event_proves_participation_but_not_separate_response(output, allowed):
    context = {"process_description": "简单与客户沟通后约定下次拜访"}
    value = false_denial(context, output)
    value["issues"][0]["comparison"]["source_actor"] = "joint"
    if allowed:
        failed, _ = validate_review(value, context, output, [])
        assert failed == ["consistency"]
    else:
        with pytest.raises(ValueError, match="comparison"):
            validate_review(value, context, output, [])
