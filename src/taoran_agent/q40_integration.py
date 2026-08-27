from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from .models import (
    EvaluationResponse,
    PostEvaluationRequest,
    Q40PeriodFactsResponse,
    Q40RecordFacts,
    RuleCompatibilityResponse,
)
from .scoring_contract import TOTAL_RULE_VERSION

Q40_INTEGRATION_CONTRACT_VERSION = "TAORAN-Q40-INTEGRATION-V2"
SUPPORTED_RULE_VERSIONS = [TOTAL_RULE_VERSION]
MINIMUM_AGENT_VERSION = "0.7.0"


def rule_compatibility(required_rule_version: str) -> RuleCompatibilityResponse:
    return RuleCompatibilityResponse(
        contract_version=Q40_INTEGRATION_CONTRACT_VERSION,
        required_rule_version=required_rule_version,
        current_rule_version=TOTAL_RULE_VERSION,
        compatible=required_rule_version in SUPPORTED_RULE_VERSIONS,
        supported_rule_versions=SUPPORTED_RULE_VERSIONS,
        minimum_agent_version=MINIMUM_AGENT_VERSION,
    )


def build_period_facts(
    tenant_id: str,
    employee_id: str,
    period_start: date,
    period_end: date,
    required_rule_version: str,
    stored_records: list[dict[str, Any]],
    expected_visit_record_count: int | None,
) -> Q40PeriodFactsResponse:
    facts = [_record_facts(record) for record in stored_records]
    if expected_visit_record_count is None:
        coverage_rate = None
        coverage_basis = "taoran_store_only"
    else:
        coverage_rate = (
            1.0
            if expected_visit_record_count == 0
            else min(1.0, len(facts) / expected_visit_record_count)
        )
        coverage_basis = "caller_expected_count"
    return Q40PeriodFactsResponse(
        tenant_id=tenant_id,
        employee_id=employee_id,
        period_start=period_start,
        period_end=period_end,
        required_rule_version=required_rule_version,
        compatible=True,
        records=facts,
        evaluated_record_count=len(facts),
        expected_visit_record_count=expected_visit_record_count,
        coverage_rate=coverage_rate,
        coverage_basis=coverage_basis,
        generated_at=datetime.now(UTC),
    )


def _record_facts(record: dict[str, Any]) -> Q40RecordFacts:
    request = PostEvaluationRequest.model_validate(record["request"])
    response = EvaluationResponse.model_validate(record["response"])
    q33 = next(item for item in response.question_scores if item.question_code == "Q33")
    q34 = next(item for item in response.question_scores if item.question_code == "Q34")
    completeness = next(
        item for item in q33.components if item.code == "information_completeness"
    )
    timeliness = next(item for item in q33.components if item.code == "submission_timeliness")
    consistency = next(
        item for item in q34.components if item.code == "self_evaluation_consistency"
    )
    next_action = next(item for item in q34.components if item.code == "next_action_quality")
    return Q40RecordFacts(
        evaluation_id=response.evaluation_id,
        visit_record_code=response.visit_record_code,
        employee_id=request.visit.employee_id,
        visit_date=request.visit.visit_date,
        customer_type_ii=request.visit.customer_type_ii,
        visit_method=request.visit.visit_method,
        included_in_q40_q34_aggregate=bool(
            q34.calculation_trace.get("included_in_q40_formal_aggregate")
        ),
        q33_present_field_count=int(completeness.details.get("present_field_count", 0)),
        q33_required_field_count=int(completeness.details.get("required_field_count", 0)),
        q33_completeness_rate=float(completeness.rate or 0),
        q33_timely_submission=bool(timeliness.passed),
        q34_key_result_quality_ok=response.semantic_facts.key_result_quality_ok,
        q34_process_fact_based=response.semantic_facts.process_fact_based,
        q34_purpose_achievement=response.semantic_facts.purpose_achievement,
        q34_self_evaluation_consistent=bool(consistency.passed),
        q34_next_action_qualified=bool(next_action.passed),
        q34_customer_consensus_met=response.semantic_facts.customer_consensus_met,
        q33_score_projection=response.q33_score,
        q34_score_projection=response.q34_score,
        total_score_projection=response.total_score,
        q33_max_score=q33.max_score,
        q34_max_score=q34.max_score,
        total_max_score=response.total_max_score,
        semantic_status=response.semantic_facts.status,
        rule_version=response.rule_version,
        agent_version=response.agent_version,
        input_snapshot_hash=response.input_snapshot_hash,
        completed_at=response.completed_at,
    )
