import asyncio
import sqlite3
import sys
from types import SimpleNamespace

from taoran_agent import api
from taoran_agent.front_v46.experimental_final_diagnostics import safe_semantic_audit


def test_startup_never_recovers_or_runs_historical_observations(tmp_path, monkeypatch):
    path = tmp_path / "history.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE semantic_observation_jobs (id TEXT, status TEXT)")
        db.executemany("INSERT INTO semantic_observation_jobs VALUES (?, ?)",
                       [("old-failure", "failed"), ("old-pending", "queued")])
    before = path.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("Independent semantic observer must never start")

    monkeypatch.setitem(sys.modules, "taoran_agent.semantic_observation_jobs",
                        SimpleNamespace(start=forbidden, snapshot=forbidden, enqueue=forbidden))
    monkeypatch.setattr(api, "prewarm_runtime", lambda: None)
    monkeypatch.setattr(api, "recover_background_jobs", lambda: None)
    monkeypatch.setattr("taoran_agent.evaluation_operations.recover_operations", lambda: None)

    async def run():
        async with api.lifespan(api.app):
            pass

    asyncio.run(run())
    assert path.read_bytes() == before


def test_removed_audit_is_disabled_not_reported_as_failure():
    audit = safe_semantic_audit({"status": "disabled", "latency_ms": 0})
    assert audit["status"] == "disabled"
    assert audit["latency_ms"] == 0
    assert audit["observation_id"] is None
