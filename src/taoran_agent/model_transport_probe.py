"""Private timing evidence only; never changes prompts, timeouts or UI payloads."""

import hashlib
import json
from datetime import UTC, datetime
from time import monotonic

from .model_failure_evidence import save_failure_evidence


class TransportProbe:
    def __init__(self, settings, source, *, stage="frontend_final"):
        self.settings = settings
        self.started = monotonic()
        self.starts = {}
        self.data = {
            "stage": stage,
            "started_at": datetime.now(UTC).isoformat(),
            "source_hash": hashlib.sha256(json.dumps(source, sort_keys=True,
                ensure_ascii=False, default=str).encode()).hexdigest(),
            "tcp_connect_ms": None, "tls_handshake_ms": None,
            "response_headers_ms": None, "request_body_sent_ms": None,
            "provider_request_id": None, "provider_response_id": None,
            "connection_observation": "not_observed",
        }

    def trace(self, name, info):
        # Never retain info: it may contain authorization headers or exception bodies.
        try:
            event, state = name.rsplit(".", 1)
            if state == "failed" and isinstance(info.get("exception"), GeneratorExit):
                self.data["stream_reader_closed"] = True
                return  # SSE [DONE] closes the generator before transport EOF; not a failure.
            now = monotonic()
            if state == "started":
                self.starts[event] = now
            if event in ("connection.connect_tcp", "connection.start_tls"):
                self.data["connection_observation"] = "new_connection_observed"
                key = "tcp_connect_ms" if event.endswith("connect_tcp") else "tls_handshake_ms"
                if state == "complete" and event in self.starts:
                    self.data[key] = round((now - self.starts[event]) * 1000)
                elif state == "failed":
                    self.data["failed_phase"] = event
            if event.endswith("send_request_body") and state == "complete":
                self.data["request_body_sent_ms"] = round((now-self.started)*1000)
            if state == "failed":
                self.data["failed_phase"] = event
        except (ValueError, TypeError):
            self.data["trace_unavailable"] = True

    @staticmethod
    def safe_id(value):
        if not isinstance(value, str):
            return None
        return "".join(c for c in value.strip()[:200] if c.isalnum() or c in "-_.:") or None

    def headers(self, response):
        self.data["response_headers_ms"] = round((monotonic()-self.started)*1000)
        self.data["http_status"] = response.status_code
        for name in ("x-request-id", "x-requestid", "request-id"):
            value = self.safe_id(response.headers.get(name))
            if value:
                self.data["provider_request_id"] = value
                break
        if self.data["connection_observation"] == "not_observed":
            # No connect event can also mean unsupported transport, not necessarily reuse.
            self.data["connection_observation"] = "reused_or_not_observed"

    def completed(self, envelope, first, last):
        self.data.update(first_content_ms=first, complete_ms=last,
                         provider_response_id=self.safe_id(envelope.get("id")))

    def save(self):
        self.data["elapsed_ms"] = round((monotonic()-self.started)*1000)
        return save_failure_evidence(self.settings, stage="model_transport",
                                     candidate=None, details=self.data)
