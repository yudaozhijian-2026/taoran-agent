import json
from time import monotonic

import httpx
import pytest

from taoran_agent.config import Settings
from taoran_agent.front_v46.observed_feedback import generate
from taoran_agent.front_v46.reviewer import FrontReviewer
from taoran_agent.llm import ModelCallError, _read_chat_response


def test_preview_has_no_local_token_or_byte_cap(tmp_path, monkeypatch):
    from test_post_policy import visit

    from taoran_agent.front_v46 import experimental_semantic_streaming_v22 as preview
    body = '客户已记录方案，后续安排仍需确认。' * 1500
    def provider(request):
        assert 'max_tokens' not in json.loads(request.content)
        events = [{'choices': [{'delta': {'content': body}}]},
                  {'choices': [{'delta': {}, 'finish_reason': 'stop'}]}]
        return httpx.Response(200, text=''.join('data: ' + json.dumps(e) + '\n\n' for e in events))
    real = httpx.Client
    monkeypatch.setattr(preview.httpx, 'Client', lambda **k: real(transport=httpx.MockTransport(provider)))
    settings = Settings(_env_file=None, database_path=str(tmp_path/'db'), llm_enabled=True,
                        llm_model='test', llm_api_url='https://example.test/chat', llm_api_key='test')
    emitted = []
    result = preview.stream_semantic_preview_v22(settings, visit(), emitted.append, interactive=True)
    assert result['status'] == 'completed' and ''.join(emitted) == body


@pytest.mark.parametrize('stream', [False, True])
def test_long_final_and_all_evidence_survive_generation_and_persistence(tmp_path, monkeypatch, stream):
    quote = '客户已确认方案。' * 30
    text = '本次目标已取得客户确认。' * 1500
    raw = {'analysis_points': [{'kind': 'objective_result', 'text': text,
            'proofs': [{'field': 'process_description', 'quote': quote}] * 25}],
           'items': [{'code': 'R', 'suggestion': '请核对下一步安排。' * 300,
                      'proofs': [{'field': 'process_description', 'quote': quote}] * 25}],
           'suggestion_status': 'has_suggestions', 'suggestion_reason': '需补充后续安排。' * 200}
    calls = []
    def provider(request):
        body = json.loads(request.content)
        calls.append(body)
        assert 'max_tokens' not in body and 'max_completion_tokens' not in body
        if stream:
            events = [{'choices': [{'delta': {'content': json.dumps(raw, ensure_ascii=False)}}]},
                      {'choices': [{'delta': {}, 'finish_reason': 'stop'}]}]
            return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                  text=''.join('data: ' + json.dumps(e) + '\n\n' for e in events))
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(raw)},
                                                     'finish_reason': 'stop'}]})
    settings = Settings(_env_file=None, database_path=str(tmp_path/'db'), llm_model='test',
                        llm_api_url='https://example.test/chat', llm_api_key='test',
                        knowledge_semantic_max_output_tokens=1200)
    reviewer = FrontReviewer(settings, None, transport=httpx.MockTransport(provider))
    monkeypatch.setattr(reviewer, '_experimental_audit_wording', lambda *a, **k: None)
    try:
        result = generate(reviewer, [{'code': 'R'}],
                          {'visit_analysis_context': {'process_description': quote}}, 30)
        assert result.status == 'completed' and len(calls) == 1
        assert result.visit_analysis == text.rstrip('。')
        assert result.items[0].suggestion == raw['items'][0]['suggestion']
        assert len(result.visit_analysis_evidence) == 25
        assert all(e.quote == quote for e in result.visit_analysis_evidence)
        restored = type(result).model_validate_json(result.model_dump_json())
        assert restored == result
    finally:
        reviewer.close()


@pytest.mark.parametrize('stream', [False, True])
def test_other_callers_keep_explicit_transport_limits(stream):
    if stream:
        response = httpx.Response(200, headers={'content-type': 'text/event-stream'},
            text='data: ' + json.dumps({'choices': [{'delta': {'content': 'x'*100}}]}) + '\n\n')
    else:
        response = httpx.Response(200, json={'content': 'x'*100})
    with pytest.raises(ModelCallError, match='output_too_large'):
        _read_chat_response(response, started=monotonic(), timeout=30, max_bytes=10)


def test_provider_truncation_still_fails_after_one_retry(tmp_path, monkeypatch):
    def provider(request):
        return httpx.Response(200, json={'choices': [{'message': {'content': '{'},
                                                     'finish_reason': 'length'}]})
    settings = Settings(_env_file=None, database_path=str(tmp_path/'db'), llm_model='test',
                        llm_api_url='https://example.test/chat', llm_api_key='test')
    reviewer = FrontReviewer(settings, None, transport=httpx.MockTransport(provider))
    try:
        result = generate(reviewer, [{'code': 'R'}], {'visit_analysis_context': {}}, 30)
        assert result.status == 'unavailable'
        assert result.failure_reason == 'output_truncated' and result.attempt_count == 2
    finally:
        reviewer.close()
