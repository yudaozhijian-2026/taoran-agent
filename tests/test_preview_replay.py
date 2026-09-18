import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from queue import Queue
from types import SimpleNamespace

from taoran_agent import api


class Request:
    async def is_disconnected(self):
        return False


def task(preview=None):
    future = Future()
    future.set_result({'preview': preview or {'status': 'completed'}, 'final': {
        'status': 'completed', 'feedback_text': '正式反馈',
        'final_feedback_hash': 'hash', 'full_feedback_ms': 1}})
    return {'check_id': 'local', 'future': future, 'events': Queue(),
            'status': 'processing', 'check_sequence': 1, 'input_hash': 'hash', 'created_at': 'test'}


def collect(t):
    async def run():
        return ''.join([event async for event in api._interactive_quick_check_events(Request(), t)])
    return asyncio.run(run())


def test_repeated_open_and_poll_replay_exact_preview():
    t = task()
    t['events'].put({'type': 'preview_delta', 'text': '客户已确认。'})
    t['events'].put({'type': 'preview_complete', 'status': 'completed'})
    first = collect(t)
    second = collect(t)
    assert '客户已确认。' in first and '客户已确认。' in second
    assert second.count('event: final_completed') == 1
    assert api._quick_check_task_response(t)['preview_feedback_text'] == '客户已确认。'


def test_multiple_readers_receive_same_snapshot_without_consuming_it():
    t = task()
    t['events'].put({'type': 'preview_delta', 'text': '同一份建议'})
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: api._quick_check_task_response(t), range(8)))
    assert all(r['preview_feedback_text'] == '同一份建议' for r in results)


def test_v46_mode_finishes_validated_analysis_before_starting_advice(monkeypatch):
    calls=[]
    def generate_analysis(_visit, _settings, events):
        calls.append('analysis')
        events.put({'type':'preview_replace','text':'第一阶段分析'})
        events.put({'type':'preview_complete','status':'completed'})
        return {'status':'completed','feedback_text':'第一阶段分析','first_real_ai_text_ms':8}
    def generate_advice(*args, **kwargs):
        assert calls == ['analysis']
        assert kwargs['analysis_emit'] is None
        assert kwargs['suggestion_emit'] is None
        calls.append('advice')
        return {'status':'completed','feedback_text':'本次拜访分析：第二阶段内部分析\n\nAI改善建议：\n补充联系时间。'}
    monkeypatch.setattr(api,'_quick_check_run_preview',generate_analysis)
    monkeypatch.setattr(api,'_quick_check_run_final',generate_advice)
    events=task()['events']
    outcome=api._quick_check_run(SimpleNamespace(visit=None),None,events)
    assert outcome['preview']['status']=='completed'
    assert calls==['analysis','advice']
    assert outcome['final']['feedback_text']==(
        '本次拜访分析：第一阶段分析\n\nAI改善建议：\n补充联系时间。'
    )
    queued=[]
    while not events.empty():
        queued.append(events.get())
    analysis_index = next(i for i,item in enumerate(queued) if item.get('type')=='preview_complete')
    advice_index = next(i for i,item in enumerate(queued) if item.get('type')=='suggestion_replace')
    assert analysis_index < advice_index
    assert any(item.get('type') == 'preview_replace' and item.get('text') == '第一阶段分析' for item in queued)
    assert any(item.get('type') == 'suggestion_replace' and item.get('text') == '补充联系时间。' for item in queued)


def test_failed_final_does_not_remove_preview():
    t = task()
    t['future'] = Future()
    t['future'].set_result({'preview': {'status': 'completed'}, 'final': {'status': 'failed'}})
    t['events'].put({'type': 'preview_delta', 'text': '独立的建议'})
    result = collect(t)
    assert '独立的建议' in result and 'event: final_failed' in result


def test_snapshots_are_task_isolated():
    a, b = task(), task()
    a['events'].put({'type': 'preview_delta', 'text': 'A客户'})
    assert api._quick_check_task_response(a)['preview_feedback_text'] == 'A客户'
    assert api._quick_check_task_response(b)['preview_feedback_text'] == ''


def test_live_reset_replaces_failed_attempt_for_all_readers():
    t = task({'status': 'processing'})
    t['future'] = Future()
    t['events'].put({'type': 'preview_delta', 'text': '旧尝试部分文字'})
    assert api._quick_check_preview_snapshot(t)['text'] == '旧尝试部分文字'
    t['events'].put({'type': 'preview_reset'})
    assert api._quick_check_preview_snapshot(t) == {'text': '', 'status': 'processing'}
    t['events'].put({'type': 'preview_delta', 'text': '新尝试正文'})
    t['events'].put({'type': 'preview_complete', 'status': 'completed'})
    assert api._quick_check_preview_snapshot(t) == {'text': '新尝试正文', 'status': 'completed'}
    assert api._quick_check_preview_snapshot(t)['text'] == '新尝试正文'


def test_validated_analysis_replaces_draft_atomically_without_empty_snapshot():
    t = task({'status': 'processing'})
    t['future'] = Future()
    t['events'].put({'type': 'preview_delta', 'text': '草稿分析正文'})
    assert api._quick_check_preview_snapshot(t)['text'] == '草稿分析正文'
    t['events'].put({'type': 'preview_replace', 'text': '校验后的最终分析正文'})
    snapshot = api._quick_check_preview_snapshot(t)
    assert snapshot == {'text': '校验后的最终分析正文', 'status': 'processing'}
    assert snapshot['text']


def test_each_stage_releases_only_its_validated_module(monkeypatch):
    def analysis(_visit, _settings, events):
        events.put({'type':'preview_replace','text':'校验后的分析。'})
        events.put({'type':'preview_complete','status':'completed'})
        return {'status':'completed','feedback_text':'校验后的分析。'}

    def advice(_request, _settings, **kwargs):
        assert kwargs['analysis_emit'] is None
        assert kwargs['suggestion_emit'] is None
        return {
            'status': 'completed',
            'feedback_text': '本次拜访分析：第二阶段内部分析。\n\nAI改善建议：\n校验后的建议。',
        }

    monkeypatch.setattr(api, '_quick_check_run_preview', analysis)
    monkeypatch.setattr(api, '_quick_check_run_final', advice)
    events = Queue()
    result = api._quick_check_run(SimpleNamespace(visit=None), None, events)
    queued = []
    while not events.empty():
        queued.append(events.get())

    assert result['final']['status'] == 'completed'
    assert not any(item['type'] == 'preview_delta' for item in queued)
    assert not any(item['type'] == 'suggestion_delta' for item in queued)
    assert not any(item['type'] == 'validation_started' for item in queued)
    assert not any(item['type'] in {'preview_reset', 'suggestion_reset'} for item in queued)
    assert any(
        item == {'type': 'preview_replace', 'text': '校验后的分析。'}
        for item in queued
    )
    assert any(
        item == {'type': 'suggestion_replace', 'text': '校验后的建议。'}
        for item in queued
    )
    assert result['final']['feedback_text'].startswith('本次拜访分析：校验后的分析。')
    assert result['final']['phase_timings']['two_stage']['analysis_status']=='completed'


def test_validation_status_preserves_retained_snapshot_until_final_replace():
    t = task({'status': 'processing'})
    t['future'] = Future()
    t['events'].put({'type': 'preview_delta', 'text': '已显示的第一版。'})
    assert api._quick_check_preview_snapshot(t)['text'] == '已显示的第一版。'

    t['events'].put({'type': 'validation_started'})
    validating = api._quick_check_preview_snapshot(t)
    assert validating == {
        'text': '已显示的第一版。', 'status': 'processing', 'validating': True,
    }

    t['events'].put({'type': 'preview_replace', 'text': '最终修正版。'})
    t['events'].put({'type': 'suggestion_complete', 'status': 'completed'})
    t['events'].put({'type': 'preview_complete', 'status': 'completed'})
    assert api._quick_check_preview_snapshot(t) == {
        'text': '最终修正版。', 'status': 'completed',
    }


def test_sse_delivers_partial_snapshot_while_final_is_pending():
    t = task({'status': 'processing'})
    t['future'] = Future()

    async def run():
        stream = api._interactive_quick_check_events(Request(), t)
        await anext(stream)  # started
        await anext(stream)  # stage
        t['events'].put({'type': 'preview_delta', 'text': '第一段。'})
        first = await anext(stream)
        assert '第一段。' in first and 'processing' in first
        assert not t['future'].done()
        await anext(stream)  # keepalive
        t['events'].put({'type': 'preview_delta', 'text': '第二段。'})
        second = await anext(stream)
        assert '第一段。第二段。' in second
        assert not t['future'].done()
        await stream.aclose()

    asyncio.run(run())
