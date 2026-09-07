from taoran_agent.experimental_receipt_role import receipt_role_hint
from taoran_agent.experimental_semantic_streaming_v22 import _messages, _interactive_messages
from taoran_agent.experimental_final_consistency import asserts_unrecorded_receipt
from taoran_agent.experimental_receipt_role import proxy_receipt_goal_conflict
from taoran_agent.experimental_final_diagnostics import category
from taoran_agent.llm import _wording_format_failure


def test_clarification_binds_to_unchanged_goal_and_process():
    context = {"expected_key_result": "现场收货", "process_description": "现场给客户收货，送卡"}
    hint = receipt_role_hint(context)
    assert "销售替客户收货" in hint
    assert "不代表业务已确认所有目标及自评均合格" in hint
    assert hint in _interactive_messages(context)[0]["content"]
    assert hint not in _messages(context)[0]["content"]
    assert receipt_role_hint({**context, "process_description": "客户已签收"}) == ""
    assert receipt_role_hint({**context, "expected_key_result": "客户验收"}) == ""
    assert receipt_role_hint({}) == ""


def test_sales_proxy_receipt_does_not_prove_customer_receipt():
    context = {"process_description": "现场给客户收货，送卡"}
    assert asserts_unrecorded_receipt("客户已签收货物。", context)
    assert not asserts_unrecorded_receipt("销售替客户收货并送卡。", context)
    assert not asserts_unrecorded_receipt("客户已签收货物。", {"process_description": "客户已签收货物。"})


def test_customer_acceptance_cannot_replace_sales_proxy_goal():
    context = {"expected_key_result": "现场收货", "process_description": "现场给客户收货，送卡"}
    bad = "尚不足以证明现场收货这一关键结果已由客户确认完成。"
    assert proxy_receipt_goal_conflict(bad, context)
    assert not proxy_receipt_goal_conflict("销售已代收货，客户是否签收未记录。", context)
    assert not proxy_receipt_goal_conflict(bad, {**context, "expected_key_result": "客户签收"})
    reason = "wording_experimental_receipt_role_conflict"
    assert _wording_format_failure(reason)
    assert category(reason) == "final_receipt_role_conflict"
