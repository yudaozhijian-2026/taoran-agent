import json
from types import SimpleNamespace

import httpx
import pytest

from taoran_agent import api
from taoran_agent.config import Settings
from taoran_agent.experimental_final_diagnostics import audit as safe_audit
from taoran_agent.experimental_final_diagnostics import category
from taoran_agent.experimental_record_state import build
from taoran_agent.experimental_semantic_audit import CHECKS, messages, validate
from taoran_agent.llm import ChatModelReviewer, ModelCallError
from taoran_agent.models import KnowledgeWordingResult

CONTEXT = {"expected_key_result": "现场收货", "process_description": "现场给客户收货，送卡"}
BAD = "虽然自评为达到目的，但过程仅记录销售代收与送卡，尚无客户接收或确认的事实。"


def wire_verdict(payload, context):
    """Mock-provider fixture for the new comparison protocol."""
    sources = build(context)["sources"]
    issues = []
    for item in payload["issues"]:
        source = next(s for s in sources if s["field"] == item["field"] and item["quote"] in s["quote"])
        relation = "denial" if item["check"] == "consistency" else "unsupported"
        actor = source["actor_hint"]
        issues.append({k: item[k] for k in ("check", "error_type", "target", "output_quote", "reason")} | {
            "source_ids": [source["id"]], "comparison": {
                "source_actor": actor, "candidate_actor": actor,
                "source_state": "reported", "candidate_state": "not_attained",
                "relation": relation, "examined_targets": [item["target"]]}})
    return {"checks": payload["checks"], "issues": issues}


def verdict(passed=True):
    checks = {key: True for key in CHECKS}
    issues = []
    if not passed:
        checks["assessment"] = False
        issues = [{"check": "assessment", "field": "expected_key_result", "quote": "现场收货", "output_quote": BAD,
                   "target": "analysis:0", "error_type": "unsupported_attainment", "reason": "客户接收不能作为销售代收完成的前提。"}]
    return {"checks": checks, "issues": issues}


def test_complete_verdict_and_exact_source_required():
    assert validate(verdict(), CONTEXT, BAD, []) == []  # Parser doesn't pretend to be a semantic model.
    assert validate(verdict(False), CONTEXT, BAD, []) == ["assessment"]
    for change in ("missing", "bool_string", "unanchored", "missing_issue", "extra", "output"):
        value = verdict(False)
        if change == "missing": del value["checks"]["actor"]
        if change == "bool_string": value["checks"]["actor"] = "true"
        if change == "unanchored": value["issues"][0]["quote"] = "客户已签收"
        if change == "missing_issue": value["issues"] = []
        if change == "extra": value["approved"] = True
        if change == "output": value["issues"][0]["output_quote"] = "模型捏造的输出"
        with pytest.raises(ValueError): validate(value, CONTEXT, BAD, [])


def test_prompt_uses_raw_record_and_clarification_not_derived_findings():
    value = messages({**CONTEXT, "opportunity_stages": ["P1"], "confirmed_findings": "不要相信待复核数据", "experimental_speaker_hints": "derived"}, BAD, ["建议"])
    assert "销售替客户收货" in value[0]["content"]
    assert "confirmed_findings" not in value[1]["content"]
    assert json.loads(value[1]["content"])["candidate"]["units"][-1]["text"] == "建议"
    assert json.loads(value[1]["content"])["record"]["opportunity_stages"] == ["P1"]


def test_typed_units_and_error_anchors_cannot_move_between_fields():
    source = {"expected_key_result": "拉近客户关系", "process_description": "已约定再访。"}
    analysis, advice = "已约定再访。", "目标可以明确期望的关系变化。"
    data = json.loads(messages(source, analysis, [advice], suggestion_codes=["O_KR"])[1]["content"])
    assert data["candidate"]["units"][1]["code"] == "O_KR"
    payload = {"checks": {key: key != "goal" for key in CHECKS}, "issues": [{
        "check": "goal", "error_type": "goal_inflation", "target": "suggestion:0",
        "field": "expected_key_result", "quote": "拉近客户关系",
        "output_quote": advice, "reason": "仅验证结构，不代表这条建议真的扩大目标。"}]}
    assert validate(payload, source, analysis, [advice], suggestion_codes=["O_KR"]) == ["goal"]
    for key, value in [("target", "analysis:0"), ("error_type", "fact_denial"),
                       ("reason", ""), ("quote", "不存在的目标")]:
        changed = {**payload, "issues": [{**payload["issues"][0], key: value}]}
        with pytest.raises(ValueError):
            validate(changed, source, analysis, [advice])


def test_multiple_anchored_issues_for_one_check_are_preserved():
    payload = verdict(False)
    payload["issues"].append({**payload["issues"][0], "target": "suggestion:0"})
    assert validate(payload, CONTEXT, BAD, [BAD]) == ["assessment"]


def test_repair_evidence_is_private_including_model_copy():
    result = KnowledgeWordingResult(status="unavailable")
    result._experimental_repair_context = {"private": "RETRY_ONLY_TEXT"}
    assert result.model_copy()._experimental_repair_context == result._experimental_repair_context
    assert "RETRY_ONLY_TEXT" not in result.model_dump_json()
    assert "RETRY_ONLY_TEXT" not in str(safe_audit(result))


@pytest.mark.parametrize("kind,reason", [("pass", None), ("reject", "wording_experimental_audit_assessment"),
    ("http", "wording_experimental_audit_upstream"), ("malformed", "wording_experimental_audit_contract"),
    ("truncated", "wording_experimental_audit_truncated")])
def test_transport_never_passes_errors_and_releases_capacity(kind, reason):
    def provider(request):
        assert json.loads(request.content)["stream"] is True
        if kind == "http": return httpx.Response(503, text="SECRET")
        payload = wire_verdict(verdict(kind != "reject"), CONTEXT) if kind != "malformed" else {"pass": True}
        return httpx.Response(200, json={"choices": [{"finish_reason": "length" if kind == "truncated" else "stop",
            "message": {"content": json.dumps(payload)}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
    reviewer = ChatModelReviewer(Settings(llm_model="glm-test", llm_api_url="https://example.test/chat", llm_api_key="test"), None,
                                 transport=httpx.MockTransport(provider))
    report = {}
    try:
        if reason:
            with pytest.raises(ModelCallError, match=reason): reviewer._experimental_audit_wording(CONTEXT, BAD, [], 1, report)
        else: reviewer._experimental_audit_wording(CONTEXT, BAD, [], 1, report)
        assert reviewer.model_capacity.snapshot()["active_frontend"] == 0
        assert "SECRET" not in str(report)
        assert report["status"] == ("passed" if kind == "pass" else "rejected" if kind == "reject" else "unavailable")
    finally:
        reviewer.close()


def test_safe_diagnostics_keep_review_cost_not_text():
    result = KnowledgeWordingResult(status="unavailable", model_attempts=[{"experimental_semantic_audit": {
        "status": "rejected", "failed_checks": ["assessment", "secret"], "latency_ms": 123,
        "prompt_tokens": 20, "quote": "secret", "provider_failure": "secret"}}])
    value = safe_audit(result)
    assert "secret" not in str(value)
    assert value["experimental_semantic_audits"][0]["latency_ms"] == 123
    assert category("wording_experimental_audit_assessment") == "final_semantic_review_rejected"
    assert category("wording_experimental_audit_contract") == "final_semantic_review_unavailable"


def test_current_result_cannot_use_future_evidence_and_retry_keeps_location():
    context = {"process_description": "催款", "expected_key_result": "1",
               "next_action_expected_result": "再次联系"}
    payload = {"analysis_points": [{"kind": "objective_result", "text": "本次催款，计划再次联系。",
        "proofs": [{"field": "process_description", "quote": "催款"},
                   {"field": "next_action_expected_result", "quote": "再次联系"}]}],
        "items": [{"code": "R", "suggestion": "缺少客户对催款的具体回应。", "present": [], "proofs": []}]}

    payload['analysis_points'][0].update(contract_id='C_PROCESS', goal_id='', claim_type='recorded_fact', fact_ids=[])
    def provider(request):
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps(payload)}}]})

    reviewer = ChatModelReviewer(Settings(_env_file=None, llm_model="glm-test",
        llm_api_url="https://example.test/chat", llm_api_key="test"), None,
        transport=httpx.MockTransport(provider))
    try:
        result = reviewer.verbalize_knowledge_issues([{"code": "R", "source_fields": context}], 2,
            taoran_snapshot={"visit_analysis_context": context}, experimental=True)
        assert result.status == "unavailable"
        assert not result.visit_analysis
        details = result._experimental_repair_context["rejection"]["binding_errors"]
        assert details[0]["contract_id"] == "C_PROCESS"
        assert details[0]["code"] == "BOUND_PROOF_MISMATCH"
        assert "binding_errors" not in result.model_dump_json()
    finally:
        reviewer.close()


PIPELINE_CONTEXT = {
    "expected_key_result": "沟通审批进度",
    "process_description": "客户表示审批暂缓。",
}
PIPELINE_ANALYSIS = "客户表示审批暂缓。"


def pipeline_provider(audit_results, calls):
    def respond(request):
        body = json.loads(request.content)
        incoming = json.loads(body["messages"][1]["content"])
        if "candidate" in incoming:
            calls.append("audit")
            outcome = audit_results.pop(0)
            if outcome == "upstream":
                return httpx.Response(503, text="PRIVATE_PROVIDER_ERROR")
            payload = {"checks": {key: True for key in CHECKS}, "issues": []}
            if outcome == "reject":
                payload["checks"]["consistency"] = False
                payload["issues"] = [{
                    "check": "consistency", "field": "process_description",
                    "quote": PIPELINE_CONTEXT["process_description"],
                    "output_quote": PIPELINE_ANALYSIS,
                    "target": "analysis:0", "error_type": "fact_denial", "reason": "模拟事实冲突",
                }]
            assert incoming["candidate"]["units"][0]["text"] == PIPELINE_ANALYSIS
            assert incoming["candidate"]["units"][0]["kind"] == "objective_result"
            payload = wire_verdict(payload, incoming["record"])
        else:
            if calls == ["generate", "audit"]:
                evidence = json.loads(body["messages"][-1]["content"])["retry_evidence_data"]
                assert evidence["rejection"]["semantic_issues"][0]["target"] == "analysis:0"
                assert evidence["previous_candidate"]["analysis_points"][0]["text"] == PIPELINE_ANALYSIS
            calls.append("generate")
            payload = {
                "analysis_points": [{"kind": "customer_fact", "text": PIPELINE_ANALYSIS,
                    "proofs": [{"field": "process_description",
                                "quote": PIPELINE_CONTEXT["process_description"]}]}],
                "items": [{"code": "R", "suggestion": "",
                    "present": ["customer_expression_action"],
                    "proofs": [{"features": ["customer_expression_action"],
                                "field": "process", "quote": PIPELINE_ANALYSIS}]}],
            }
        if "RENDERING_CONTRACTS" in incoming:
            contract = incoming['RENDERING_CONTRACTS'][0]
            payload['analysis_points'][0].update(kind='objective_result', contract_id=contract['contract_id'], goal_id=contract['goal_id'],
                claim_type=contract['allowed_claim_types'][0], fact_ids=contract['supporting_fact_ids'])
            payload['analysis_points'][0]['proofs'].append({'field': 'expected_key_result', 'quote': PIPELINE_CONTEXT['expected_key_result']})
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(payload)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })
    return respond


@pytest.mark.parametrize("experimental,outcome,expected", [
    (False, "pass", "completed"),
    (True, "pass", "completed"),
    (True, "reject", "unavailable"),
    (True, "upstream", "unavailable"),
])
def test_complete_wording_path_gates_only_candidate(experimental, outcome, expected):
    calls = []
    respond = pipeline_provider([outcome], calls)

    def inspect_prompt(request):
        body = json.loads(request.content)
        if "candidate" not in json.loads(body["messages"][1]["content"]):
            instruction = body["messages"][0]["content"]
            data = json.loads(body["messages"][1]["content"])
            assert ("BUSINESS_SEMANTIC_STATE" in data) is experimental
            if experimental:
                assert data["BUSINESS_SEMANTIC_STATE"]["field_values"]["process_description"] == PIPELINE_CONTEXT["process_description"]
            assert ("confirmed_findings是规则引擎已确认" in instruction) is (not experimental)
            assert ("confirmed_findings是规则检查提示，不是客户事实" in instruction) is experimental
        return respond(request)

    reviewer = ChatModelReviewer(Settings(
        _env_file=None, llm_model="glm-test", llm_api_url="https://example.test/chat",
        llm_api_key="test",
    ), None, transport=httpx.MockTransport(inspect_prompt))
    try:
        result = reviewer.verbalize_knowledge_issues(
            [{"code": "R", "source_fields": PIPELINE_CONTEXT}], 2,
            taoran_snapshot={"visit_analysis_context": PIPELINE_CONTEXT},
            experimental=experimental,
        )
        assert result.status == expected, result.model_dump()
        assert calls == (["generate", "audit"] if experimental else ["generate"])
        assert reviewer.model_capacity.snapshot()["active_frontend"] == 0
        if expected == "unavailable":
            assert not result.visit_analysis
            assert not result.items
            assert "PRIVATE_PROVIDER_ERROR" not in str(result.model_dump())
            assert "模拟事实冲突" not in result.model_dump_json()
            assert "_experimental_repair_context" not in KnowledgeWordingResult.model_json_schema()["properties"]
        if experimental and outcome in {"pass", "reject"}:
            assert result.total_tokens == 30
    finally:
        reviewer.close()


@pytest.mark.parametrize("outcomes,expected,saved_count", [
    (["reject", "pass"], "completed", 1),
    (["reject", "reject"], "unavailable", 0),
])
def test_real_wording_gate_retry_and_success_only_persistence(monkeypatch, outcomes, expected, saved_count):
    calls, saved = [], []
    settings = Settings(_env_file=None, llm_model="glm-test",
        llm_api_url="https://example.test/chat", llm_api_key="test",
        frontend_model_format_retries=1, knowledge_semantic_cache_seconds=0)
    reviewer = ChatModelReviewer(settings, None,
        transport=httpx.MockTransport(pipeline_provider(list(outcomes), calls)))
    store = SimpleNamespace(get_feedback_artifact=lambda *args: None,
        save_feedback_artifact=lambda *args: saved.append(args[-1]) or args[-1])
    monkeypatch.setattr(api, "get_store", lambda configured: store)
    monkeypatch.setattr(api, "_front_specificity_items", lambda *args: [
        {"code": "R", "source_fields": PIPELINE_CONTEXT},
    ])
    monkeypatch.setattr(api, "_cached_knowledge_wording", lambda key: None)
    monkeypatch.setattr(api, "_apply_knowledge_wording", lambda response, wording, configured, **kwargs: wording)
    response = SimpleNamespace(knowledge_snapshot_hash="knowledge", knowledge_references=["ref"],
        issues=[], tenant_id="test-tenant", input_snapshot_hash="input")
    try:
        result = api._enhance_front_suggestions(response, reviewer, settings, 2,
            {"visit_snapshot": PIPELINE_CONTEXT}, experimental=True)
        assert result.status == expected, result.model_dump()
        assert calls == ["generate", "audit", "generate", "audit"]
        assert result.attempt_count == 2
        assert result.recovered_after_retry is (expected == "completed")
        assert len(saved) == saved_count
        assert "模拟事实冲突" not in str(saved)
        assert "模拟事实冲突" not in result.model_dump_json()
        assert result.total_tokens == 60
        assert len(safe_audit(result)["experimental_semantic_audits"]) == 2
    finally:
        reviewer.close()


@pytest.mark.parametrize("experimental", [False, True])
def test_provisional_assessment_is_withheld_only_from_candidate_prompt(monkeypatch, experimental):
    captured = []
    issues = [SimpleNamespace(source="rule", severity="warning", code=code, message=message)
        for code, message in [("TAORAN_ASSESSMENT_NOT_EVIDENCED", "达成评价缺少关键结果和过程事实支持。"),
                              ("TAORAN_ASSESSMENT_MISSING", "缺少达成评价。")]]
    response = SimpleNamespace(knowledge_snapshot_hash="k", knowledge_references=["r"],
        issues=issues, tenant_id="t", input_snapshot_hash="i")
    store = SimpleNamespace(get_feedback_artifact=lambda *args: None,
        save_feedback_artifact=lambda *args: args[-1])
    monkeypatch.setattr(api, "get_store", lambda settings: store)
    monkeypatch.setattr(api, "_front_specificity_items", lambda *args: [])
    monkeypatch.setattr(api, "_cached_knowledge_wording", lambda key: None)
    monkeypatch.setattr(api, "_apply_knowledge_wording", lambda *args, **kwargs: response)

    def generate(*args, **kwargs):
        captured.append(kwargs["taoran_snapshot"]["visit_analysis_context"])
        return KnowledgeWordingResult(status="unavailable", failure_reason="queue_timeout")

    reviewer = SimpleNamespace(verbalize_knowledge_issues=generate)
    settings = Settings(_env_file=None, frontend_model_format_retries=0)
    api._enhance_front_suggestions(response, reviewer, settings, 2, {}, experimental=experimental)
    findings = captured[0]["confirmed_findings"]
    assert ("缺少关键结果和过程事实支持" in findings) is (not experimental)
    assert "缺少达成评价。" in findings
    assert response.issues == issues and len(response.issues) == 2


@pytest.mark.parametrize("mode", ["review_contract", "record_state"])
def test_retry_routes_do_not_rewrite_content_for_invalid_review(monkeypatch, mode):
    calls, saved = [], []
    ordinary = pipeline_provider(["pass"], calls)

    def provider(request):
        body = json.loads(request.content)
        data = json.loads(body["messages"][1]["content"])
        if mode == "review_contract" and "candidate" in data:
            calls.append("audit")
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                "message": {"content": '{"wrong":true}'}}]})
        if mode == "record_state" and calls == ["generate"]:
            repair = json.loads(body["messages"][-1]["content"])["retry_evidence_data"]
            assert repair["rejection"]["invariant_errors"][0]["code"] == "NOT_RECORDED_AS_NEGATIVE_FACT"
            assert "BUSINESS_SEMANTIC_STATE" in repair["rejection"]
        response = ordinary(request)
        if mode == "record_state" and calls == ["generate"]:
            value = response.json()
            payload = json.loads(value["choices"][0]["message"]["content"])
            payload["analysis_points"][0]["text"] += "尚未约定具体联系时间。"
            value["choices"][0]["message"]["content"] = json.dumps(payload)
            return httpx.Response(200, json=value)
        return response

    settings = Settings(_env_file=None, llm_model="glm-test", llm_api_url="https://example.test/chat",
        llm_api_key="test", frontend_model_format_retries=1, knowledge_semantic_cache_seconds=0)
    reviewer = ChatModelReviewer(settings, None, transport=httpx.MockTransport(provider))
    monkeypatch.setattr(api, "get_store", lambda _: SimpleNamespace(get_feedback_artifact=lambda *a: None,
        save_feedback_artifact=lambda *a: saved.append(a[-1]) or a[-1]))
    monkeypatch.setattr(api, "_front_specificity_items", lambda *a: [{"code": "R", "source_fields": PIPELINE_CONTEXT}])
    monkeypatch.setattr(api, "_cached_knowledge_wording", lambda _: None)
    monkeypatch.setattr(api, "_apply_knowledge_wording", lambda _, wording, *a, **k: wording)
    response = SimpleNamespace(knowledge_snapshot_hash="k", knowledge_references=["r"],
        issues=[], tenant_id="t", input_snapshot_hash="i")
    try:
        result = api._enhance_front_suggestions(response, reviewer, settings, 2,
            {"visit_snapshot": PIPELINE_CONTEXT}, experimental=True)
        if mode == "review_contract":
            assert calls == ["generate", "audit", "audit"]
            assert result.status == "unavailable" and not saved
        else:
            assert calls == ["generate", "generate", "audit"]
            assert result.status == "completed" and len(saved) == 1
            assert "state_errors" not in str(saved)
    finally:
        reviewer.close()
