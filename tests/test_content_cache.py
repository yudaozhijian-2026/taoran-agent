from concurrent.futures import ThreadPoolExecutor
from time import time

from taoran_agent.content_cache import fingerprint, reuse_allowed


def key(**changes):
    args = {
        "snapshot": {"process": "客户确认试用", "opportunities": [{"stage": "P3"}]},
        "visit": {"customer_id": "C1"},
        "tenant": "t1",
        "credential": "test-key",
        "user": "u1",
        "settings": {"model": "glm-5.2"},
        "mapping": {"source_entry_id": "f1"},
        "local_knowledge_hash": "local-v1",
        "live_knowledge_hash": "live-v1",
        "implementation": "code-v1",
    }
    args.update(changes)
    return fingerprint(**args)


def test_order_stable():
    assert key() == key(snapshot={"opportunities": [{"stage": "P3"}], "process": "客户确认试用"})


def test_partitions_and_revisions():
    for change in [
        {"tenant": "t2"},
        {"credential": "other"},
        {"user": "u2"},
        {"mapping": {"source_entry_id": "f2"}},
        {"settings": {"model": "other"}},
        {"local_knowledge_hash": "local-v2"},
        {"live_knowledge_hash": "live-v2"},
        {"implementation": "code-v2"},
    ]:
        assert key() != key(**change)


def test_related_data_changes():
    assert key() != key(snapshot={"process": "客户确认试用", "opportunities": [{"stage": "P4"}]})


def test_text_not_normalized_away():
    assert key() != key(snapshot={"process": "客户未确认试用", "opportunities": [{"stage": "P3"}]})


def test_only_complete_success_reuses():
    task = {"expires_at": 100, "cache_until": time() + 100}
    good = {
        "status": "completed",
        "content_complete": True,
        "final_status": "completed",
        "preview_status": "completed",
        "final_feedback_text": "正式意见",
        "preview_feedback_text": "实时意见",
    }
    assert reuse_allowed(task, good, 1)
    for bad in [
        {"status": "failed"},
        {"recoverable": True},
        {"content_complete": False},
        {"preview_feedback_text": ""},
        {"final_status": "failed"},
    ]:
        assert not reuse_allowed(task, {**good, **bad}, 1)
    assert not reuse_allowed({**task, "cache_until": time() - 1}, good, 1)
    assert not reuse_allowed(task, good, 101)


def test_running_and_final_done_preview_running():
    task = {"expires_at": 100, "cache_until": time() - 1}
    assert reuse_allowed(task, {"status": "processing"}, 1)
    assert reuse_allowed(task, {"status": "completed", "preview_status": "processing"}, 1)
    assert not reuse_allowed(task, {"status": "failed", "preview_status": "processing"}, 1)


def test_api_blank_business_id_and_concurrent_dedup(monkeypatch):
    from concurrent.futures import Future
    from types import SimpleNamespace

    from taoran_agent import api
    from taoran_agent.config import Settings
    from taoran_agent.models import PrecheckRequest

    settings = Settings(_env_file=None, quick_check_interactive_enabled=True)
    mapping = {"source_application_id": "app", "source_entry_id": "form"}

    def parse(request, *args):
        return PrecheckRequest.model_validate(
            {
                "context": request["context"],
                "visit": {
                    "visit_date": "2026-09-08",
                    "employee_id": "employee",
                    "process_description": request["form_data"]["process_description"],
                },
            }
        ), settings

    monkeypatch.setattr(api, "_canonicalize_button_request", parse)
    monkeypatch.setattr(api, "tenant_mapping", lambda *a: mapping)
    from taoran_agent.knowledge import load_taoran_knowledge_snapshot

    snapshot = load_taoran_knowledge_snapshot()
    monkeypatch.setattr(api, "load_taoran_knowledge_snapshot", lambda *a: snapshot)
    monkeypatch.setattr(api, "_fetch_live_knowledge_snapshot", lambda *a: snapshot)
    monkeypatch.setattr(
        api, "get_store", lambda *a: SimpleNamespace(find_quick_check=lambda *a: None)
    )
    monkeypatch.setattr(api, "_quick_check_tasks", {})
    monkeypatch.setattr(api, "_quick_check_idempotency", {})
    calls = []

    def schedule(task, *a):
        from taoran_agent.front_v46 import POLICY_VERSION

        task["front_policy"] = POLICY_VERSION
        calls.append(task["check_id"])
        task["future"] = Future()

    monkeypatch.setattr(api, "_quick_check_schedule", schedule)
    monkeypatch.setattr(
        api,
        "_quick_check_task_response",
        lambda t: {
            "check_id": t["check_id"],
            "status": "processing",
            "input_hash": t["input_hash"],
        },
    )
    payload = {"form_snapshot": {"process_description": "客户确认A"}}
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: api.create_interactive_quick_check_task(payload, "t", "key"), range(12)
            )
        )
    assert len(calls) == 1
    assert len({r["check_id"] for r in results}) == 1
    assert sum(not r["reused"] for r in results) == 1
    assert len({r["opening_id"] for r in results}) == 12
    changed = api.create_interactive_quick_check_task(
        {"form_snapshot": {"process_description": "客户确认B"}}, "t", "key"
    )
    assert changed["check_id"] != results[0]["check_id"]
    assert len(calls) == 2
    # Business IDs no longer affect cache identity.
    again = api.create_interactive_quick_check_task(
        {**payload, "record_code": "BFJL-new"}, "t", "key"
    )
    assert again["check_id"] == results[0]["check_id"]
    task = api._quick_check_tasks[again["check_id"]]
    assert task["knowledge_basis"]["live"] == snapshot.model_dump(mode="json")
    complete = {
        "status": "completed",
        "content_complete": True,
        "final_status": "completed",
        "preview_status": "completed",
        "final_feedback_text": "正式意见",
        "preview_feedback_text": "实时意见",
    }
    monkeypatch.setattr(
        api,
        "_quick_check_task_response",
        lambda t: {**complete, "check_id": t["check_id"], "input_hash": t["input_hash"]},
    )
    assert api.create_interactive_quick_check_task(payload, "t", "key")["reused"]
    task["cache_until"] = time() - 1
    expired = api.create_interactive_quick_check_task(payload, "t", "key")
    assert not expired["reused"]
    assert expired["check_id"] != again["check_id"]
    monkeypatch.setattr(
        api,
        "_quick_check_task_response",
        lambda t: {
            **complete,
            "content_complete": False,
            "recoverable": True,
            "check_id": t["check_id"],
            "input_hash": t["input_hash"],
        },
    )
    failed = api.create_interactive_quick_check_task(payload, "t", "key")
    assert not failed["reused"]
    assert failed["check_id"] != expired["check_id"]


def test_knowledge_is_pinned_and_context_resets():
    import pytest

    from taoran_agent import api
    from taoran_agent.config import Settings
    from taoran_agent.content_cache import knowledge_basis, run_with_knowledge_basis
    from taoran_agent.knowledge import load_taoran_knowledge_snapshot

    snapshot = load_taoran_knowledge_snapshot()
    settings = Settings(_env_file=None)
    basis = {"local": snapshot.model_dump(mode="json"), "live": snapshot.model_dump(mode="json")}

    def work(request, config):
        # No API credential: succeeds only by consuming the exact pinned snapshot.
        return api._fetch_live_knowledge_snapshot(config, 1).snapshot_hash

    assert run_with_knowledge_basis(work, None, settings, basis) == snapshot.snapshot_hash
    assert knowledge_basis.get() is None
    with pytest.raises(ValueError, match="pinned_knowledge_unavailable"):
        run_with_knowledge_basis(work, None, settings, {**basis, "live": None})
    assert knowledge_basis.get() is None


def test_parallel_workers_keep_separate_knowledge(monkeypatch):
    from queue import Queue
    from types import SimpleNamespace

    from taoran_agent import api
    from taoran_agent.config import Settings
    from taoran_agent.content_cache import knowledge_basis
    from taoran_agent.front_v46 import experimental_semantic_streaming_v22 as preview
    from taoran_agent.knowledge import load_taoran_knowledge_snapshot

    first = load_taoran_knowledge_snapshot()
    second = first.model_copy(deep=True)
    second.records[0].version += "-next"
    bases = [
        {"live": snapshot.model_dump(mode="json"), "local": snapshot.model_dump(mode="json")}
        for snapshot in (first, second)
    ]
    monkeypatch.setattr(api, "get_agent", lambda *args: SimpleNamespace(semantic_reviewer=None))
    monkeypatch.setattr(
        preview, "stream_semantic_preview_v22", lambda *args, **kwargs: {"status": "completed"}
    )
    monkeypatch.setattr(
        api,
        "_quick_check_run_final",
        lambda request, settings: {
            "status": "completed",
            "hash": api._fetch_live_knowledge_snapshot(settings, 1).snapshot_hash,
        },
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda basis: api._quick_check_run(
                    SimpleNamespace(visit=None), Settings(_env_file=None), Queue(), basis
                ),
                bases,
            )
        )
    assert [result["final"]["hash"] for result in results] == [
        first.snapshot_hash,
        second.snapshot_hash,
    ]
    assert knowledge_basis.get() is None


def test_no_knowledge_success_cache_but_active_dedup():
    task = {"expires_at": 100, "cache_until": time() + 100, "knowledge_basis": {"live": None}}
    assert reuse_allowed(task, {"status": "processing"}, 1)
    assert not reuse_allowed(
        task,
        {
            "status": "completed",
            "content_complete": True,
            "final_status": "completed",
            "preview_status": "completed",
            "final_feedback_text": "a",
            "preview_feedback_text": "b",
        },
        1,
    )


def test_recovery_preserves_basis_and_cache_expiry():
    from types import SimpleNamespace

    from taoran_agent.quick_check_recovery import load, save

    rows = {}
    store = SimpleNamespace(
        save_quick_check=lambda key, tenant, expiry, payload: rows.update({key: payload}),
        get_quick_check=lambda key: rows.get(key),
    )
    task = {
        "check_id": "qc_test",
        "tenant_id": "tenant",
        "request_snapshot": {"test": "snapshot"},
        "idempotency_key": ["tenant", "user", "content:hash", "hash"],
        "retention_until": time() + 600,
        "cache_until": time() + 100,
        "knowledge_basis": {"local": {"version": "v1"}, "live": {"version": "v2"}},
        "status": "completed",
        "outcome": {"final": {"status": "completed"}, "preview": {"status": "completed"}},
    }
    save(task, store)
    restored = load("qc_test", store)
    assert restored["knowledge_basis"] == task["knowledge_basis"]
    assert restored["cache_until"] == task["cache_until"]
    assert restored["future"].done()


def test_content_retry_requires_current_form(monkeypatch):
    import pytest
    from fastapi import HTTPException

    from taoran_agent import api

    monkeypatch.setattr(
        api,
        "_quick_check_task",
        lambda *args: {"knowledge_basis": {"live": {}}, "request_snapshot": {}},
    )
    monkeypatch.setattr(api, "_quick_check_task_response", lambda task: {"recoverable": True})
    with pytest.raises(HTTPException) as error:
        api.resume_interactive_quick_check_task("qc_test", "token")
    assert error.value.status_code == 409
    assert "重新点击AI检测" in error.value.detail


def test_sqlite_reopen_ack_restart_and_changed_content(tmp_path, monkeypatch):
    from concurrent.futures import Future

    from taoran_agent import api
    from taoran_agent.config import Settings
    from taoran_agent.front_v46 import POLICY_VERSION
    from taoran_agent.knowledge import load_taoran_knowledge_snapshot
    from taoran_agent.models import PrecheckRequest
    from taoran_agent.storage import AgentStore

    settings = Settings(
        _env_file=None,
        quick_check_interactive_enabled=True,
        database_path=str(tmp_path / "cache.sqlite"),
    )
    store = AgentStore(settings.database_path)
    snapshot = load_taoran_knowledge_snapshot()
    monkeypatch.setattr(api, "get_store", lambda *args: store)
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    monkeypatch.setattr(api, "tenant_mapping", lambda *args: {"source_entry_id": "form"})
    monkeypatch.setattr(api, "_fetch_live_knowledge_snapshot", lambda *args: snapshot)
    monkeypatch.setattr(api, "load_taoran_knowledge_snapshot", lambda *args: snapshot)
    monkeypatch.setattr(
        api,
        "_canonicalize_button_request",
        lambda request, *args: (
            PrecheckRequest.model_validate(
                {
                    "context": request["context"],
                    "visit": {
                        "visit_date": "2026-09-08",
                        "employee_id": "test",
                        "process_description": request["form_data"]["process_description"],
                    },
                }
            ),
            settings,
        ),
    )
    monkeypatch.setattr(api, "_quick_check_tasks", {})
    monkeypatch.setattr(api, "_quick_check_idempotency", {})
    calls = []

    def schedule(task, request, config):
        calls.append(task["check_id"])
        task.update(
            _settings=config,
            request_snapshot=request.model_dump(mode="json"),
            front_policy=POLICY_VERSION,
            future=Future(),
            preview_snapshot={"status": "completed", "text": "实时分析"},
        )
        task["future"].set_result(
            {
                "preview": {"status": "completed"},
                "final": {"status": "completed", "feedback_text": "完整AI反馈"},
            }
        )

    monkeypatch.setattr(api, "_quick_check_schedule", schedule)
    payload = {"form_snapshot": {"process_description": "客户确认了A"}}
    first = api.create_interactive_quick_check_task(payload, "tenant", "test-key")
    assert first["content_complete"] and not first["superseded"]
    assert (
        api.acknowledge_interactive_quick_check_task(first["check_id"], first["stream_token"])[
            "final_feedback_text"
        ]
        == "完整AI反馈"
    )
    # Simulated restart: only durable SQLite remains; a prior acknowledgement
    # must not make the cached result unavailable on a subsequent opening.
    api._quick_check_tasks.clear()
    api._quick_check_idempotency.clear()
    reopened = api.create_interactive_quick_check_task(payload, "tenant", "test-key")
    assert reopened["reused"] and reopened["check_id"] == first["check_id"]
    assert reopened["opening_id"] != first["opening_id"]
    assert len(calls) == 1
    assert (
        api.acknowledge_interactive_quick_check_task(
            reopened["check_id"], reopened["stream_token"]
        )["final_feedback_text"]
        == "完整AI反馈"
    )
    changed = api.create_interactive_quick_check_task(
        {"form_snapshot": {"process_description": "客户否认A"}}, "tenant", "test-key"
    )
    assert changed["check_id"] != first["check_id"] and len(calls) == 2
    # Going back to A reuses A, not B; separate forms do not invalidate A.
    again = api.create_interactive_quick_check_task(payload, "tenant", "test-key")
    assert again["reused"] and not again["superseded"]
    assert again["check_id"] == first["check_id"]
    # A knowledge revision creates a different content cache namespace.
    snapshot = snapshot.model_copy(deep=True)
    snapshot.records[0].version += "-updated"
    revised = api.create_interactive_quick_check_task(payload, "tenant", "test-key")
    assert revised["check_id"] != first["check_id"] and len(calls) == 3
