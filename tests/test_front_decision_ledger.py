from taoran_agent.front_v46.decision_ledger import (
    build,
    deterministic_advice,
    with_validated_analysis,
)
from taoran_agent.front_v46.experimental_record_state import boundary_issues
from taoran_agent.front_v46.experimental_semantic_streaming_v22 import _supported_commitment
from taoran_agent.front_v46.joint_consistency import errors, repair_advice
from taoran_agent.models import VisitDraftInput


def visit(**updates):
    values = {
        "visit_date": "2026-09-17",
        "employee_id": "ledger-test",
        "customer_type_ii": "potential",
        "visit_method": "asynchronous_message",
        "is_appointment": False,
        "purpose_code": "其他目的",
        "other_purpose": "协助项目实施",
        "expected_key_result": "确认六台设备的安装位置及电源准备情况",
        "process_description": "客户已发来设备清单并确认安装位置。",
        "self_assessment": "achieved",
        "next_action_purpose": "保持关系",
        "next_action_expected_result": "保持联系",
        "next_contact_at": None,
    }
    values.update(updates)
    return VisitDraftInput(**values)


def test_next_action_composite_gap_binds_only_actual_fields():
    ledger = build(visit())
    required = {(item["code"], item["field"]) for item in ledger["required_advice"]}
    assert ("N", "next_action_expected_result") in required
    assert ("N", "next_contact_at") in required
    assert ("N", "next_action_purpose") not in required


def test_customer_time_boundary_is_kept_in_shared_ledger():
    ledger = build(visit(next_action_expected_result="跟进设备调试进度", next_contact_at="2026-09-28T10:00:00+08:00"))
    required = {(item["code"], item["field"]) for item in ledger["required_advice"]}
    assert ledger["contact_time_standard"] == "different_calendar_quarter"
    assert ("N", "next_contact_at") in required


def test_deterministic_advice_uses_actual_value_and_customer_time_standard():
    current = visit().model_dump(mode="json")
    advice = deterministic_advice(current, build(visit()))
    by_field = {item["field"]: item["text"] for item in advice}
    assert "保持联系" in by_field["next_action_expected_result"]
    assert "潜力客户" in by_field["next_contact_at"]
    assert "不同自然季度" in by_field["next_contact_at"]


def test_validated_analysis_is_shared_with_advice_stage():
    ledger = with_validated_analysis(build(visit()), "原定目标已达成，自评与事实一致。")
    assert ledger["validated_analysis"].startswith("原定目标已达成")
    assert errors(
        ledger["validated_analysis"],
        "请修改想取得的关键结果。",
        ledger,
    ) == ["goal_achievement_conflict"]


def test_joint_check_accepts_advice_for_actual_next_step_gaps():
    ledger = with_validated_analysis(
        build(visit()),
        "原定目标已达成，自评与事实一致。下一步保持联系不够具体，且联系时间未填写。",
    )
    assert not errors(
        ledger["validated_analysis"],
        "请将保持联系写成具体跟进事项，并补充联系时间。",
        ledger,
    )


def test_local_joint_repair_removes_only_conflicting_clause():
    analysis = "原定目标已达成，下一步联系时间未填写。"
    advice = "1、请修改想取得的关键结果。\n2、请补充下一次联系时间。"
    repaired, applied = repair_advice(
        analysis,
        advice,
        ["goal_achievement_conflict"],
    )
    assert applied == ["goal_achievement_conflict"]
    assert repaired == "1、请补充下一次联系时间。"


def test_local_joint_repair_defers_missing_ledger_to_model():
    advice = "请补充下一次联系时间。"
    repaired, applied = repair_advice("", advice, ["validated_analysis_missing"])
    assert repaired == advice
    assert applied == []


def test_training_schedule_does_not_impersonate_missing_contact_date():
    context = visit(next_contact_at=None).model_dump(mode="json")
    text = "客户说明计划下季度开展培训，参训名单尚未确定，预计时间需等待部门排期。"
    assert not boundary_issues(text, context)


def test_missing_contact_date_claim_still_requires_contact_scope():
    context = visit(next_contact_at=None).model_dump(mode="json")
    issues = boundary_issues("尚未确定下一次联系时间。", context)
    assert any(item.get("field") == "next_contact_at" for item in issues)


def test_recorded_commitment_paraphrase_keeps_business_payload():
    source = "过程记录：客户承诺下周一提供六台设备清单。"
    assert _supported_commitment("客户已确认下周一提供六台设备清单", source)
    assert _supported_commitment("客户提供六台设备清单的承诺", source)
    assert not _supported_commitment("客户提供报价单的承诺", source)
