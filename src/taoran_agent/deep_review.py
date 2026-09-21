"""Reconcile acknowledged front findings with authoritative formal findings."""
from __future__ import annotations

from time import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .front_analysis_artifact import (
    SUPPORTED_SCHEMA_VERSIONS,
    FrontAnalysisArtifact,
    analysis_input_hash,
    is_trusted_artifact_user_id,
)
from .models import PostEvaluationRequest, Q34SemanticFacts


class DeepReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    finding_id: str
    source_front_finding_id: str | None = None
    status: Literal["confirmed", "deepened", "corrected", "new_finding", "unresolved"]
    dimension: str
    front_statement: str | None = None
    final_statement: str
    evidence: list[str] = Field(default_factory=list)
    reason: str | None = None


class DeepReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    front_artifact_status: Literal[
        "used", "not_found", "input_mismatch", "invalid", "expired",
        "identity_untrusted", "user_mismatch", "record_mismatch", "schema_incompatible",
    ]
    front_artifact_used: bool = False
    front_artifact_id: str | None = None
    front_input_hash: str | None = None
    submitted_input_hash: str
    findings: list[DeepReviewFinding] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    fallback_reason: str | None = None
    identity_match: bool | None = None
    record_match: bool | None = None
    other_user_candidate_count: int = 0


def load_front_context(store, request: PostEvaluationRequest) -> tuple[FrontAnalysisArtifact | None, DeepReviewResult]:
    submitted_hash = analysis_input_hash(request.visit)
    user_id = request.context.user_id
    source_record_id = request.context.source_record_id
    if not is_trusted_artifact_user_id(user_id):
        return None, DeepReviewResult(
            front_artifact_status="identity_untrusted",
            submitted_input_hash=submitted_hash,
            identity_match=False,
            record_match=None,
        )
    try:
        exact = store.find_front_analysis_artifact(
            request.context.tenant_id,
            submitted_hash,
            user_id=user_id,
            source_record_id=source_record_id,
        )
    except (ValueError, TypeError, KeyError):
        return None, DeepReviewResult(
            front_artifact_status="invalid",
            submitted_input_hash=submitted_hash,
        )
    if exact is not None:
        raw_payload = exact.get("payload") or {}
        if raw_payload.get("schema_version") not in SUPPORTED_SCHEMA_VERSIONS:
            return None, DeepReviewResult(
                front_artifact_status="schema_incompatible",
                submitted_input_hash=submitted_hash,
                front_artifact_id=exact.get("artifact_id"),
                identity_match=True,
                record_match=True,
            )
        try:
            artifact = FrontAnalysisArtifact.model_validate(raw_payload)
        except (ValueError, TypeError, KeyError):
            return None, DeepReviewResult(
                front_artifact_status="invalid",
                submitted_input_hash=submitted_hash,
                front_artifact_id=exact.get("artifact_id"),
                identity_match=True,
                record_match=True,
            )
        if artifact.input_hash != submitted_hash:
            return None, DeepReviewResult(
                front_artifact_status="input_mismatch",
                submitted_input_hash=submitted_hash,
                front_input_hash=artifact.input_hash,
                front_artifact_id=artifact.artifact_id,
                identity_match=True,
                record_match=True,
            )
        linked = store.link_front_analysis_artifact(
            artifact.artifact_id,
            source_record_id,
        )
        if source_record_id and not linked:
            return None, DeepReviewResult(
                front_artifact_status="record_mismatch",
                submitted_input_hash=submitted_hash,
                front_input_hash=artifact.input_hash,
                front_artifact_id=artifact.artifact_id,
                identity_match=True,
                record_match=False,
            )
        return artifact, DeepReviewResult(
            front_artifact_status="used",
            front_artifact_used=True,
            submitted_input_hash=submitted_hash,
            front_input_hash=artifact.input_hash,
            front_artifact_id=artifact.artifact_id,
            identity_match=True,
            record_match=True,
        )

    counts = store.front_analysis_artifact_match_counts(
        request.context.tenant_id,
        submitted_hash,
        user_id=user_id,
        source_record_id=source_record_id,
    )
    if counts["record_mismatch_count"]:
        return None, DeepReviewResult(
            front_artifact_status="record_mismatch",
            submitted_input_hash=submitted_hash,
            identity_match=True,
            record_match=False,
        )
    if counts["other_user_count"]:
        return None, DeepReviewResult(
            front_artifact_status="user_mismatch",
            submitted_input_hash=submitted_hash,
            identity_match=False,
            record_match=None,
            other_user_candidate_count=counts["other_user_count"],
        )

    try:
        latest = store.latest_front_analysis_artifact(
            request.context.tenant_id,
            request.context.user_id,
        )
    except (ValueError, TypeError, KeyError):
        return None, DeepReviewResult(
            front_artifact_status="invalid",
            submitted_input_hash=submitted_hash,
        )
    if latest is not None and latest.get("input_hash") != submitted_hash:
        return None, DeepReviewResult(
            front_artifact_status="input_mismatch",
            submitted_input_hash=submitted_hash,
            front_input_hash=str(latest.get("input_hash") or "") or None,
            front_artifact_id=latest.get("artifact_id"),
            identity_match=True,
            record_match=None,
        )
    try:
        expired = store.latest_front_analysis_artifact(
            request.context.tenant_id,
            request.context.user_id,
            include_expired=True,
        )
    except (ValueError, TypeError, KeyError):
        return None, DeepReviewResult(
            front_artifact_status="invalid",
            submitted_input_hash=submitted_hash,
        )
    if (
        expired is not None
        and expired.get("input_hash") == submitted_hash
        and float(expired.get("retention_until") or 0) < time()
    ):
        return None, DeepReviewResult(
            front_artifact_status="expired",
            submitted_input_hash=submitted_hash,
            front_input_hash=expired.get("input_hash"),
            front_artifact_id=expired.get("artifact_id"),
            identity_match=True,
            record_match=None,
        )
    return None, DeepReviewResult(
        front_artifact_status="not_found",
        submitted_input_hash=submitted_hash,
        identity_match=True,
        record_match=None,
    )


def evaluate_with_front_fallback(
    agent,
    request: PostEvaluationRequest,
    job_id: str,
    artifact: FrontAnalysisArtifact | None,
    diagnostic: DeepReviewResult,
):
    """Run the authoritative formal review without any Front Artifact context."""
    del artifact
    return agent.evaluate(request, job_id), diagnostic


def _evidence(section) -> list[str]:
    result = []
    for item in section.evidence:
        value = item.evidence_id or item.quote
        if value:
            result.append(value)
    return list(dict.fromkeys(result))


def reconcile(
    artifact: FrontAnalysisArtifact | None,
    semantic_facts: Q34SemanticFacts,
    diagnostic: DeepReviewResult,
) -> DeepReviewResult:
    """Classify continuity without changing semantic facts or scores."""
    if artifact is None:
        return diagnostic
    front_by_dimension = {item.dimension: item for item in artifact.findings}
    formal_by_dimension = {item.code: item for item in semantic_facts.sections}
    findings: list[DeepReviewFinding] = []

    for dimension, front in front_by_dimension.items():
        formal = formal_by_dimension.get(dimension)
        if formal is None or formal.verdict == "not_evaluated":
            findings.append(DeepReviewFinding(
                finding_id=f"deep-{front.finding_id}",
                source_front_finding_id=front.finding_id,
                status="unresolved",
                dimension=dimension,
                front_statement=front.statement,
                final_statement=(formal.reason if formal else front.statement),
                evidence=_evidence(formal) if formal else [],
                reason="formal_not_evaluated" if formal else "formal_dimension_missing",
            ))
            continue
        if formal.verdict != front.conclusion:
            status = "corrected"
            reason = "formal_evidence_changed_conclusion"
        elif formal.reason.strip() == front.statement.strip():
            status = "confirmed"
            reason = "same_conclusion_and_statement"
        else:
            status = "deepened"
            reason = "same_conclusion_with_formal_detail"
        findings.append(DeepReviewFinding(
            finding_id=f"deep-{front.finding_id}",
            source_front_finding_id=front.finding_id,
            status=status,
            dimension=dimension,
            front_statement=front.statement,
            final_statement=formal.reason,
            evidence=_evidence(formal),
            reason=reason,
        ))

    for dimension, formal in formal_by_dimension.items():
        if dimension in front_by_dimension or formal.verdict == "not_evaluated":
            continue
        findings.append(DeepReviewFinding(
            finding_id=f"deep-new-{dimension}",
            status="new_finding",
            dimension=dimension,
            final_statement=formal.reason,
            evidence=_evidence(formal),
            reason="formal_dimension_not_present_in_front_artifact",
        ))

    counts = {
        name: 0
        for name in ("confirmed", "deepened", "corrected", "new_finding", "unresolved")
    }
    for item in findings:
        counts[item.status] += 1
    return diagnostic.model_copy(update={"findings": findings, "counts": counts})


def diagnostics_payload(result: DeepReviewResult) -> dict[str, Any]:
    return result.model_dump(mode="json")
