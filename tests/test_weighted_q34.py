from taoran_agent.models import Q34SemanticFacts, VisitDraftInput
from taoran_agent.scoring import score_q34
from taoran_agent.scoring_contract import Q34_WEIGHTED_POLICY_VERSION


def _visit(**changes) -> VisitDraftInput:
    payload = {
        "visit_date": "2026-09-17",
        "employee_id": "E-WEIGHTED",
        "customer_id": "C-WEIGHTED",
        "customer_type_ii": "opportunity",
        "visit_method": "face_to_face",
        "is_appointment": True,
        "purpose_code": "推进商机",
        "expected_key_result": "客户确认方案试用范围和完成时间",
        "process_description": "客户确认先安排两个部门试用，并约定下周提供名单。",
        "self_assessment": "achieved",
        "next_action_purpose": "推进试用",
        "next_action_expected_result": "客户提供两个试用部门名单并确认启动时间",
        "next_contact_at": "2026-09-24T10:00:00+08:00",
    }
    payload.update(changes)
    return VisitDraftInput.model_validate(payload)


def _facts(**changes) -> Q34SemanticFacts:
    payload = {
        "provider": "test",
        "key_result_quality_ok": True,
        "process_fact_based": True,
        "purpose_achievement": "achieved",
        "next_action_logic_ok": True,
        "customer_consensus_met": True,
        "reason": "测试固定权重评分。",
    }
    payload.update(changes)
    return Q34SemanticFacts.model_validate(payload)


def test_all_q34_standards_receive_full_50_points():
    score, _ = score_q34(_visit(), _facts())
    consistency, action = score.components

    assert score.score == 50
    assert (consistency.rate, consistency.band_score, consistency.score) == (1.0, 4, 35)
    assert (action.rate, action.band_score, action.score) == (1.0, 4, 15)
    assert score.calculation_trace["weighted_policy_version"] == Q34_WEIGHTED_POLICY_VERSION


def test_self_assessment_mismatch_uses_weighted_compliance_band():
    score, _ = score_q34(_visit(self_assessment="partially_achieved"), _facts())
    consistency = score.components[0]

    # The other standards contribute 65%; the inconsistent 35% item contributes zero.
    assert consistency.rate == 0.65
    assert consistency.band_score == 1
    assert consistency.score == 8.75
    assert consistency.passed is False


def test_invalid_next_contact_date_uses_85_percent_band():
    visit = _visit(next_contact_at="2026-09-17T18:00:00+08:00")
    score, issues = score_q34(visit, _facts())
    action = score.components[1]

    assert action.rate == 0.85
    assert action.band_score == 3
    assert action.score == 11.25
    assert action.passed is False
    assert "Q34_NEXT_ACTION_NOT_QUALIFIED" in {issue.code for issue in issues}


def test_target_same_month_loses_only_customer_type_standard_weight():
    visit = _visit(
        customer_type_ii="target",
        is_appointment=False,
        next_contact_at="2026-09-25T10:00:00+08:00",
    )
    score, _ = score_q34(visit, _facts(customer_consensus_met=False))
    consistency, action = score.components

    # Target-customer appointment is not a single-record deduction.
    assert consistency.rate == 1.0
    # Same-month scheduling loses the 25% segment standard and maps to 50% of 15.
    assert action.rate == 0.75
    assert action.band_score == 2
    assert action.score == 7.5


def test_weight_details_are_auditable_in_response():
    score, _ = score_q34(_visit(), _facts())
    consistency, action = score.components

    assert sum(consistency.details["standard_weights"].values()) == 1
    assert sum(action.details["standard_weights"].values()) == 1
    assert consistency.details["standard_checks"]["system_self_assessment_consistency"] is True
    assert action.details["standard_checks"]["customer_type_standard"] is True
