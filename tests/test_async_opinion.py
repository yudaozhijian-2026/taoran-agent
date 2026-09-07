import json

import httpx
import pytest
from pydantic import SecretStr
from test_post_policy import visit
from test_post_repair import reviewer, valid_payload

from taoran_agent.async_opinion import (
    Opinion,
    basic_feedback,
    current_data,
    generate,
    render_opinion,
    sources,
)


def response(payload):
    content = json.dumps(payload, ensure_ascii=False)
    data = {"choices": [{"delta": {"content": content}, "finish_reason": None}]}
    end = {"choices": [{"delta": {}, "finish_reason": "stop"}]}
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text="data: "
        + json.dumps(data, ensure_ascii=False)
        + "\n\ndata: "
        + json.dumps(end)
        + "\n\ndata: [DONE]\n\n",
    )


def opinion():
    return {
        "goals": [],
        "recorded": [{"text": "已记录现场给客户收货。", "source_ids": ["S2"]}],
        "uncertain": [{"text": "确认方不足以判断。", "source_ids": []}],
        "suggestions": [],
    }


def test_basic_feedback_is_truthful_and_never_a_score():
    text = basic_feedback(visit(process_description="客户表示预算尚未批准。"))
    assert "基础检查" in text and "非 AI 分析" in text and "不代表正式评分" in text
    assert "客户表示预算尚未批准。" in text
    assert "已批准" not in text


def test_quotes_are_extracted_not_written_by_model_and_observations_do_not_block():
    v = visit(process_description="现场给客户收货。")
    data = current_data(v)
    catalog = sources(data)
    source = next(s for s in catalog if s["field"] == "process_description")
    result = Opinion.model_validate(
        {"recorded": [{"text": "客户已经签收。", "source_ids": [source["id"], "invented"]}]}
    )
    text, observations = render_opinion(result, data, catalog)
    assert "客户已经签收。" in text and "「现场给客户收货。」" in text
    assert any(x["rule"] == "unknown_source_id" for x in observations)
    assert len(observations) > 1


@pytest.mark.parametrize(
    "failure", [httpx.ReadTimeout("simulated timeout"), httpx.ConnectError("offline"), 429, 503]
)
def test_transient_failure_retries_then_completes_same_input(tmp_path, monkeypatch, failure):
    r = reviewer(tmp_path)
    calls = []
    payloads = []
    r.settings = r.settings.model_copy(
        update={
            "llm_api_url": "https://model.invalid",
            "llm_api_key": SecretStr("test-only"),
            "llm_model": "glm-test",
        }
    )

    def handler(request):
        calls.append(1)
        payloads.append(json.loads(request.content))
        if len(calls) == 1:
            if isinstance(failure, Exception):
                raise failure
            return httpx.Response(failure)
        return response(opinion())

    r._client.close()
    r._client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("taoran_agent.async_opinion.sleep", lambda _: None)
    try:
        result = generate(r, visit(), r.settings)
        assert result["status"] == "completed" and len(calls) == 2
        assert payloads[0]["messages"] == payloads[1]["messages"]
        assert result["phase_timings"]["attempts"][0]["status"] != "completed"
        assert result["diagnostics"]["semantic_policy"] == "observe_only"
    finally:
        r.close()


def test_persistent_failure_is_bounded_recoverable_and_not_success(tmp_path, monkeypatch):
    r = reviewer(tmp_path)
    calls = []
    r.settings = r.settings.model_copy(
        update={
            "llm_api_url": "https://model.invalid",
            "llm_api_key": SecretStr("test-only"),
            "llm_model": "glm-test",
        }
    )

    def handler(request):
        calls.append(1)
        return httpx.Response(429)

    r._client.close()
    r._client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("taoran_agent.async_opinion.sleep", lambda _: None)
    try:
        result = generate(r, visit(), r.settings)
        assert result["status"] == "failed" and result["recoverable"] and len(calls) == 3
        assert "feedback_text" not in result
    finally:
        r.close()


def test_backend_program_restores_quotes_without_changing_model_scoring_facts(tmp_path):
    r = reviewer(tmp_path)
    v = visit()
    payload = valid_payload(r, v)
    data = r._input(v, precheck=False)
    from taoran_agent.llm import _evidence_catalog

    ev = next(e for e in _evidence_catalog(data) if e["field"] == "process_description")
    payload["sections"][3]["evidence"] = [
        {
            "evidence_id": ev["evidence_id"],
            "category": "customer_fact",
            "quote": "fabricated",
            "field": "customer_feedback",
        }
    ]
    try:
        parsed, _ = r._validate_observed(payload, data)
        assert parsed.sections[3].evidence[0].quote == ev["quote"]
        assert parsed.facts.model_dump() == payload["facts"]
        assert parsed._semantic_gate["observation_count"] > 0
    finally:
        r.close()
