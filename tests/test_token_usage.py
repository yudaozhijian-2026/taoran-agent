import csv
import json
import sqlite3
from threading import Barrier
from types import SimpleNamespace

import httpx
import pytest

from taoran_agent.llm import _read_chat_response
from taoran_agent.token_usage import UsageClient, UsageExecutor, attribution, usage_scope
from taoran_agent.token_usage_report import export_usage


def request(owner="sales-1", tenant="tenant-1"):
    return SimpleNamespace(visit=SimpleNamespace(employee_id=owner), context=SimpleNamespace(
        tenant_id=tenant, request_id="request-" + owner, source_record_id="record-" + owner),
        visit_record_code="visit-" + owner)


def rows(path):
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute("SELECT * FROM model_token_usage ORDER BY rowid")]


class Chunks(httpx.SyncByteStream):
    def __init__(self, data):
        self.data = data

    def __iter__(self):
        # Split Unicode and usage across arbitrary transport chunks.
        for n in range(0, len(self.data), 7):
            yield self.data[n:n + 7]


def client(path, payload, sse=True, code=200):
    data = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
    return UsageClient(SimpleNamespace(database_path=path), transport=httpx.MockTransport(
        lambda req: httpx.Response(code, headers={"content-type": "text/event-stream" if sse else "application/json"},
                                  stream=Chunks(data))))


def consume(c, line_mode=False):
    with c.stream("POST", "https://model.test/chat", json={"model": "glm-test"}) as response:
        response.raise_for_status()
        return list(response.iter_lines()) if line_mode else b"".join(response.iter_bytes())


def sse(usage):
    return ("data: " + json.dumps({"id": "provider-id", "choices": [{"delta": {"content": "客户回应"}}]}, ensure_ascii=False)
            + "\n\ndata: " + json.dumps({"choices": [], "usage": usage}) + "\n\ndata: [DONE]\n\n")


@pytest.mark.parametrize("line_mode", [True, False])
def test_sse_usage_only_tail_utf8_preserved_and_owner_not_clicker(tmp_path, line_mode):
    path = tmp_path / "usage.db"
    wire = sse({"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                "prompt_tokens_details": {"cached_tokens": 60}})
    with client(path, wire) as c, attribution(request(), "frontend_preview"):
        result = consume(c, line_mode)
    assert result == (wire.splitlines() if line_mode else wire.encode())
    row, = rows(path)
    assert (row["employee_id"], row["record_id"], row["stage"]) == ("sales-1", "record-sales-1", "frontend_preview")
    assert (row["input_tokens"], row["output_tokens"], row["total_tokens"], row["cached_input_tokens"]) == (100, 20, 120, 60)
    assert row["provider_request_id"] == "provider-id"
    assert row["usage_status"] == "known"


@pytest.mark.parametrize("usage,expected,status", [
    ({}, None, "unknown"),
    ({"prompt_tokens": 0, "completion_tokens": 0}, 0, "known"),
    ({"prompt_tokens": 5, "completion_tokens": 2}, 7, "known"),
    ({"total_tokens": 9}, 9, "known"),
    ({"prompt_tokens": True, "completion_tokens": -1}, None, "unknown"),
    ({"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 8}, 8, "inconsistent"),
])
def test_json_usage_unknown_and_explicit_zero_are_distinct(tmp_path, usage, expected, status):
    path = tmp_path / "usage.db"
    with client(path, {"id": "p", "usage": usage}, False) as c, attribution(request(), "backend"):
        consume(c)
    row, = rows(path)
    assert row["total_tokens"] == expected
    assert row["usage_status"] == status


def test_http_failure_keeps_unknown_and_business_error(tmp_path):
    path = tmp_path / "usage.db"
    with (client(path, "failure", code=503) as c, attribution(request(), "backend"),
          pytest.raises(httpx.HTTPStatusError)):
        consume(c)
    row, = rows(path)
    assert row["call_status"] == "interrupted_or_failed"
    assert row["total_tokens"] is None


def test_retry_counts_actual_calls_cache_has_no_new_call_and_job_binding(tmp_path):
    path = tmp_path / "usage.db"
    @usage_scope("backend")
    def evaluate(request, job_id, cached=False):
        if cached:
            return "cached"
        with client(path, sse({"total_tokens": 8})) as c:
            consume(c)
            consume(c)
    evaluate(request(), "job-1")
    evaluate(request(), "job-1", cached=True)
    found = rows(path)
    assert len(found) == 2 and sum(r["total_tokens"] for r in found) == 16
    assert len({r["call_id"] for r in found}) == 2
    assert {r["job_id"] for r in found} == {"job-1"}


def test_two_tenants_concurrent_workers_keep_context_after_scope_exits(tmp_path):
    path = tmp_path / "usage.db"
    # Initialize schema before racing the independent model calls.
    with client(path, sse({"total_tokens": 1})) as c, attribution(request(), "backend"):
        consume(c)
    barrier = Barrier(2)
    def run():
        barrier.wait(timeout=5)
        with client(path, sse({"total_tokens": 3})) as c:
            consume(c)
    with UsageExecutor(max_workers=2) as executor:
        with attribution(request("a", "tenant-a"), "frontend_final"):
            a = executor.submit(run)
        with attribution(request("b", "tenant-b"), "backend"):
            b = executor.submit(run)
        a.result(timeout=5)
        b.result(timeout=5)
    assert {(r["tenant_id"], r["employee_id"], r["stage"]) for r in rows(path)[1:]} == {
        ("tenant-a", "a", "frontend_final"), ("tenant-b", "b", "backend")}


def test_recorder_disk_failure_does_not_change_model_result(tmp_path, caplog):
    # A directory cannot be opened as SQLite.
    with client(tmp_path, sse({"total_tokens": 1})) as c:
        result = consume(c)
    assert b"[DONE]" in result
    assert "TOKEN_USAGE_PERSIST_FAILED" in caplog.text


def test_existing_chat_reader_sees_identical_result(tmp_path):
    from time import monotonic
    path = tmp_path / "usage.db"
    wire = sse({"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3})
    with (client(path, wire) as c, attribution(request(), "backend"),
          c.stream("POST", "https://model.test/chat", json={"model": "test"}) as response):
        envelope, _, _ = _read_chat_response(response, started=monotonic(), timeout=10, max_bytes=10000)
    assert envelope["choices"][0]["message"]["content"] == "客户回应"
    assert rows(path)[0]["total_tokens"] == 3


def test_report_shanghai_day_exclusion_tenant_and_unknown_total(tmp_path):
    path = tmp_path / "usage.db"
    for owner, tenant, usage in [("a", "t", {"total_tokens": 7}), ("a", "t", {}),
                                 ("sela", "t", {"total_tokens": 99}), ("b", "other", {"total_tokens": 900})]:
        with client(path, sse(usage)) as c, attribution(request(owner, tenant), "backend"):
            consume(c)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE model_token_usage SET started_at='2026-09-10T16:01:00+00:00'")
    directory = tmp_path / "people.json"
    directory.write_text(json.dumps({"sales": [
        {"tenant_id": "t", "employee_id": "a", "name": "张三", "company": "公司甲"},
        {"tenant_id": "t", "employee_id": "sela", "name": "sela", "company": "测试", "exclude": True},
    ]}))
    result = export_usage(path, tmp_path / "out", "t", "2026-09-11", "2026-09-11", directory)
    assert (result["calls"], result["known_tokens"], result["unknown_calls"], result["excluded_calls"]) == (2, 7, 1, 1)
    with (tmp_path / "out/01_每人每天Token.csv").open(encoding="utf-8-sig") as handle:
        row, = list(csv.DictReader(handle))
    assert row["所属公司"] == "公司甲" and row["日期/期间"] == "2026-09-11"
    assert row["完整总Token"] == "" and row["已核实Token小计"] == "7"


def test_actual_front_dispatch_labels_preview_and_final_with_same_record_owner(tmp_path, monkeypatch):
    from queue import Queue

    from taoran_agent import api

    path = tmp_path / "usage.db"
    settings = SimpleNamespace(database_path=path, frontend_model_timeout_seconds=5)
    def provider_call(*args, **kwargs):
        with client(path, sse({"total_tokens": 4})) as c:
            consume(c)
        return {"status": "completed"}
    monkeypatch.setattr(api, "_quick_check_run_final", provider_call)
    monkeypatch.setattr(api, "get_agent", lambda *_: SimpleNamespace(semantic_reviewer=None))
    # Initial schema creation has the same serialization requirements as the production DB.
    from taoran_agent.token_usage import UsageLedger
    UsageLedger(path)._write("SELECT 1")
    api._quick_check_run(request("record-owner", "t"), settings, Queue())
    assert {(r["stage"], r["employee_id"], r["tenant_id"]) for r in rows(path)} == {
        ("frontend_final", "record-owner", "t")}


def test_tracking_schema_is_additive_to_existing_business_database(tmp_path):
    from taoran_agent.storage import AgentStore
    path = tmp_path / "usage.db"
    AgentStore(path)
    with sqlite3.connect(path) as db:
        before = db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    with client(path, sse({"total_tokens": 3})) as c, attribution(request(), "backend"):
        consume(c)
    with sqlite3.connect(path) as db:
        after = db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' "
                           "AND name!='model_token_usage' ORDER BY name").fetchall()
    assert before == after



def test_predeployment_database_is_not_reported_as_zero(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE existing_business (id TEXT)")
    with pytest.raises(ValueError, match="尚无Token"):
        export_usage(path, tmp_path / "out", "t", "2026-09-11", "2026-09-11")
