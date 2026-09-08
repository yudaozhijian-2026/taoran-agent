import json
from copy import deepcopy

import httpx
import pytest
from test_launch_readiness import env  # noqa: F401
from test_post_policy import visit
from test_semantic_observation_flow import candidate

from taoran_agent import api, writeback
from taoran_agent.config import Settings
from taoran_agent.front_v46 import experimental_semantic_streaming_v22 as preview
from taoran_agent.front_v46.reviewer import FrontReviewer
from taoran_agent.llm import _AdviceBasis
from taoran_agent.models import WritebackResult
from taoran_agent.workflow_status import workflow_status


def test_empty_source_is_not_invalid_model_output():
    value = _AdviceBasis(fields=['next_contact_at'], existing_content='',
        missing_detail='缺少时间', decision_impact='无法判断跨月安排', gap_kind='missing_value')
    assert value.existing_content == ''


@pytest.mark.parametrize('mode', ['none', 'long', 'unknown', 'repair', 'persistent'])
def test_final_shape_recovery_retains_analysis_and_diagnostics(tmp_path, monkeypatch, mode):
    good = candidate(False)
    good['items'] = []
    if mode == 'long':
        good['analysis_points'][0]['text'] = '客户已说明预算尚未批准。' * 40
    if mode == 'unknown':
        good['items'] = [{'code': 'unknown', 'suggestion': '未知建议'}]
    calls = []
    def provider(request):
        calls.append(json.loads(request.content))
        raw = deepcopy(good)
        if mode == 'persistent' or (mode == 'repair' and len(calls) == 1):
            raw['analysis_points'] = 'bad'
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(raw)}, 'finish_reason': 'stop'}]})
    settings = Settings(_env_file=None, database_path=str(tmp_path / 'db'), llm_model='test',
        llm_api_url='https://example.test/chat', llm_api_key='test')
    reviewer = FrontReviewer(settings, None, transport=httpx.MockTransport(provider))
    monkeypatch.setattr(reviewer, '_experimental_audit_wording', lambda *a, **k: None)
    try:
        result = reviewer.verbalize_knowledge_issues([{'code': 'R'}],
            taoran_snapshot={'visit_analysis_context': {'process_description': '客户表示预算尚未批准'}}, experimental=True)
        assert result.status == ('unavailable' if mode == 'persistent' else 'completed')
        assert len(calls) == (2 if mode in {'repair', 'persistent'} else 1)
        if mode in {'repair', 'persistent'}:
            assert 'format_errors' in json.loads(calls[1]['messages'][1]['content'])
            evidence = result.model_attempts[0]['diagnostic_evidence_id']
            saved = json.loads((tmp_path / 'model-failure-evidence' / (evidence + '.json')).read_text())
            assert saved['details']['validation_errors'][0]['location'] == 'analysis_points'
        if mode == 'long':
            assert result.visit_analysis == good['analysis_points'][0]['text'].rstrip('。')
        if mode != 'persistent':
            assert result.items == []
    finally:
        reviewer.close()


@pytest.mark.parametrize('text', ['不足以判断。', '客户反馈数量<10，尚未批准。', '客户明确反馈。' * 100])
def test_preview_text_format_never_discards_nonempty_complete_analysis(monkeypatch, tmp_path, text):
    settings = Settings(_env_file=None, database_path=str(tmp_path/'db'), llm_model='test', llm_enabled=True,
        llm_api_url='https://example.test/chat', llm_api_key='test')
    real_client = httpx.Client
    events = [{'choices': [{'delta': {'content': text}}]}, {'choices': [{'delta': {}, 'finish_reason': 'stop'}]}]
    monkeypatch.setattr(preview.httpx, 'Client', lambda **k: real_client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text=''.join('data: '+json.dumps(e)+'\n\n' for e in events)))))
    emitted = []
    result = preview.stream_semantic_preview_v22(settings, visit(), emitted.append, interactive=True)
    assert result['status'] == 'completed'
    assert ''.join(emitted) == text


def test_workflow_does_not_claim_fallback_score_is_success():
    r = {'semantic_facts': {'status': 'fallback'}, 'writeback': {'status': 'failed'}}
    assert workflow_status('completed', r) == 'analysis_failed'
    r['semantic_facts']['status'] = 'completed'
    assert workflow_status('completed', r) == 'writeback_failed'
    r['writeback']['status'] = 'succeeded'
    assert workflow_status('completed', r) == 'succeeded'


def test_delivery_must_verify_actual_fields(env, monkeypatch):  # noqa: F811
    settings, store, raw, _mapping, create, _calls = env
    request, response = create()
    monkeypatch.setattr(writeback.httpx, 'post', lambda *a, **k: httpx.Response(200, json={'data':raw}, request=httpx.Request('POST', a[0])))
    result = writeback.writeback_evaluation(settings, request, response, store=store)
    assert result.error_message == 'WRITEBACK_VERIFY_MISMATCH'


def test_retry_delivery_never_calls_model(env, monkeypatch):  # noqa: F811
    _settings, store, _raw, _mapping, create, _calls = env
    _request, response = create()
    response = response.model_copy(update={'writeback': WritebackResult(status='failed')})
    store.complete_evaluation(response)
    monkeypatch.setattr(api, 'get_agent', lambda: pytest.fail('must not regenerate'))
    value = api.retry_evaluation_writeback('job1', 'a', 'a', 'key-a')
    assert value['writeback']['status'] == 'succeeded'
    assert store.get_evaluation('a', 'job1')['workflow_status'] == 'succeeded'


def test_interrupted_delivery_uses_durable_analysis_checkpoint(env, monkeypatch):  # noqa: F811
    _settings, store, _raw, _mapping, create, _calls = env
    request, response = create()
    with store._connection:
        store._connection.execute("UPDATE evaluation_jobs SET status='running' WHERE job_id='job1'")
    store.checkpoint_evaluation(response)
    monkeypatch.setattr(api, 'get_agent', lambda: pytest.fail('restart must reuse saved analysis'))
    api.execute_evaluation('job1', request)
    assert store.get_evaluation('a', 'job1')['workflow_status'] == 'succeeded'


@pytest.mark.parametrize('http_status,expected_calls', [(429, 3), (503, 3), (403, 1)])
def test_delivery_retry_is_bounded_and_permission_failure_not_retried(env, monkeypatch, http_status, expected_calls):  # noqa: F811
    settings, store, _raw, _mapping, create, _calls = env
    request, response = create()
    calls = []
    def fail(url, **kwargs):
        calls.append(kwargs)
        return httpx.Response(http_status, request=httpx.Request('POST', url))
    monkeypatch.setattr(writeback.httpx, 'post', fail)
    monkeypatch.setattr(writeback, 'sleep', lambda _: None)
    with pytest.raises(writeback.JiandaoyunWritebackError):
        writeback.writeback_evaluation(settings, request, response, store=store)
    assert len(calls) == expected_calls
    assert len({c['json']['transaction_id'] for c in calls}) == 1
