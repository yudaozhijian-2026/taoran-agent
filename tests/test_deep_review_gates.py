import pytest

from taoran_agent.deep_review_gates import (
    FinalFeedbackIncomplete,
    achievement_boundary_hits,
    actual_outcome_evidence,
    advice_truthfulness_hits,
    commitment_boundary_hits,
    final_feedback_issues,
    formal_achievement_status,
    outcome_preservation_hits,
    preserve_outcomes,
    require_complete_feedback,
    unsupported_requirement_hits,
)


def test_golden_final_feedback_rejects_truncated_analysis():
    text = "本次拜访分析：联系日期晚于拜访日期且跨自然月，"
    with pytest.raises(FinalFeedbackIncomplete):
        require_complete_feedback(text, needs_advice=False)


def test_golden_complete_feedback_with_advice_passes():
    text = "本次拜访分析：客户已确认当前需求。\n\nAI改善建议：\n1. 建议进一步明确下一步。"
    assert require_complete_feedback(text, needs_advice=True) == text


def test_golden_complete_feedback_without_advice_passes():
    text = "本次拜访分析：目标、过程和下一步记录完整。"
    assert final_feedback_issues(text, needs_advice=False) == []


def test_golden_advice_cannot_invent_confirmed_person():
    source = "客户表示内部审批负责人尚未明确。"
    unsafe = "请在过程描述中补充内部审批负责人已确认的具体信息。"
    assert advice_truthfulness_hits(unsafe, source, "A2")
    safe = "如实际已确认负责人，请据实补充；若尚未确认，请保持真实状态并继续跟进。"
    assert advice_truthfulness_hits(safe, source, "A2") == []


@pytest.mark.parametrize(
    ("text", "expected_quote"),
    [
        ("本次记录中销售方已按USD690/kg报价。", None),
        ("根据记录，销售方已报价，客户尚未回应。", None),
        ("请补充客户已接受USD690/kg报价。", "请补充客户已接受USD690/kg报价"),
        (
            "本次记录中我方已报价，请补充客户已同意采购。",
            "请补充客户已同意采购",
        ),
        ("请记录客户是否接受报价；尚未回应则保持未确认。", None),
        ("如客户已明确同意采购，请据实补充；若尚未确认则继续跟进。", None),
    ],
)
def test_advice_truthfulness_binds_completion_to_a_local_writing_directive(text, expected_quote):
    hits = advice_truthfulness_hits(text, "客户尚未接受报价。", "N")
    if expected_quote is None:
        assert hits == []
    else:
        assert [hit["quote"] for hit in hits] == [expected_quote]


def test_golden_semantic_recommendation_cannot_be_company_rule():
    text = "联系时间应调整为客户评审日期当天或之后。"
    assert unsupported_requirement_hits(text, "semantic_recommendation", "N")
    assert unsupported_requirement_hits(text, "deterministic_rule", "N") == []


class Goal:
    def __init__(self, status):
        self.status = status


def test_golden_unassessable_is_unresolved_not_not_achieved():
    status = formal_achievement_status(
        [Goal("not_assessable")],
        "not_achieved",
    )
    assert status == "unresolved"
    assert achievement_boundary_hits(
        "由于目标不可评估，所以目标尚未达成。",
        achievement_status=status,
        target="facts.reason",
    )


def test_golden_long_unassessable_reason_cannot_end_as_not_achieved():
    text = (
        "原定关键结果为项目顺利实施，过于宽泛无法核验，"
        "过程记录客户将提供设备清单不足以证明项目顺利实施已达成，故判定未达成。"
    )
    assert achievement_boundary_hits(
        text,
        achievement_status="unresolved",
        target="facts.reason",
    )
    rendered = preserve_outcomes(
        text,
        achievement_status="unresolved",
        outcomes=[],
        source_text="客户下周一提供设备清单。",
    )
    assert "故判定未达成" not in rendered
    assert "不足以可靠判断" in rendered


def test_golden_specific_outcome_is_preserved_under_vague_goal():
    data = {
        "process_description": "客户明确需要3套办公桌，规格为1500×700mm。",
        "customer_feedback": "",
    }
    outcomes = actual_outcome_evidence(data)
    assert outcomes
    assert outcome_preservation_hits(
        "原目标较宽泛，本次拜访没有取得成果。",
        outcomes,
        "facts.reason",
    )
    rendered = preserve_outcomes(
        "原目标较宽泛。",
        achievement_status="unresolved",
        outcomes=outcomes,
        source_text=data["process_description"],
    )
    assert "3套办公桌" in rendered
    assert "不足以可靠判断" in rendered


def test_golden_customer_commitment_is_progress_not_completion():
    source = "客户承诺下周一提供设备清单。"
    assert commitment_boundary_hits(
        "客户已提供设备清单。",
        source,
        "facts.reason",
    )
    assert commitment_boundary_hits(
        "客户已承诺下周一提供设备清单，清单尚未实际收到。",
        source,
        "facts.reason",
    ) == []


@pytest.mark.parametrize(
    "candidate",
    [
        "当前记录不足以证明客户已同意下一步安排。",
        "尚无证据表明客户已同意下周采购。",
        "需要确认客户是否同意下周采购。",
        "客户尚未接受报价。",
    ],
)
def test_evidence_insufficiency_and_pending_state_are_not_positive_commitments(candidate):
    assert commitment_boundary_hits(
        candidate,
        "客户希望价格USD650/kg，我方按USD690/kg报价。",
        "N.reason",
    ) == []


def test_evidence_insufficiency_does_not_hide_a_separate_future_promise():
    hits = commitment_boundary_hits(
        "尚无证据表明客户已同意下周采购，但客户承诺下周付款。",
        "客户希望价格USD650/kg，我方按USD690/kg报价。",
        "facts.reason",
    )
    assert [hit["rule"] for hit in hits] == ["unsupported_future_customer_commitment"]
    assert hits[0]["candidate_event"]["action"] == "付款"


@pytest.mark.parametrize(
    ("source", "candidate", "expected_rule"),
    [
        (
            "客户承诺下周一提供三台设备清单。",
            "客户承诺下周一提供三台设备清单。",
            None,
        ),
        (
            "客户承诺下周一提供三台设备清单。",
            "客户承诺周五提供三台设备清单。",
            "unsupported_future_customer_commitment",
        ),
        (
            "客户承诺下周一提供三台设备清单。",
            "客户承诺下周一提供五台设备清单。",
            "unsupported_future_customer_commitment",
        ),
        (
            "客户承诺下周一提供三台设备清单。",
            "客户承诺下周一付款。",
            "unsupported_future_customer_commitment",
        ),
        (
            "客户表示审批通过后再安排采购。",
            "客户承诺下周采购。",
            "unsupported_future_customer_commitment",
        ),
        (
            "客户希望价格USD650/kg，我方按USD690/kg报价。",
            "客户已同意下周采购。",
            "unsupported_future_customer_commitment",
        ),
    ],
)
def test_future_commitment_requires_same_action_object_time_and_condition(
    source, candidate, expected_rule,
):
    hits = commitment_boundary_hits(candidate, source, "facts.reason")
    assert (hits[0]["rule"] if hits else None) == expected_rule


def test_completed_outcome_is_not_rewritten_as_future_commitment():
    rendered = preserve_outcomes(
        "原目标已达成。",
        achievement_status="achieved",
        outcomes=[{
            "field": "process_description",
            "quote": "客户已邮件发来六台设备清单并确认安装位置",
        }],
        source_text=(
            "客户已邮件发来六台设备清单并确认安装位置。"
            "客户承诺本周五完成电源准备。"
        ),
    )
    assert "后续承诺" not in rendered
    assert "客户已邮件发来六台设备清单" in rendered


def test_future_outcome_remains_progress_not_completed_result():
    rendered = preserve_outcomes(
        "原目标较宽泛。",
        achievement_status="unresolved",
        outcomes=[{
            "field": "process_description",
            "quote": "客户下周一提供两台设备清单",
        }],
        source_text="客户下周一提供两台设备清单。",
    )
    assert "这是有效进展" in rendered
    assert "尚待后续实际完成" in rendered
