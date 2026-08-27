from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .agent import TaoranAgent
from .models import EvaluationResponse, PostEvaluationRequest
from .scoring_contract import TOTAL_RULE_VERSION

QualityBand = Literal["low", "medium", "high"]
Recommendation = Literal["yes", "manager_review", "no"]


class ExpertLabel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    q33_score: float = Field(ge=0, le=50)
    q34_score: float = Field(ge=0, le=50)
    recommendation: Recommendation
    critical_issue_codes: list[str] = Field(default_factory=list)
    notes: str | None = None


class CalibrationSample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_id: str = Field(min_length=1)
    quality_band: QualityBand
    evaluation_request: PostEvaluationRequest
    expert: ExpertLabel

    @model_validator(mode="after")
    def reject_writeback(self) -> CalibrationSample:
        if self.evaluation_request.writeback_target is not None:
            raise ValueError("校准样例不得配置简道云回写目标")
        return self


class CalibrationDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_id: str = Field(min_length=1)
    rule_version: Literal["TAORAN-Q33-Q34-100-V2"]
    samples: list[CalibrationSample] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_sample_ids(self) -> CalibrationDataset:
        sample_ids = [sample.sample_id for sample in self.samples]
        if len(sample_ids) != len(set(sample_ids)):
            raise ValueError("校准样例sample_id不得重复")
        return self


class CalibrationThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    q33_tolerance: float = Field(default=0.005, ge=0, le=50)
    q34_tolerance: float = Field(default=5, ge=0, le=50)
    minimum_sample_count: int = Field(default=30, ge=1)
    minimum_samples_per_band: int = Field(default=10, ge=1)
    minimum_q33_agreement_rate: float = Field(default=1, ge=0, le=1)
    minimum_q34_agreement_rate: float = Field(default=0.85, ge=0, le=1)


class CalibrationSampleResult(BaseModel):
    sample_id: str
    quality_band: QualityBand
    expert_q33_score: float
    agent_q33_score: float
    q33_absolute_error: float
    q33_within_tolerance: bool
    expert_q34_score: float
    agent_q34_score: float
    q34_absolute_error: float
    q34_within_tolerance: bool
    total_absolute_error: float
    expert_recommendation: Recommendation
    agent_recommendation: Recommendation
    recommendation_matches: bool
    missed_critical_issue_codes: list[str]
    serious_miss: bool
    agent_issue_codes: list[str]
    rule_version: str


class CalibrationReport(BaseModel):
    dataset_id: str
    sample_count: int
    band_distribution: dict[str, int]
    q33_agreement_rate: float
    q34_agreement_rate: float
    recommendation_agreement_rate: float
    q33_mean_absolute_error: float
    q34_mean_absolute_error: float
    total_mean_absolute_error: float
    serious_miss_count: int
    sample_count_gate_passed: bool
    band_balance_gate_passed: bool
    q33_gate_passed: bool
    q34_gate_passed: bool
    serious_miss_gate_passed: bool
    ready_for_business_signoff: bool
    rule_versions: list[str]
    thresholds: CalibrationThresholds
    results: list[CalibrationSampleResult]
    generated_at: datetime


class EvaluationAgent(Protocol):
    def evaluate(self, request: PostEvaluationRequest, job_id: str) -> EvaluationResponse: ...


def run_calibration(
    dataset: CalibrationDataset,
    thresholds: CalibrationThresholds | None = None,
    agent: EvaluationAgent | None = None,
) -> CalibrationReport:
    active_thresholds = thresholds or CalibrationThresholds()
    active_agent = agent or TaoranAgent()
    results = [
        _evaluate_sample(sample, active_thresholds, active_agent) for sample in dataset.samples
    ]
    sample_count = len(results)
    band_distribution = dict(Counter(result.quality_band for result in results))
    q33_agreement_rate = _rate(result.q33_within_tolerance for result in results)
    q34_agreement_rate = _rate(result.q34_within_tolerance for result in results)
    recommendation_agreement_rate = _rate(
        result.recommendation_matches for result in results
    )
    serious_miss_count = sum(result.serious_miss for result in results)
    sample_count_gate_passed = sample_count >= active_thresholds.minimum_sample_count
    band_balance_gate_passed = all(
        band_distribution.get(band, 0) >= active_thresholds.minimum_samples_per_band
        for band in ("low", "medium", "high")
    )
    q33_gate_passed = q33_agreement_rate >= active_thresholds.minimum_q33_agreement_rate
    q34_gate_passed = q34_agreement_rate >= active_thresholds.minimum_q34_agreement_rate
    serious_miss_gate_passed = serious_miss_count == 0
    ready = all(
        (
            sample_count_gate_passed,
            band_balance_gate_passed,
            q33_gate_passed,
            q34_gate_passed,
            serious_miss_gate_passed,
        )
    )
    return CalibrationReport(
        dataset_id=dataset.dataset_id,
        sample_count=sample_count,
        band_distribution=band_distribution,
        q33_agreement_rate=q33_agreement_rate,
        q34_agreement_rate=q34_agreement_rate,
        recommendation_agreement_rate=recommendation_agreement_rate,
        q33_mean_absolute_error=_mean(result.q33_absolute_error for result in results),
        q34_mean_absolute_error=_mean(result.q34_absolute_error for result in results),
        total_mean_absolute_error=_mean(result.total_absolute_error for result in results),
        serious_miss_count=serious_miss_count,
        sample_count_gate_passed=sample_count_gate_passed,
        band_balance_gate_passed=band_balance_gate_passed,
        q33_gate_passed=q33_gate_passed,
        q34_gate_passed=q34_gate_passed,
        serious_miss_gate_passed=serious_miss_gate_passed,
        ready_for_business_signoff=ready,
        rule_versions=sorted({result.rule_version for result in results}),
        thresholds=active_thresholds,
        results=results,
        generated_at=datetime.now(UTC),
    )


def _evaluate_sample(
    sample: CalibrationSample,
    thresholds: CalibrationThresholds,
    agent: EvaluationAgent,
) -> CalibrationSampleResult:
    response = agent.evaluate(sample.evaluation_request, f"calibration_{sample.sample_id}")
    if response.rule_version != TOTAL_RULE_VERSION or response.total_max_score != 100:
        raise ValueError("校准结果不是当前100分制，禁止混合评分量纲")
    agent_issue_codes = sorted({issue.code for issue in response.issues})
    missed_critical_codes = sorted(
        set(sample.expert.critical_issue_codes) - set(agent_issue_codes)
    )
    q33_error = round(abs(response.q33_score - sample.expert.q33_score), 4)
    q34_error = round(abs(response.q34_score - sample.expert.q34_score), 4)
    expert_total = sample.expert.q33_score + sample.expert.q34_score
    total_error = round(abs(response.total_score - expert_total), 4)
    unsafe_recommendation = (
        sample.expert.recommendation == "no"
        and response.count_as_effective_visit_recommendation == "yes"
    )
    return CalibrationSampleResult(
        sample_id=sample.sample_id,
        quality_band=sample.quality_band,
        expert_q33_score=sample.expert.q33_score,
        agent_q33_score=response.q33_score,
        q33_absolute_error=q33_error,
        q33_within_tolerance=q33_error <= thresholds.q33_tolerance,
        expert_q34_score=sample.expert.q34_score,
        agent_q34_score=response.q34_score,
        q34_absolute_error=q34_error,
        q34_within_tolerance=q34_error <= thresholds.q34_tolerance,
        total_absolute_error=total_error,
        expert_recommendation=sample.expert.recommendation,
        agent_recommendation=response.count_as_effective_visit_recommendation,
        recommendation_matches=(
            sample.expert.recommendation == response.count_as_effective_visit_recommendation
        ),
        missed_critical_issue_codes=missed_critical_codes,
        serious_miss=unsafe_recommendation or bool(missed_critical_codes),
        agent_issue_codes=agent_issue_codes,
        rule_version=response.rule_version,
    )


def _rate(values) -> float:
    items = list(values)
    return round(sum(items) / len(items), 4)


def _mean(values) -> float:
    items = list(values)
    return round(sum(items) / len(items), 4)
