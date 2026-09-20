"""Validated submit-time analysis shared with the post-submit deep review.

The artifact is internal.  It never appears in Jiandaoyun fields and never
controls Q33/Q34.  Only acknowledged, unexpired artifacts whose normalized
business input matches the saved record are eligible for formal review.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .models import VisitDraftInput
from .rules import canonical_hash

SCHEMA_VERSION = "front-analysis-artifact-v1"

# These values are filled by the platform or derived from policy.  They can
# legitimately differ between the unsaved page and the saved record even when
# the salesperson has not changed any TAORAN business content.
_VOLATILE_VISIT_FIELDS = {"submitted_at", "metadata", "evidence_ids", "purpose_policy"}


class FrontFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    finding_id: str = Field(min_length=1, max_length=120)
    dimension: str = Field(min_length=1, max_length=40)
    finding_type: Literal["observation", "gap", "uncertain"]
    conclusion: Literal["met", "needs_revision", "not_evaluated"]
    statement: str = Field(min_length=1, max_length=600)
    evidence: list[str] = Field(default_factory=list, max_length=12)
    evidence_fields: list[str] = Field(default_factory=list, max_length=20)
    confidence: Literal["high", "medium", "low"] | None = None


class FrontSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    suggestion_id: str = Field(min_length=1, max_length=120)
    finding_id: str | None = Field(default=None, max_length=120)
    dimension: str | None = Field(default=None, max_length=40)
    text: str = Field(min_length=1, max_length=600)


class FrontAnalysisArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    artifact_id: str = Field(min_length=1, max_length=120)
    schema_version: str = SCHEMA_VERSION
    input_hash: str = Field(min_length=64, max_length=64)
    quick_check_input_hash: str = Field(min_length=64, max_length=64)
    generated_at: datetime
    source: Literal["quick_check"] = "quick_check"
    tenant_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    check_id: str = Field(min_length=1)
    source_record_id: str | None = None
    analysis_summary: str | None = Field(default=None, max_length=4000)
    findings: list[FrontFinding] = Field(default_factory=list, max_length=24)
    suggestions: list[FrontSuggestion] = Field(default_factory=list, max_length=16)
    confirmation_items: list[str] = Field(default_factory=list, max_length=8)
    decision_ledger: dict[str, Any] | None = None
    policy_version: str | None = None
    prompt_version: str | None = None


def analysis_input_payload(visit: VisitDraftInput) -> dict[str, Any]:
    """Return the normalized user-controlled TAORAN content for matching."""
    raw = visit.model_dump(mode="json")
    return {key: value for key, value in raw.items() if key not in _VOLATILE_VISIT_FIELDS}


def analysis_input_hash(visit: VisitDraftInput) -> str:
    return canonical_hash(analysis_input_payload(visit))


def _section_findings(front_review: dict[str, Any]) -> list[FrontFinding]:
    findings: list[FrontFinding] = []
    for index, section in enumerate(front_review.get("sections") or [], 1):
        if not isinstance(section, dict):
            continue
        dimension = str(section.get("code") or "").strip()
        verdict = str(section.get("verdict") or "not_evaluated").strip()
        reason = str(section.get("reason") or "").strip()
        if not dimension or not reason or verdict not in {"met", "needs_revision", "not_evaluated"}:
            continue
        evidence = []
        evidence_fields = []
        for item in section.get("evidence") or []:
            if not isinstance(item, dict):
                continue
            quote = str(item.get("quote") or "").strip()
            field = str(item.get("field") or "").strip()
            if quote:
                evidence.append(quote)
            if field:
                evidence_fields.append(field)
        findings.append(
            FrontFinding(
                finding_id=f"front-{dimension}-{index}",
                dimension=dimension,
                finding_type=(
                    "gap" if verdict == "needs_revision" else
                    "uncertain" if verdict == "not_evaluated" else "observation"
                ),
                conclusion=verdict,
                statement=reason,
                evidence=list(dict.fromkeys(evidence))[:12],
                evidence_fields=list(dict.fromkeys(evidence_fields))[:20],
                confidence="high" if evidence or verdict == "needs_revision" else "medium",
            )
        )
    return findings


def _split_tail(feedback_text: str) -> tuple[str, list[str], list[str]]:
    text = str(feedback_text or "").replace("【AI反馈意见】", "", 1).strip()
    analysis = ""
    if "本次拜访分析：" in text:
        analysis = text.split("本次拜访分析：", 1)[1]
        analysis = analysis.split("AI改善建议：", 1)[0].strip()
    advice = text.split("AI改善建议：", 1)[1].strip() if "AI改善建议：" in text else ""
    confirmations = []
    for heading in ("需确认补充事项：", "需确认事项："):
        if heading in advice:
            advice, confirmation_text = advice.split(heading, 1)
            confirmations = _numbered_items(confirmation_text)
            break
    return analysis, _numbered_items(advice), confirmations


def _numbered_items(value: str) -> list[str]:
    lines = []
    for line in str(value or "").splitlines():
        cleaned = re.sub(r"^\s*\d+[\u3001．.]\s*", "", line).strip()
        if (
            cleaned
            and not cleaned.startswith("提交后，系统将")
            and cleaned not in {
                "本次无需额外补充填写。",
                "本次没有需要补充的AI改善建议或需确认事项。",
            }
        ):
            lines.append(cleaned)
    return list(dict.fromkeys(lines))


def _suggestion_dimension(text: str) -> str | None:
    value = str(text or "")
    if re.search(r"下一次|下次|下一步|联系时间|期望结果", value):
        return "N"
    if re.search(r"自评|评价|达到目的|部分达到", value):
        return "A2"
    if re.search(r"过程|客户事实|表达|动作|依据", value):
        return "R"
    if re.search(r"关键结果|原定目标|拜访目的|具体目的", value):
        return "O_KR"
    if re.search(r"预约|拜访方式", value):
        return "A1"
    if re.search(r"客户类型|商机阶段", value):
        return "T"
    return None


def build_artifact(
    *,
    visit: VisitDraftInput,
    tenant_id: str,
    user_id: str,
    check_id: str,
    quick_check_input_hash: str,
    source_record_id: str | None,
    feedback_text: str,
    decision_ledger: dict[str, Any] | None,
    front_review: dict[str, Any] | None,
    policy_version: str | None,
) -> FrontAnalysisArtifact:
    analysis, advice_items, parsed_confirmations = _split_tail(feedback_text)
    review = front_review or {}
    findings = _section_findings(review)
    finding_by_dimension = {item.dimension: item.finding_id for item in findings}
    suggestions = []
    for index, text in enumerate(advice_items, 1):
        dimension = _suggestion_dimension(text)
        suggestions.append(
            FrontSuggestion(
                suggestion_id=f"front-suggestion-{index}",
                finding_id=finding_by_dimension.get(dimension or ""),
                dimension=dimension,
                text=text,
            )
        )
    confirmations = [
        str(item).strip() for item in (review.get("confirmation_items") or [])
        if str(item).strip()
    ]
    confirmations.extend(parsed_confirmations)
    stable_hash = analysis_input_hash(visit)
    artifact_id = "fa_" + canonical_hash(
        {"tenant_id": tenant_id, "check_id": check_id, "input_hash": stable_hash}
    )[:28]
    return FrontAnalysisArtifact(
        artifact_id=artifact_id,
        input_hash=stable_hash,
        quick_check_input_hash=quick_check_input_hash,
        generated_at=datetime.now(UTC),
        tenant_id=tenant_id,
        user_id=user_id,
        check_id=check_id,
        source_record_id=source_record_id,
        analysis_summary=analysis or None,
        findings=findings,
        suggestions=suggestions,
        confirmation_items=list(dict.fromkeys(confirmations))[:8],
        decision_ledger=decision_ledger,
        policy_version=policy_version,
        prompt_version=(str(review.get("prompt_version")) if review.get("prompt_version") else None),
    )


def model_context(artifact: FrontAnalysisArtifact) -> dict[str, Any]:
    """Bounded context for the formal model; excludes storage/session details."""
    return {
        "schema_version": artifact.schema_version,
        "analysis_summary": artifact.analysis_summary,
        "findings": [item.model_dump(mode="json") for item in artifact.findings],
        "suggestions": [item.model_dump(mode="json") for item in artifact.suggestions],
        "confirmation_items": artifact.confirmation_items,
        "decision_ledger": artifact.decision_ledger,
        "policy_version": artifact.policy_version,
        "prompt_version": artifact.prompt_version,
    }
