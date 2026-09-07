import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from queue import Queue
from threading import Event
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


def test_final_first_keeps_preview_worker_and_stream_alive(monkeypatch):
    release = Event()
    entered = Event()
    def preview(settings, visit, emit, **kwargs):
        entered.set()
        assert release.wait(3)
        emit('迟到但有效的建议')
        return {'status': 'completed'}
    monkeypatch.setattr(api, 'stream_semantic_preview_v22', preview)
    monkeypatch.setattr(api, '_quick_check_run_final', lambda *args: {
        'status': 'completed', 'feedback_text': '正式反馈',
        'final_feedback_hash': 'hash', 'full_feedback_ms': 1})
    t = task()
    try:
        outcome = api._quick_check_run(SimpleNamespace(visit=None), None, t['events'])
        assert entered.wait(1)
        assert not outcome['preview_future'].done()
        t['future'] = Future()
        t['future'].set_result(outcome)
        async def stream():
            seen = []
            async for event in api._interactive_quick_check_events(Request(), t):
                seen.append(event)
                if 'event: final_completed' in event:
                    assert not outcome['preview_future'].done()
                    release.set()
            return ''.join(seen)
        result = asyncio.run(stream())
        assert result.index('event: final_completed') < result.index('迟到但有效的建议')
        assert '迟到但有效的建议' in collect(t)
    finally:
        release.set()


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
