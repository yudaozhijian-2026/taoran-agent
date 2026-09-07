"""Release guards operate only on TAORAN and refuse unfinished work."""

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def guard():
    spec = importlib.util.spec_from_file_location(
        "release_guard", Path(__file__).resolve().parents[1] / "deploy/release_v47.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_durable_popup_jobs_are_included_in_deployment_activity(guard, tmp_path, monkeypatch):
    path = tmp_path / "taoran.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE quick_check_recovery(payload_json TEXT)")
        connection.executemany(
            "INSERT INTO quick_check_recovery VALUES (?)",
            [
                (json.dumps({"status": status}),)
                for status in ("processing", "queued", "completed", "failed")
            ],
        )
    monkeypatch.setattr(guard, "DB", path)
    result = guard.database()
    assert result["integrity"] == "ok"
    assert result["active"]["quick_check_recovery"] == 2
    monkeypatch.setattr(
        guard,
        "health",
        lambda: {"monitoring": {"model_capacity": {"active_frontend": 0, "active_backend": 0}}},
    )
    with pytest.raises(AssertionError, match="Database jobs active"):
        guard.activity()


def test_new_server_image_refuses_preflight_before_backup(guard, monkeypatch):
    monkeypatch.setattr(
        guard,
        "inspect",
        lambda: {"Config": {"Image": "dsm-taoran-v2:" + guard.OLD}, "Image": "sha256:newer"},
    )
    with pytest.raises(AssertionError, match="Server image changed"):
        guard.main("preflight")


def test_stopped_containers_are_also_protected(guard, monkeypatch):
    calls = []

    def run(args):
        calls.append(args)
        if args[:2] == ["docker", "ps"]:
            return "other"
        return json.dumps(
            [
                {
                    "Name": "/knowledge",
                    "Id": "other",
                    "Image": "unchanged",
                    "State": {"StartedAt": "original", "Status": "exited"},
                    "RestartCount": 2,
                }
            ]
        )

    monkeypatch.setattr(guard, "run", run)
    assert guard.others()["/knowledge"]["status"] == "exited"
    assert calls[0] == ["docker", "ps", "-aq"]
