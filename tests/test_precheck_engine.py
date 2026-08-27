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


def test_next_step_requires_customer_target_when_field_is_available() -> None:
    payload = complete_precheck_payload()
    payload["visit"]["participants"] = []
    payload["visit"]["next_action_target_id"] = None
    request = PrecheckRequest.model_validate(payload)

    result = _engine().check(request.visit, None, _vague_phrases())

    assert "TAORAN_NSA_TARGET_MISSING" in {issue.code for issue in result.issues}


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
