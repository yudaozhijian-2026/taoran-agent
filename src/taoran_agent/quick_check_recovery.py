"""Durable, task-scoped recovery. Never writes customer records or starts a model."""

import hashlib
import json
from concurrent.futures import Future
from datetime import UTC, datetime
from queue import Queue
from time import monotonic, time

FIELDS = (
    "check_id",
    "tenant_id",
    "user_id",
    "record_code",
    "input_hash",
    "idempotency_key",
    "check_sequence",
    "status",
    "created_at",
    "completed_at",
    "acknowledged_at",
    "stream_token",
    "session_token",
    "request_snapshot",
    "attempt",
    "attempt_history",
    "phase_timings",
    "preview_snapshot",
    "basic_feedback",
    "front_policy",
    "retention_until",
    "cache_until",
    "knowledge_basis",
    "stream_token_until",
    "source",
)


def save(task, store):
    if not task.get("request_snapshot"):
        return  # Synthetic/legacy in-memory tasks have no recoverable source.
    payload = {k: task[k] for k in FIELDS if k in task}
    if "outcome" in task:
        payload["outcome"] = {k: v for k, v in task["outcome"].items() if k != "preview_future"}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    if task.get("_persisted_hash") != digest:
        store.save_quick_check(
            task["check_id"], task["tenant_id"], task["retention_until"], payload
        )
        task["_persisted_hash"] = digest


def load(check_id, store):
    payload = store.get_quick_check(check_id)
    if not payload or payload.get("retention_until", 0) <= time():
        return None
    task = dict(payload)
    task["events"] = Queue()
    task["expires_at"] = monotonic() + max(0, task["retention_until"] - time())
    task["stream_token_expires_at"] = monotonic() + max(
        0, task.get("stream_token_until", 0) - time()
    )
    task["idempotency_key"] = tuple(task["idempotency_key"])
    outcome = task.get("outcome")
    if task.get("status") == "processing" or outcome is None:
        task["status"] = "failed"
        task["completed_at"] = datetime.now(UTC).isoformat()
        outcome = {
            "preview": {"status": "failed"},
            "final": {"status": "failed", "failure_category": "worker_interrupted"},
        }
    elif outcome.get("preview", {}).get("status") == "processing":
        outcome["preview"] = {"status": "failed", "failure_category": "worker_interrupted"}
    task["outcome"] = outcome
    task["future"] = Future()
    task["future"].set_result(outcome)
    return task


def phase_timings(review, total_ms):
    attempts = getattr(review, "model_attempts", []) or []
    result = {"total_ms": total_ms, "attempts": []}
    for item in attempts:
        first, complete = item.get("model_first_byte_ms"), item.get("model_complete_ms")
        audit = item.get("experimental_semantic_audit") or {}
        result["attempts"].append(
            {
                "model_queue_ms": item.get("model_queue_ms"),
                "first_byte_wait_ms": first,
                "generation_ms": max(0, complete - first)
                if isinstance(first, int) and isinstance(complete, int)
                else None,
                "semantic_review_ms": audit.get("latency_ms"),
                "status": item.get("failure_reason") or "completed",
            }
        )
    return result
