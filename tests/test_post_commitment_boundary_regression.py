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
