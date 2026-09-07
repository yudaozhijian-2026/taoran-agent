import pytest

from taoran_agent.experimental_assessment import (
    expands_goal,
    has_assessment_evidence,
    retain_analysis,
)
from taoran_agent.experimental_final_diagnostics import category, repair_instruction
from taoran_agent.llm import _wording_format_failure


@pytest.mark.parametrize("r", [True, False, None])
def test_candidate_retains_validated_gap_independently_of_process_quality(r):
    entries = [("customer_fact", "客户已提交申请。", []), ("assessment_gap", "记录不足以证明已确认合同。", [])]
    assert retain_analysis(entries, {"R": r}, experimental=True) == entries
    assert retain_analysis(entries, {"R": r}, experimental=False) == (entries if r is False else entries[:1])


def test_no_gap_is_invented_for_achieved_goal():
    entries = [("customer_fact", "目标确认预算卡点，客户明确预算冻结。", [])]
    assert retain_analysis(entries, {"R": True}, experimental=True) == entries
    assert retain_analysis([], {"R": False}, experimental=True) == []


@pytest.mark.parametrize("fields, expected", [
    ({"self_assessment", "expected_key_result", "process_description"}, True),
    ({"self_assessment", "expected_key_result", "customer_feedback"}, True),
    ({"self_assessment", "process_description"}, False),
    ({"expected_key_result", "process_description"}, False),
    ({"self_assessment", "expected_key_result"}, False),
    ({"self_assessment", "expected_key_result", "confirmed_findings"}, False),
])
def test_gap_requires_all_three_evidence_roles(fields, expected):
    assert has_assessment_evidence(fields) is expected


def test_missing_evidence_is_traceable_and_retryable():
    reason = "wording_experimental_assessment_evidence_missing"
    assert _wording_format_failure(reason)
    assert category(reason) == "final_assessment_evidence_missing"
    assert "三类连续原文" in repair_instruction(reason)


def test_do_not_raise_contract_commitment_to_actual_signature():
    context = {"expected_key_result": "客户确认合同文本最终版本，并承诺在约定日期完成签署"}
    assert expands_goal("记录尚不足以证明客户已实际签署。", context)
    assert not expands_goal("客户尚未确认版本或承诺签署。", context)
    assert not expands_goal("记录尚不足以证明客户已实际签署。", {"expected_key_result": "客户完成合同实际签署"})


def test_do_not_raise_current_blocker_to_all_blockers():
    assert expands_goal("尚不足以证明已确认完整审批卡点。", {"expected_key_result": "确认客户当前预算审批卡点"})
    assert not expands_goal("尚不足以证明已确认完整审批卡点。", {"expected_key_result": "确认全部审批卡点"})
    reason = "wording_experimental_goal_inflation"
    assert _wording_format_failure(reason)
    assert category(reason) == "final_goal_inflation"


def test_do_not_add_sufficient_communication_to_stated_goal():
    assert expands_goal("不足以证明已充分沟通物资采购情况。", {"expected_key_result": "与工程主管沟通物资采购情况"})
    assert not expands_goal("不足以证明已充分沟通物资采购情况。", {"expected_key_result": "充分沟通物资采购情况"})
    assert expands_goal("记录尚不足以证明沟通采购情况的目标已充分实现。", {"expected_key_result": "沟通采购情况"})
    assert expands_goal("不足以证明获得参与这一目标已实现。", {"expected_key_result": "办公物资", "purpose_code": "获得参与"})
    assert not expands_goal("不足以证明获得参与这一目标已实现。", {"expected_key_result": "获得参与", "purpose_code": "获得参与"})


@pytest.mark.parametrize("text,blocked", [
    ("关键结果只填1，不能把拜访目的当作获得参与这一目标。", False),
    ("本次目标并非获得参与这一目标。", False),
    ("不足以证明获得参与这一目标已实现。", True),
    ("不能把目的当作获得参与这一目标，但获得参与这一目标已经完成。", True),
])
def test_goal_reference_polarity_is_checked_per_clause(text, blocked):
    assert expands_goal(text, {"expected_key_result": "1", "purpose_code": "获得参与"}) is blocked


def test_approved_procurement_scope_is_candidate_only_and_conditional():
    from taoran_agent.experimental_assessment import ASSESSMENT_GUIDANCE
    from taoran_agent.experimental_semantic_streaming_v22 import _interactive_messages, _messages

    assert ASSESSMENT_GUIDANCE in _interactive_messages({})[0]["content"]
    assert ASSESSMENT_GUIDANCE not in _messages({})[0]["content"]
    assert "合作意愿中立仅描述后续合作状态" in ASSESSMENT_GUIDANCE
    assert "若过程只有问候" in ASSESSMENT_GUIDANCE
    assert "原记录自评部分达成保持原样" in ASSESSMENT_GUIDANCE


def test_approved_baseline_is_exact_input_bound(monkeypatch):
    import hashlib

    from taoran_agent import experimental_assessment as module
    context = {"expected_key_result": "沟通采购", "process_description": "预算及审批已沟通"}
    monkeypatch.setattr(module, "_APPROVED_PROCUREMENT", hashlib.sha256("沟通采购\n预算及审批已沟通".encode()).hexdigest())
    assert module.approved_procurement_goal(context)
    assert module.approved_goal_instruction(context)
    assert module.contradicts_approved_goal("assessment_gap", "不足以证明沟通已完成", context)
    assert not module.contradicts_approved_goal("customer_fact", "已沟通预算及审批", context)
    for changed in ({**context, "process_description": "未沟通"}, {**context, "expected_key_result": "取得订单"}):
        assert not module.approved_procurement_goal(changed)
        assert not module.approved_goal_instruction(changed)
        assert not module.contradicts_approved_goal("assessment_gap", "未完成", changed)
    assert category("wording_experimental_approved_goal_conflict") == "final_approved_goal_conflict"
    assert _wording_format_failure("wording_experimental_approved_goal_conflict")
