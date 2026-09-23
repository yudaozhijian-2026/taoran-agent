import json
from pathlib import Path

import pytest

from taoran_agent.deep_review_gates import commitment_boundary_hits

REGRESSION_SET = json.loads(
    (Path(__file__).parent / "data" / "post_commitment_boundary_regression_set.json").read_text()
)["cases"]


@pytest.mark.parametrize("case", REGRESSION_SET, ids=lambda case: case["case_id"])
def test_post_commitment_boundary_regression_set(case):
    """Keep completed facts, intent and sales plans distinct from promises."""
    hits = commitment_boundary_hits(
        case["candidate"],
        case["source"],
        "facts.reason",
    )
    if case["expected"] == "pass":
        assert hits == []
    else:
        assert [hit["rule"] for hit in hits] == [case["rule"]]


def test_deterministic_n_repair_for_0353_keeps_completed_source_fact():
    """0353 reached this boundary after deterministic N repair in rc68."""
    case = next(case for case in REGRESSION_SET if case["case_id"] == "BFJL2026092100353")
    repaired_reason = (
        "客户张老师确认六台设备安装位置与电源准备均已完成，"
        "销售已逐项核对现场照片。双方尚未约定下一次联系日期。"
    )
    assert commitment_boundary_hits(repaired_reason, case["source"], "facts.reason") == []


def test_commitment_hit_retains_event_level_diagnostics():
    hits = commitment_boundary_hits(
        "客户已经发货。",
        "客户已确认下周发货，发货尚待发生。",
        "facts.reason",
    )
    assert len(hits) == 1
    assert hits[0]["rule"] == "future_commitment_presented_as_completed"
    assert hits[0]["candidate_event"] == {
        "clause": "客户已经发货",
        "event_window": "客户已经发货",
        "subject": "customer",
        "action": "发货",
        "state": "completed_fact",
        "usage": "fact",
        "time_or_condition": "",
    }
    assert hits[0]["source_future_commitments"][0]["action"] == "发货"
    assert hits[0]["source_future_commitments"][0]["state"] == "future_commitment"


def test_weekday_commitment_without_source_evidence_is_blocked_cleanly():
    hits = commitment_boundary_hits(
        "客户已同意周五收货，请据实补充双方约定的联系时间。",
        "本次未形成客户未来承诺。",
        "facts.reason",
    )
    assert [hit["rule"] for hit in hits] == ["unsupported_future_customer_commitment"]
    assert hits[0]["candidate_event"]["action"] == "收货"
    assert hits[0]["source_future_commitments"] == []
