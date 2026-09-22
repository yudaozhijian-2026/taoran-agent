from copy import deepcopy

import pytest

from taoran_agent.backend_salesperson_wording_v2 import (
    VERSION,
    render_backend_business_feedback,
)
from taoran_agent.contact_policy import contact_policy
from taoran_agent.models import ModelSectionAnalysis, Q34SemanticFacts, VisitDraftInput


def visit(**changes) -> VisitDraftInput:
    data = {
        "visit_date": "2026-09-17",
        "employee_id": "E1",
        "customer_id": "C1",
        "customer_type_ii": "potential",
        "visit_method": "asynchronous_message",
        "is_appointment": True,
        "purpose_code": "保持关系",
        "expected_key_result": "确认培训日期、参训部门和资料清单",
        "process_description": "客户确认参训部门为设备保障部并已收到资料，培训日期仍待排期。",
        "self_assessment": "partially_achieved",
        "next_action_purpose": "收集信息",
        "next_action_expected_result": "测试",
        "next_contact_at": None,
    }
    data.update(changes)
    return VisitDraftInput.model_validate(data)


def facts(v: VisitDraftInput, *, status="partially_achieved", source=None, fields=None,
          consensus=True, n_reason="下一步期望结果不具体，下一次联系时间未填写。",
          n_suggestion="建议补充具体期望结果，并填写下一次联系时间，确保跨自然季度。"):
    sections = [
        ModelSectionAnalysis(
            code=code,
            verdict="needs_revision" if code == "N" else "met",
            field_paths=list(fields or ["next_action_expected_result", "next_contact_at"])
            if code == "N" else [],
            reason=n_reason if code == "N" else "记录清楚。",
            suggestion=n_suggestion if code == "N" else "",
            evidence=[],
        )
        for code in ("T", "A1", "O_KR", "R", "A2", "N")
    ]
    policy = contact_policy(v)
    return Q34SemanticFacts(
        provider="llm-test",
        key_result_quality_ok=True,
        process_fact_based=True,
        purpose_achievement=status,
        next_action_logic_ok=False,
        customer_consensus_met=consensus,
        reason="原目标要求确认培训日期、参训部门和资料清单，过程已确认参训部门和资料，但日期未确认，故为部分达成。",
        sections=sections,
        quality_audit={
            "achievement_status": status,
            "goal_reviews": [{
                "status": "partially_supported",
                "reason": "原目标要求确认培训日期、参训部门和资料清单，过程已确认参训部门和资料，但日期未确认，故为部分达成。",
            }],
            "actual_outcomes": [{"field": "process_description", "quote": v.process_description}],
            "authoritative_checks": {
                "source_text": source if source is not None else v.process_description,
                "next_contact_policy": policy,
            },
            "advice_basis": {
                "N": {
                    "fields": list(fields or ["next_action_expected_result", "next_contact_at"]),
                    "gap_kind": "insufficient_specificity",
                }
            },
        },
    )


def render(v, f):
    return render_backend_business_feedback(
        v,
        f,
        f.reason,
        [section.suggestion for section in f.sections if section.suggestion],
    )


def test_version_and_pure_wording_layer_do_not_mutate_formal_result():
    v = visit()
    f = facts(v)
    before_visit = deepcopy(v.model_dump())
    before_facts = deepcopy(f.model_dump())
    analysis, advice = render(v, f)
    assert VERSION == "BACKEND-SALESPERSON-WORDING-V2.1-20260922"
    assert analysis and advice
    assert v.model_dump() == before_visit
    assert f.model_dump() == before_facts


def test_case_1_vague_follow_up_becomes_customer_result_coaching():
    v = visit(next_action_expected_result="继续跟进")
    f = facts(v)
    analysis, advice = render(v, f)
    visible = analysis + "\n" + "\n".join(advice)
    assert "继续跟进" in visible
    assert "推动客户确认、提供、决定或完成" in visible
    assert "逻辑不完整" not in visible


def test_case_2_sales_action_is_separated_from_customer_result():
    v = visit(next_action_expected_result="发送方案")
    f = facts(v, fields=["next_action_expected_result"],
              n_suggestion="当前只有发送方案这一销售动作，建议明确希望客户确认或反馈什么。")
    analysis, advice = render(v, f)
    assert "发送方案" in analysis + "".join(advice)
    assert "客户确认或反馈" in "".join(advice)


def test_case_3_no_customer_response_uses_business_fact_not_consensus_label():
    v = visit(customer_type_ii="opportunity", process_description="销售计划下周发送方案。")
    f = facts(v, source=v.process_description, consensus=False,
              fields=["process_description"], n_suggestion="客户共识不足，请确认下一步。")
    analysis, advice = render(v, f)
    visible = analysis + "".join(advice)
    assert "主要体现了销售侧计划" in visible
    assert "客户共识不足" not in visible


def test_case_4_conditional_customer_intent_is_preserved():
    source = "客户表示如果预算批准，可以安排试用。"
    v = visit(customer_type_ii="opportunity", process_description=source)
    f = facts(v, source=source, consensus=False, fields=["process_description"])
    analysis, advice = render(v, f)
    visible = analysis + "".join(advice)
    assert "有条件的推进意向" in visible
    assert "条件是否满足" in visible
    assert "没有共识" not in visible


def test_case_5_partial_goal_leads_with_obtained_progress():
    v = visit()
    f = facts(v)
    analysis, _ = render(v, f)
    assert analysis.startswith("本次已确认参训部门和资料")
    assert "更接近部分完成" in analysis
    assert not analysis.startswith("原目标")


def test_case_6_not_assessable_is_not_rendered_as_failed():
    v = visit(expected_key_result="项目顺利实施", process_description="客户承诺下周提供设备清单。")
    f = facts(v, status="not_achieved")
    f.quality_audit["achievement_status"] = "unresolved"
    f.quality_audit["goal_reviews"] = [{"status": "not_assessable", "reason": "原目标项目顺利实施过于宽泛无法核验。"}]
    analysis, _ = render(v, f)
    assert "不足以判断" in analysis
    assert "目标未达成" not in analysis


def test_case_7_broad_goal_keeps_actual_outcome():
    v = visit(expected_key_result="收集信息", process_description="客户明确需要4张1600mm×600mm办公桌。")
    f = facts(v)
    f.quality_audit["goal_reviews"] = [{"status": "partially_supported", "reason": "原目标收集信息比较宽泛，无法判断是否完成。"}]
    f.quality_audit["actual_outcomes"] = [{"field": "process_description", "quote": v.process_description}]
    analysis, _ = render(v, f)
    assert "4张1600mm×600mm办公桌" in analysis
    assert "目标本身比较宽" in analysis


def test_case_8_missing_date_is_soft_and_uses_actual_customer_schedule():
    v = visit(next_contact_at=None)
    f = facts(v)
    _, advice = render(v, f)
    visible = "".join(advice)
    assert "结合客户实际安排" in visible
    assert "跨自然季度" not in visible
    assert "必须" not in visible and "确保" not in visible


def test_case_9_confirmed_customer_schedule_overrides_generic_period_advice():
    source = "客户确认9月28日再次沟通培训排期。"
    v = visit(next_contact_at="2026-09-28T09:00:00+08:00", process_description=source)
    f = facts(v, source=source, fields=["next_contact_at"])
    assert f.quality_audit["authoritative_checks"]["next_contact_policy"]["period_met"] is False
    _, advice = render(v, f)
    visible = "".join(advice)
    assert "跨自然季度" not in visible
    assert "改变已经形成的客户约定" not in visible


def test_case_10_sales_only_date_plan_requests_confirmation_not_period_compliance():
    source = "销售计划9月28日联系客户沟通培训排期。"
    v = visit(next_contact_at="2026-09-28T09:00:00+08:00", process_description=source)
    f = facts(v, source=source, fields=["next_contact_at"])
    _, advice = render(v, f)
    visible = "".join(advice)
    assert "核实客户是否方便" in visible
    assert "跨自然季度" not in visible
    assert "必须" not in visible


def test_case_11_date_not_after_visit_remains_clear_hard_problem():
    v = visit(next_contact_at="2026-09-17T09:00:00+08:00")
    f = facts(v, fields=["next_contact_at"])
    analysis, advice = render(v, f)
    visible = analysis + "".join(advice)
    assert "不晚于本次拜访日期" in visible
    assert "重新确认一个后续可执行的联系时间" in visible


def test_polite_hard_cadence_clause_is_removed_without_sentence_fragment():
    v = visit(next_contact_at=None)
    f = facts(
        v,
        fields=["next_contact_at"],
        n_suggestion=(
            "请在下一次联系客户时间安排中填写晚于本次拜访日期且跨自然季度的"
            "具体日期，以符合要求。"
        ),
    )
    analysis, advice = render(v, f)
    visible = analysis + "\n" + "\n".join(advice)
    assert "原目标要求" not in visible
    assert "跨自然季度" not in visible
    assert "以。" not in visible
    assert "当前还没有明确下一次联系时间" in visible


@pytest.mark.parametrize("forbidden", [
    "next_action_logic_ok", "customer_consensus_met", "needs_revision", "not_evaluated",
    "N-01", "finding_id", "decision_ledger", "field_paths", "validator", "schema",
    "逻辑不成立", "逻辑不完整", "客户共识不足",
])
def test_salesperson_visible_text_never_exposes_internal_terms(forbidden):
    v = visit()
    f = facts(v)
    analysis, advice = render(v, f)
    assert forbidden not in analysis + "\n" + "\n".join(advice)


def test_analysis_is_one_paragraph_with_front_style_sentence_flow():
    v = visit(next_action_expected_result="继续确认培训安排", next_contact_at=None)
    f = facts(v, consensus=False, fields=["next_action_expected_result", "next_contact_at"])
    for section in f.sections:
        if section.code == "A2":
            section.verdict = "needs_revision"
            section.suggestion = "请结合实际进展核对自评。"
    f.quality_audit["advice_basis"]["A2"] = {"fields": ["self_assessment"]}
    analysis, _ = render(v, f)
    assert "\n" not in analysis
    assert analysis.endswith("。")
    assert "当前还没有明确下一次联系时间。" in analysis
