from __future__ import annotations

import hashlib

import httpx
import pytest

from taoran_agent.knowledge import (
    REQUIRED_KNOWLEDGE_IDS,
    KnowledgeUnavailableError,
    TaoranKnowledgeSnapshot,
    assess_approved_fallback,
    load_taoran_knowledge_snapshot,
    select_knowledge_snapshot,
    taoran_runtime_records,
)


def remote_snapshot(
    *,
    empty_id: str | None = None,
    missing_id: str | None = None,
) -> TaoranKnowledgeSnapshot:
    fallback = load_taoran_knowledge_snapshot()
    records = []
    for record in fallback.records:
        if record.id not in REQUIRED_KNOWLEDGE_IDS or record.id == missing_id:
            continue
        copied = record.model_copy(deep=True)
        if record.id == empty_id:
            copied.content = ""
            copied.content_hash = hashlib.sha256(b"").hexdigest()
        records.append(copied)
    return TaoranKnowledgeSnapshot(
        source="https://knowledge.api.example/v1/knowledge/search",
        retrieved_at=fallback.retrieved_at,
        record_count=len(records),
        records=records,
    )


def test_complete_remote_uses_remote_without_fallback() -> None:
    selection = select_knowledge_snapshot(
        remote_snapshot(),
        load_taoran_knowledge_snapshot(),
    )
    assert selection.source == "remote_api"
    assert selection.remote_status == "COMPLETE"
    assert selection.fallback_active is False
    assert selection.fallback_reason is None


@pytest.mark.parametrize(
    "knowledge_id",
    ["DSM-BS-01-06", "DSM-BS-01-07", "DSM-MP-01"],
)
def test_empty_required_content_activates_approved_fallback(knowledge_id: str) -> None:
    fallback = load_taoran_knowledge_snapshot()
    selection = select_knowledge_snapshot(
        remote_snapshot(empty_id=knowledge_id),
        fallback,
    )
    assert selection.snapshot.snapshot_hash == fallback.snapshot_hash
    assert selection.source == "approved_bundled_snapshot"
    assert selection.remote_status == "INCOMPLETE"
    assert selection.fallback_active is True
    assert selection.fallback_reason == "required_content_missing"


def test_missing_required_record_activates_approved_fallback() -> None:
    selection = select_knowledge_snapshot(
        remote_snapshot(missing_id="DSM-MP-01"),
        load_taoran_knowledge_snapshot(),
    )
    assert selection.source == "approved_bundled_snapshot"
    assert selection.fallback_reason == "required_record_missing"


def test_remote_http_500_activates_approved_fallback() -> None:
    request = httpx.Request("GET", "https://knowledge.api.example/v1/knowledge/search")
    response = httpx.Response(500, request=request)
    error = httpx.HTTPStatusError("server error", request=request, response=response)
    selection = select_knowledge_snapshot(
        None,
        load_taoran_knowledge_snapshot(),
        remote_error=error,
    )
    assert selection.remote_status == "UNAVAILABLE"
    assert selection.fallback_reason == "remote_http_error"


def test_remote_timeout_activates_approved_fallback() -> None:
    error = httpx.ReadTimeout("timeout")
    selection = select_knowledge_snapshot(
        None,
        load_taoran_knowledge_snapshot(),
        remote_error=error,
    )
    assert selection.remote_status == "UNAVAILABLE"
    assert selection.fallback_reason == "remote_timeout"


def test_remote_parse_error_activates_approved_fallback() -> None:
    selection = select_knowledge_snapshot(
        None,
        load_taoran_knowledge_snapshot(),
        remote_error=ValueError("invalid remote payload"),
    )
    assert selection.source == "approved_bundled_snapshot"
    assert selection.fallback_reason == "remote_parse_error"


def test_invalid_fallback_is_explicitly_unavailable() -> None:
    fallback = load_taoran_knowledge_snapshot().model_copy(deep=True)
    fallback.records = [
        record for record in fallback.records if record.id != "DSM-MP-01"
    ]
    with pytest.raises(
        KnowledgeUnavailableError,
        match="fallback_required_record_missing",
    ):
        select_knowledge_snapshot(
            remote_snapshot(empty_id="DSM-MP-01"),
            fallback,
        )


def test_approved_fallback_provenance_and_hash_are_valid() -> None:
    fallback = load_taoran_knowledge_snapshot()
    result = assess_approved_fallback(fallback)
    assert result.complete
    assert fallback.source_knowledge_base_version == "1.18.0"
    assert (
        fallback.source_master_snapshot_hash
        == "d42289b24083d8b917e008a71bd722cf72a09725d06082ea048286dd99935e3a"
    )
    assert fallback.record_count == fallback.included_records == 151
    assert fallback.excluded_records == 7


def test_runtime_prompt_projection_uses_only_taoran_records_from_selected_snapshot() -> None:
    fallback = load_taoran_knowledge_snapshot()
    runtime_records = taoran_runtime_records(fallback)
    assert [record.id for record in runtime_records] == [
        "DSM-BS-01-06",
        "DSM-BS-01-07",
        "DSM-MP-01",
    ]
    assert sum(len(record.content) for record in runtime_records) < 5_000


def test_remote_recovery_switches_back_to_remote_source() -> None:
    fallback = load_taoran_knowledge_snapshot()
    degraded = select_knowledge_snapshot(
        remote_snapshot(empty_id="DSM-MP-01"),
        fallback,
    )
    recovered = select_knowledge_snapshot(remote_snapshot(), fallback)
    assert degraded.source == "approved_bundled_snapshot"
    assert recovered.source == "remote_api"
    assert recovered.fallback_active is False


def test_runtime_status_exposes_selected_source_and_fallback_reason(monkeypatch) -> None:
    from taoran_agent import api
    from taoran_agent.config import Settings

    incomplete = remote_snapshot(empty_id="DSM-MP-01")
    monkeypatch.setattr(
        api.KnowledgeApiClient,
        "fetch_taoran_snapshot",
        lambda self: incomplete,
    )
    settings = Settings(
        _env_file=None,
        knowledge_api_key="synthetic-read-key",
        knowledge_snapshot_cache_seconds=0,
    )
    with api._live_knowledge_cache_lock:
        api._live_knowledge_cache.clear()
        api._live_knowledge_inflight.clear()
    selected = api._fetch_live_knowledge_snapshot(settings, 1)
    state = api._monitoring_snapshot()["knowledge"]
    assert selected.source == "approved_bundled_snapshot"
    assert state["usable"] is True
    assert state["source"] == "approved_bundled_snapshot"
    assert state["remote_status"] == "INCOMPLETE"
    assert state["fallback_active"] is True
    assert state["fallback_reason"] == "required_content_missing"
    assert state["source_master_version"] == "1.18.0"
