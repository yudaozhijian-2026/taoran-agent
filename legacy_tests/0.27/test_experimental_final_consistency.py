from types import SimpleNamespace

import pytest

from taoran_agent import api
from taoran_agent.experimental_final_consistency import (
    asserts_unrecorded_receipt,
    denies_recorded_customer_action,
    has_analysis_conflict,
    has_field_role_conflict,
    has_grounded_visit_result,
    invents_sales_forecast,
    pending_finding_codes,
)
from taoran_agent.llm import _wording_format_failure


def test_only_rechecked_findings_are_withheld():
    codes = pending_finding_codes([{"code": "R"}])
    assert "Q34_PROCESS_NOT_FACT_BASED" in codes
    assert "RESULT_LACKS_CUSTOMER_FACTS" in codes
    assert "TAORAN_RESULT_MISSING" not in codes
    assert "Q34_KEY_RESULT_QUALITY_NOT_MET" not in codes
    assert "TAORAN_ASSESSMENT_MISSING" not in codes
    assert pending_finding_codes([]) == {"TAORAN_ASSESSMENT_NOT_EVIDENCED"}


@pytest.mark.parametrize("text", [
    "客户已下单500g M13和DDQ，但过程缺少可核验的客户事实。",
    "过程没有记录客户的具体表达或动作。",
    "当前记录缺乏客户实际反馈。",
])
def test_concrete_process_cannot_be_denied(text):
    assert has_analysis_conflict(text, {"R": True})
    assert not has_analysis_conflict(text, {"R": False})
    assert not has_analysis_conflict(text, {})


def test_objective_gap_is_not_process_gap():
    assert not has_analysis_conflict(
        "客户已下单500g M13和DDQ，但本次目标不具体，尚未明确要解决哪些实施阻碍。",
        {"R": True, "O_KR": False},
    )
    assert not has_analysis_conflict("客户没有增加二供计划，先反馈QA和生产部门。", {"R": True})
    assert not has_analysis_conflict("过程有客户反馈，但下次期望结果不清楚。", {"R": True, "N": False})


def test_consistency_failure_enters_existing_single_retry_path():
    assert _wording_format_failure("wording_analysis_consistency_conflict")
    assert _wording_format_failure("wording_experimental_visit_result_missing")


def test_analysis_must_contain_grounded_visit_result_not_just_overview():
    proof = SimpleNamespace(field="process_description")
    assert not has_grounded_visit_result([("visit_context", "概况", [proof]), ("next_step", "下一步", [proof])])
    assert has_grounded_visit_result([("customer_fact", "客户下单", [proof])])
    assert not has_grounded_visit_result([("customer_fact", "缺少事实", [SimpleNamespace(field="confirmed_findings")])])


def test_explicit_target_process_conflation_rejected():
    assert has_field_role_conflict("本次想取得的关键结果和过程均为核对发票。", {"expected_key_result": "收集信息", "process_description": "核对发票"})
    assert has_field_role_conflict("本次目标是催款，但过程缺少反馈。", {"expected_key_result": "1", "process_description": "催款"})
    assert not has_field_role_conflict("本次目标是催款，但过程缺少反馈。", {"expected_key_result": "催款", "process_description": "催款"})


def test_source_action_cannot_be_denied_even_if_model_items_are_also_wrong():
    context = {"process_description": "客户下单500g M13和DDQ，正在走合同流程。"}
    assert denies_recorded_customer_action("过程缺少客户表达或动作的客观描述。", context)
    assert denies_recorded_customer_action("缺少可核验的客户事实。", context)
    assert not denies_recorded_customer_action("尚未记录客户付款确认。", context)
    assert not denies_recorded_customer_action("该动作不足以支撑项目可按计划推进的判断。", context)
    assert not denies_recorded_customer_action("缺少客户表达或动作的客观描述。", {"process_description": "争取客户下单"})
    assert not denies_recorded_customer_action("缺少客户表达或动作的客观描述。", {"process_description": "客户未下单"})


def test_sales_delivery_does_not_prove_customer_receipt():
    assert asserts_unrecorded_receipt("客户已接收货物与卡片。", {"process_description": "现场给客户收货，送卡"})
    assert not asserts_unrecorded_receipt("客户已接收货物。", {"process_description": "客户已接收货物。"})
    assert not asserts_unrecorded_receipt("尚未明确客户是否接收。", {"process_description": "现场给客户收货，送卡"})


def test_recorded_arrangement_is_not_blanket_absence_of_actions():
    text = "没有客户表达或动作的客观描述。"
    assert denies_recorded_customer_action(text, {"process_description": "客户因开会未深入交流，简单与客户沟通后约定下次拜访"})
    for source in ("计划与客户沟通并约定下次拜访", "计划与客户沟通后约定下次拜访", "与客户沟通后未约定下次拜访", "争取约定下次拜访"):
        assert not denies_recorded_customer_action(text, {"process_description": source})
    assert not denies_recorded_customer_action("约定再访不足以证明关系拉近。", {"process_description": "与客户沟通后约定下次拜访"})


def test_do_not_invent_forecast_to_criticize():
    text = "无法支撑项目可按计划推进。"
    assert invents_sales_forecast(text, {"process_description": "还差蒋总签字，领导外出未归，明早再下单"})
    assert not invents_sales_forecast(text, {"process_description": "我判断项目可按计划推进"})
    assert not invents_sales_forecast("签字尚未完成，不能认定已下单。", {"process_description": "待签字"})


def test_persistent_conflict_is_traceable_failure_and_cleans_draft(monkeypatch):
    result = SimpleNamespace(
        semantic_review=SimpleNamespace(status="unavailable", failure_reason="wording_analysis_consistency_conflict"),
        feedback_text="不能返回这段矛盾反馈",
    )
    deleted = []
    monkeypatch.setattr(api, "_execute_unified_button_feedback", lambda *args, **kwargs: result)
    monkeypatch.setattr(api, "UnifiedButtonPrecheckResponse", SimpleNamespace(from_precheck=lambda *args, **kwargs: result))
    monkeypatch.setattr(api, "get_store", lambda settings: SimpleNamespace(delete_prechecks_by_request_ids=lambda tenant, ids: deleted.extend(ids)))
    request = SimpleNamespace(context=SimpleNamespace(request_id="experimental-test", tenant_id="test"))
    final = api._quick_check_run_final(request, SimpleNamespace())
    assert final["status"] == "failed"
    assert final["failure_category"] == "final_consistency_conflict"
    assert final["diagnostics"]["failure_reason"] == "wording_analysis_consistency_conflict"
    assert "feedback_text" not in final
    assert deleted == ["experimental-test", "experimental-test__enrichment"]
