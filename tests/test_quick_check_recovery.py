from concurrent.futures import Future
from datetime import UTC, datetime
from time import monotonic, time

import pytest
from fastapi import HTTPException
from test_post_policy import visit

from taoran_agent import api
from taoran_agent.config import Settings
from taoran_agent.models import PrecheckRequest, RequestContext
from taoran_agent.quick_check_recovery import load, phase_timings
from taoran_agent.storage import AgentStore


class InlineExecutor:
    def submit(self, fn, *args):
        f = Future()
        try:
            f.set_result(fn(*args))
        except RuntimeError as e:
            f.set_exception(e)
        return f


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, database_path=str(tmp_path / "tasks.db"))
    store = AgentStore(settings.database_path)
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    monkeypatch.setattr(api, "get_store", lambda *_: store)
    monkeypatch.setattr(api, "_quick_check_executor", InlineExecutor())
    monkeypatch.setattr(api, "_quick_check_tasks", {})
    request = PrecheckRequest(
        context=RequestContext(tenant_id="tenant-a", request_id="test", user_id="user-a"),
        visit=visit(),
    )
    task = {
        "check_id": "qc_durable",
        "tenant_id": "tenant-a",
        "user_id": "user-a",
        "record_code": "TEST",
        "input_hash": "original-hash",
        "idempotency_key": ("tenant-a", "user-a", "TEST", "original-hash"),
        "check_sequence": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "status": "processing",
        "stream_token": "a" * 40,
        "session_token": "b" * 40,
        "stream_token_expires_at": monotonic() + 120,
        "expires_at": monotonic() + 604800,
        "stream_token_until": time() + 120,
        "retention_until": time() + 604800,
    }
    return settings, store, request, task


def test_failed_task_survives_restart_and_resumes_same_identity(recovery, monkeypatch):
    settings, store, request, task = recovery
    calls = []

    def run(request, settings, events):
        calls.append(request)
        return {
            "preview": {"status": "completed"},
            "final": {"status": "failed", "failure_category": "timeout"},
        }

    monkeypatch.setattr(api, "_quick_check_run", run)
    api._quick_check_schedule(task, request, settings)
    assert store.get_quick_check(task["check_id"])["status"] == "failed"
    assert not api._quick_check_tasks  # Simulate absence after restart.
    found = api.get_interactive_quick_check_task(task["check_id"], "b" * 40)
    assert found["recoverable"] and found["input_hash"] == "original-hash"
    assert "request_snapshot" not in found and "session_token" not in found
    with pytest.raises(HTTPException) as denied:
        api.resume_interactive_quick_check_task(task["check_id"], "z" * 40)
    assert denied.value.status_code == 404 and len(calls) == 1
    result = api.resume_interactive_quick_check_task(task["check_id"], "b" * 40)
    assert result["check_id"] == task["check_id"] and result["attempt"] == 2 and len(calls) == 2
    assert calls[0].visit == calls[1].visit
    saved = store.get_quick_check(task["check_id"])
    assert (
        len(saved["attempt_history"]) == 1
        and saved["attempt_history"][0]["outcome"]["final"]["failure_category"] == "timeout"
    )


def test_completed_task_resume_never_dispatches_again(recovery, monkeypatch):
    settings, _store, request, task = recovery
    calls = []

    def run(*args):
        calls.append(1)
        return {
            "preview": {"status": "completed"},
            "final": {
                "status": "completed",
                "feedback_text": "真实最终结果",
                "final_feedback_hash": "hash",
                "full_feedback_ms": 9,
            },
        }

    monkeypatch.setattr(api, "_quick_check_run", run)
    api._quick_check_schedule(task, request, settings)
    result = api.resume_interactive_quick_check_task(task["check_id"], "b" * 40)
    assert result["status"] == "completed" and result["final_feedback_text"] == "真实最终结果"
    assert len(calls) == 1


def test_interrupted_worker_is_recoverable_but_never_automatically_replayed(recovery):
    _settings, store, request, task = recovery
    from taoran_agent.quick_check_recovery import save

    task["request_snapshot"] = request.model_dump(mode="json")
    save(task, store)
    restored = load(task["check_id"], store)
    assert restored["status"] == "failed"
    assert restored["outcome"]["final"]["failure_category"] == "worker_interrupted"
    assert restored["request_snapshot"] == task["request_snapshot"]


def test_wait_generation_and_review_timings_have_distinct_meanings():
    from types import SimpleNamespace

    result = phase_timings(
        SimpleNamespace(
            model_attempts=[
                {
                    "model_queue_ms": 50,
                    "model_first_byte_ms": 1200,
                    "model_complete_ms": 5000,
                    "experimental_semantic_audit": {"latency_ms": 300},
                }
            ]
        ),
        5500,
    )
    assert result["attempts"][0] == {
        "model_queue_ms": 50,
        "first_byte_wait_ms": 1200,
        "generation_ms": 3800,
        "semantic_review_ms": 300,
        "status": "completed",
    }


def test_duplicate_click_reuses_running_task_even_when_force_and_after_restart(
    recovery, monkeypatch
):
    settings, _store, request, _unused = recovery
    settings.quick_check_interactive_enabled = True
    monkeypatch.setattr(api, "_quick_check_idempotency", {})
    monkeypatch.setattr(
        api,
        "_canonicalize_interactive_quick_check",
        lambda *args: (request, settings, "TEST", "same-version", "user-a", True),
    )
    pending = Future()
    calls = []

    class PendingExecutor:
        def submit(self, *args):
            calls.append(1)
            return pending

    monkeypatch.setattr(api, "_quick_check_executor", PendingExecutor())
    first = api.create_interactive_quick_check_task({}, "tenant-a", "secret")
    second = api.create_interactive_quick_check_task({}, "tenant-a", "secret")
    assert first["check_id"] == second["check_id"] and second["reused"] and len(calls) == 1
    assert first["basic_feedback"].startswith("基础检查") and first["status"] == "processing"
    api._quick_check_tasks.clear()
    api._quick_check_idempotency.clear()
    reopened = api.create_interactive_quick_check_task({}, "tenant-a", "secret")
    assert reopened["check_id"] == first["check_id"] and reopened["recoverable"] and len(calls) == 1
    assert reopened["basic_feedback"] == first["basic_feedback"]


def test_old_version_late_result_cannot_be_acknowledged(recovery, monkeypatch):
    settings, store, request, task = recovery
    monkeypatch.setattr(
        api,
        "_quick_check_run",
        lambda *args: {
            "preview": {"status": "completed"},
            "final": {"status": "completed", "feedback_text": "旧意见"},
        },
    )
    api._quick_check_schedule(task, request, settings)
    newer = dict(
        store.get_quick_check(task["check_id"]),
        check_id="qc_new",
        input_hash="new-version",
        created_at="2099-01-01T00:00:00Z",
    )
    store.save_quick_check("qc_new", task["tenant_id"], task["retention_until"], newer)
    result = api.get_interactive_quick_check_task(task["check_id"], "b" * 40)
    assert result["superseded"]
    with pytest.raises(HTTPException) as denied:
        api.acknowledge_interactive_quick_check_task(task["check_id"], "b" * 40)
    assert denied.value.status_code == 409


def test_first_html_shows_waiting_without_saved_basic_feedback(
    recovery, monkeypatch
):
    settings, _store, _request, task = recovery
    settings.quick_check_interactive_enabled = True
    task["basic_feedback"] = "基础检查：<script>alert(1)</script>__TAORAN_SESSION_TOKEN__"
    monkeypatch.setattr(api, "_quick_check_task", lambda *args: task)
    from starlette.requests import Request

    response = api.interactive_quick_check_page(
        Request({"type": "http", "headers": []}), task["check_id"], "b" * 40
    )
    html = response.body.decode()
    assert "alert(1)" not in html and "__TAORAN_SESSION_TOKEN__" not in html
    assert html.count("<script>") == 1
    assert "基础检查" not in html and "const taskVersion=" in html
    assert "AI正在分析" in html
    assert 'id="timings"' not in html and 'id="versionNote"' not in html
    assert response.headers["referrer-policy"] == "no-referrer"
