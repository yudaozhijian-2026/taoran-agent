from __future__ import annotations

import json
import re
from importlib.resources import files
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import ClassifiedEvidence, VisitDraftInput


class EvidenceSectionStandard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    standard: str
    evidence: list[str] = Field(min_length=1)


class QualityEvidenceStandard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str
    standard_id: str
    standard_version: str
    source_file: str
    scoring_policy: str
    sections: dict[str, EvidenceSectionStandard]

    @model_validator(mode="after")
    def validate_sections(self) -> QualityEvidenceStandard:
        if set(self.sections) != {"T", "A1", "O_KR", "R", "A2", "N"}:
            raise ValueError("TAORAN证据标准必须包含且仅包含六项")
        return self


def load_quality_evidence_standard() -> QualityEvidenceStandard:
    resource = files("taoran_agent.data").joinpath(
        "taoran_quality_evidence_standard_v1.json"
    )
    return QualityEvidenceStandard.model_validate_json(resource.read_text(encoding="utf-8"))


def model_guidance(standard: QualityEvidenceStandard | None = None) -> str:
    selected = standard or load_quality_evidence_standard()
    compact: dict[str, Any] = {
        code: {
            "standard": item.standard,
            "required_evidence": item.evidence,
        }
        for code, item in selected.sections.items()
    }
    return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))


_ASSUMPTION_MARKERS = ("估计", "可能", "大概", "预计", "猜测", "或许", "应该会")
_JUDGMENT_MARKERS = ("我认为", "我感觉", "我判断", "应该", "沟通顺利", "非常满意")
_COMMITMENT_MARKERS = ("同意", "确认", "认可", "约定", "承诺", "愿意")
_OBJECTION_MARKERS = ("异议", "不同意", "拒绝", "条件", "前提", "担心", "质疑", "要求")
_CUSTOMER_FACT_MARKERS = (
    "客户", "院方", "校方", "主任", "经理", "负责人", "采购", "技术", "决策人",
    "提出", "反馈", "表示", "说明", "提供", "决定",
)


def classify_visit_evidence(visit: VisitDraftInput, section_code: str) -> list[ClassifiedEvidence]:
    evidence: list[ClassifiedEvidence] = []
    if section_code == "T":
        if visit.customer_type_ii:
            evidence.append(ClassifiedEvidence(
                field_path="customer_type_ii",
                quote=visit.customer_type_ii.value,
                category="system_fact",
                source="input",
            ))
        for stage in _current_stages(visit):
            evidence.append(ClassifiedEvidence(
                field_path="opportunities[].current_stage" if visit.opportunities else "opportunity_stage",
                quote=stage,
                category="system_fact",
                source="input",
            ))
        provenance = visit.metadata.get("field_provenance")
        if isinstance(provenance, dict):
            for field_path in ("customer_type_ii", "opportunities", "opportunity_stage"):
                source_name = provenance.get(field_path)
                if isinstance(source_name, str) and source_name:
                    evidence.append(ClassifiedEvidence(
                        field_path=f"metadata.field_provenance.{field_path}",
                        quote=source_name,
                        category="system_fact",
                        source="input",
                    ))
    elif section_code == "A1":
        if visit.visit_method:
            evidence.append(ClassifiedEvidence(
                field_path="visit_method", quote=visit.visit_method.value,
                category="system_fact", source="input",
            ))
        if visit.is_appointment is not None:
            evidence.append(ClassifiedEvidence(
                field_path="is_appointment", quote=str(visit.is_appointment).lower(),
                category="system_fact", source="input",
            ))
    elif section_code in {"R", "A2", "N"}:
        evidence.extend(_classify_process(visit.process_description))
        if visit.customer_feedback:
            evidence.extend(_classify_text("customer_feedback", visit.customer_feedback))
        if section_code == "A2" and visit.self_assessment:
            evidence.append(ClassifiedEvidence(
                field_path="self_assessment", quote=visit.self_assessment.value,
                category="sales_judgment", source="input",
            ))
        if section_code == "N":
            for field_path, value in (
                ("next_action_purpose", visit.next_action_purpose),
                ("next_action_other_purpose", visit.next_action_other_purpose),
                ("next_action_expected_result", visit.next_action_expected_result),
            ):
                if value:
                    evidence.append(ClassifiedEvidence(
                        field_path=field_path, quote=value[:300],
                        category="planned_action", source="input",
                    ))
    return _unique_evidence(evidence)


def _classify_process(text: str | None) -> list[ClassifiedEvidence]:
    if not text:
        return []
    return _classify_text("process_description", text)


def _classify_text(field_path: str, text: str) -> list[ClassifiedEvidence]:
    evidence: list[ClassifiedEvidence] = []
    parts = [part.strip() for part in re.split(r"[。！？!?；;\n]+", text) if part.strip()]
    for part in parts[:12]:
        category = "other"
        if any(marker in part for marker in _ASSUMPTION_MARKERS):
            category = "assumption"
        elif any(marker in part for marker in _JUDGMENT_MARKERS):
            category = "sales_judgment"
        elif any(marker in part for marker in _OBJECTION_MARKERS):
            category = "customer_objection_or_condition"
        elif any(marker in part for marker in _COMMITMENT_MARKERS):
            category = "customer_commitment"
        elif any(marker in part for marker in _CUSTOMER_FACT_MARKERS):
            category = "customer_fact"
        evidence.append(ClassifiedEvidence(
            field_path=field_path,
            quote=part[:300],
            category=category,
            source="rule",
        ))
    return evidence


def _current_stages(visit: VisitDraftInput) -> list[str]:
    values = [item.current_stage for item in visit.opportunities if item.current_stage]
    if not values and visit.opportunity_stage:
        values = [visit.opportunity_stage]
    return list(dict.fromkeys(values))


def _unique_evidence(items: list[ClassifiedEvidence]) -> list[ClassifiedEvidence]:
    seen: set[tuple[str, str, str]] = set()
    result: list[ClassifiedEvidence] = []
    for item in items:
        identity = (item.field_path, item.quote, item.category)
        if identity not in seen:
            result.append(item)
            seen.add(identity)
    return result[:16]
