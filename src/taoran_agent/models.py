from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class VisitMethod(str, Enum):
    FACE_TO_FACE = "face_to_face"
    VIDEO = "video"
    PHONE = "phone"
    ASYNCHRONOUS_MESSAGE = "asynchronous_message"


class CustomerTypeII(str, Enum):
    POTENTIAL = "potential"
    TARGET = "target"
    OPPORTUNITY = "opportunity"


class SelfAssessment(str, Enum):
    ACHIEVED = "achieved"
    PARTIALLY_ACHIEVED = "partially_achieved"
    NOT_ACHIEVED = "not_achieved"


class Severity(str, Enum):
    BLOCKING = "blocking"
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class RequestContext(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    tenant_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    source: Literal["jiandaoyun", "dsm", "api", "test"] = "api"
    form_revision: str | None = None
    source_record_id: str | None = None
    requested_at: datetime | None = None


class ParticipantInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contact_id: str = Field(min_length=1)
    role: str | None = None


class OpportunityStageInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    opportunity_id: str = Field(min_length=1)
    historical_stage: str | None = None
    current_stage: str | None = None


class PurposePolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_version: str = Field(min_length=1)
    status: Literal["draft", "published", "active", "retired"]
    allowed_purposes: list[str] = Field(default_factory=list)
    effective_from: date
    effective_to: date | None = None

    def is_effective_on(self, visit_date: date) -> bool:
        return (
            self.status in {"published", "active"}
            and self.effective_from <= visit_date
            and (self.effective_to is None or visit_date <= self.effective_to)
        )


class VisitDraftInput(BaseModel):
    """提交前可获得的拜访草稿及最小必要上下文。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    visit_date: date
    employee_id: str = Field(min_length=1)
    customer_id: str | None = None
    customer_type_ii: CustomerTypeII | None = None
    opportunity_id: str | None = None
    opportunity_stage: str | None = None
    opportunities: list[OpportunityStageInput] = Field(default_factory=list)
    visit_method: VisitMethod | None = None
    is_appointment: bool | None = None
    participants: list[ParticipantInput] = Field(default_factory=list)
    purpose_code: str | None = None
    other_purpose: str | None = None
    expected_key_result: str | None = None
    process_description: str | None = None
    customer_feedback: str | None = None
    self_assessment: SelfAssessment | None = None
    deviation_reason: str | None = None
    next_action_target_id: str | None = None
    next_action_purpose: str | None = None
    next_action_other_purpose: str | None = None
    next_action_expected_result: str | None = None
    next_contact_at: datetime | None = None
    actual_start_at: datetime | None = None
    actual_end_at: datetime | None = None
    duration_minutes: int | None = Field(default=None, ge=0)
    submitted_at: datetime | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    purpose_policy: PurposePolicyInput | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_dates(self) -> VisitDraftInput:
        if self.next_contact_at and self.next_contact_at.date() < self.visit_date:
            raise ValueError("next_contact_at 不得早于 visit_date")
        if (
            self.actual_start_at
            and self.actual_end_at
            and self.actual_end_at < self.actual_start_at
        ):
            raise ValueError("actual_end_at 不得早于 actual_start_at")
        return self


class PrecheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: RequestContext
    visit: VisitDraftInput


class Issue(BaseModel):
    code: str
    dimension: str
    severity: Severity
    field_paths: list[str] = Field(default_factory=list)
    message: str
    suggestion: str
    source: Literal["rule", "semantic", "system"] = "rule"


class DimensionScore(BaseModel):
    code: str
    name: str
    score: float
    max_score: float


class SemanticReview(BaseModel):
    status: Literal["completed", "not_configured", "unavailable", "timeout"]
    issues: list[Issue] = Field(default_factory=list)
    provider: str
    latency_ms: int = 0


class PrecheckResponse(BaseModel):
    check_id: str
    trace_id: str
    request_id: str
    tenant_id: str
    status: Literal["passed", "needs_revision", "review"]
    can_submit: bool
    submission_policy: Literal["advisory_only"] = "advisory_only"
    submission_blocked: Literal[False] = False
    record_quality_score: int
    level: Literal["A", "B", "C", "D", "E"]
    dimensions: list[DimensionScore]
    blocking_issues: list[Issue]
    issues: list[Issue]
    questions: list[str]
    suggestions: list[str]
    feedback_text: str
    semantic_review: SemanticReview
    input_snapshot_hash: str
    rule_version: str
    agent_version: str
    checked_at: datetime
    latency_ms: int


class ScoreComponent(BaseModel):
    code: str
    name: str
    score: float
    max_score: float
    rate: float | None = None
    band_score: int | None = None
    passed: bool | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class QuestionScore(BaseModel):
    question_code: Literal["Q33", "Q34"]
    name: str
    score: float
    max_score: Literal[100] = 100
    rule_version: str
    components: list[ScoreComponent]
    calculation_trace: dict[str, Any] = Field(default_factory=dict)


class Q34SemanticFacts(BaseModel):
    status: Literal["completed", "fallback", "unavailable", "timeout"] = "completed"
    provider: str
    key_result_quality_ok: bool
    process_fact_based: bool
    purpose_achievement: SelfAssessment
    next_action_logic_ok: bool
    customer_consensus_met: bool
    evidence_fields: list[str] = Field(default_factory=list)
    reason: str
    latency_ms: int = 0


class EvidenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(min_length=1)
    source_object: str = Field(min_length=1)
    source_record_id: str | None = None
    field_path: str | None = None
    content_hash: str | None = None


class JiandaoyunWritebackTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    app_id: str = Field(min_length=1)
    entry_id: str = Field(min_length=1)
    data_id: str = Field(min_length=1)


class PostEvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: RequestContext
    visit_record_code: str = Field(min_length=1)
    visit: VisitDraftInput
    previous_visit_summary: str | None = None
    evidence: list[EvidenceInput] = Field(default_factory=list)
    information_collection_updated: bool | None = None
    opportunity_updated: bool | None = None
    manager_comment: str | None = None
    writeback_target: JiandaoyunWritebackTarget | None = None


class EvaluationAccepted(BaseModel):
    job_id: str
    trace_id: str
    status: Literal["queued", "completed"]
    input_snapshot_hash: str


class WritebackResult(BaseModel):
    status: Literal["skipped", "succeeded", "failed"]
    target_data_id: str | None = None
    written_fields: list[str] = Field(default_factory=list)
    error_message: str | None = None
    attempted_at: datetime | None = None


class EvaluationResponse(BaseModel):
    evaluation_id: str
    job_id: str
    trace_id: str
    tenant_id: str
    visit_record_code: str
    status: Literal["completed", "failed"]
    q33_score: float
    q34_score: float
    total_score: float
    total_max_score: Literal[200] = 200
    overall_percentage: float
    question_scores: list[QuestionScore]
    effectiveness_score: int
    effectiveness_level: Literal["high_quality", "acceptable", "low_quality", "invalid_or_unclear"]
    count_as_effective_visit_recommendation: Literal["yes", "manager_review", "no"]
    dimensions: list[DimensionScore]
    issues: list[Issue]
    manager_coaching_suggestions: list[str]
    recommended_training_projects: list[str]
    ai_opinion: str
    semantic_facts: Q34SemanticFacts
    writeback: WritebackResult
    input_snapshot_hash: str
    rule_version: str
    agent_version: str
    completed_at: datetime


class JiandaoyunCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: RequestContext
    form_data: dict[str, Any]


class JiandaoyunEvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: RequestContext
    visit_record_code: str = Field(min_length=1)
    form_data: dict[str, Any]
    previous_visit_summary: str | None = None
    evidence: list[EvidenceInput] = Field(default_factory=list)
    information_collection_updated: bool | None = None
    opportunity_updated: bool | None = None
    manager_comment: str | None = None
    writeback_target: JiandaoyunWritebackTarget


class Q40RecordFacts(BaseModel):
    evaluation_id: str
    visit_record_code: str
    employee_id: str
    visit_date: date
    customer_type_ii: CustomerTypeII | None = None
    visit_method: VisitMethod | None = None
    included_in_q40_q34_aggregate: bool
    q33_present_field_count: int
    q33_required_field_count: int
    q33_completeness_rate: float
    q33_timely_submission: bool
    q34_key_result_quality_ok: bool
    q34_process_fact_based: bool
    q34_purpose_achievement: SelfAssessment
    q34_self_evaluation_consistent: bool
    q34_next_action_qualified: bool
    q34_customer_consensus_met: bool
    q33_score_projection: float
    q34_score_projection: float
    total_score_projection: float
    semantic_status: str
    rule_version: str
    agent_version: str
    input_snapshot_hash: str
    completed_at: datetime


class Q40PeriodFactsResponse(BaseModel):
    tenant_id: str
    employee_id: str
    period_start: date
    period_end: date
    required_rule_version: str
    compatible: bool
    records: list[Q40RecordFacts]
    evaluated_record_count: int
    expected_visit_record_count: int | None = None
    coverage_rate: float | None = None
    coverage_basis: Literal["taoran_store_only", "caller_expected_count"]
    generated_at: datetime


class Q40BatchEvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    requested_by: str = Field(min_length=1)
    required_rule_version: str = Field(min_length=1)
    evaluations: list[PostEvaluationRequest] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_batch_scope(self) -> Q40BatchEvaluationRequest:
        for evaluation in self.evaluations:
            if evaluation.context.tenant_id != self.tenant_id:
                raise ValueError("批量评价记录的tenant_id必须与批次一致")
            if evaluation.writeback_target is not None:
                raise ValueError("Q40批量补评不得触发简道云回写")
        request_ids = [item.context.request_id for item in self.evaluations]
        if len(request_ids) != len(set(request_ids)):
            raise ValueError("批量评价中的request_id不得重复")
        return self


class Q40BatchAccepted(BaseModel):
    batch_job_id: str
    status: Literal["queued", "completed", "completed_with_errors"]
    evaluation_count: int
    input_snapshot_hash: str


class Q40BatchItemResult(BaseModel):
    request_id: str
    visit_record_code: str
    job_id: str
    status: Literal["completed", "failed", "reused", "pending"]
    error_message: str | None = None


class Q40BatchResult(BaseModel):
    batch_job_id: str
    tenant_id: str
    status: Literal["completed", "completed_with_errors"]
    requested_count: int
    completed_count: int
    reused_count: int
    failed_count: int
    pending_count: int
    items: list[Q40BatchItemResult]
    required_rule_version: str
    completed_at: datetime


class RuleCompatibilityResponse(BaseModel):
    integration: Literal["dsm-q40-agent"] = "dsm-q40-agent"
    contract_version: str
    required_rule_version: str
    current_rule_version: str
    compatible: bool
    supported_rule_versions: list[str]
    minimum_agent_version: str
