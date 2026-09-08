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


def test_v46_mode_restores_independent_preview_and_final(monkeypatch):
    from taoran_agent.front_v46 import experimental_semantic_streaming_v22 as preview
    calls=[]
    def generate(settings, visit, emit, **kwargs):
        assert kwargs['live'] is True and callable(kwargs['reset'])
        calls.append('preview')
        emit('V4.6实时意见')
        return {'status':'completed'}
    monkeypatch.setattr(preview,'stream_semantic_preview_v22',generate)
    monkeypatch.setattr(api,'_quick_check_run_final',lambda *args: calls.append('final') or {'status':'completed','feedback_text':'最终意见'})
    events=task()['events']
    outcome=api._quick_check_run(SimpleNamespace(visit=None),None,events)
    assert outcome['preview_future'].result()['status']=='completed'
    assert sorted(calls)==['final','preview']
    assert outcome['final']['feedback_text']=='最终意见'
    assert events.get()['text']=='V4.6实时意见'


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
