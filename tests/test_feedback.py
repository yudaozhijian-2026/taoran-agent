import pytest
from test_agent import complete_precheck_payload
from test_llm import deep_payload, reviewer_for
from test_writeback import evaluation_request

from taoran_agent.agent import TaoranAgent
from taoran_agent.feedback import build_precheck_feedback
from taoran_agent.models import Issue, PrecheckRequest, Severity


@pytest.mark.parametrize("missing", [False, True])
def test_precheck_hides_provenance_but_keeps_advice_and_audit(missing):
    payload = complete_precheck_payload()
    if missing:
        payload["visit"]["expected_key_result"] = ""
    result = TaoranAgent().precheck(PrecheckRequest.model_validate(payload))
    assert "知识依据" not in result.feedback_text
    assert "分析方式" not in result.feedback_text
    assert "DSM-BS-000" not in result.feedback_text
    assert "检查结论：" in result.feedback_text
    assert "TAORAN六项检查：" in result.feedback_text
    assert "TAORAN六项检查（未达标）：" not in result.feedback_text
    assert "优先修改建议：" in result.feedback_text
    assert "/100" not in result.feedback_text
    assert result.knowledge_snapshot_hash
    assert {r.id for r in result.knowledge_references} == {"DSM-BS-000", "DSM-BS-01-07"}
    assert result.semantic_review.provider == "heuristic-v1"
    if missing:
        assert "O/KR｜" in result.feedback_text


@pytest.mark.parametrize("model_success", [False, True])
def test_post_feedback_hides_method_without_losing_analysis_or_failure_warning(model_success):
    reviewer, _ = reviewer_for(deep_payload if model_success else lambda data: {})
    try:
        result = TaoranAgent(reviewer).evaluate(evaluation_request(), "feedback-display-test")
    finally:
        reviewer.close()
    assert "知识依据" not in result.ai_opinion
    assert "分析方式" not in result.ai_opinion
    assert "glm-5.2" not in result.ai_opinion
    assert "综合得分：" in result.ai_opinion
    assert "TAORAN六项判断：" in result.ai_opinion
    assert "优先改进建议：" in result.ai_opinion
    assert result.semantic_facts.model == "glm-5.2"
    assert result.semantic_facts.prompt_version
    assert result.semantic_facts.model_attempts
    if model_success:
        assert result.total_score == 100
        assert "模型分析：" in result.ai_opinion
        assert "模型事实依据：" in result.ai_opinion
    else:
        assert "暂停正式评分回写" in result.ai_opinion


@pytest.mark.parametrize(("code", "name", "field", "standard_hint"), [
    ("TAORAN_TYPE_MISSING", "客户类型", "customer_type_ii", "客户分类II"),
    ("TAORAN_APPOINTMENT_MISSING", "预约与拜访方式", "is_appointment", "应优先预约"),
    ("TAORAN_KR_MISSING", "拜访目的与关键结果", "expected_key_result", "具体、可验证"),
    ("TAORAN_RESULT_MISSING", "过程事实与结果", "process_description", "个人判断和假设"),
    ("TAORAN_ASSESSMENT_MISSING", "达成评价", "self_assessment", "缺少达成证据"),
    ("TAORAN_NSA_TIME_NOT_AFTER_VISIT", "下一步客户行动", "next_contact_at", "同日不算晚于"),
])
def test_each_failed_section_displays_its_standard_analysis_and_suggestion(
    code, name, field, standard_hint,
):
    visit = PrecheckRequest.model_validate(complete_precheck_payload()).visit
    issue = Issue(
        code=code, dimension="test", severity=Severity.ERROR, field_paths=[field],
        message="本条记录中待核对的问题。", suggestion="请按实际情况核对后重新检测。",
    )
    feedback = build_precheck_feedback(visit, 90, "needs_revision", [issue], None)

    assert f"｜{name}：未达标。\n检查标准：" in feedback
    assert standard_hint in feedback
    assert feedback.count("检查标准：") == 6
    assert feedback.index("检查标准：") < feedback.index("数据分析：")
    assert feedback.index("数据分析：") < feedback.index("修改建议：")
    assert issue.message in feedback
    assert issue.suggestion in feedback
    assert field not in feedback
    assert "知识依据" not in feedback
    assert "分析方式" not in feedback


def test_passed_and_unreceived_sections_display_standards_with_accurate_status():
    payload = complete_precheck_payload()
    passed = TaoranAgent().precheck(PrecheckRequest.model_validate(payload))
    assert passed.feedback_text.count("检查标准：") == 6
    assert passed.feedback_text.count("：达标。") == 6
    assert "数据分析：" not in passed.feedback_text
    assert "\n修改建议：" not in passed.feedback_text

    payload["visit"].pop("process_description")
    payload["visit"]["metadata"] = {"source_supplied_fields": list(payload["visit"])}
    partial = TaoranAgent().precheck(PrecheckRequest.model_validate(payload))
    assert "R｜过程事实与结果：未检查。\n检查标准：" in partial.feedback_text
    assert "A｜达成评价：待复核。\n检查标准：" in partial.feedback_text
    assert partial.feedback_text.count("检查标准：") == 6
    assert partial.feedback_text.count("：达标。") == 4
    assert "建议补充“过程详细描述”" not in partial.feedback_text


def test_date_anomaly_shows_standard_and_actual_dates_together():
    payload = complete_precheck_payload()
    payload["visit"]["next_contact_at"] = "2026-08-17T10:00:00+08:00"
    result = TaoranAgent().precheck(PrecheckRequest.model_validate(payload))
    feedback = result.feedback_text

    assert '检查标准：“下一次联系客户时间安排”应晚于“拜访日期”' in feedback
    assert "数据分析：下一次联系客户时间安排：异常。异常说明：" in feedback
    assert "2026-08-17" in feedback and "2026-08-18" in feedback
    assert "按实际拜访及后续联系计划修正日期" in feedback
    assert feedback.count("检查标准：") == 6
    assert feedback.count("：达标。") == 5
    assert result.can_submit is True and result.submission_blocked is False


def test_no_received_fields_still_show_six_standards_without_passing():
    payload = complete_precheck_payload()
    payload["visit"]["metadata"] = {"source_supplied_fields": []}
    result = TaoranAgent().precheck(PrecheckRequest.model_validate(payload))

    assert result.feedback_text.count("检查标准：") == 6
    assert result.feedback_text.count("：未检查。") == 6
    assert "：达标。" not in result.feedback_text
    assert "：未达标。" not in result.feedback_text
    assert "请检查AI检测按钮的字段传递配置" in result.feedback_text


def test_default_date_does_not_make_next_action_display_as_passed():
    payload = complete_precheck_payload()
    payload["visit"]["metadata"] = {
        "source_supplied_fields": list(payload["visit"]),
        "precheck_defaulted_fields": ["visit_date"],
    }
    result = TaoranAgent().precheck(PrecheckRequest.model_validate(payload))

    assert "N｜下一步客户行动：待复核。\n检查标准：" in result.feedback_text
    assert "尚不能判定整项达标" in result.feedback_text
    assert "N｜下一步客户行动：达标" not in result.feedback_text


def test_missing_engine_results_are_not_assumed_passed_by_feedback_builder():
    visit = PrecheckRequest.model_validate(complete_precheck_payload()).visit
    feedback = build_precheck_feedback(visit, 100, "passed", [], None)

    assert feedback.count("检查标准：") == 6
    assert feedback.count("：待复核。") == 6
    assert "：达标。" not in feedback


def test_scope_notice_does_not_turn_passed_check_into_failure():
    payload = complete_precheck_payload()
    payload["visit"]["visit_method"] = "video"
    result = TaoranAgent().precheck(PrecheckRequest.model_validate(payload))

    assert "A｜预约与拜访方式：达标。\n检查标准：" in result.feedback_text
    assert "说明：权威TAORAN标准的正式适用范围为TOB面对面销售" in result.feedback_text
    assert "A｜预约与拜访方式：未达标" not in result.feedback_text
    assert result.feedback_text.count("检查标准：") == 6


def test_actual_warning_is_still_shown_as_unmet_with_standard():
    payload = complete_precheck_payload()
    payload["visit"]["is_appointment"] = False
    result = TaoranAgent().precheck(PrecheckRequest.model_validate(payload))

    assert "A｜预约与拜访方式：未达标。\n检查标准：" in result.feedback_text
    assert "数据分析：当前客户类型的本次拜访未体现预约" in result.feedback_text
    assert result.feedback_text.count("检查标准：") == 6
