import pytest

from taoran_agent.experimental_semantic_streaming_v22 import detect_unsupported_specific_facts
from taoran_agent.front_v46.experimental_semantic_streaming_v22 import (
    detect_unsupported_specific_facts as detect_front_unsupported_specific_facts,
)


def _result(text: str, source: str = "客户确认下周一提供三台设备清单。") -> dict:
    return detect_unsupported_specific_facts(
        text,
        {"process_description": source},
        interactive=True,
    )


@pytest.mark.parametrize(
    ("record_code", "source", "candidate"),
    [
        (
            "BFJL2026092100367",
            "【独立模拟提交实验20260916-C-修改后】客户确认下周一提供三台设备清单。",
            "客户承诺下周一提供三台设备清单，后续动作尚待发生。",
        ),
        (
            "BFJL2026092100368",
            "【独立模拟提交实验20260916-B-修改后】客户改为下周一提供两台设备清单。",
            "客户承诺下周一提供两台设备清单，后续动作尚待发生。",
        ),
    ],
)
def test_backend_preview_allows_confirmed_future_action_as_a_faithful_promise(
    record_code: str,
    source: str,
    candidate: str,
):
    assert _result(candidate, source)["failure_category"] is None, record_code


def test_backend_preview_keeps_action_and_completion_boundaries_for_future_promise():
    assert _result("客户承诺下周一付款。 ")["failure_category"] == "unsupported_customer_commitment"
    assert _result("客户已完成提供三台设备清单。 ")["failure_category"] == "unsupported_customer_commitment"


@pytest.mark.parametrize(
    ("candidate", "blocked"),
    [
        ("本次记录中销售方已按USD690/kg报价，双方尚未达成一致。", False),
        ("当前记录不足以证明客户已同意下一步安排。", False),
        ("客户已同意下周采购。", True),
        ("客户承诺周五提供三台设备清单。", True),
    ],
)
def test_front_and_backend_preview_keep_0497_fact_and_promise_boundaries_aligned(
    candidate, blocked,
):
    source = "客户希望价格USD650/kg，我方按USD690/kg报价。客户承诺下周一提供三台设备清单。"
    snapshot = {"process_description": source}
    backend = detect_unsupported_specific_facts(candidate, snapshot, interactive=True)["failure_category"]
    front = detect_front_unsupported_specific_facts(candidate, snapshot, interactive=True)["failure_category"]
    assert bool(backend) is blocked
    assert bool(front) is blocked
