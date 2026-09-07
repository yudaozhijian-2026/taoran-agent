from copy import deepcopy

import pytest
from test_post_policy import visit
from test_post_repair import reviewer, valid_payload

from taoran_agent.agent import TaoranAgent
from taoran_agent.llm import ModelCallError, _format_retry_allowed
from taoran_agent.models import PostEvaluationRequest, RequestContext
from taoran_agent.post_claim_guards import advice_hits, claim_hits
from taoran_agent.post_review_policy import requirement_hits


@pytest.mark.parametrize("customer,text,blocked", [
    ("opportunity", "填写晚于拜访日期且跨自然月的联系时间", True),
    ("商机客户", "下次联系须跨季度安排", True),
    ("opportunity", "商机客户无需跨自然月", False),
    ("opportunity", "下次联系须晚于拜访日期", False),
    ("target", "下次联系须跨自然月", False),
])
def test_opportunity_cannot_acquire_an_extra_period_requirement(customer, text, blocked):
    assert bool(requirement_hits(text, customer)) == blocked


@pytest.mark.parametrize("source,output,blocked", [
    ("现场给客户收货，送卡", "客户现场收货动作已完成", True),
    ("现场给客户收货，送卡", "客户已实际接收货物", True),
    ("现场给客户收货，送卡", "证据表明客户本人已实际接收货物", True),
    ("客户本人已收货", "客户已实际接收货物", False),
    ("现场给客户收货，送卡", "客户已经收到商品", True),
    ("客户已收货", "客户已实际接收货物", False),
    ("客户已接收货物", "客户已经收货", False),
    ("替客户核对清单", "客户已核对清单", True),
    ("协助客户验收设备", "客户已经验收设备", True),
    ("现场给客户收货", "记录为现场给客户收货，主体需核实", False),
    ("现场给客户收货", "记录尚不能证明客户已经收货", False),
    ("现场给客户收货", "下一步请客户确认是否收货", False),
    ("现场给客户收货", "核实客户收货的实际主体", False),
    ("现场给客户收货", '原文写给客户收货而非客户已收货或客户已签收', False),
    ("现场给客户收货", "补充客户实际接收货物的事实依据后重新核对自评", False),
    ("现场给客户收货", "依据客户实际签收或确认接收的证据校准自评", False),
    ("正在走合同流程", "该表述缺少可验证标志——如客户签收等节点", False),
    ("正在走合同流程", "如跟进合同签署状态或确认客户收货安排", False),
    ("给客户收货，客户已收货", "客户已收货", False),
    ("客户现场收货已完成", "客户现场收货动作已完成", False),
    ("收集劳保订单，本周不会再点单", "客户收集劳保订单", True),
    ("收集劳保订单，本周不会再点单", "记录包含劳保订单收集与本周不再点单的信息", False),
    ("给客户收货，客户未收货", "客户已收货", True),
    ("客户下单，正在走合同流程", "当前仅完成下单和合同流程", True),
    ("客户下单，合同流程进行中", "合同流程已完成", True),
    ("客户下单，正在走合同流程", "客户已下单，合同流程仍在进行", False),
    ("客户下单，正在走合同流程", "下一步建议完成合同流程", False),
    ("客户下单，正在走合同流程", "尚不能证明合同流程已完成", False),
    ("此前正在走合同流程，现合同流程已完成", "合同流程已完成", False),
])
def test_customer_actor_and_process_state(source, output, blocked):
    hits = claim_hits(output, "facts.reason", {"source_text": source})
    assert bool(hits) == blocked
    for hit in hits:
        assert output[hit["start"]:hit["end"]] == hit["quote"]


@pytest.mark.parametrize("output,target,blocked", [
    ("应将关键结果写明为收集订单信息，使自评有依据", "A2", True),
    ("将本次关键结果校准为合同流程完成", "O_KR", True),
    ("建议把原定目标改为核对发票", "A2", True),
    ("核实本次原定具体目标，保留原目标及补充说明", "O_KR", False),
    ("在想取得的关键结果中补充本次具体收集事项", "A2", False),
    ("在关键结果中补充本次原定要了解的事项并保留原记录", "O_KR", False),
    ("请在想取得的关键结果中补充本次计划收集的具体信息项", "O_KR", False),
    ("下次拜访可将关键结果写明为核对发票", "O_KR", False),
    ("不得将本次关键结果改为已取得的结果", "A2", False),
    ("期望结果争取合作与本次过程无衔接", "N", True),
    ("下一步与本次结果不衔接", "facts.reason", True),
    ("下一步仍围绕合作，但未说明期望客户确认的具体事项", "N", False),
])
def test_goal_history_and_precise_next_step_advice(output, target, blocked):
    assert bool(advice_hits(output, target)) == blocked


def test_factual_observation_keeps_model_decision_and_preserves_audit(tmp_path, monkeypatch):
    r = reviewer(tmp_path)
    v = visit(process_description="现场给客户收货，送卡")
    original = valid_payload(r, v)
    original["facts"]["reason"] = "客户现场收货动作已完成。"
    # A separate repairable defect must not hide the factual error.
    original["sections"][0]["field_paths"].append("other_purpose")
    calls = []
    def model(messages, precheck, timeout, lease, **kwargs):
        lease.release()
        calls.append(kwargs)
        return deepcopy(original), {}
    monkeypatch.setattr(r, "_request", model)
    attempts = []
    try:
        parsed,_=r._analyze(v, False, attempts)
        assert len(calls) == 1
        assert parsed.facts.reason == original['facts']['reason']
        assert parsed._semantic_gate['observation_count'] > 0
        assert parsed._semantic_gate['diagnostic_evidence_id']
        assert _format_retry_allowed(ModelCallError("post_fact_grounding_conflict"))
    finally:
        r.close()


def test_goal_advice_repair_preserves_facts(tmp_path, monkeypatch):
    r = reviewer(tmp_path)
    v = visit()
    original = valid_payload(r, v)
    original["sections"][2]["suggestion"] = "将本次关键结果改为已取得的设备信息。"
    patched = deepcopy(original["sections"][2])
    patched["suggestion"] = "请核实本次原定要取得的具体设备信息，保留原目标和补充说明。"
    calls = []
    def model(messages, precheck, timeout, lease, repair=False, **kwargs):
        lease.release()
        calls.append(repair)
        return ({"sections": [patched], "facts_reason": ""} if repair else deepcopy(original)), {}
    monkeypatch.setattr(r, "_request", model)
    try:
        parsed, _ = r._analyze(v, False)
        assert calls == [False]
        assert parsed.facts.model_dump() == original["facts"]
    finally:
        r.close()


def test_observed_conflict_does_not_block_current_model_scoring(tmp_path, monkeypatch):
    r = reviewer(tmp_path)
    v = visit(process_description="现场给客户收货，送卡")
    original = valid_payload(r, v)
    original["facts"]["reason"] = "客户现场收货动作已完成。"
    def model(messages, precheck, timeout, lease, **kwargs):
        lease.release()
        return deepcopy(original), {}
    monkeypatch.setattr(r, "_request", model)
    try:
        request = PostEvaluationRequest(context=RequestContext(tenant_id="test", request_id="guard", user_id="test-user"), visit=v, visit_record_code="test-record")
        result = TaoranAgent(semantic_reviewer=r).evaluate(request, "guard-test")
        assert result.semantic_facts.status == "completed"
        assert result.semantic_facts.quality_audit['semantic_gate']['observation_count'] > 0
        assert "已暂停正式评分回写" not in result.ai_opinion
    finally:
        r.close()
