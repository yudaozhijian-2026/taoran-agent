"""Synthetic regressions for the seven approved fixes; no real model/network calls."""
from copy import deepcopy

import pytest
from test_agent import complete_precheck_payload
from test_llm import deep_payload, model_settings, reviewer_for, visit
from test_writeback import evaluation_request

from taoran_agent import TaoranAgent
from taoran_agent.feedback import build_evaluation_feedback
from taoran_agent.models import PrecheckRequest, SelfAssessment
from taoran_agent.writeback import writeback_evaluation


def check(changes=None, omitted=()):
    payload = complete_precheck_payload()
    payload["visit"].update(changes or {})
    for field in omitted:
        payload["visit"].pop(field, None)
    if omitted:
        payload["visit"]["metadata"] = {"source_supplied_fields": list(payload["visit"])}
    return TaoranAgent().precheck(PrecheckRequest.model_validate(payload))


def section(result, code):
    return next(s for s in result.taoran_sections if s.code == code)


@pytest.mark.parametrize("purpose", ["其他", "other", "其他目的"])
@pytest.mark.parametrize("description", [None, "", "  ", "继续跟进", "确认确认确认确认"])
def test_n_other_requires_specific_description(purpose, description):
    result = check({"next_action_purpose": purpose, "next_action_other_purpose": description})
    assert section(result, "N").status == "needs_revision"
    assert "下一次具体其他目的" in result.feedback_text
    assert result.status == "needs_revision"
    assert result.can_submit and not result.submission_blocked


def test_n_other_omitted_is_transport_gap_not_user_empty():
    result = check({"next_action_purpose": "其他"}, omitted=("next_action_other_purpose",))
    assert section(result, "N").status == "partial_input"
    assert section(result, "N").unreceived_fields == ["next_action_other_purpose"]
    assert "TAORAN_NSA_OTHER_PURPOSE_MISSING" not in {i.code for i in result.issues}
    assert result.status == "review"


def test_n_other_with_concrete_description_passes():
    result = check({"next_action_purpose": "其他", "next_action_other_purpose": "安排客户技术交流"})
    assert section(result, "N").status == "met"


@pytest.mark.parametrize("assessment", list(SelfAssessment))
@pytest.mark.parametrize("field", ["process_description", "expected_key_result"])
def test_all_self_assessments_need_evidence(assessment, field):
    result = check({"self_assessment": assessment, field: ""})
    assert section(result, "A2").status == "needs_revision"


def test_negative_result_can_be_valid_evidence_for_honest_negative_assessment():
    result = check({"self_assessment": "not_achieved",
                    "process_description": "采购经理明确拒绝确认预算，要求等待审批，本次未完成确认。"})
    assert section(result, "R").status == "met"
    assert section(result, "A2").status == "met"


@pytest.mark.parametrize("field,value,code", [
    ("expected_key_result", "确认确认确认确认确认", "O_KR"),
    ("expected_key_result", "确认，确认，确认，确认", "O_KR"),
    ("process_description", "经理经理经理经理经理经理", "R"),
    ("process_description", "确认\n确认\n确认\n确认\n确认", "R"),
])
def test_filler_is_not_valid_kr_or_customer_fact(field, value, code):
    result = check({field: value})
    assert section(result, code).status == "needs_revision"
    assert section(result, "A2").status == "needs_revision"
    assert result.status == "needs_revision"


def test_repeated_real_terms_in_real_sentences_are_not_rejected():
    result = check({"process_description": "采购经理确认预算，技术经理确认范围，项目经理确认时间。"})
    assert section(result, "R").status == "met"


def test_warning_and_coverage_are_reflected_in_overall_summary():
    result = check({"is_appointment": False})
    assert section(result, "A1").status == "needs_revision"
    assert result.status == "needs_revision"
    assert "未发现明显规范问题" not in result.feedback_text
    result = check(omitted=("process_description",))
    assert result.status == "review"
    assert section(result, "A2").unreceived_fields == ["process_description"]
    assert "接口未获取：“过程详细描述”" in result.feedback_text
    assert "接口未获取：“评价”" not in result.feedback_text
    assert "建议补充“过程详细描述”" not in result.feedback_text
    assert "暂不能给出完整结论" in result.feedback_text


def test_missing_date_only_lists_date_not_received_contact_date():
    payload = complete_precheck_payload()
    payload["visit"]["metadata"] = {
        "source_supplied_fields": list(payload["visit"]),
        "precheck_defaulted_fields": ["visit_date"],
    }
    result = TaoranAgent().precheck(PrecheckRequest.model_validate(payload))
    assert section(result, "N").unreceived_fields == ["visit_date"]
    assert result.status == "review"


@pytest.mark.parametrize("code", ["T", "A1", "O_KR", "R", "A2", "N"])
def test_post_model_cannot_skip_any_section(code):
    def incomplete(data):
        payload = deep_payload(data)
        item = next(s for s in payload["sections"] if s["code"] == code)
        item.update(verdict="not_evaluated", evidence=[])
        return payload
    reviewer, calls = reviewer_for(incomplete)
    try:
        result = reviewer.review_q34(visit())
        assert result.status == "fallback"
        assert result.failure_reason == "required_analysis_not_completed"
        assert len(calls) == 1
    finally:
        reviewer.close()


@pytest.mark.parametrize("fact", ["key_result_quality_ok", "process_fact_based", "next_action_logic_ok"])
def test_passing_section_cannot_conflict_with_its_negative_fact(fact):
    def conflicting(data):
        payload = deep_payload(data)
        payload["facts"][fact] = False
        return payload
    reviewer, _ = reviewer_for(conflicting)
    try:
        result = reviewer.review_q34(visit())
        assert result.status == "fallback"
        assert result.failure_reason == "section_fact_conflict"
    finally:
        reviewer.close()


def test_a2_pass_cannot_disagree_with_actual_achievement():
    def conflicting(data):
        payload = deep_payload(data)
        payload["facts"]["purpose_achievement"] = "not_achieved"
        return payload
    reviewer, _ = reviewer_for(conflicting)
    try:
        result = reviewer.review_q34(visit())
        assert result.failure_reason == "assessment_fact_conflict"
    finally:
        reviewer.close()


def test_one_true_fact_does_not_force_whole_section_to_pass():
    def other_gap(data):
        payload = deep_payload(data)
        payload["sections"][2].update(verdict="needs_revision", suggestion="核对拜访目的。")
        return payload
    reviewer, _ = reviewer_for(other_gap)
    try:
        assert reviewer.review_q34(visit()).status == "completed"
    finally:
        reviewer.close()


def test_honest_not_achieved_is_not_automatically_inconsistent():
    def negative(data):
        payload = deep_payload(data)
        payload["facts"]["purpose_achievement"] = "not_achieved"
        return payload
    reviewer, _ = reviewer_for(negative)
    draft = visit().model_copy(update={"self_assessment": SelfAssessment.NOT_ACHIEVED})
    try:
        assert reviewer.review_q34(draft).status == "completed"
    finally:
        reviewer.close()


def test_incomplete_model_never_writes_or_displays_default_t_a_pass(monkeypatch):
    def incomplete(data):
        payload = deep_payload(data)
        payload["sections"][0].update(verdict="not_evaluated", evidence=[])
        return payload
    reviewer, _ = reviewer_for(incomplete)
    request = evaluation_request()
    try:
        result = TaoranAgent(reviewer).evaluate(request, "incomplete-no-write")
    finally:
        reviewer.close()
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid model output must not write to Jiandaoyun")
    monkeypatch.setattr("taoran_agent.writeback.httpx.post", forbidden)
    assert writeback_evaluation(model_settings(), request, result).status == "failed"
    assert "客户类型和商机阶段信息满足" not in result.ai_opinion
    assert "预约状态和拜访方式与" not in result.ai_opinion
    # Also guard the formatter when handed older persisted partial model output.
    facts = deepcopy(result.semantic_facts)
    facts.provider, facts.status = "llm-chat", "completed"
    feedback = build_evaluation_feedback(request.visit, 50, 50, 100, [], facts)
    assert "客户类型：本项检测未完成" in feedback
