from taoran_agent.experimental_attribution import (
    attribution_conflict,
    attribution_hints,
    speaker_spans,
)
from taoran_agent.experimental_final_diagnostics import category, repair_instruction
from taoran_agent.llm import _wording_format_failure

SOURCE = "李部长说他先将龙泽情况反馈QA和生产部门，后续看机会什么时候增加。客户反馈目前没有新的采购计划。后面继续推进龙泽LKXA的增供。"


def test_actual_regression_wrong_sales_attribution_rejected():
    assert attribution_conflict("销售判断后续看机会增加并继续推进增供，但客户未确认启动时间。", SOURCE)


def test_correct_customer_and_neutral_plan_allowed():
    assert not attribution_conflict("李部长表示先反馈QA和生产部门，后续看机会增加。记录提到后续继续推进增供。", SOURCE)
    assert not attribution_conflict("暂无新采购计划，后续继续跟进。", SOURCE)


def test_reverse_attribution_and_same_statement_by_both_sides():
    source = "销售表示下周继续推动增供。客户表示暂无采购计划。"
    assert attribution_conflict("客户表示下周继续推动增供。", source)
    assert not attribution_conflict("客户表示下周继续推动增供。", source + "客户表示下周继续推动增供。")


def test_explicit_sales_manager_is_not_customer():
    assert speaker_spans("我方张经理说下周继续推动增供。")[0]["role"] == "sales"


def test_hints_are_exact_quotes_and_stop_before_sales_intro():
    source = "李部长反馈没有二供计划，给客户介绍龙泽资质。" + SOURCE
    hints = attribution_hints(source)
    assert hints
    assert all(hint["quote"] in source for hint in hints)
    assert "给客户介绍" not in hints[0]["quote"]
    assert all("后面继续推进" not in hint["quote"] for hint in hints)


def test_nested_report_not_treated_as_direct_speech():
    assert speaker_spans("销售表示客户说后续看机会增加。") == []
    assert not attribution_conflict("不是销售判断后续看机会增加。", SOURCE)


def test_new_failure_is_traceable_and_retriable():
    code = "wording_speaker_attribution_conflict"
    assert category(code) == "final_attribution_conflict"
    assert _wording_format_failure(code)
    assert "另一方" in repair_instruction(code)
