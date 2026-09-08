"""Exercise delivery before upstream completion, not simulated typing after it."""
import hashlib
import json

import httpx
import pytest
from test_post_policy import visit

from taoran_agent.config import Settings
from taoran_agent.front_v46 import experimental_semantic_streaming_v22 as preview

FIRST = "本次记录了客户咨询办公桌的数量与规格，属于已经取得的需求信息。"
SECOND = "销售承诺后续答复，尚不能视为已经完成回复。"


def settings(tmp_path):
    return Settings(_env_file=None, database_path=str(tmp_path / "db"),
                    llm_enabled=True, llm_model="test", llm_api_key="test",
                    llm_api_url="https://example.test/chat")


def wire(text="", finish=None):
    return ("data: " + json.dumps({"choices": [{"delta": {"content": text},
                                               "finish_reason": finish}]}) + "\n\n").encode()


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("chunk_size", [1, 7, 999])
def test_preview_is_delivered_before_upstream_finishes(tmp_path, monkeypatch, wrapped, chunk_size):
    pieces = []
    progress = []

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            text = (preview._OPEN if wrapped else "") + FIRST
            for pos in range(0, len(text), chunk_size):
                yield wire(text[pos:pos + chunk_size])
            # The second sentence and provider finish have NOT arrived yet.
            assert "".join(pieces) == FIRST
            progress.append("before_second_sentence")
            text = SECOND + (preview._CLOSE if wrapped else "")
            for pos in range(0, len(text), chunk_size):
                yield wire(text[pos:pos + chunk_size])
            yield wire(finish="stop")

    real = httpx.Client
    monkeypatch.setattr(preview.httpx, "Client", lambda **kwargs: real(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream()))))
    result = preview.stream_semantic_preview_v22(
        settings(tmp_path), visit(), pieces.append, interactive=True, live=True,
        reset=lambda: pytest.fail("successful stream must not reset"),
    )
    assert result["status"] == "completed"
    assert progress == ["before_second_sentence"]
    assert "".join(pieces) == FIRST + SECOND
    assert result["feedback_hash"] == hashlib.sha256((FIRST + SECOND).encode()).hexdigest()


@pytest.mark.parametrize("recovers", [False, True])
def test_truncated_attempt_is_retracted_before_retry(tmp_path, monkeypatch, recovers):
    events = []
    calls = []

    def provider(request):
        calls.append(request)
        if len(calls) == 2:
            assert events[-1] == ("reset", "")
        text = FIRST if len(calls) == 1 else SECOND
        finish = "stop" if recovers and len(calls) == 2 else "length"
        return httpx.Response(200, content=wire(text) + wire(finish=finish))

    real = httpx.Client
    monkeypatch.setattr(preview.httpx, "Client", lambda **kwargs: real(
        transport=httpx.MockTransport(provider)))
    result = preview.stream_semantic_preview_v22(
        settings(tmp_path), visit(), lambda text: events.append(("text", text)),
        interactive=True, live=True, reset=lambda: events.append(("reset", "")),
    )
    rendered = ""
    for kind, text in events:
        rendered = "" if kind == "reset" else rendered + text
    assert len(calls) == 2
    assert rendered == (SECOND if recovers else "")
    assert result["status"] == ("completed" if recovers else "failed")


def test_legacy_atomic_consumer_stays_atomic(tmp_path, monkeypatch):
    pieces = []

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield wire(FIRST)
            assert pieces == []
            yield wire(SECOND)
            assert pieces == []
            yield wire(finish="stop")

    real = httpx.Client
    monkeypatch.setattr(preview.httpx, "Client", lambda **kwargs: real(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream()))))
    result = preview.stream_semantic_preview_v22(settings(tmp_path), visit(), pieces.append,
                                                interactive=True)
    assert result["status"] == "completed"
    assert pieces == [FIRST + SECOND]


def test_live_delivery_requires_reset_consumer(tmp_path):
    with pytest.raises(ValueError, match="requires_interactive_reset"):
        preview.stream_semantic_preview_v22(settings(tmp_path), visit(), lambda text: None,
                                            interactive=True, live=True)
