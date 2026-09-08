import json
from copy import deepcopy

import httpx
import pytest
from test_launch_readiness import env  # noqa: F401 - shared isolated database fixture
from test_post_policy import visit

from taoran_agent import api
from taoran_agent.agent import TaoranAgent
from taoran_agent.config import Settings
from taoran_agent.feedback import build_front_ai_suggestions_with_model
from taoran_agent.front_v46.reviewer import FrontReviewer
from taoran_agent.knowledge import load_taoran_knowledge_snapshot
from taoran_agent.models import (
    ModelSectionAnalysis,
    PrecheckRequest,
    Q34SemanticFacts,
    RequestContext,
)
from taoran_agent.post_quality import quality_context
from taoran_agent.semantic import HeuristicSemanticReviewer


def candidate(ambiguous=True):
    text = "已确认采购方案" if ambiguous else "客户表示预算尚未批准"
    return {
        "analysis_points": [{"kind": "objective_result", "text": "方案确认方不明确，不足以判断客户认可" if ambiguous else "客户已明确说明预算尚未批准",
                             "proofs": [{"field": "process_description", "quote": text}]}],
        "items": [{"code": "R", "suggestion": "", "present": [], "proofs": []}],
        "confirmations": [{"field": "process_description", "quote": text,
                           "question": "请核对实际确认方和确认内容。", "impact": "是否取得客户方案认可"}] if ambiguous else [],
    }


@pytest.mark.parametrize("audit", ["disagreement", "timeout", "outage"])
def test_ambiguous_source_completes_with_scoped_confirmation_without_regeneration(tmp_path, monkeypatch, audit):
    calls = []
    raw = candidate()
    def provider(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(raw)}, "finish_reason": "stop"}]})
    settings = Settings(_env_file=None, database_path=str(tmp_path / "db"), llm_model="glm-test",
                        llm_api_url="https://example.test/chat", llm_api_key="test")
    reviewer = FrontReviewer(settings, load_taoran_knowledge_snapshot(), transport=httpx.MockTransport(provider))
    def check(*args, **kwargs):
        kwargs["repair_details"]["semantic_issues"] = [{"target": "analysis:0", "error_type": "actor_unclear"}]
        raise {"timeout": httpx.ReadTimeout("wait"), "outage": RuntimeError("observer down"),
               "disagreement": ValueError("wording_experimental_audit_consistency")}[audit]
    monkeypatch.setattr(reviewer, "_experimental_audit_wording", check)
    snapshot = {"visit_analysis_context": {"expected_key_result": "取得客户方案认可",
                                            "process_description": "已确认采购方案",
                                            "confirmed_findings": "历史AI说已经完成"}}
    before = deepcopy(snapshot)
    try:
        result = reviewer.verbalize_knowledge_issues([{"code": "R"}], taoran_snapshot=snapshot, experimental=True)
        assert result.status == "completed" and result.semantic_observations
        assert len(calls) == 1 and snapshot == before
        assert "历史AI说已经完成" not in json.dumps(calls, ensure_ascii=False)
        assert "影响：是否取得客户方案认可" in result.confirmation_items[0]
        request = PrecheckRequest(context=RequestContext(tenant_id="test", request_id="front", user_id="test"), visit=visit())
        feedback = build_front_ai_suggestions_with_model(TaoranAgent().precheck(request), result, experimental=True)
        assert "本次拜访分析：" in feedback and "需确认事项：" in feedback
        assert "已确认采购方案" in feedback and "AI调用异常" not in feedback
    finally:
        reviewer.close()


@pytest.mark.parametrize("mutation", ["invalid_shape", "invented_quote", "clear_actor", "checker_crash"])
def test_shape_errors_remain_failures_but_semantic_problems_do_not(tmp_path, monkeypatch, mutation):
    raw = candidate(mutation != "clear_actor")
    if mutation == "invalid_shape":
        raw["analysis_points"] = "not an array"
    if mutation == "invented_quote":
        raw["analysis_points"][0]["proofs"][0]["quote"] = "客户已签字验收"
        raw["confirmations"][0]["quote"] = "客户已签字验收"
    if mutation == "checker_crash":
        monkeypatch.setattr("taoran_agent.post_quality.quality_hits", lambda *a: (_ for _ in ()).throw(RuntimeError("checker unavailable")))
    settings = Settings(_env_file=None, database_path=str(tmp_path / "db"), llm_model="glm-test",
                        llm_api_url="https://example.test/chat", llm_api_key="test")
    reviewer = FrontReviewer(settings, None, transport=httpx.MockTransport(lambda _: httpx.Response(200,
        json={"choices": [{"message": {"content": json.dumps(raw)}, "finish_reason": "stop"}]})))
    monkeypatch.setattr(reviewer, "_experimental_audit_wording", lambda *a, **kw: None)
    try:
        text = "客户表示预算尚未批准" if mutation == "clear_actor" else "已确认采购方案"
        result = reviewer.verbalize_knowledge_issues([{"code": "R"}], taoran_snapshot={"visit_analysis_context": {"process_description": text}}, experimental=True)
        assert result.status == ("unavailable" if mutation == "invalid_shape" else "completed")
        if mutation == "invented_quote":
            assert not result.visit_analysis_evidence and not result.confirmation_items
        if mutation == "clear_actor":
            assert not result.confirmation_items
    finally:
        reviewer.close()


@pytest.mark.parametrize("advice", ["建议将关键结果改为销售方本次可执行并验证的具体事项。",
                                   "建议将关键结果调整为与P2阶段匹配的技术方案认可或方案确认类目标。"])
def test_final_conflict_does_not_change_score_or_block_real_writeback_pipeline(env, monkeypatch, advice):  # noqa: F811
    _settings, store, _raw, _mapping, create, calls = env
    request, _ = create()
    snapshot = load_taoran_knowledge_snapshot()
    class Reviewer(HeuristicSemanticReviewer):
        def review_q34(self, latest):
            assert latest.process_description == request.visit.process_description
            return Q34SemanticFacts(provider="llm-test", key_result_quality_ok=True,
                process_fact_based=True, purpose_achievement="achieved", next_action_logic_ok=True,
                customer_consensus_met=True, reason="原目标收到订单，过程已记录收到订单。",
                sections=[ModelSectionAnalysis(code=code, verdict="needs_revision" if code == "O_KR" else "met", reason="已记录依据。", field_paths=[], suggestion=advice if code == "O_KR" else "", evidence=[]) for code in ("T", "A1", "O_KR", "R", "A2", "N")],
                quality_audit={"authoritative_checks": quality_context(latest, snapshot), "advice_basis": {"O_KR": {}}})
    agent = TaoranAgent(Reviewer())
    expected = agent.evaluate(request, "job1").total_score
    monkeypatch.setattr(api, "get_agent", lambda *a: agent)
    def enrichment(req, _settings):
        result = TaoranAgent().precheck(req)
        return result.model_copy(update={"suggestions": [advice], "issues": []})
    monkeypatch.setattr(api, "_execute_post_submit_rule_enrichment", enrichment)
    api.execute_evaluation("job1", request)
    job = store.get_evaluation("a", "job1")
    assert job["status"] == "completed", job
    result = job["response"]
    assert result["writeback"]["status"] == "succeeded" and len(calls) == 1
    assert result["total_score"] == expected
    observations = result["semantic_facts"]["quality_audit"]["final_review"]
    assert observations["policy"] == "observe_only"
    assert any(h["rule"] == "retroactive_goal_replacement" and h["scope"].startswith("suggestions.") for h in observations["hits"])
