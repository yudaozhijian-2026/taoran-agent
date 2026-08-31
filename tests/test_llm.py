import json
from copy import deepcopy
from threading import Event
from time import monotonic

import httpx
import pytest
from pydantic import ValidationError
from test_agent import complete_precheck_payload
from test_writeback import evaluation_request

from taoran_agent.agent import TaoranAgent
from taoran_agent.config import Settings
from taoran_agent.knowledge import load_taoran_knowledge_snapshot
from taoran_agent.llm import ChatModelReviewer
from taoran_agent.models import FeedbackMode, PrecheckRequest, VisitDraftInput
from taoran_agent.runtime import build_agent
from taoran_agent.semantic import HeuristicSemanticReviewer
from taoran_agent.writeback import writeback_evaluation


def model_settings(**overrides):
    return Settings(_env_file=None, **{
        "llm_enabled": True,
        "llm_api_url": "https://model.example.test/chat/completions",
        "llm_api_key": "model-test-secret",
        "llm_model": "glm-5.2",
        **overrides,
    })


def visit():
    return VisitDraftInput.model_validate(complete_precheck_payload()["visit"])


def section_payload(data):
    fields = {
        "T": ["customer_type_ii"], "A1": ["is_appointment"],
        "O_KR": ["expected_key_result"], "R": ["process_description"],
        "A2": ["self_assessment", "expected_key_result", "process_description"],
        "N": [
            "next_action_purpose",
            "next_action_expected_result",
            "process_description",
        ],
    }
    sections = []
    for code, paths in fields.items():
        available = [field for field in paths if field in data]
        evidence = [
            {"field": field, "quote": data[field] if isinstance(data[field], str)
             else json.dumps(data[field], ensure_ascii=False)}
            for field in available if data[field] is not None and data[field] != ""
        ]
        sections.append({
            "code": code, "verdict": "met" if evidence else "not_evaluated",
            "field_paths": available, "reason": "输入记录中已有对应事实依据。",
            "suggestion": "", "evidence": evidence,
        })
    return {"sections": sections}


def deep_payload(data):
    return {
        **section_payload(data),
        "facts": {
            "key_result_quality_ok": True, "process_fact_based": True,
            "purpose_achievement": "achieved", "next_action_logic_ok": True,
            "customer_consensus_met": True,
            "reason": "过程详细描述记录了客户确认事项，下一行动承接该结果。",
        },
    }


def envelope(payload, finish_reason="stop"):
    return {"choices": [{"finish_reason": finish_reason,
                          "message": {"content": json.dumps(payload, ensure_ascii=False)}}]}


def tool_envelope(payload):
    return {"choices": [{
        "finish_reason": "tool_calls",
        "message": {
            "content": None,
            "tool_calls": [{
                "type": "function",
                "function": {
                    "name": "submit_taoran_precheck",
                    "arguments": json.dumps(payload, ensure_ascii=False),
                },
            }],
        },
    }]}


def reviewer_for(payload_fn=section_payload, **overrides):
    captured = []

    def handler(request):
        body = json.loads(request.content)
        data = json.loads(body["messages"][1]["content"])["untrusted_visit_data"]
        captured.append((request, body, data))
        return httpx.Response(200, json=envelope(payload_fn(data)))

    reviewer = ChatModelReviewer(
        model_settings(**overrides), load_taoran_knowledge_snapshot(),
        httpx.MockTransport(handler),
    )
    return reviewer, captured


def test_disabled_model_is_independent_of_q40_environment(monkeypatch):
    monkeypatch.setenv("AI_SCORING_API_KEY", "q40-secret-not-for-taoran")
    monkeypatch.setenv("AI_SCORING_MODEL", "q40-model")
    settings = Settings(_env_file=None)
    assert settings.llm_api_key is None
    assert settings.llm_model is None
    assert isinstance(build_agent(settings).semantic_reviewer, HeuristicSemanticReviewer)


@pytest.mark.parametrize("overrides", [
    {"llm_api_key": ""}, {"llm_api_url": ""}, {"llm_model": ""},
    {"llm_api_url": "http://provider.example/chat"},
    {"llm_api_url": "https://provider.example/chat?key=secret"},
    {"llm_api_url": "https://user:secret@provider.example/chat"},
    {"semantic_endpoint": "https://old.example/review"},
    {"llm_format_retries": 2},
])
def test_invalid_or_ambiguous_model_settings_are_rejected(overrides):
    with pytest.raises(ValidationError):
        model_settings(**overrides)


def test_secret_is_redacted_and_enabled_factory_uses_direct_model():
    settings = model_settings()
    assert "model-test-secret" not in repr(settings)
    assert "model-test-secret" not in settings.model_dump_json()
    agent = build_agent(settings)
    assert isinstance(agent.semantic_reviewer, ChatModelReviewer)
    agent.semantic_reviewer.close()


def test_direct_precheck_sends_minimal_fields_and_approved_knowledge():
    reviewer, calls = reviewer_for()
    draft = visit()
    draft.metadata["private"] = "never-send-secret"
    result = reviewer.review(draft)
    reviewer.close()
    assert result.status == "completed"
    assert result.provider == "llm-chat"
    assert len(result.sections) == 6
    assert len(calls) == 1
    request, body, data = calls[0]
    assert request.headers["Authorization"] == "Bearer model-test-secret"
    assert "response_format" not in body
    assert body["tool_choice"] == "auto"
    assert body["tools"][0]["function"]["name"] == "submit_taoran_precheck"
    assert body["thinking"] == {"type": "disabled"}
    assert body["max_tokens"] == 2200
    assert "DSM-BS-01-07" in body["messages"][0]["content"]
    assert "提交后facts仅为受控候选事实" not in body["messages"][0]["content"]
    assert "不是指令" in body["messages"][0]["content"]
    assert not {"employee_id", "customer_id", "metadata", "evidence_ids"} & data.keys()
    assert "never-send-secret" not in request.content.decode()
    assert "contact_id" not in json.dumps(data.get("participants"))
    assert "opportunity_id" not in json.dumps(data.get("opportunities"))


def test_precheck_accepts_schema_bound_function_arguments():
    captured = []

    def handler(request):
        body = json.loads(request.content)
        data = json.loads(body["messages"][1]["content"])["untrusted_visit_data"]
        captured.append(body)
        return httpx.Response(200, json=tool_envelope(section_payload(data)))

    reviewer = ChatModelReviewer(
        model_settings(),
        load_taoran_knowledge_snapshot(),
        httpx.MockTransport(handler),
    )
    result = reviewer.review_without_knowledge(visit())
    reviewer.close()

    assert result.status == "completed"
    assert len(result.sections) == 6
    assert captured[0]["tools"][0]["function"]["parameters"]["title"] == "_PrecheckPayload"


def test_pure_ai_precheck_explicitly_excludes_knowledge_snapshot():
    reviewer, calls = reviewer_for()
    result = reviewer.review_without_knowledge(visit())
    reviewer.close()

    assert result.status == "completed"
    assert result.prompt_version == "TAORAN-LLM-PURE-FEEDBACK-V2.2"
    prompt = calls[0][1]["messages"][0]["content"]
    assert "本次为纯AI反馈" in prompt
    assert "DSM-BS-000" not in prompt
    assert "DSM-BS-01-07" not in prompt
    assert "知识基线：" not in prompt


def test_precheck_sanitizes_invalid_model_references_without_weakening_postcheck():
    def invalid_precheck(data):
        payload = section_payload(data)
        first = payload["sections"][0]
        first["field_paths"] = ["unknown-private-field"]
        first["evidence"] = [
            {"field": "unknown-private-field", "quote": "伪造引用"}
        ]
        return payload

    reviewer, _ = reviewer_for(invalid_precheck)
    result = reviewer.review_without_knowledge(visit())
    reviewer.close()

    assert result.status == "completed"
    section = next(item for item in result.sections if item.code == "T")
    assert "unknown-private-field" not in section.field_paths
    assert section.evidence == []


@pytest.mark.parametrize(
    ("mode", "title", "has_knowledge"),
    [
        (FeedbackMode.AI, "纯AI反馈", False),
        (FeedbackMode.KNOWLEDGE, "知识库反馈", True),
    ],
)
def test_precheck_supports_separate_ai_and_knowledge_feedback_modes(
    mode, title, has_knowledge,
):
    reviewer, calls = reviewer_for()
    payload = complete_precheck_payload(f"mode-{mode.value}")
    payload["feedback_mode"] = mode.value
    result = TaoranAgent(reviewer).precheck(PrecheckRequest.model_validate(payload))
    reviewer.close()

    assert result.feedback_mode == mode
    assert f"｜{title}】" in result.feedback_text
    assert result.status == "passed"
    assert len(calls) == 1
    assert bool(result.knowledge_references) is has_knowledge
    assert bool(result.knowledge_snapshot_hash) is has_knowledge
    prompt = calls[0][1]["messages"][0]["content"]
    assert ("DSM-BS-01-07" in prompt) is has_knowledge
    assert "TAORAN_KR_NOT_VERIFIABLE" not in {issue.code for issue in result.issues}


def test_ai_feedback_without_configured_model_does_not_masquerade_as_rule_feedback():
    payload = complete_precheck_payload("mode-ai-not-configured")
    payload["feedback_mode"] = "ai"
    result = TaoranAgent().precheck(PrecheckRequest.model_validate(payload))

    assert result.feedback_mode == FeedbackMode.AI
    assert result.status == "review"
    assert result.semantic_review.status == "not_configured"
    assert "纯AI反馈" in result.feedback_text
    assert "AI调用异常" in result.feedback_text
    assert "TAORAN专用大模型尚未配置" in result.feedback_text
    assert "检查标准：" not in result.feedback_text


def test_precheck_keeps_local_rules_and_never_calls_configured_model():
    payload = complete_precheck_payload()
    payload["visit"]["expected_key_result"] = "采购部周五签回七台设备的清单"
    reviewer, calls = reviewer_for()
    result = TaoranAgent(reviewer).precheck(PrecheckRequest.model_validate(payload))
    reviewer.close()
    assert "TAORAN_KR_NOT_VERIFIABLE" in {i.code for i in result.issues}
    assert "分析方式" not in result.feedback_text
    assert "知识依据" not in result.feedback_text
    assert "规则＋大模型" not in result.feedback_text
    assert "O/KR｜" in result.feedback_text
    assert "/100" not in result.feedback_text
    assert calls == []


def test_post_failed_sections_are_shown_with_chinese_labels_and_real_evidence():
    def failed(data):
        payload = deep_payload(data)
        item = payload["sections"][2]
        item.update({
            "verdict": "needs_revision",
            "reason": "expected_key_result未明确客户验证的交付边界。",
            "suggestion": "补充客户验证完成后将提供的具体交付物。",
        })
        return payload

    reviewer, _ = reviewer_for(failed)
    result = TaoranAgent(reviewer).evaluate(evaluation_request(), "model-advice")
    reviewer.close()
    assert result.semantic_facts.status == "completed"
    assert "O/KR｜" in result.ai_opinion
    assert "模型分析：" in result.ai_opinion
    assert "expected_key_result" not in result.ai_opinion
    assert "输入依据" in next(i.message for i in result.issues
                           if i.code == "LLM_O_KR_NEEDS_REVISION")


def test_unreceived_fields_never_leave_server_or_become_model_failures():
    reviewer, calls = reviewer_for()
    draft = visit()
    draft.metadata["source_supplied_fields"] = ["expected_key_result"]
    result = reviewer.review(draft)
    reviewer.close()
    assert result.status == "completed"
    assert set(calls[0][2]) == {"expected_key_result"}
    assert next(s for s in result.sections if s.code == "R").verdict == "not_evaluated"


@pytest.mark.parametrize("case", ["forged_quote", "unknown_field", "score", "duplicate"])
def test_precheck_sanitizes_references_but_rejects_invalid_contract(case):
    def invalid(data):
        payload = section_payload(data)
        if case == "forged_quote":
            payload["sections"][2]["evidence"][0]["quote"] = "客户承诺明天付款一亿元"
        elif case == "unknown_field":
            payload["sections"][2]["field_paths"] = ["API_KEY"]
        elif case == "score":
            payload["total_score"] = 200
        else:
            payload["sections"][-1]["code"] = "T"
        return payload

    reviewer, calls = reviewer_for(invalid)
    result = reviewer.review(visit())
    reviewer.close()
    expected = "completed" if case in {"forged_quote", "unknown_field"} else "unavailable"
    assert result.status == expected
    assert "客户承诺明天付款一亿元" not in result.model_dump_json()
    assert "API_KEY" not in result.model_dump_json()
    assert len(calls) == 1


@pytest.mark.parametrize("kind", ["429", "401", "invalid_json", "truncated", "refused"])
def test_provider_failure_does_not_retry_or_leak_response(kind):
    calls = []

    def handler(request):
        calls.append(request)
        if kind in {"429", "401"}:
            return httpx.Response(int(kind), text="provider-private-error-body")
        if kind == "invalid_json":
            return httpx.Response(200, text="not json provider-private-error-body")
        return httpx.Response(200, json=envelope({}, kind))

    reviewer = ChatModelReviewer(
        model_settings(), load_taoran_knowledge_snapshot(), httpx.MockTransport(handler)
    )
    result = reviewer.review(visit())
    reviewer.close()
    assert result.status == "unavailable"
    assert len(calls) == 1
    assert "provider-private-error-body" not in result.model_dump_json()


def test_frontend_has_wall_clock_limit_and_bounded_background_calls():
    release = Event()
    finished = Event()
    calls = []

    def slow_handler(request):
        calls.append(request)
        release.wait(1)
        finished.set()
        return httpx.Response(200, json=envelope({}))

    reviewer = ChatModelReviewer(
        model_settings(llm_precheck_timeout_seconds=0.03, llm_max_concurrency=1),
        load_taoran_knowledge_snapshot(), httpx.MockTransport(slow_handler),
    )
    try:
        started = monotonic()
        first = reviewer.review(visit())
        second = reviewer.review(visit())
        assert monotonic() - started < 0.3
        assert first.status == "timeout"
        assert second.status == "timeout"
        assert second.failure_reason == "queue_timeout"
        assert len(calls) == 1
    finally:
        release.set()
        finished.wait(1)
        reviewer.close()


def test_oversize_input_fails_without_truncating_or_sending():
    reviewer, calls = reviewer_for(llm_max_input_chars=1000)
    assert reviewer.review(visit()).status == "unavailable"
    assert not calls
    reviewer.close()


def test_post_model_facts_feed_fixed_q33_q34_and_feedback():
    reviewer, calls = reviewer_for(deep_payload)
    result = TaoranAgent(reviewer).evaluate(evaluation_request(), "job-model")
    reviewer.close()
    assert result.q33_score == 50
    assert result.q34_score == 50
    assert result.total_score == 100
    assert result.semantic_facts.provider == "llm-chat"
    assert "模型分析：" in result.ai_opinion
    assert "模型事实依据：" in result.ai_opinion
    assert "fields" not in result.semantic_facts.reason
    assert len(calls) == 1


def test_negative_model_facts_change_q34_but_leave_q33_formula_unchanged():
    def negative(data):
        payload = deep_payload(data)
        payload["facts"].update({
            "purpose_achievement": "partially_achieved", "next_action_logic_ok": False,
        })
        for item in payload["sections"]:
            if item["code"] in {"A2", "N"}:
                item.update({"verdict": "needs_revision", "suggestion": "补充实际达成依据。"})
        return payload

    reviewer, _ = reviewer_for(negative)
    result = TaoranAgent(reviewer).evaluate(evaluation_request(), "negative-model")
    reviewer.close()
    assert result.q33_score == 50
    assert result.q34_score == 0
    assert result.total_score == 50


@pytest.mark.parametrize("case", ["boolean_string", "no_evidence"])
def test_post_invalid_semantic_facts_cannot_be_treated_as_success(case):
    def invalid(data):
        payload = deep_payload(data)
        if case == "boolean_string":
            payload["facts"]["customer_consensus_met"] = "false"
        else:
            for section in payload["sections"]:
                section["verdict"] = "not_evaluated"
                section["evidence"] = []
        return payload

    reviewer, _ = reviewer_for(invalid)
    result = reviewer.review_q34(visit())
    reviewer.close()
    assert result.status == "fallback"
    assert result.provider == "llm-unavailable-local-fallback"


def test_model_failure_never_overwrites_formal_score(monkeypatch):
    reviewer, _ = reviewer_for(lambda data: {})
    request = evaluation_request()
    result = TaoranAgent(reviewer).evaluate(request, "job-model-fail")
    reviewer.close()

    def forbidden_call(*args, **kwargs):
        raise AssertionError("must not write to Jiandaoyun on model failure")

    monkeypatch.setattr("taoran_agent.writeback.httpx.post", forbidden_call)
    writeback = writeback_evaluation(model_settings(), request, result)
    assert writeback.status == "failed"
    assert writeback.written_fields == []
    assert "暂停正式评分回写" in result.ai_opinion


def test_missing_required_data_cannot_be_overridden_by_model():
    draft = deepcopy(complete_precheck_payload())
    draft["visit"]["next_contact_at"] = None
    reviewer, _ = reviewer_for()
    result = TaoranAgent(reviewer).precheck(PrecheckRequest.model_validate(draft))
    reviewer.close()
    assert "TAORAN_NSA_TIME_MISSING" in {i.code for i in result.issues}


def test_schema_prompt_uses_actual_types_and_constraints():
    reviewer, calls = reviewer_for(deep_payload)
    result = reviewer.review_q34(visit())
    reviewer.close()
    assert result.status == "completed"
    system = calls[0][1]["messages"][0]["content"]
    schema = json.loads(system.split("JSON输出Schema（字段类型、枚举、必填、长度限制均须满足）：")[1])
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["_FactsPayload"]["properties"]["customer_consensus_met"]["type"] == "boolean"
    assert schema["$defs"]["ModelSectionAnalysis"]["properties"]["reason"]["maxLength"] == 500
    assert "不能是字符串" in system
    assert calls[0][1]["max_tokens"] == 3000
    assert result.prompt_version == "TAORAN-LLM-FACTS-V2.4"
    assert "禁止为它们创建evidence元素" in system


def test_format_failure_is_regenerated_once_with_safe_diagnostics():
    count = 0

    def initially_invalid(data):
        nonlocal count
        count += 1
        payload = deep_payload(data)
        if count == 1:
            payload["sections"][0]["reason"] = None
        return payload

    reviewer, calls = reviewer_for(initially_invalid)
    result = reviewer.review_q34(visit())
    reviewer.close()
    assert result.status == "completed"
    assert len(calls) == 2
    assert len(result.model_attempts) == 2
    first = result.model_attempts[0]
    assert first.failure_reason == "invalid_contract"
    assert first.validation_errors[0].location == "sections.0.reason"
    assert first.validation_errors[0].code == "string_type"
    retry = calls[1][1]["messages"][-1]["content"]
    assert "sections.0.reason" in retry
    assert "provider-private-error-body" not in retry
    assert calls[0][2] == calls[1][2]


def test_repeated_invalid_contract_stops_after_two_attempts_without_coercion():
    def invalid(data):
        payload = deep_payload(data)
        payload["facts"]["customer_consensus_met"] = "false"
        return payload

    reviewer, calls = reviewer_for(invalid)
    result = reviewer.review_q34(visit())
    reviewer.close()
    assert len(calls) == len(result.model_attempts) == 2
    assert result.status == "fallback"
    assert result.failure_reason == "invalid_contract"
    assert result.model_attempts[-1].validation_errors[0].code == "bool_type"


@pytest.mark.parametrize("case", ["score", "forged_quote", "unknown_field", "extra_secret"])
def test_evidence_and_unexpected_fields_are_not_repaired_or_retried(case):
    def invalid(data):
        payload = deep_payload(data)
        if case == "score":
            payload["total_score"] = 200
        elif case == "extra_secret":
            payload["provider-private-error-body"] = "secret-value"
        elif case == "forged_quote":
            payload["sections"][0]["evidence"][0]["quote"] = "伪造客户承诺"
        else:
            payload["sections"][0]["field_paths"] = ["unknown-secret-field"]
        return payload

    reviewer, calls = reviewer_for(invalid)
    result = reviewer.review_q34(visit())
    reviewer.close()
    assert result.status == "fallback"
    assert len(calls) == 1
    assert "provider-private-error-body" not in result.model_dump_json()
    assert "secret-value" not in result.model_dump_json()
    assert "unknown-secret-field" not in result.model_dump_json()


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_post_http_failures_are_not_retried(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="private-provider-error")

    reviewer = ChatModelReviewer(
        model_settings(), load_taoran_knowledge_snapshot(), httpx.MockTransport(handler),
    )
    result = reviewer.review_q34(visit())
    reviewer.close()
    assert result.status == "fallback"
    assert len(calls) == 1
    assert "private-provider-error" not in result.model_dump_json()


def test_format_retry_shares_original_deadline(monkeypatch):
    clock = [100.0]
    budgets = []
    monkeypatch.setattr("taoran_agent.llm.monotonic", lambda: clock[0])

    def handler(request):
        budgets.append(request.extensions["timeout"]["read"])
        clock[0] += 30 if len(budgets) == 1 else 16
        body = json.loads(request.content)
        data = json.loads(body["messages"][1]["content"])["untrusted_visit_data"]
        payload = deep_payload(data)
        if len(budgets) == 1:
            payload["sections"][0]["reason"] = None
        return httpx.Response(200, json=envelope(payload))

    reviewer = ChatModelReviewer(
        model_settings(), load_taoran_knowledge_snapshot(), httpx.MockTransport(handler),
    )
    result = reviewer.review_q34(visit())
    reviewer.close()
    assert budgets == [45, 15]
    assert result.status == "fallback"
    assert result.failure_reason == "timeout"
    assert len(result.model_attempts) == 2


def test_format_retry_can_be_disabled():
    reviewer, calls = reviewer_for(lambda data: {}, llm_format_retries=0)
    result = reviewer.review_q34(visit())
    reviewer.close()
    assert result.status == "fallback"
    assert len(calls) == 1


def test_duplicate_json_keys_are_rejected():
    from taoran_agent.llm import ModelCallError, _load_json

    with pytest.raises(ModelCallError, match="invalid_json"):
        _load_json('{"facts":{},"facts":{"total_score":200}}')
    with pytest.raises(ModelCallError, match="invalid_json"):
        _load_json('{"value":NaN}')


def test_precheck_ignores_unreceived_fields_even_when_remote_model_is_unavailable():
    reviewer, calls = reviewer_for(lambda data: {})
    draft = visit()
    draft.process_description = "沟通了"
    draft.metadata["source_supplied_fields"] = ["expected_key_result"]
    payload = complete_precheck_payload()
    payload["visit"] = draft.model_dump(mode="json")
    result = TaoranAgent(reviewer).precheck(PrecheckRequest.model_validate(payload))
    reviewer.close()
    assert calls == []
    assert not any("process_description" in issue.field_paths for issue in result.issues)
    assert "大模型暂未完成" not in result.feedback_text


def test_model_probe_only_calls_post_model_and_never_writes(monkeypatch, capsys):
    from taoran_agent import cli

    reviewer, calls = reviewer_for(deep_payload)
    monkeypatch.setattr(cli, "get_settings", model_settings)
    monkeypatch.setattr(cli, "build_agent", lambda settings: TaoranAgent(reviewer))
    monkeypatch.setattr("sys.argv", ["taoran-agent", "test-model", "--samples", "1"])
    cli.main()
    output = capsys.readouterr().out
    assert len(calls) == 1
    assert '"precheck_model_calls": 0' in output
    assert '"jiandaoyun_writeback": false' in output
    assert '"passed": 1' in output
    assert "model-test-secret" not in output


def test_empty_fields_are_excluded_from_schema_evidence_and_empty_quotes_stay_invalid():
    reviewer, calls = reviewer_for(deep_payload)
    draft = visit()
    draft.process_description = ""
    data = reviewer._input(draft, precheck=False)
    prompt = reviewer._messages(data, False)[0]["content"]
    schema = json.loads(prompt.split("JSON输出Schema（字段类型、枚举、必填、长度限制均须满足）：")[1])
    allowed = schema["$defs"]["ModelEvidence"]["properties"]["field"]["enum"]
    assert "process_description" not in allowed
    assert "expected_key_result" in allowed
    payload = deep_payload(data)
    payload["sections"][3]["evidence"] = [{"field": "process_description", "quote": ""}]
    with pytest.raises(ValidationError):
        reviewer._validate(payload, data, False)
    assert calls == []
    reviewer.close()


def test_no_format_retry_when_total_budget_is_almost_used(monkeypatch):
    clock = [100.0]
    calls = []
    monkeypatch.setattr("taoran_agent.llm.monotonic", lambda: clock[0])

    def handler(request):
        calls.append(request)
        clock[0] += 44
        return httpx.Response(200, json=envelope({}))

    reviewer = ChatModelReviewer(
        model_settings(), load_taoran_knowledge_snapshot(), httpx.MockTransport(handler),
    )
    result = reviewer.review_q34(visit())
    reviewer.close()
    assert result.status == "fallback"
    assert len(calls) == 1


def test_precheck_is_independent_while_all_model_slots_are_busy():
    from concurrent.futures import ThreadPoolExecutor

    entered = Event()
    release = Event()
    calls = []

    def handler(request):
        calls.append(request)
        entered.set()
        release.wait(2)
        body = json.loads(request.content)
        data = json.loads(body["messages"][1]["content"])["untrusted_visit_data"]
        return httpx.Response(200, json=envelope(deep_payload(data)))

    reviewer = ChatModelReviewer(
        model_settings(llm_max_concurrency=1), load_taoran_knowledge_snapshot(),
        httpx.MockTransport(handler),
    )
    agent = TaoranAgent(reviewer)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(reviewer.review_q34, visit())
        try:
            assert entered.wait(1)
            started = monotonic()
            result = agent.precheck(PrecheckRequest.model_validate(complete_precheck_payload()))
            assert monotonic() - started < 0.3
            assert result.semantic_review.provider == "heuristic-v1"
            assert len(calls) == 1
            assert not future.done()
        finally:
            release.set()
            future.result(timeout=2)
            reviewer.close()
