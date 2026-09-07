from datetime import UTC, date, datetime

from taoran_agent.experimental_semantic_streaming_v22 import (
    _interactive_messages,
    _interactive_preview_safe,
    _interactive_snapshot,
    _snapshot,
)
from taoran_agent.models import VisitDraftInput


def visit():
    return VisitDraftInput(
        visit_date=date(2026, 8, 25), employee_id="u", customer_id="c",
        customer_type_ii="opportunity", visit_method="face_to_face",
        purpose_code="收集信息", expected_key_result="确认采购需求",
        process_description="事实" * 500 + "客户暂不增加供应商。",
        self_assessment="achieved",
        opportunities=[{"opportunity_id": "o", "current_stage": "P5"}],
        next_contact_at=datetime(2026, 9, 3, 16, tzinfo=UTC),
    )


def test_candidate_snapshot_preserves_long_text_and_subform_stage():
    value = visit()
    snapshot = _interactive_snapshot(value)
    assert snapshot["process_description"] == value.process_description
    assert snapshot["opportunity_stages"] == ["P5"]
    assert snapshot["opportunity_stage"] == "P5"
    assert snapshot["next_contact_at"] == "2026-09-04"
    assert snapshot["self_assessment"] == "达到目的"
    assert snapshot["customer_type_ii"] == "商机客户"


def test_existing_v22_snapshot_remains_unchanged():
    snapshot = _snapshot(visit())
    assert len(snapshot["process_description"]) == 500
    assert "opportunity_stages" not in snapshot


def test_invented_stage_and_false_missing_stage_are_blocked_before_display():
    snapshot = _interactive_snapshot(visit())
    assert not _interactive_preview_safe("商机阶段目前为P2。", snapshot)
    assert not _interactive_preview_safe("商机阶段未填写，请补充P1至P6。", snapshot)
    assert not _interactive_preview_safe("商机阶段未填写。", snapshot)
    assert not _interactive_preview_safe("自评为achieved。", snapshot)
    assert _interactive_preview_safe("当前关联商机处于P5，请补充采购需求细节。", snapshot)


def test_candidate_prompt_does_not_request_invented_stage_range():
    system = _interactive_messages(_interactive_snapshot(visit()))[0]["content"]
    assert "P6" not in system
    assert "已有阶段不得说未填写" in system
    assert "非商机客户不要求" in system


def test_contract_goal_clarification_is_interactive_only():
    from taoran_agent.experimental_goal_guidance import EXPERIMENTAL_GOAL_GUIDANCE
    from taoran_agent.experimental_semantic_streaming_v22 import _messages

    snapshot = _interactive_snapshot(visit())
    original = _messages(snapshot)[0]["content"]
    candidate = _interactive_messages(snapshot)[0]["content"]
    assert EXPERIMENTAL_GOAL_GUIDANCE not in original
    assert EXPERIMENTAL_GOAL_GUIDANCE in candidate
    assert "不是已发生客户事实" in candidate
    assert "不要只见合同二字就判通过" in candidate
    assert _messages(snapshot)[0]["content"] == original
