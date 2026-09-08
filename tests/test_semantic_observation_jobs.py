import json
from types import SimpleNamespace

from taoran_agent.config import Settings
from taoran_agent.semantic_observation_jobs import connect, enqueue, recover, run_one, snapshot


def setup(tmp_path):
    settings = Settings(_env_file=None, database_path=str(tmp_path/'db'))
    payload = {'identity': {'tenant_id': 't1', 'record_version': 'v1'},
               'context': {'process_description': '客户预算未批准'}, 'analysis': '预算未批准',
               'candidate': {'analysis_points': [{'kind': 'objective_result', 'text': '预算未批准'}],
                             'items': []}}
    return settings, payload


def reviewer(callback, busy=False):
    return SimpleNamespace(model_capacity=SimpleNamespace(snapshot=lambda: {
        'active_frontend': int(busy), 'active_backend': 0}), _experimental_audit_wording=callback)


def test_durable_identity_isolated_versions_and_no_feedback_writes(tmp_path):
    s, p = setup(tmp_path)
    oid = enqueue(s, p)
    assert enqueue(s, p) == oid
    p['identity']['record_version'] = 'v2'
    assert enqueue(s, p) != oid
    p['identity']['tenant_id'] = 't2'
    assert enqueue(s, p) != oid
    assert snapshot(s)['queued'] == 3
    original = []
    def audit(context, analysis, suggestions, timeout, result, **kw):
        original.append(context)
        result['status'] = 'passed'
    assert not run_one(s, reviewer(audit, busy=True))
    assert run_one(s, reviewer(audit))
    assert snapshot(s)['completed'] == 1
    with connect(s) as db:
        row = db.execute('SELECT * FROM semantic_observation_jobs WHERE observation_id=?', (oid,)).fetchone()
        assert json.loads(row['payload'])['identity']['record_version'] == 'v1'
        assert json.loads(row['result'])['candidate_hash'] == row['candidate_hash']
        assert len(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()) == 1


def test_retries_bounded_and_interrupted_work_recovers(tmp_path):
    s, p = setup(tmp_path)
    enqueue(s, p)
    def outage(*args, **kwargs):
        raise TimeoutError('simulated')
    assert run_one(s, reviewer(outage))
    with connect(s) as db:
        db.execute('UPDATE semantic_observation_jobs SET available_at=0')
    assert run_one(s, reviewer(outage))
    assert snapshot(s)['failed'] == 1 and snapshot(s)['queued'] == 0
    p['identity']['record_version'] = 'v2'
    oid = enqueue(s, p)
    with connect(s) as db:
        db.execute("UPDATE semantic_observation_jobs SET status='running',attempts=1 WHERE observation_id=?", (oid,))
    recover(s)
    assert snapshot(s)['queued'] == 1


def test_semantic_disagreement_is_observation_not_failure(tmp_path):
    s, p = setup(tmp_path)
    enqueue(s, p)
    def disagreement(*args, **kwargs):
        kwargs['repair_details']['semantic_issues'] = [{'error_type': 'actor_unclear'}]
        raise ValueError('observed disagreement')
    assert run_one(s, reviewer(disagreement))
    assert snapshot(s)['completed'] == 1 and snapshot(s)['failed'] == 0


def test_partial_advice_repair_keeps_analysis_and_queues_only_complete_candidate(tmp_path, monkeypatch):
    import httpx

    from taoran_agent.front_v46.observed_feedback import generate
    from taoran_agent.front_v46.reviewer import FrontReviewer
    s = Settings(_env_file=None, database_path=str(tmp_path/'db'), llm_model='test',
                 llm_api_url='https://example.test/chat', llm_api_key='test')
    calls = []
    first_analysis = [{'kind': 'objective_result', 'text': '客户预算未批准，后续方案需确认。',
                       'requires_followup': True}]
    def provider(request):
        data = json.loads(request.content)
        calls.append(data)
        raw = {'analysis_points': first_analysis, 'items': [], 'suggestion_status': None,
               'suggestion_reason': ''} if len(calls) == 1 else {
                   'analysis_points': [{'kind': 'objective_result', 'text': '不应采纳的重写分析'}],
                   'items': [{'code': 'R', 'suggestion': '请核对客户预算审批进度。'}],
                   'suggestion_status': 'has_suggestions', 'suggestion_reason': '预算状态影响下一步。'}
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(raw)}, 'finish_reason': 'stop'}]})
    r = FrontReviewer(s, None, transport=httpx.MockTransport(provider))
    monkeypatch.setattr(r, '_experimental_audit_wording', lambda *a, **k: (_ for _ in ()).throw(AssertionError('must not wait on observer')))
    try:
        result = generate(r, [{'code': 'R', 'source_fields': {'process_description': '客户预算未批准'}}],
                          {'visit_analysis_context': {'process_description': '客户预算未批准'}}, 30)
        assert len(calls) == 2 and result.suggestion_status == 'has_suggestions'
        assert result.visit_analysis == first_analysis[0]['text'].rstrip('。')
        assert '不应采纳' not in result.visit_analysis
        assert snapshot(s)['queued'] == 1
        input1 = json.loads(calls[0]['messages'][1]['content'])
        assert 'source_fields' not in input1['field_specificity_checks'][0]
        assert input1['visit_analysis_context']['process_description'] == '客户预算未批准'
        assert json.loads(calls[1]['messages'][1]['content'])['candidate_analysis'] == first_analysis
    finally:
        r.close()


def test_background_format_repair_uses_failed_review_context(tmp_path):
    import httpx

    from taoran_agent.front_v46.reviewer import FrontReviewer
    s, p = setup(tmp_path)
    s = s.model_copy(update={'llm_model': 'test', 'llm_api_url': 'https://example.test/chat'})
    from pydantic import SecretStr
    s.llm_api_key = SecretStr('test')
    calls = []
    def provider(request):
        calls.append(json.loads(request.content))
        raw = {'checks': {}} if len(calls) == 1 else {
            'checks': {k: True for k in ['actor', 'goal', 'temporal', 'coverage', 'consistency', 'assessment']}, 'issues': []}
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(raw)}, 'finish_reason': 'stop'}]})
    r = FrontReviewer(s, None, transport=httpx.MockTransport(provider))
    r.observation_workload = 'backend'
    r.observation_max_attempts = 2
    try:
        enqueue(s, p)
        assert run_one(s, r)
        assert len(calls) == 2
        assert 'review_repair' in calls[1]['messages'][-1]['content']
        assert snapshot(s)['completed'] == 1
    finally:
        r.close()
