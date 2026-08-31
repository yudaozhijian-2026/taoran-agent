from __future__ import annotations

from datetime import UTC, datetime

import pytest

from taoran_agent.knowledge import KnowledgeRecord, TaoranKnowledgeSnapshot
from taoran_agent.models import VisitDraftInput
from taoran_agent.purpose_mapping import (
    PurposeMappingError,
    purpose_mapping_record,
    purpose_policy_for_visit,
    structure_purpose_mapping,
)

MAPPING_CONTENT = """标准行为：
潜力客户：收集信息、保持接触、其他目的；
目标客户：收集信息、发展关系、其他目的；
商机客户按P1-P6使用收集信息、强化关系和阶段目的：P1获得参与资格、P2认可技术方案、P3赢得商务竞争、P4完成合同签署、P5协助项目实施、P6争取客户满意；其余统一归入“其他目的”并加文本说明。
质量要求：拜访目的应与客户类型和当前商机阶段一致。
"""


def mapping_record(content: str = MAPPING_CONTENT) -> KnowledgeRecord:
    return KnowledgeRecord(
        id="DSM-BS-01-06",
        title="拜访目的与关键结果标准",
        status="已确认",
        version="v1.0.0",
        summary="按客户类型和商机阶段选择拜访目的。",
        content=content,
        content_hash="purpose-mapping-hash",
        updated_at=datetime(2026, 8, 27, tzinfo=UTC),
    )


def visit(**updates) -> VisitDraftInput:
    payload = {
        "visit_date": "2026-08-27",
        "employee_id": "EMP001",
        "customer_type_ii": "opportunity",
        "opportunity_stage": "P3",
        "purpose_code": "许可参与商务谈判的机会",
    }
    payload.update(updates)
    return VisitDraftInput.model_validate(payload)


def test_mapping_is_extracted_p5_is_retained_and_p6_is_removed() -> None:
    mapping = structure_purpose_mapping(mapping_record())

    assert mapping.potential == ("收集信息", "保持接触", "其他目的")
    assert mapping.target == ("收集信息", "发展关系", "其他目的")
    assert mapping.opportunity_common == ("收集信息", "强化关系", "其他目的")
    assert mapping.opportunity_by_stage["P3"] == ("赢得商务竞争",)
    assert mapping.opportunity_by_stage["P5"] == ("协助项目实施",)
    assert all(
        "P6" not in purpose and "争取客户满意" not in purpose
        for values in mapping.opportunity_by_stage.values()
        for purpose in values
    )


@pytest.mark.parametrize(
    ("customer_type", "stage", "expected", "unexpected"),
    [
        ("potential", None, "保持接触", "发展关系"),
        ("target", None, "发展关系", "保持接触"),
        ("opportunity", "P3", "赢得商务竞争", "完成合同签署"),
    ],
)
def test_policy_is_scoped_to_customer_type_and_current_stage(
    customer_type: str,
    stage: str | None,
    expected: str,
    unexpected: str,
) -> None:
    mapping = structure_purpose_mapping(mapping_record())
    policy = purpose_policy_for_visit(
        mapping,
        visit(customer_type_ii=customer_type, opportunity_stage=stage),
    )

    assert policy is not None
    assert expected in policy.allowed_purposes
    assert unexpected not in policy.allowed_purposes
    assert "争取客户满意" not in policy.allowed_purposes
    assert policy.source_knowledge_id == "DSM-BS-01-06"


def test_opportunity_without_stage_is_not_failed_by_t02_and_allows_p1_to_p5() -> None:
    mapping = structure_purpose_mapping(mapping_record())
    policy = purpose_policy_for_visit(mapping, visit(opportunity_stage=None))

    assert policy is not None
    assert "获得参与资格" in policy.allowed_purposes
    assert "协助项目实施" in policy.allowed_purposes
    assert "争取客户满意" not in policy.allowed_purposes


def test_p6_is_terminal_and_retires_sales_visit_purpose_matching() -> None:
    mapping = structure_purpose_mapping(mapping_record())

    policy = purpose_policy_for_visit(mapping, visit(opportunity_stage="P6"))

    assert policy is not None
    assert policy.status == "retired"
    assert policy.opportunity_stages == ["P6"]
    assert policy.allowed_purposes == []


@pytest.mark.parametrize(
    ("customer_type", "stage", "purpose"),
    [
        ("potential", None, "保持接触"),
        ("potential", None, "保持关系"),
        ("opportunity", "P1", "获得参与"),
        ("opportunity", "P5", "协助项目实施"),
        ("opportunity", "P5", "完成合同签署"),
    ],
)
def test_confirmed_company_purpose_additions_are_allowed(
    customer_type: str,
    stage: str | None,
    purpose: str,
) -> None:
    mapping = structure_purpose_mapping(mapping_record())
    policy = purpose_policy_for_visit(
        mapping,
        visit(
            customer_type_ii=customer_type,
            opportunity_stage=stage,
            purpose_code=purpose,
        ),
    )

    assert policy is not None
    assert purpose in policy.allowed_purposes
    assert policy.policy_version.endswith("+COMPANY-PURPOSE-MAP-20260831-V1")
    assert "争取客户满意" not in policy.allowed_purposes


def test_invalid_or_incomplete_mapping_is_rejected_instead_of_invented() -> None:
    with pytest.raises(PurposeMappingError):
        structure_purpose_mapping(mapping_record("潜力客户：收集信息。"))


def test_parser_accepts_common_knowledge_wording_variants() -> None:
    content = MAPPING_CONTENT.replace("潜力客户：", "潜力客户目的可选择：").replace(
        "P3赢得商务竞争",
        "P3：赢得商务竞争",
    )

    mapping = structure_purpose_mapping(mapping_record(content))

    assert "保持接触" in mapping.potential
    assert mapping.opportunity_by_stage["P3"] == ("赢得商务竞争",)


def test_parser_keeps_legacy_flat_wording_compatible() -> None:
    content = """标准行为：
潜力客户：收集信息、保持关系、其他；
目标客户：收集信息、发展关系、其他；
商机客户：收集信息、加强关系、P1可以获得参与、P2获得提方案的机会、P3许可参与商务谈判的机会、P4赢得客户的订单、P5协助项目实施、P6争取客户满意。
质量要求：拜访目的应与客户类型和当前商机阶段一致。
"""

    mapping = structure_purpose_mapping(mapping_record(content))

    assert mapping.opportunity_by_stage["P5"] == ("协助项目实施",)
    assert "争取客户满意" not in {
        purpose for values in mapping.opportunity_by_stage.values() for purpose in values
    }


def test_snapshot_record_lookup_requires_exact_id_and_title() -> None:
    record = mapping_record()
    snapshot = TaoranKnowledgeSnapshot(
        source="test",
        retrieved_at=datetime.now(UTC),
        record_count=1,
        records=[record],
    )

    assert purpose_mapping_record(snapshot) == record
