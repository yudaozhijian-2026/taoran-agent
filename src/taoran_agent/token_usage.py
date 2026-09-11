"""Independent model-call ledger. Never stores prompts, responses or credentials."""
from __future__ import annotations

import codecs
import inspect
import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from uuid import uuid4

import httpx

_log = logging.getLogger(__name__)
_scope: ContextVar[dict | None] = ContextVar("token_usage_scope", default=None)


class UsageExecutor(ThreadPoolExecutor):
    """Each submission owns a context, including calls outliving an API timeout."""
    def submit(self, fn, /, *args, **kwargs):
        captured = _scope.get()
        def run():
            token = _scope.set(dict(captured) if captured else None)
            try:
                return fn(*args, **kwargs)
            finally:
                _scope.reset(token)
        return super().submit(run)


@contextmanager
def attribution(request, stage, *, job_id=None):
    previous = _scope.get()
    # Nested enrichment belongs to the original business operation.
    if previous is not None:
        yield
        return
    token = _scope.set({
        "tenant_id": request.context.tenant_id,
        "employee_id": request.visit.employee_id,
        "request_id": request.context.request_id,
        "record_id": request.context.source_record_id or "",
        "record_code": getattr(request, "visit_record_code", "") or "",
        "job_id": job_id or "", "stage": stage,
    })
    try:
        yield
    finally:
        _scope.reset(token)


def usage_scope(stage):
    def decorate(fn):
        signature = inspect.signature(fn)
        @wraps(fn)
        def wrapped(*args, **kwargs):
            request = next((v for v in (*args, *kwargs.values())
                            if hasattr(v, "visit") and hasattr(v, "context")), None)
            if request is None:
                return fn(*args, **kwargs)
            bound = signature.bind_partial(*args, **kwargs)
            with attribution(request, stage, job_id=bound.arguments.get("job_id")):
                return fn(*args, **kwargs)
        return wrapped
    return decorate


def usage_stage(stage):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            current = _scope.get()
            token = _scope.set({**current, "stage": stage}) if current else None
            try:
                return fn(*args, **kwargs)
            finally:
                if token is not None:
                    _scope.reset(token)
        return wrapped
    return decorate


_SCHEMA = """
CREATE TABLE IF NOT EXISTS model_token_usage (
 call_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 tenant_id TEXT NOT NULL, employee_id TEXT NOT NULL, request_id TEXT NOT NULL,
 record_id TEXT NOT NULL, record_code TEXT NOT NULL, job_id TEXT NOT NULL,
 stage TEXT NOT NULL, model TEXT NOT NULL, provider_request_id TEXT,
 input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER,
 cached_input_tokens INTEGER, usage_status TEXT NOT NULL, call_status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_model_token_usage_owner_day
 ON model_token_usage(tenant_id, started_at, employee_id);
"""


class UsageLedger:
    def __init__(self, path):
        self.path = str(path)
        self.ready = False

    def _write(self, sql, params=()):
        # A failed recorder never prevents AI feedback or writeback. Log only a
        # safe code, not database exceptions that could contain user content.
        try:
            if self.path == ":memory:":
                raise ValueError("persistent ledger requires a file")
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path, timeout=0.25)) as db, db:
                if not self.ready:
                    db.executescript(_SCHEMA)
                    self.ready = True
                db.execute(sql, params)
        except Exception:  # noqa: BLE001 - accounting must not interrupt business work
            _log.error("TOKEN_USAGE_PERSIST_FAILED: usage totals may be incomplete")

    def start(self, model):
        call_id = uuid4().hex
        now = datetime.now(UTC).isoformat()
        context = _scope.get() or {}
        self._write(
            "INSERT INTO model_token_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (call_id, now, now, *(str(context.get(k) or "") for k in
              ("tenant_id", "employee_id", "request_id", "record_id", "record_code", "job_id")),
             context.get("stage", "unattributed"), str(model or ""),
             None, None, None, None, None, "unknown", "started"),
        )
        if not context:
            _log.warning("TOKEN_USAGE_UNATTRIBUTED: model call has no business owner context")
        return call_id

    def observe(self, call_id, envelope):
        if not isinstance(envelope, dict):
            return
        raw = envelope.get("usage")
        if not isinstance(raw, dict) or not raw:
            return
        def count(value):
            return value if type(value) is int and value >= 0 else None
        inp = count(raw.get("prompt_tokens", raw.get("input_tokens")))
        out = count(raw.get("completion_tokens", raw.get("output_tokens")))
        total = count(raw.get("total_tokens"))
        # Derived total is exact only when both provider components are present.
        if total is None and inp is not None and out is not None:
            total = inp + out
        details = raw.get("prompt_tokens_details") or raw.get("input_tokens_details") or {}
        cached = count(details.get("cached_tokens")) if isinstance(details, dict) else None
        status = "known" if total is not None else "unknown"
        if (inp is not None and out is not None and total != inp + out
                or cached is not None and inp is not None and cached > inp):
            status = "inconsistent"
        request_id = envelope.get("id") or envelope.get("request_id")
        self._write(
            "UPDATE model_token_usage SET updated_at=?, provider_request_id=COALESCE(?, "
            "provider_request_id), input_tokens=?,output_tokens=?,total_tokens=?,"
            "cached_input_tokens=?,usage_status=? WHERE call_id=?",
            (datetime.now(UTC).isoformat(), str(request_id) if request_id else None,
             inp, out, total, cached, status, call_id),
        )

    def identify(self, call_id, provider_id):
        self._write("UPDATE model_token_usage SET provider_request_id=? WHERE call_id=?",
                    (str(provider_id), call_id))

    def finish(self, call_id, status):
        self._write("UPDATE model_token_usage SET updated_at=?,call_status=? WHERE call_id=?",
                    (datetime.now(UTC).isoformat(), status, call_id))


class _ObservedResponse:
    def __init__(self, response, ledger, call_id):
        self.response, self.ledger, self.call_id = response, ledger, call_id
        self.is_sse = "text/event-stream" in response.headers.get("content-type", "").lower()
        self.buffer = ""
        self.provider_id = None
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def __getattr__(self, name):
        return getattr(self.response, name)

    def _line(self, line):
        if self.is_sse:
            if not line.startswith("data:"):
                return
            line = line[5:].strip()
        try:
            envelope = json.loads(line)
            if isinstance(envelope, dict):
                provider_id = envelope.get("id") or envelope.get("request_id")
                if provider_id and provider_id != self.provider_id:
                    self.ledger.identify(self.call_id, provider_id)
                    self.provider_id = provider_id
                self.ledger.observe(self.call_id, envelope)
        except (ValueError, TypeError):
            pass

    def iter_lines(self):
        for line in self.response.iter_lines():
            self._line(line)
            yield line

    def iter_bytes(self, *args, **kwargs):
        # Observe decoded bytes, so HTTP compression and fragmented UTF-8 work.
        for chunk in self.response.iter_bytes(*args, **kwargs):
            self.buffer += self.decoder.decode(chunk)
            if self.is_sse:
                while "\n" in self.buffer:
                    line, self.buffer = self.buffer.split("\n", 1)
                    self._line(line.rstrip("\r"))
            # Bound in-memory observation independently of business validation.
            if len(self.buffer) > 2_000_000:
                self.buffer = ""
            yield chunk
        self.buffer += self.decoder.decode(b"", final=True)
        if self.buffer:
            self._line(self.buffer)
        self.buffer = ""


class UsageClient:
    """Wrap only dedicated model clients; preserve HTTP payload and timeouts."""
    def __init__(self, settings, **kwargs):
        self.client = httpx.Client(**kwargs)
        self.ledger = UsageLedger(settings.database_path)

    def __getattr__(self, name):
        return getattr(self.client, name)

    def __enter__(self):
        self.client.__enter__()
        return self

    def __exit__(self, *args):
        return self.client.__exit__(*args)

    @contextmanager
    def stream(self, *args, **kwargs):
        body = kwargs.get("json") or {}
        call_id = self.ledger.start(body.get("model"))
        try:
            with self.client.stream(*args, **kwargs) as response:
                yield _ObservedResponse(response, self.ledger, call_id)
        except BaseException:
            self.ledger.finish(call_id, "interrupted_or_failed")
            raise
        else:
            self.ledger.finish(call_id, "returned")
