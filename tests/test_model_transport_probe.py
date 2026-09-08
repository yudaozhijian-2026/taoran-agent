import json
from types import SimpleNamespace

import httpx

from taoran_agent.model_transport_probe import TransportProbe


def test_timings_ids_and_redaction(tmp_path, monkeypatch):
    now = [1.0]
    monkeypatch.setattr('taoran_agent.model_transport_probe.monotonic', lambda: now[0])
    probe = TransportProbe(SimpleNamespace(database_path=str(tmp_path/'db')), {'secret': 'record'})
    probe.trace('connection.connect_tcp.started', {'authorization': 'secret-key'})
    now[0] = 1.02
    probe.trace('connection.connect_tcp.complete', {})
    probe.trace('connection.start_tls.started', {})
    now[0] = 1.05
    probe.trace('connection.start_tls.complete', {})
    probe.trace('http11.send_request_body.complete', {})
    now[0] = 1.1
    probe.headers(httpx.Response(200, headers={'x-request-id': 'req-123', 'secret': 'key'}))
    probe.completed({'id': 'body-456', 'content': 'private'}, 500, 900)
    reference = probe.save()
    path = tmp_path/'model-failure-evidence'/f'{reference}.json'
    text = path.read_text()
    d = json.loads(text)['details']
    assert (d['tcp_connect_ms'], d['tls_handshake_ms'], d['response_headers_ms']) == (20, 30, 100)
    assert d['provider_request_id'] == 'req-123' and d['provider_response_id'] == 'body-456'
    assert 'secret-key' not in text and 'private' not in text and 'record' not in text
    assert path.stat().st_mode & 0o777 == 0o600


def test_missing_connection_is_not_zero_and_failure_is_preserved(tmp_path):
    p = TransportProbe(SimpleNamespace(database_path=str(tmp_path/'db')), {})
    p.headers(httpx.Response(200))
    assert p.data['tcp_connect_ms'] is None
    assert p.data['connection_observation'] == 'reused_or_not_observed'
    p.trace('http11.receive_response_body.failed', {'exception': 'sensitive'})
    assert p.data['failed_phase'] == 'http11.receive_response_body'
    assert p.data['provider_request_id'] is None


def test_disk_failure_does_not_fail_request(tmp_path):
    blocker = tmp_path/'model-failure-evidence'
    blocker.write_text('not a directory')
    p = TransportProbe(SimpleNamespace(database_path=str(tmp_path/'db')), {})
    assert p.save() is None


def test_sse_done_generator_close_is_not_failure(tmp_path):
    p = TransportProbe(SimpleNamespace(database_path=str(tmp_path/'db')), {})
    p.trace('http11.receive_response_body.failed', {'exception': GeneratorExit()})
    assert p.data['stream_reader_closed'] is True
    assert 'failed_phase' not in p.data
