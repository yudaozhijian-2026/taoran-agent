from copy import deepcopy
from queue import Queue
from types import SimpleNamespace

import pytest
from test_front_analysis_artifact import visit

from taoran_agent import api
from taoran_agent.front_quick_check_wording_v2 import enabled, project
from taoran_agent.models import PrecheckRequest, RequestContext, SelfAssessment


def render(**updates):
    return project(visit(**updates))


def test_no_mechanical_form_repetition():
    text = render()["feedback_text"]
    assert all(x not in text for x in ("原目标为", "原定目标", "客户类型为", "拜访方式为", "自评一致"))


def contact_advice(output):
    return next(s for s in output["suggestions"] if s["key"] == "contact")


def test_confirmed_next_contact_exact_date():
    output = render(process_description="双方约定9月28日再次沟通。", next_contact_at=None)
    assert output["contact_state"] == "agreed"
    assert output["contact_timing_kind"] == "exact"
    assert "9月28日" in contact_advice(output)["text"]
    assert "同步填写" in contact_advice(output)["text"]


def test_confirmed_next_contact_relative_date():
    output = render(process_description="销售已与客户约定一周后再次沟通。", next_contact_at=None)
    assert output["contact_state"] == "agreed"
    assert output["contact_timing_kind"] == "relative"
    assert "一周后再次沟通" in contact_advice(output)["text"]
    assert "如果已约定" not in output["feedback_text"]


def test_confirmed_next_contact_after_event():
    output = render(
        process_description="客户确认本周五完成电源准备，届时再联系核对。",
        next_contact_at=None,
    )
    assert output["contact_state"] == "agreed"
    assert output["contact_timing_kind"] == "after_event"
    assert "本周五再次联系核对" in contact_advice(output)["text"]


def test_future_customer_event_not_contact_agreement():
    output = render(process_description="客户确认下周一提供设备清单。", next_contact_at=None)
    advice = contact_advice(output)
    assert output["contact_state"] == "unknown"
    assert advice["suggestion_basis"]["source"] == "future_customer_event"
    assert "还没有明确联系时间" in advice["text"]
    assert "下周一填写" not in advice["text"]


def test_missing_contact_time_potential_customer_cadence():
    output = render(
        customer_type_ii="potential",
        process_description="客户反馈设备运行稳定。",
        next_contact_at=None,
    )
    advice = contact_advice(output)["text"]
    assert "规划联系时间" in advice
    assert "可以参考跨自然季度" in advice
    assert "具体时间以客户实际推进情况为准" in advice
    assert "必须" not in advice


def test_missing_contact_time_target_customer_cadence():
    output = render(
        customer_type_ii="target",
        process_description="客户反馈设备运行稳定。",
        next_contact_at=None,
    )
    advice = contact_advice(output)["text"]
    assert "规划联系时间" in advice
    assert "可以参考跨自然月" in advice
    assert "必须" not in advice


def test_unresolved_preserves_stage_progress():
    output = render(expected_key_result="项目顺利实施", self_assessment="achieved",
                    process_description="客户承诺下周提供六台设备清单。")
    assert output["achievement"] == "unresolved"
    assert "客户明确将在下周提供六台设备清单" in output["analysis"]
    assert "不足以判断是否已经完整实现" in output["analysis"]
    assert "未达到" not in output["feedback_text"]
    assert "assessment" not in {x["key"] for x in output["suggestions"]}


def test_placeholder_goal_uses_missing_goal_wording_and_preserves_facts():
    output = render(
        expected_key_result="-",
        self_assessment="achieved",
        process_description="客户承诺下周提供六台设备清单。",
    )
    assert output["goal_state"] == "missing_placeholder"
    assert output["goal_assessable"] is False
    assert output["goal_source"] == "key_result"
    assert output["achievement"] == "unresolved"
    assert "当前没有填写可用于判断达成情况的具体关键结果，因此无法据此判断本次目标是否达成。" in output["analysis"]
    assert "关键结果表述比较宽" not in output["analysis"]
    assert "提供六台设备清单" in output["analysis"]


def test_broad_goal_keeps_broad_goal_wording():
    output = render(
        expected_key_result="项目顺利实施",
        process_description="客户承诺下周提供六台设备清单。",
    )
    assert output["goal_state"] == "broad"
    assert output["goal_assessable"] is False
    assert "当前关键结果表述比较宽，现有记录不足以判断是否已经完整实现。" in output["analysis"]


def test_normal_assessment_silent_and_clear_conflict_visible():
    v = visit(expected_key_result="确认预算", process_description="客户确认预算。", self_assessment="achieved")
    normal = project(v, front_review={"sections": [{"code": "A2", "verdict": "met", "reason": "自评一致"}]})
    assert "自评" not in normal["feedback_text"]
    assert normal["achievement"] == "achieved"
    conflict = project(v.model_copy(update={"self_assessment": SelfAssessment.NOT_ACHIEVED}), front_review={
        "sections": [{"code": "A2", "verdict": "needs_revision", "reason": "实际已达到，自评未达到。"}]})
    assert any(s["key"] == "assessment" for s in conflict["suggestions"])


def test_no_new_sales_strategy_and_all_suggestions_have_basis():
    output = render(expected_key_result="收集信息", process_description="客户需要4张1600×600办公桌。",
                    next_action_expected_result="保持联系", next_contact_at=None)
    assert not any(word in output["advice"] for word in ("报价", "供货", "客户已经同意", "客户已经确认"))
    assert all(x["suggestion_basis"]["source"] and x["suggestion_basis"]["fields"] for x in output["suggestions"])


def test_existing_commitment_can_ground_next_step():
    output = render(expected_key_result="项目顺利实施", process_description="客户承诺下周提供设备清单。",
                    next_action_expected_result="保持联系")
    advice = next(x for x in output["suggestions"] if x["key"] == "next_result")
    assert "清单" in advice["text"]
    assert any("客户承诺" in x["quote"] for x in advice["suggestion_basis"]["evidence"])


def test_never_turns_future_into_completed_fact():
    source = "同梁老师电话沟通按单位最新要求重新做资料，并下班前给到他，他尽快处理安排签字流程。"
    output = render(process_description=source)
    assert "已在下班前" not in output["feedback_text"]
    for evidence in output["analysis_basis"]:
        assert evidence["quote"] in source


def test_at_most_three_prioritized_suggestions():
    output = render(expected_key_result="项目顺利实施", process_description=None,
                    next_action_expected_result="保持联系", next_contact_at=None,
                    next_action_purpose=None)
    assert len(output["suggestions"]) == 3
    assert [s["priority"] for s in output["suggestions"]] == sorted(s["priority"] for s in output["suggestions"])
    assert output["suggestions"][0]["key"] == "goal"


def test_customer_agreement_not_overridden_by_cadence():
    output = render(customer_type_ii="potential", process_description="客户确认下周一联系。")
    assert not any(s["key"] == "contact" for s in output["suggestions"])
    assert "跨季度" not in output["feedback_text"]


def test_valid_contact_time_no_cadence_warning():
    output = render(
        customer_type_ii="target",
        visit_date="2026-09-21",
        next_contact_at="2026-09-28T08:00:00Z",
        process_description="客户反馈设备运行稳定。",
    )
    assert not any(s["key"] == "contact" for s in output["suggestions"])
    assert "跨自然月" not in output["feedback_text"]


def test_contact_record_field_conflict():
    output = render(
        visit_date="2026-09-21",
        next_contact_at="2026-09-29T08:00:00Z",
        process_description="双方约定9月28日再次沟通。",
    )
    assert "与记录中的实际约定不一致" in contact_advice(output)["text"]


def test_evidence_specific_suggestion():
    output = render(
        process_description="客户确认培训预算已初步预留，但内部审批负责人尚未明确。",
        next_action_expected_result="保持联系",
    )
    advice = next(item for item in output["suggestions"] if item["key"] == "next_result")
    assert "内部审批负责人" in advice["text"]
    assert advice["suggestion_basis"]["evidence"]
    assert "按已有计划" not in advice["text"]


def test_no_long_raw_record_echo():
    source = (
        "目的：办公桌咨询 过程： 因为客户之前来参观过我们公司，此次客户想要了解一下"
        "我们组合的办公桌，想要采购4张办公桌，规格是1600mm×600mm，跟客户回复先帮"
        "客户了解下，具体的再来回复客户。"
    )
    output = render(expected_key_result="收集信息", process_description=source)
    assert "4张1600×600mm组合办公桌" in output["analysis"]
    assert "因为客户之前来参观过我们公司" not in output["analysis"]
    assert len(output["analysis"]) < len(source)


def test_purchase_evidence_does_not_become_installation_fact():
    output = render(
        process_description=(
            "客户确认采购预算审批已通过，并明确采购负责人为李主任；"
            "销售取得设备清单和审批邮件。"
        )
    )
    assert "预算审批已通过" in output["analysis"]
    assert "采购负责人" in output["analysis"]
    assert "安装位置" not in output["analysis"]


def test_no_record_highlight_template_overuse():
    assert "记录中的重点是" not in render()["feedback_text"]


def test_only_isolated_confirmation_flow_enabled():
    assert enabled(SimpleNamespace(environment="isolated-submit-test", submit_confirmation_enabled=True))
    assert not enabled(SimpleNamespace(environment="production", submit_confirmation_enabled=True))
    assert not enabled(None)


def test_full_artifact_and_deep_review_input_unchanged(monkeypatch):
    from taoran_agent.front_analysis_artifact import build_artifact
    from taoran_agent.front_v46 import POLICY_VERSION, joint_consistency
    from taoran_agent.front_v46.decision_ledger import build, with_validated_analysis

    v = visit(expected_key_result="项目顺利实施", process_description="客户承诺下周提供设备清单。",
              next_action_expected_result="保持联系", next_contact_at=None)
    req = PrecheckRequest(context=RequestContext(tenant_id="tenant-a", user_id="sales-a", request_id="test"), visit=v)
    analysis = "原目标为项目顺利实施，自评达到与事实一致。"
    full = f"本次拜访分析：{analysis}\n\nAI改善建议：\n1. 请补充具体日期。\n2. 请补充下一步。\n3. 请核对自评。\n4. 请核对过程。"
    review = {"sections": [{"code": c, "verdict": "needs_revision", "reason": "待确认",
                           "evidence": [{"field": "process_description", "quote": "客户承诺下周提供设备清单"}]}
                          for c in ("T", "A1", "O_KR", "R", "A2", "N")]}
    initial = deepcopy(review)
    saved = []
    monkeypatch.setattr(api, "get_store", lambda _: SimpleNamespace(save_front_analysis_artifact=lambda a, **kw: saved.append(a)))
    monkeypatch.setattr(api, "_quick_check_run_preview", lambda *a, **kw: {"status": "completed", "feedback_text": analysis})
    monkeypatch.setattr(api, "_quick_check_run_final", lambda *a, **kw: {"status": "completed", "feedback_text": full, "front_review": review})
    monkeypatch.setattr(joint_consistency, "errors", lambda *a: [])
    events = Queue()
    out = api._quick_check_run(req, SimpleNamespace(environment="isolated-submit-test", submit_confirmation_enabled=True,
                              quick_check_recovery_ttl_seconds=60), events,
                              check_id="qc", quick_check_input_hash="a"*64)
    expected = build_artifact(visit=v, tenant_id="tenant-a", user_id="sales-a", check_id="qc",
                             quick_check_input_hash="a"*64, source_record_id=None, feedback_text=full,
                             decision_ledger=with_validated_analysis(build(v), analysis), front_review=review,
                             policy_version=POLICY_VERSION).model_dump(mode="json")
    for key in expected:
        if key not in {"artifact_id", "generated_at"}:
            assert saved[0][key] == expected[key], key
    assert len(saved[0]["findings"]) == 6
    assert len(saved[0]["suggestions"]) == 4
    assert review == initial
    assert len(out["final"]["front_wording_v2"]["suggestions"]) <= 3
    assert "原目标为" not in out["final"]["feedback_text"]
    assert not {"q33_score", "q34_score", "total_score"} & out["final"].keys()
    while not events.empty():
        assert "原目标为" not in events.get().get("text", "")


def test_missing_transport_fields_are_system_notices_only():
    v = visit(metadata={"source_supplied_fields": ["visit_date", "expected_key_result", "process_description"]})
    result = project(v)
    assert result["system_notices"]
    assert "系统提示：" in result["advice"]
    assert not any(x["key"] == "contact" for x in result["suggestions"])
    assert not any("系统" in x["text"] for x in result["suggestions"])


@pytest.mark.parametrize("self_value,expected", [("achieved", "achieved"), ("partially_achieved", "partially_achieved"), ("not_achieved", "not_achieved")])
def test_explicit_consistent_findings_preserve_all_outcomes(self_value, expected):
    result = project(visit(self_assessment=self_value), front_review={"sections": [
        {"code": "A2", "verdict": "met", "reason": "自评与实际一致"}]})
    assert result["achievement"] == expected
    assert not any(x["key"] == "assessment" for x in result["suggestions"])


def test_projection_does_not_change_q33_q34_or_total():
    from test_front_analysis_artifact import request

    from taoran_agent.agent import TaoranAgent
    req = request(visit())
    before = req.model_dump(mode="json")
    agent = TaoranAgent()
    baseline = agent.evaluate(req, "before")
    project(req.visit)
    after = agent.evaluate(req, "after")
    assert req.model_dump(mode="json") == before
    assert (baseline.q33_score, baseline.q34_score, baseline.total_score) == (
        after.q33_score, after.q34_score, after.total_score)


def test_missing_contact_time_does_not_invalidate_specific_next_result():
    v = visit(next_action_expected_result="确认设备运行情况与客户使用反馈", next_contact_at=None)
    result = project(v, findings=[{"dimension": "N", "conclusion": "needs_revision",
                     "statement": "下一步主题衔接但联系时间未明确，请补充日期。"}])
    assert [s["key"] for s in result["suggestions"]] == ["contact"]
