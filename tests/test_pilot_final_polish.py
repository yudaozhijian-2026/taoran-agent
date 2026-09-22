"""Regression coverage for the pilot-facing wording polish.

These tests exercise only the read-only Front and Backend wording projections.
They intentionally do not invoke or change the scoring, semantic-review,
artifact, writeback, or production integration paths.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from test_backend_salesperson_wording_v2 import facts as backend_facts
from test_backend_salesperson_wording_v2 import render as render_backend
from test_backend_salesperson_wording_v2 import visit as backend_visit
from test_front_analysis_artifact import visit as front_visit

from taoran_agent.backend_salesperson_wording_v2 import render_backend_safe_feedback
from taoran_agent.front_quick_check_wording_v2 import project
from taoran_agent.models import VisitDraftInput
from taoran_agent.pilot_final_consistency import (
    contact_plan_state,
    customer_response_state,
    goal_presentation_state,
    protected_tokens,
    source_text,
    visible_consistency_errors,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "pilot_final_polish_100.json"
_CUSTOMER_TYPES = {"潜力客户": "potential", "目标客户": "target", "商机客户": "opportunity"}
_VISIT_METHODS = {
    "面对面拜访": "face_to_face",
    "视频会议": "video",
    "电话拜访": "phone",
    "微信/邮件/QQ沟通": "asynchronous_message",
}
_ASSESSMENTS = {"达到目的": "achieved", "部分达到目的": "partially_achieved", "未达到目的": "not_achieved"}


def _backend(*, source: str, status: str = "partially_achieved", **updates):
    consensus = updates.pop("consensus", True)
    visit = backend_visit(process_description=source, **updates)
    semantic_facts = backend_facts(visit, source=source, status=status, consensus=consensus)
    return visit, semantic_facts, render_backend(visit, semantic_facts)


def _fixture_visit(snapshot: dict) -> VisitDraftInput:
    """Normalize the immutable raw snapshots only for the wording projector."""
    data = dict(snapshot)
    data["customer_type_ii"] = _CUSTOMER_TYPES.get(data.get("customer_type_ii"), data.get("customer_type_ii"))
    data["visit_method"] = _VISIT_METHODS.get(data.get("visit_method"), data.get("visit_method"))
    data["self_assessment"] = _ASSESSMENTS.get(data.get("self_assessment"), data.get("self_assessment"))
    data["is_appointment"] = {"是": True, "否": False}.get(data.get("is_appointment"), data.get("is_appointment"))
    return VisitDraftInput.model_validate(data)


def test_customer_no_response_never_becomes_responded():
    source = "销售已发送方案，客户暂未回复本次培训安排。"
    _, _, (analysis, advice) = _backend(source=source, consensus=False, customer_type_ii="opportunity")
    visible = analysis + "\n" + "\n".join(advice)
    assert customer_response_state(source).state == "no_response"
    assert "当前尚未记录客户对该事项的明确回应。" in visible
    assert "客户已经对相关事项作出回应" not in visible


def test_partial_response_consistent_across_sections():
    source = "客户确认已收到方案，但对培训日期暂未回复。"
    _, _, (analysis, advice) = _backend(source=source, consensus=False, customer_type_ii="opportunity")
    visible = analysis + "\n" + "\n".join(advice)
    assert customer_response_state(source).state == "partial_response"
    assert "客户已经对部分事项作出回应" in visible
    assert "客户尚未回应的事项" in visible


def test_aligned_self_assessment_no_mismatch_message():
    source = "客户确认参训部门为设备保障部，培训日期仍待排期。"
    visit, semantic_facts, _ = _backend(source=source)
    for section in semantic_facts.sections:
        if section.code == "A2":
            section.verdict = "needs_revision"
            section.suggestion = "请核对自评。"
    semantic_facts.quality_audit["advice_basis"]["A2"] = {"fields": ["self_assessment"]}
    analysis, advice = render_backend(visit, semantic_facts)
    visible = analysis + "\n" + "\n".join(advice)
    assert "当前自评与本次已经取得的实际结果存在差异" not in visible
    assert "自评不一致" not in visible


def test_unresolved_never_becomes_not_achieved():
    source = "客户承诺下周提供设备清单。"
    _, _, (analysis, advice) = _backend(
        source=source,
        status="not_achieved",
        expected_key_result="项目顺利实施",
    )
    visible = analysis + "\n" + "\n".join(advice)
    assert "目标未达成" not in visible
    assert "本次目标未达到" not in visible


def test_safe_fallback_keeps_specific_unresolved_goal_neutral():
    source = "销售已发送方案，尚未取得客户确认。"
    visit, semantic_facts, _ = _backend(
        source=source,
        expected_key_result="确认培训日期",
    )
    semantic_facts.quality_audit["achievement_status"] = "unresolved"
    analysis, _ = render_backend_safe_feedback(visit, semantic_facts)
    assert "目标描述较宽" not in analysis
    assert "未达成" not in analysis


def test_original_goal_not_extended_by_customer_confirmation():
    source = "客户确认培训日期为9月28日。"
    visit, _, (analysis, _) = _backend(
        source=source,
        status="achieved",
        expected_key_result="确认培训日期",
        self_assessment="achieved",
    )
    assert visit.expected_key_result == "确认培训日期"
    assert "原目标" not in analysis
    assert "确认培训日期和客户确认" not in analysis


def test_visit_purpose_never_replaces_goal():
    source = "客户确认培训日期为9月28日。"
    visit, _, (analysis, _) = _backend(
        source=source,
        status="achieved",
        purpose_code="收集信息",
        expected_key_result="确认培训日期",
        self_assessment="achieved",
    )
    assert visit.expected_key_result == "确认培训日期"
    assert "本次目标为收集信息" not in analysis


def test_relative_contact_time_is_detected():
    plan = contact_plan_state({"process_description": "双方约定下周再次沟通培训排期。"})
    assert plan.state == "relative_time"


def test_event_trigger_contact_plan_is_detected():
    plan = contact_plan_state({"process_description": "安装完成后再联系核对。"})
    assert plan.state == "event_trigger"


def test_empty_contact_field_does_not_mean_no_plan():
    output = project(front_visit(
        next_contact_at=None,
        process_description="双方约定下周再次沟通培训排期。",
    ))
    assert output["contact_plan_state"] == "relative_time"
    assert "记录中也未看到明确的后续联系安排" not in output["feedback_text"]


def test_cadence_reference_never_becomes_requirement():
    front = project(front_visit(
        customer_type_ii="potential",
        next_contact_at=None,
        process_description="客户反馈设备运行稳定。",
    ))
    _, _, (analysis, advice) = _backend(
        source="客户反馈设备运行稳定。",
        next_contact_at=None,
    )
    visible = front["feedback_text"] + "\n" + analysis + "\n" + "\n".join(advice)
    assert not re.search(r"跨(?:北京时间)?(?:自然)?(?:月|季度)", visible)
    assert "必须" not in visible and "确保" not in visible


def test_actor_is_preserved():
    source = "销售决定暂不参加集团采购，待条件明确后再安排。"
    visit, semantic_facts, _ = _backend(source=source)
    semantic_facts.quality_audit["goal_reviews"] = [{
        "status": "partially_supported",
        "reason": "原目标要求确认培训日期，过程显示客户决定暂不参加集团采购，故为部分达成。",
    }]
    analysis, _ = render_backend(visit, semantic_facts)
    assert "销售决定暂不参加集团采购" in analysis
    assert "客户决定暂不参加集团采购" not in analysis


def test_product_code_is_preserved():
    source = "收到客户关于167kgLN-02二次招标通知，计划明天进行报价。"
    output = project(front_visit(process_description=source))
    _, _, (analysis, _) = _backend(source=source, expected_key_result="收集信息")
    assert "LN-02" in output["analysis"]
    assert "LN-02" in analysis
    assert "LN-02" in protected_tokens(analysis)


def test_numbers_are_preserved():
    source = "客户提出采购4张1600mm×600mm办公桌。"
    output = project(front_visit(process_description=source))
    assert {"4张", "1600mm", "600mm"} <= protected_tokens(output["analysis"])


def test_summary_never_hard_truncated():
    source = "销售已完成方案初稿并向客户说明，客户表示需要内部确认后再安排下一次沟通，当前尚未形成明确日期。"
    output = project(front_visit(process_description=source))
    assert "内部确认后再安排下一次沟通" in output["analysis"]
    assert output["analysis"].endswith("。")


def test_summary_ends_with_complete_sentence():
    output = project(front_visit(process_description="销售已完成方案初稿，客户表示继续确认。"))
    assert output["analysis"].endswith(("。", "！", "？"))
    assert not output["analysis"].split("。")[-1].startswith(("不过", "但是", "然而"))


def test_frontend_no_blocking_issue_consistency():
    output = project(
        front_visit(
            expected_key_result="确认培训日期",
            process_description="客户确认培训日期为9月28日，双方约定9月28日再次沟通。",
            next_action_expected_result="确认培训安排是否落实",
            next_contact_at="2026-09-28T09:00:00+08:00",
        ),
        front_review={"sections": [
            {"code": code, "verdict": "met", "reason": "记录一致"}
            for code in ("T", "A1", "O_KR", "R", "A2", "N")
        ]},
    )
    assert output["front_final_consistency"] == "no_blocking_issue"
    assert output["advice"] == "当前没有影响提交的明显问题。"


def test_frontend_final_consistency_gate_keeps_an_existing_high_priority_finding():
    output = project(
        front_visit(
            next_contact_at=None,
            process_description="客户反馈设备运行稳定。",
        ),
        front_review={"sections": [
            {"code": "R", "verdict": "needs_revision", "reason": "过程需要补充"},
        ]},
    )
    assert output["front_final_consistency"] == "blocking_issue"
    assert any(item["key"] == "front_final_consistency" for item in output["suggestions"])
    assert "过程事实" in output["advice"]


def test_backend_recommendations_max_three_if_safe():
    source = "销售计划下周发送方案，客户暂未回复。"
    visit, semantic_facts, _ = _backend(source=source, consensus=False)
    for section in semantic_facts.sections:
        section.verdict = "needs_revision"
        section.suggestion = "请根据实际情况补充记录。"
        semantic_facts.quality_audit["advice_basis"][section.code] = {
            "fields": ["process_description"],
        }
    _, advice = render_backend(visit, semantic_facts)
    assert len(advice) <= 3


def test_backend_keeps_independent_findings_when_a_three_item_cap_would_hide_them():
    source = "销售已发送方案，等待客户进一步确认。"
    visit, semantic_facts, _ = _backend(source=source)
    relevant = ("T", "A1", "O_KR", "R")
    for section in semantic_facts.sections:
        if section.code in relevant:
            section.verdict = "needs_revision"
            section.suggestion = f"建议核对{section.code}对应的实际记录。"
            semantic_facts.quality_audit["advice_basis"][section.code] = {
                "fields": ["process_description"],
            }
    _, advice = render_backend(visit, semantic_facts)
    visible = "\n".join(advice)
    assert all(code in visible for code in relevant)


def test_fixed_100_source_inputs_drive_front_content_regression():
    payload = json.loads(_FIXTURE.read_text())
    records = payload["records"]
    codes = {item["source_record_code"] for item in records}
    golden = {
        "BFJL2026092045697", "BFJL2026091745597", "BFJL2026091645535",
        "BFJL2026091745618", "BFJL2026091745602", "BFJL2026091745589",
        "BFJL2026092045705", "BFJL2026092045728", "BFJL2026091845660",
        "BFJL2026091645555", "BFJL2026091845644", "BFJL2026091445425",
        "BFJL2026092045696", "BFJL2026091445419",
    }
    assert len(records) == len(codes) == 100
    assert golden <= codes

    for record in records:
        visit = _fixture_visit(record["form_snapshot"])
        output = project(visit)
        raw = visit.model_dump(mode="json")
        goal = goal_presentation_state(
            visit.expected_key_result,
            output["achievement"],
            visit.self_assessment.value if visit.self_assessment else None,
            goal_quality=output["goal_state"],
        )
        errors = visible_consistency_errors(
            output["feedback_text"],
            response=customer_response_state(source_text(raw)),
            contact=contact_plan_state(raw),
            goal=goal,
        )
        assert not errors, (record["source_record_code"], errors, output["feedback_text"])

        backend_status = (
            "unresolved"
            if output["goal_state"] != "specific"
            else (visit.self_assessment.value if visit.self_assessment else "unresolved")
        )
        semantic_facts = backend_facts(
            visit,
            source=source_text(raw),
            status=(
                backend_status
                if backend_status != "unresolved"
                else "partially_achieved"
            ),
        )
        semantic_facts.quality_audit["achievement_status"] = backend_status
        analysis, advice = render_backend(visit, semantic_facts)
        backend_visible = analysis + "\n" + "\n".join(advice)
        backend_goal = goal_presentation_state(
            visit.expected_key_result,
            backend_status,
            visit.self_assessment.value if visit.self_assessment else None,
        )
        backend_errors = visible_consistency_errors(
            backend_visible,
            response=customer_response_state(source_text(raw)),
            contact=contact_plan_state(raw),
            goal=backend_goal,
        )
        assert not backend_errors, (
            record["source_record_code"], backend_errors, backend_visible
        )
        assert len(advice) <= 3
