from copy import deepcopy

import pytest
from test_agent import complete_precheck_payload

from taoran_agent.knowledge import load_taoran_knowledge_snapshot
from taoran_agent.models import PrecheckRequest
from taoran_agent.precheck_engine import TaoranPrecheckEngine
from taoran_agent.rules import load_rule_catalog, normalized_text


def _engine() -> TaoranPrecheckEngine:
    return TaoranPrecheckEngine(load_taoran_knowledge_snapshot())


def _vague_phrases() -> set[str]:
    return {
        normalized_text(value)
        for value in load_rule_catalog()["vague_exact_phrases"]
    }


def test_snapshot_contains_only_active_taoran_knowledge() -> None:
    snapshot = load_taoran_knowledge_snapshot()

    assert snapshot.record_count == 2
    assert {record.id for record in snapshot.records} == {
        "DSM-BS-000",
        "DSM-BS-01-07",
    }
    assert {record.status for record in snapshot.records} == {"已批准", "已确认"}
    assert len(snapshot.snapshot_hash) == 64


def test_complete_visit_meets_all_six_knowledge_dimensions() -> None:
    request = PrecheckRequest.model_validate(complete_precheck_payload())

    result = _engine().check(request.visit, None, _vague_phrases())

    assert result.score == 100
    assert [section.code for section in result.sections] == [
        "T",
        "A1",
        "O_KR",
        "R",
        "A2",
        "N",
    ]
    assert all(section.status == "met" for section in result.sections)
    assert result.issues == []


def test_result_rejects_unverifiable_feeling_only_record() -> None:
    payload = complete_precheck_payload()
    payload["visit"]["process_description"] = "我感觉客户非常满意，沟通顺利。"
    request = PrecheckRequest.model_validate(payload)

    result = _engine().check(request.visit, None, _vague_phrases())
    codes = {issue.code for issue in result.issues}

    assert "TAORAN_RESULT_NOT_FACT_BASED" in codes
    assert "TAORAN_FACT_JUDGMENT_MIXED" in codes
    assert "TAORAN_ASSESSMENT_NOT_EVIDENCED" in codes
    assert result.score < 100


def test_video_visit_requires_appointment_for_every_customer_type() -> None:
    payload = complete_precheck_payload()
    payload["visit"].update({
        "customer_type_ii": "potential",
        "opportunity_id": None,
        "opportunity_stage": None,
        "visit_method": "video",
        "is_appointment": False,
    })
    request = PrecheckRequest.model_validate(payload)

    result = _engine().check(request.visit, None, _vague_phrases())

    assert "TAORAN_VIDEO_APPOINTMENT_REQUIRED" in {
        issue.code for issue in result.issues
    }


def test_target_single_unappointed_is_not_a_precheck_failure() -> None:
    payload = complete_precheck_payload()
    payload["visit"].update({
        "customer_type_ii": "target",
        "opportunity_id": None,
        "opportunity_stage": None,
        "is_appointment": False,
    })
    request = PrecheckRequest.model_validate(payload)

    result = _engine().check(request.visit, None, _vague_phrases())

    assert "TAORAN_APPOINTMENT_NOT_ALIGNED" not in {
        issue.code for issue in result.issues
    }


@pytest.mark.parametrize("stage", ["P0", "P7", "阶段三", "P3-推进"])
def test_opportunity_stage_must_be_exact_p1_to_p6(stage) -> None:
    payload = complete_precheck_payload()
    payload["visit"]["opportunity_stage"] = stage
    request = PrecheckRequest.model_validate(payload)

    result = _engine().check(request.visit, None, _vague_phrases())

    assert "TAORAN_OPPORTUNITY_STAGE_INVALID" in {
        issue.code for issue in result.issues
    }


def test_process_evidence_is_classified_for_audit() -> None:
    payload = complete_precheck_payload()
    payload["visit"]["process_description"] = (
        "信息中心主任确认8月25日验证。客户提出预算审批是前提。"
        "我认为项目可以推进。可能月底完成审批。"
    )
    request = PrecheckRequest.model_validate(payload)

    result = _engine().check(request.visit, None, _vague_phrases())
    section = next(item for item in result.sections if item.code == "R")

    assert {item.category for item in section.classified_evidence} >= {
        "customer_commitment",
        "customer_objection_or_condition",
        "sales_judgment",
        "assumption",
    }


def test_connector_omitted_process_is_not_reported_as_user_missing() -> None:
    payload = deepcopy(complete_precheck_payload())
    payload["visit"].pop("process_description")
    payload["visit"]["metadata"] = {
        "source_supplied_fields": [
            key for key in payload["visit"] if key != "metadata"
        ]
    }
    request = PrecheckRequest.model_validate(payload)
    supplied = set(request.visit.metadata["source_supplied_fields"])

    result = _engine().check(request.visit, supplied, _vague_phrases())
    result_section = next(section for section in result.sections if section.code == "R")

    assert result_section.status == "not_received"
    assert all("process_description" not in issue.field_paths for issue in result.issues)


def test_next_step_defaults_to_current_customer_without_specific_contact() -> None:
    payload = complete_precheck_payload()
    payload["visit"]["participants"] = []
    payload["visit"]["next_action_target_id"] = None
    request = PrecheckRequest.model_validate(payload)

    result = _engine().check(request.visit, None, _vague_phrases())

    assert "TAORAN_NSA_TARGET_MISSING" not in {issue.code for issue in result.issues}
    assert next(section for section in result.sections if section.code == "N").status == "met"


def test_next_step_object_means_current_customer() -> None:
    payload = complete_precheck_payload()
    payload["visit"]["customer_id"] = None
    request = PrecheckRequest.model_validate(payload)

    result = _engine().check(request.visit, None, _vague_phrases())

    assert "TAORAN_NSA_CUSTOMER_MISSING" in {
        issue.code for issue in result.issues
    }
    issue = next(
        item for item in result.issues if item.code == "TAORAN_NSA_CUSTOMER_MISSING"
    )
    assert "不要求另填具体联系人" in issue.suggestion


@pytest.mark.parametrize(
    ("customer_type", "contact_at", "expected_issue"),
    [
        ("target", "2026-08-28T10:00:00+08:00", True),
        ("target", "2026-09-01T10:00:00+08:00", False),
        ("potential", "2026-09-30T10:00:00+08:00", True),
        ("potential", "2026-10-01T10:00:00+08:00", False),
        ("opportunity", "2026-08-20T10:00:00+08:00", False),
    ],
)
def test_next_step_uses_company_n06_natural_periods(
    customer_type, contact_at, expected_issue
) -> None:
    payload = complete_precheck_payload()
    payload["visit"]["customer_type_ii"] = customer_type
    payload["visit"]["next_contact_at"] = contact_at
    if customer_type != "opportunity":
        payload["visit"]["opportunity_id"] = None
        payload["visit"]["opportunity_stage"] = None
    request = PrecheckRequest.model_validate(payload)

    result = _engine().check(request.visit, None, _vague_phrases())

    assert (
        "TAORAN_NSA_PERIOD_NOT_ALIGNED" in {issue.code for issue in result.issues}
    ) is expected_issue


def test_opportunity_next_step_requires_explicit_customer_consensus() -> None:
    payload = complete_precheck_payload()
    payload["visit"]["process_description"] = "销售计划8月25日发送验证方案并继续推进。"
    request = PrecheckRequest.model_validate(payload)

    result = _engine().check(request.visit, None, _vague_phrases())

    assert "TAORAN_NSA_CUSTOMER_CONSENSUS_MISSING" in {
        issue.code for issue in result.issues
    }


def test_opportunity_consensus_can_coexist_with_unrelated_negative_fact() -> None:
    payload = complete_precheck_payload()
    payload["visit"]["process_description"] = (
        "客户未确认预算，但技术负责人同意8月25日验证，并确认参会人员。"
    )
    request = PrecheckRequest.model_validate(payload)

    result = _engine().check(request.visit, None, _vague_phrases())

    assert "TAORAN_NSA_CUSTOMER_CONSENSUS_MISSING" not in {
        issue.code for issue in result.issues
    }


def test_next_expected_result_accepts_customer_role_action_wording() -> None:
    payload = complete_precheck_payload()
    payload["visit"]["next_action_expected_result"] = "技术负责人将在下周确认验证范围和参会人员"
    request = PrecheckRequest.model_validate(payload)

    result = _engine().check(request.visit, None, _vague_phrases())

    assert "TAORAN_NSA_RESULT_NOT_ACTIONABLE" not in {
        issue.code for issue in result.issues
    }


def test_next_step_rejects_generic_purpose_and_unobservable_result() -> None:
    payload = complete_precheck_payload()
    payload["visit"]["next_action_purpose"] = "继续跟进"
    payload["visit"]["next_action_expected_result"] = "完成沟通"
    request = PrecheckRequest.model_validate(payload)

    result = _engine().check(request.visit, None, _vague_phrases())
    codes = {issue.code for issue in result.issues}

    assert "TAORAN_NSA_PURPOSE_VAGUE" in codes
    assert "TAORAN_NSA_RESULT_NOT_ACTIONABLE" in codes


@pytest.mark.parametrize(
    ("contact_at", "expected_code", "explanation"),
    [
        ("2026-08-17T10:00:00+08:00", "TAORAN_NSA_TIME_NOT_AFTER_VISIT", "早于"),
        ("2026-08-18T20:00:00+08:00", "TAORAN_NSA_TIME_NOT_AFTER_VISIT", "相同"),
        (None, "TAORAN_NSA_TIME_MISSING", "未填写"),
        ("2026-08-19T00:00:00+08:00", None, None),
        ("2026-08-18T16:00:00Z", None, None),
        ("2026-08-19T00:00:00", None, None),
    ],
)
def test_next_contact_date_returns_explained_advice(contact_at, expected_code, explanation):
    payload = complete_precheck_payload()
    payload["visit"]["next_contact_at"] = contact_at
    request = PrecheckRequest.model_validate(payload)

    result = _engine().check(request.visit, None, _vague_phrases())
    time_issues = [i for i in result.issues if i.code.startswith("TAORAN_NSA_TIME_")]

    assert [i.code for i in time_issues] == ([expected_code] if expected_code else [])
    section = next(s for s in result.sections if s.code == "N")
    assert section.status == ("needs_revision" if expected_code else "met")
    if expected_code:
        assert "下一次联系客户时间安排" in time_issues[0].message
        assert explanation in time_issues[0].message
        if expected_code.endswith("NOT_AFTER_VISIT"):
            assert "下一次联系客户时间安排：异常。异常说明：" in time_issues[0].message
            assert "2026-08-18" in time_issues[0].message
            assert contact_at[:10] in time_issues[0].message
            assert "补充" not in time_issues[0].suggestion
    if contact_at and contact_at.endswith("Z"):
        assert request.visit.next_contact_at.utcoffset().total_seconds() == 0
        assert request.visit.next_contact_date.isoformat() == "2026-08-19"


@pytest.mark.parametrize("unavailable_field", ["next_contact_at", "visit_date"])
def test_date_order_is_not_judged_with_unreceived_comparison_field(unavailable_field):
    payload = complete_precheck_payload()
    payload["visit"]["next_contact_at"] = "2026-08-17T10:00:00+08:00"
    request = PrecheckRequest.model_validate(payload)
    supplied = set(payload["visit"]) - {unavailable_field}

    result = _engine().check(request.visit, supplied, _vague_phrases())

    assert not any(i.code.startswith("TAORAN_NSA_TIME_") for i in result.issues)
    assert next(s for s in result.sections if s.code == "N").status == "partial_input"
