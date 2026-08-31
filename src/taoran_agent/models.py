from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from math import isclose
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .scoring_contract import LEGACY_TOTAL_RULE_VERSION, TOTAL_RULE_VERSION


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


class FeedbackMode(str, Enum):
    """提交前反馈的生成路径。"""

    RULE = "rule"
    AI = "ai"
    KNOWLEDGE = "knowledge"


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
    source_knowledge_id: str | None = None
    source_content_hash: str | None = None
    customer_type: CustomerTypeII | None = None
    opportunity_stages: list[str] = Field(default_factory=list)
    excluded_purposes: list[str] = Field(default_factory=list)

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

    @property
    def next_contact_date(self) -> date | None:
        """按简道云业务时区判断日期，不改写输入时间或审计证据。"""
        if self.next_contact_at is None:
            return None
        value = self.next_contact_at
        if value.tzinfo is not None:
            value = value.astimezone(ZoneInfo("Asia/Shanghai"))
        return value.date()

    @model_validator(mode="after")
    def validate_dates(self) -> VisitDraftInput:
        # 下一次联系日期先保留为原始事实，由前检返回建议、后评执行日期门槛。
        # 业务日期不合规不应让 AI 检测按钮在输入解析阶段报 500。
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
    feedback_mode: FeedbackMode = FeedbackMode.RULE


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


class KnowledgeReference(BaseModel):
    id: str
    title: str
    status: Literal["已批准", "已确认"]
    version: str
    content_hash: str


class TaoranSectionCheck(BaseModel):
    code: Literal["T", "A1", "O_KR", "R", "A2", "N"]
    display_code: str
    name: str
    status: Literal["met", "needs_revision", "partial_input", "not_received"]
    score: float
    max_score: float
    evaluated_fields: list[str] = Field(default_factory=list)
    unreceived_fields: list[str] = Field(default_factory=list)
    knowledge_ids: list[str] = Field(default_factory=list)


class ModelEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    field: str = Field(min_length=1, max_length=100)
    quote: str = Field(min_length=1, max_length=300)


class ModelSectionAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    code: Literal["T", "A1", "O_KR", "R", "A2", "N"]
    verdict: Literal["met", "needs_revision", "not_evaluated"]
    field_paths: list[str] = Field(max_length=12)
    reason: str = Field(min_length=1, max_length=500)
    suggestion: str = Field(max_length=400)
    evidence: list[ModelEvidence] = Field(max_length=8)


class SemanticReview(BaseModel):
    status: Literal["completed", "not_configured", "unavailable", "timeout"]
    issues: list[Issue] = Field(default_factory=list)
    provider: str
    latency_ms: int = 0
    model: str | None = None
    prompt_version: str | None = None
    sections: list[ModelSectionAnalysis] = Field(default_factory=list)
    failure_reason: str | None = None


class PrecheckResponse(BaseModel):
    check_id: str
    trace_id: str
    request_id: str
    tenant_id: str
    feedback_mode: FeedbackMode = FeedbackMode.RULE
    status: Literal["passed", "needs_revision", "review"]
    can_submit: bool
    submission_policy: Literal["advisory_only"] = "advisory_only"
    submission_blocked: Literal[False] = False
    record_quality_score: int
    level: Literal["A", "B", "C", "D", "E"]
    dimensions: list[DimensionScore]
    taoran_sections: list[TaoranSectionCheck]
    blocking_issues: list[Issue]
    issues: list[Issue]
    questions: list[str]
    suggestions: list[str]
    feedback_text: str
    semantic_review: SemanticReview
    input_snapshot_hash: str
    rule_version: str
    engine_version: str
    knowledge_snapshot_hash: str
    knowledge_references: list[KnowledgeReference]
    agent_version: str
    checked_at: datetime
    latency_ms: int


class ButtonPrecheckResponse(BaseModel):
    """单次按钮同时返回三份反馈，不提供任何正式评分字段。"""

    check_id: str
    trace_id: str
    request_id: str
    tenant_id: str
    feedback_mode: FeedbackMode = FeedbackMode.RULE
    stage: Literal["pre_submit_advice"] = "pre_submit_advice"
    official_score_generated: Literal[False] = False
    status: Literal["passed", "needs_revision", "review"]
    can_submit: bool
    submission_policy: Literal["advisory_only"] = "advisory_only"
    submission_blocked: Literal[False] = False
    issues: list[Issue]
    questions: list[str]
    suggestions: list[str]
    # feedback_text保留为规则反馈别名，确保旧按钮输出映射不失效。
    feedback_text: str
    rule_feedback_text: str = ""
    knowledge_feedback_text: str = ""
    model_feedback_text: str = ""
    rule_status: Literal["passed", "needs_revision", "review"] = "review"
    knowledge_status: Literal["passed", "needs_revision", "review"] = "review"
    model_status: Literal["passed", "needs_revision", "review"] = "review"
    knowledge_check_id: str | None = None
    model_check_id: str | None = None
    live_knowledge_snapshot_hash: str = ""
    live_knowledge_references: list[KnowledgeReference] = Field(default_factory=list)
    input_snapshot_hash: str
    rule_version: str
    engine_version: str
    knowledge_snapshot_hash: str
    knowledge_references: list[KnowledgeReference]
    agent_version: str
    checked_at: datetime
    latency_ms: int

    @classmethod
    def from_precheck(cls, response: PrecheckResponse) -> ButtonPrecheckResponse:
        return cls.model_validate(
            {
                **response.model_dump(mode="python"),
                "rule_feedback_text": response.feedback_text,
                "rule_status": response.status,
            }
        )

    @classmethod
    def from_three_prechecks(
        cls,
        rule: PrecheckResponse,
        knowledge: PrecheckResponse,
        model: PrecheckResponse,
        *,
        latency_ms: int,
    ) -> ButtonPrecheckResponse:
        return cls.model_validate(
            {
                **rule.model_dump(mode="python"),
                "feedback_mode": FeedbackMode.RULE,
                "feedback_text": rule.feedback_text,
                "rule_feedback_text": rule.feedback_text,
                "knowledge_feedback_text": knowledge.feedback_text,
                "model_feedback_text": model.feedback_text,
                "rule_status": rule.status,
                "knowledge_status": knowledge.status,
                "model_status": model.status,
                "knowledge_check_id": knowledge.check_id,
                "model_check_id": model.check_id,
                "live_knowledge_snapshot_hash": knowledge.knowledge_snapshot_hash,
                "live_knowledge_references": knowledge.knowledge_references,
                "latency_ms": latency_ms,
            }
        )


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
    max_score: Literal[50, 100] = 50
    rule_version: str
    components: list[ScoreComponent]
    calculation_trace: dict[str, Any] = Field(default_factory=dict)


class ModelValidationIssue(BaseModel):
    location: str
    code: str


class ModelAttemptAudit(BaseModel):
    attempt: int
    latency_ms: int
    failure_reason: str | None = None
    validation_errors: list[ModelValidationIssue] = Field(default_factory=list)


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
    model: str | None = None
    prompt_version: str | None = None
    sections: list[ModelSectionAnalysis] = Field(default_factory=list)
    failure_reason: str | None = None
    model_attempts: list[ModelAttemptAudit] = Field(default_factory=list)


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
    total_max_score: Literal[100, 200] = 100
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
    # 提交后保留两条独立的填写检查反馈，供简道云新增字段回写。
    knowledge_feedback_text: str = ""
    model_feedback_text: str = ""
    semantic_facts: Q34SemanticFacts
    writeback: WritebackResult
    input_snapshot_hash: str
    rule_version: str
    agent_version: str
    completed_at: datetime

    @model_validator(mode="after")
    def validate_score_contract(self) -> EvaluationResponse:
        expected_max = {
            TOTAL_RULE_VERSION: 100,
            LEGACY_TOTAL_RULE_VERSION: 200,
        }.get(self.rule_version)
        if expected_max is None or self.total_max_score != expected_max:
            raise ValueError("评分规则版本与满分量纲不一致")
        if sorted(item.question_code for item in self.question_scores) != ["Q33", "Q34"]:
            raise ValueError("评分必须包含且仅包含Q33和Q34")
        for item in self.question_scores:
            expected_version = (
                f"TAORAN-{item.question_code}-50-V2" if expected_max == 100
                else f"TAORAN-{item.question_code}-100-V1"
            )
            actual_score = self.q33_score if item.question_code == "Q33" else self.q34_score
            if (
                item.max_score != expected_max / 2
                or item.rule_version != expected_version
                or not 0 <= item.score <= item.max_score
                or not isclose(item.score, actual_score, abs_tol=0.00001)
                or not isclose(sum(c.max_score for c in item.components), item.max_score)
                or not isclose(sum(c.score for c in item.components), item.score, abs_tol=0.00001)
                or any(not 0 <= c.score <= c.max_score for c in item.components)
            ):
                raise ValueError("题目得分、子项或满分不符合评分量纲")
        if (
            not isclose(self.total_score, round(self.q33_score + self.q34_score, 2))
            or not 0 <= self.total_score <= expected_max
            or not isclose(
                self.overall_percentage, round(self.total_score / expected_max * 100, 2),
            )
        ):
            raise ValueError("总分或综合百分比与分项不一致")
        return self


class JiandaoyunCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: RequestContext
    form_data: dict[str, Any]
    feedback_mode: FeedbackMode = FeedbackMode.RULE


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


class JiandaoyunSubmittedEvent(BaseModel):
    """Minimal post-submit event; the Agent reads the authoritative record via V5 API."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    tenant_id: str = Field(min_length=1)
    data_id: str = Field(min_length=1)
    app_id: str | None = None
    entry_id: str | None = None
    user_id: str = "jiandaoyun-submit-event"
    request_id: str | None = None


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
    q33_max_score: float = 50
    q34_max_score: float = 50
    total_max_score: float = 100
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
