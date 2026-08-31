from __future__ import annotations

import re
from dataclasses import dataclass

from .knowledge import KnowledgeRecord, TaoranKnowledgeSnapshot
from .models import CustomerTypeII, PurposePolicyInput, VisitDraftInput

PURPOSE_MAPPING_KNOWLEDGE_ID = "DSM-BS-01-06"
PURPOSE_MAPPING_TITLE = "拜访目的与关键结果标准"
_SECTION_END_MARKERS = (
    "潜力客户",
    "目标客户",
    "商机客户",
    "质量要求",
    "可验证证据",
    "例外边界",
    "固定内容",
    "DSM固定内容",
    "企业参数",
    "企业建模",
    "后续接口",
)
_EXCLUDED_PURPOSE_MARKERS = ("P6", "争取客户满意")
COMPANY_PURPOSE_POLICY_VERSION = "COMPANY-PURPOSE-MAP-20260831-V1"
_COMPANY_CUSTOMER_PURPOSES = {
    CustomerTypeII.POTENTIAL: ("保持接触", "保持关系"),
}
_COMPANY_STAGE_PURPOSES = {
    "P1": ("获得参与",),
    "P5": ("完成合同签署",),
}


class PurposeMappingError(ValueError):
    """知识存在但无法安全转换为可执行的目的映射。"""


@dataclass(frozen=True)
class StructuredPurposeMapping:
    potential: tuple[str, ...]
    target: tuple[str, ...]
    opportunity_common: tuple[str, ...]
    opportunity_by_stage: dict[str, tuple[str, ...]]
    knowledge_id: str
    knowledge_version: str
    content_hash: str


def purpose_mapping_record(
    snapshot: TaoranKnowledgeSnapshot,
) -> KnowledgeRecord | None:
    return next(
        (
            record
            for record in snapshot.records
            if record.id == PURPOSE_MAPPING_KNOWLEDGE_ID and record.title == PURPOSE_MAPPING_TITLE
        ),
        None,
    )


def structure_purpose_mapping(record: KnowledgeRecord) -> StructuredPurposeMapping:
    if record.id != PURPOSE_MAPPING_KNOWLEDGE_ID:
        raise PurposeMappingError("拜访目的映射知识ID不正确")
    if record.title != PURPOSE_MAPPING_TITLE:
        raise PurposeMappingError("拜访目的映射知识标题不正确")

    text = "\n".join(value for value in (record.summary, record.content) if value)
    potential = _customer_purposes(text, "潜力客户")
    target = _customer_purposes(text, "目标客户")
    opportunity_common, opportunity_by_stage = _opportunity_purposes(text)

    if not potential or not target or not opportunity_common:
        raise PurposeMappingError("知识内容缺少客户类型对应的拜访目的")
    if any(not opportunity_by_stage[stage] for stage in opportunity_by_stage):
        raise PurposeMappingError("知识内容缺少P1-P5阶段对应的拜访目的")

    return StructuredPurposeMapping(
        potential=_unique_allowed(potential),
        target=_unique_allowed(target),
        opportunity_common=_unique_allowed(opportunity_common),
        opportunity_by_stage={
            stage: _unique_allowed(values) for stage, values in opportunity_by_stage.items()
        },
        knowledge_id=record.id,
        knowledge_version=record.version,
        content_hash=record.content_hash,
    )


def purpose_policy_for_visit(
    mapping: StructuredPurposeMapping,
    visit: VisitDraftInput,
) -> PurposePolicyInput | None:
    if visit.customer_type_ii is None:
        return None
    if visit.customer_type_ii == CustomerTypeII.POTENTIAL:
        allowed = list(mapping.potential)
        stages: list[str] = []
    elif visit.customer_type_ii == CustomerTypeII.TARGET:
        allowed = list(mapping.target)
        stages = []
    else:
        stages = _visit_stages(visit)
        allowed = list(mapping.opportunity_common)
        if stages:
            for stage in stages:
                allowed.extend(mapping.opportunity_by_stage.get(stage, ()))
        else:
            # T-02不校验阶段；阶段缺失时只收窄到商机客户全部P1-P5目的，避免误判。
            for values in mapping.opportunity_by_stage.values():
                allowed.extend(values)
    allowed.extend(_COMPANY_CUSTOMER_PURPOSES.get(visit.customer_type_ii, ()))
    if visit.customer_type_ii == CustomerTypeII.OPPORTUNITY:
        effective_stages = stages or list(mapping.opportunity_by_stage)
        for stage in effective_stages:
            allowed.extend(_COMPANY_STAGE_PURPOSES.get(stage, ()))
    allowed = [purpose for purpose in _unique_allowed(allowed) if not _excluded(purpose)]
    return PurposePolicyInput(
        policy_version=(
            f"{mapping.knowledge_id}:{mapping.knowledge_version}"
            f"+{COMPANY_PURPOSE_POLICY_VERSION}"
        ),
        status="active",
        allowed_purposes=allowed,
        effective_from=visit.visit_date,
        source_knowledge_id=mapping.knowledge_id,
        source_content_hash=mapping.content_hash,
        customer_type=visit.customer_type_ii,
        opportunity_stages=stages,
        excluded_purposes=["P6", "争取客户满意"],
    )


def _customer_purposes(text: str, customer_label: str) -> list[str]:
    section = _customer_section(text, customer_label)
    if not section:
        return []
    raw_items = re.split(r"[、,，;；|/\n]+", section)
    return [item for item in (_clean_purpose(value) for value in raw_items) if item]


def _customer_section(text: str, customer_label: str) -> str:
    next_markers = "|".join(
        re.escape(marker) for marker in _SECTION_END_MARKERS if marker != customer_label
    )
    if customer_label == "商机客户":
        pattern = re.compile(
            rf"{re.escape(customer_label)}(?:的?拜访目的|目的)?\s*"
            rf"(?:(?:包括|包含|可选择|选择|为|是|：|:)\s*)?"
            rf"(.+?)(?={next_markers}|$)",
            re.DOTALL,
        )
    else:
        pattern = re.compile(
            rf"{re.escape(customer_label)}(?:的?拜访目的|目的)?\s*"
            rf"(?:包括|包含|可选择|选择|为|是|：|:)\s*"
            rf"(.+?)(?={next_markers}|$)",
            re.DOTALL,
        )
    match = pattern.search(text)
    if not match:
        return ""
    return match.group(1)


def _opportunity_purposes(text: str) -> tuple[list[str], dict[str, list[str]]]:
    section = _customer_section(text, "商机客户")
    stages: dict[str, list[str]] = {f"P{i}": [] for i in range(1, 6)}
    if not section:
        return [], stages

    # 知识正文可能先写“按P1-P6使用……和阶段目的”，这里先移除范围文字，
    # 避免把范围中的P1误识别成P1的具体目的。
    normalized = re.sub(
        r"P1\s*(?:-|－|—|–|至|到)\s*P6",
        "商机各阶段",
        section,
        flags=re.IGNORECASE,
    )
    stage_pattern = re.compile(
        r"(P[1-6])\s*[：:、.．\-—]?\s*(.*?)"
        r"(?=(?:[、,，]\s*P[1-6]\s*[：:、.．\-—]?)|[；;。\n]|$)",
        re.IGNORECASE | re.DOTALL,
    )
    stage_matches = list(stage_pattern.finditer(normalized))
    if not stage_matches:
        return [], stages

    common_text = normalized[: stage_matches[0].start()]
    common_text = re.sub(
        r"^.*?(?:使用|包括|包含|可选择|选择)",
        "",
        common_text,
        count=1,
    )
    common_text = re.sub(
        r"(?:和|与)?\s*阶段目的\s*[：:]?\s*$",
        "",
        common_text,
    )
    common = [
        item
        for item in (_clean_purpose(value) for value in re.split(r"[、,，;；|/\n]+", common_text))
        if item and not _excluded(item)
    ]
    if "其他目的" in normalized and "其他目的" not in common:
        common.append("其他目的")

    for match in stage_matches:
        stage = match.group(1).upper()
        purpose = _clean_purpose(match.group(2))
        if stage in stages and purpose and not _excluded(f"{stage}{purpose}"):
            stages[stage].append(purpose)
    return common, stages


def _clean_purpose(value: str) -> str:
    return re.sub(r"^[\s\-—•·（(]*|[。；;，,、\s）)]+$", "", value).strip()


def _excluded(value: str) -> bool:
    compact = re.sub(r"\s+", "", value).upper()
    return any(marker.upper() in compact for marker in _EXCLUDED_PURPOSE_MARKERS)


def _unique_allowed(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value and not _excluded(value)))


def _visit_stages(visit: VisitDraftInput) -> list[str]:
    raw = [item.current_stage for item in visit.opportunities if item.current_stage]
    if not raw and visit.opportunity_stage:
        raw = [visit.opportunity_stage]
    stages: list[str] = []
    for value in raw:
        match = re.search(r"\bP([1-5])\b", value.upper())
        if match:
            stages.append(f"P{match.group(1)}")
    return list(dict.fromkeys(stages))
