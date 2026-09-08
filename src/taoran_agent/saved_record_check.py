"""Manual saved-record checks using the existing modal transport, not page text."""
import hashlib
import secrets
from datetime import UTC, datetime
from queue import Queue
from time import monotonic, sleep
from uuid import uuid4

from fastapi import BackgroundTasks, HTTPException

from .jiandaoyun_api import JiandaoyunReadError, find_jiandaoyun_record_by_field
from .models import JiandaoyunSubmittedEvent


def launch(request, tenant_id, api_key):
    from . import api

    api.authorize(tenant_id, tenant_id, api_key)
    settings = api.get_settings()
    api._require_interactive_quick_check(settings)
    mapping = api.tenant_mapping(settings, tenant_id)
    code = str(request.get("record_code") or "").strip()
    spec = mapping.get("record_fields", {}).get("visit_record_code", {})
    if not code or not isinstance(spec, dict) or not spec.get("widget_id"):
        raise HTTPException(422, "拜访记录识别配置不完整")
    app_id, entry_id = mapping["source_application_id"], mapping["source_entry_id"]
    try:
        record = find_jiandaoyun_record_by_field(
            settings, tenant_id, app_id, entry_id,
            spec["widget_id"], spec.get("widget_type", "text"), code,
        )
    except JiandaoyunReadError:
        raise HTTPException(502, "读取拜访记录失败，请稍后重试") from None
    if not record.get("_id"):
        raise HTTPException(404, "未找到拜访记录")
    # Ignore all unsaved page values, including client-supplied data_id.
    background = BackgroundTasks()
    accepted = api._enqueue_jiandaoyun_record(
        JiandaoyunSubmittedEvent(
            tenant_id=tenant_id, data_id=record["_id"], app_id=app_id,
            entry_id=entry_id, user_id=str(request.get("user_id") or "jiandaoyun-user"),
            request_id="manual_saved_" + uuid4().hex,
        ), record, mapping, background, tenant_id, api_key,
    )
    now = monotonic()
    check_id = "qc_" + uuid4().hex
    token = secrets.token_urlsafe(32)
    task = {
        "check_id": check_id, "tenant_id": tenant_id,
        "user_id": str(request.get("user_id") or "jiandaoyun-user"),
        "record_code": code, "input_hash": accepted.input_snapshot_hash,
        "idempotency_key": (tenant_id, "saved", check_id),
        "check_sequence": 1, "status": "processing", "events": Queue(),
        "created_at": datetime.now(UTC).isoformat(),
        "expires_at": now + settings.quick_check_recovery_ttl_seconds,
        "stream_token": token,
        "stream_token_expires_at": now + settings.quick_check_stream_token_ttl_seconds,
        "preview_snapshot": {"text": "", "status": "completed"},
    }

    def work():
        for job in background.tasks:
            job.func(*job.args, **job.kwargs)
        while monotonic() < task["expires_at"]:
            saved = api.get_store().get_evaluation(tenant_id, accepted.job_id)
            if saved and saved["status"] not in {"queued", "running"}:
                result = saved.get("response") or {}
                if (result.get("semantic_facts") or {}).get("status") == "completed" and (
                    result.get("writeback") or {}
                ).get("status") == "succeeded":
                    text = result.get("ai_opinion", "")
                    return {"preview": {"status": "completed"}, "final": {
                        "status": "completed", "feedback_text": text,
                        "final_feedback_hash": hashlib.sha256(text.encode()).hexdigest(),
                        "full_feedback_ms": int((monotonic() - now) * 1000),
                        "phase_timings": result.get("phase_latency_ms", {}),
                    }}
                break
            sleep(0.2)
        return {"preview": {"status": "completed"}, "final": {
            "status": "failed", "failure_category": "saved_evaluation_failed",
        }}

    task["future"] = api._quick_check_executor.submit(work)
    with api._quick_check_lock:
        api._quick_check_tasks[check_id] = task
    return {**api._quick_check_task_response(task),
            "opening_id": secrets.token_urlsafe(24), "stream_token": token,
            "reused": not bool(background.tasks),
            "expires_in_seconds": settings.quick_check_stream_token_ttl_seconds}
