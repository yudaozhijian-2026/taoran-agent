"""Administrator recovery workflow; durable operations, tenant-scoped summaries."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel

from .gateway import verify_admin_access
from .models import JiandaoyunSubmittedEvent, PostEvaluationRequest


def _no_store(response: Response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"


router = APIRouter(dependencies=[Depends(_no_store)])
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="taoran-operations")


class RecoveryAction(BaseModel):
    action: Literal["reanalyze", "retry_writeback"]
    idempotency_key: UUID


def _api():
    from . import api
    return api


def _store():
    store = _api().get_store()
    with store._lock, store._connection:
        store._connection.execute("""CREATE TABLE IF NOT EXISTS evaluation_operations (
            operation_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, job_id TEXT NOT NULL,
            action TEXT NOT NULL, idempotency_key TEXT NOT NULL, status TEXT NOT NULL,
            result_json TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(tenant_id, idempotency_key))""")
        store._connection.execute("""CREATE INDEX IF NOT EXISTS ix_operations_job
            ON evaluation_operations(tenant_id, job_id, created_at)""")
    return store


def _tenant_access(tenant_id):
    settings = _api().get_settings()
    tenant = settings.tenant_config(tenant_id)
    keys = settings.tenant_access_keys_for(tenant_id)
    if tenant is not None and not tenant.enabled:
        raise HTTPException(409, "客户已停用，请先完成接入配置")
    if not keys:
        raise HTTPException(409, "客户访问配置缺失，请先完成接入配置")
    return keys[0]


def recovery_summary(record, store):
    result = record.get("response") or {}
    model_status = result.get("semantic_facts", {}).get("status")
    writeback = result.get("writeback") or {}
    error = writeback.get("error_message") or record.get("error_message") or ""
    request = PostEvaluationRequest.model_validate(record["request"])
    latest = store.is_latest_source_job(request, record["job_id"])
    actions = []
    issue = ""
    if record["status"] in {"queued", "running"}:
        issue = "评价处理中"
    elif request.writeback_target is None:
        issue = "没有可补取的简道云来源"
    elif not latest or error == "WRITEBACK_SUPERSEDED":
        issue = "已有更新任务，本任务不再回写"
    elif record["status"] == "failed" or model_status != "completed":
        issue = ("关键字段未传入，请修复字段绑定后重新分析" if
                 "POST_INPUT_NOT_RECEIVED" in error else "分析或校验未通过，可补取最新记录重新分析")
        actions = ["reanalyze"]
    elif writeback.get("status") == "failed":
        if error in {"WRITEBACK_SOURCE_CHANGED", "WRITEBACK_POLICY_CHANGED",
                     "WRITEBACK_SOURCE_REVISION_MISSING", "WRITEBACK_KNOWLEDGE_CHANGED",
                     "WRITEBACK_MAPPING_CHANGED"}:
            issue = "记录或分析版本已变化，需要补取最新记录重新分析"
            actions = ["reanalyze"]
        elif error == "WRITEBACK_TARGET_CHANGED":
            issue = "当前表单配置与原任务不同，请核对接入配置"
        else:
            issue = "报告已生成但回写失败，可仅重试回写"
            actions = ["retry_writeback", "reanalyze"]
    return {
        "job_id": record["job_id"], "tenant_id": record["tenant_id"],
        "status": record["status"], "model_status": model_status,
        "visit_record_code": request.visit_record_code,
        "writeback_status": writeback.get("status"),
        "workflow_status": record.get("workflow_status"),
        "issue": issue, "actions": actions,
        "created_at": record["created_at"], "updated_at": record["updated_at"],
        "phase_latency_ms": result.get("phase_latency_ms", {}),
    }


def _operation(row):
    result = dict(row)
    result["result"] = json.loads(result.pop("result_json") or "{}")
    result.pop("idempotency_key", None)
    return result


@router.get("/api/v1/admin/tenants/{tenant_id}/evaluation-jobs")
def list_jobs(tenant_id: str, x_admin_key: str | None = Header(default=None),
              limit: int = Query(default=20, ge=1, le=100),
              offset: int = Query(default=0, ge=0),
              scope: Literal["attention", "all"] = "attention"):
    verify_admin_access(_api().get_settings(), x_admin_key)
    store = _store()
    condition = "tenant_id = ?"
    if scope == "attention":
        condition += """ AND (status = 'failed' OR (status = 'completed' AND
            (json_extract(response_json, '$.writeback.status') = 'failed'
             OR json_extract(response_json, '$.semantic_facts.status') != 'completed')))"""
    with store._lock:
        count = store._connection.execute(
            f"SELECT count(*) FROM evaluation_jobs WHERE {condition}", (tenant_id,)
        ).fetchone()[0]
        rows = store._connection.execute(
            f"SELECT * FROM evaluation_jobs WHERE {condition} ORDER BY rowid DESC LIMIT ? OFFSET ?",
            (tenant_id, limit, offset),
        ).fetchall()
        jobs = []
        for row in rows:
            summary = recovery_summary(store._evaluation_record(row), store)
            operations = store._connection.execute(
                "SELECT * FROM evaluation_operations WHERE tenant_id=? AND job_id=? ORDER BY created_at DESC LIMIT 5",
                (tenant_id, row["job_id"]),
            ).fetchall()
            summary["operations"] = [_operation(op) for op in operations]
            jobs.append(summary)
    return {"items": jobs, "total": count, "offset": offset, "limit": limit}


@router.post("/api/v1/admin/tenants/{tenant_id}/evaluation-jobs/{job_id}/actions", status_code=202)
def start_action(tenant_id: str, job_id: str, request: RecoveryAction,
                 background_tasks: BackgroundTasks,
                 x_admin_key: str | None = Header(default=None)):
    verify_admin_access(_api().get_settings(), x_admin_key)
    _tenant_access(tenant_id)
    store = _store()
    with store._lock, store._connection:
        existing = store._connection.execute(
            "SELECT * FROM evaluation_operations WHERE tenant_id=? AND idempotency_key=?",
            (tenant_id, str(request.idempotency_key)),
        ).fetchone()
        if existing:
            if existing["job_id"] != job_id or existing["action"] != request.action:
                raise HTTPException(409, "同一操作编号不能用于其他任务或操作")
            return _operation(existing)
        job = store.get_evaluation(tenant_id, job_id)
        if job is None:
            raise HTTPException(404, "任务不存在")
        if request.action not in recovery_summary(job, store)["actions"]:
            raise HTTPException(409, "该任务当前不支持此操作，请刷新任务状态")
        active = store._connection.execute(
            "SELECT * FROM evaluation_operations WHERE tenant_id=? AND job_id=? AND status IN ('queued','running')",
            (tenant_id, job_id),
        ).fetchone()
        if active:
            return _operation(active)
        op_id = f"op_{uuid4().hex}"
        now = datetime.now(UTC).isoformat()
        store._connection.execute(
            "INSERT INTO evaluation_operations VALUES (?,?,?,?,?,'queued',NULL,NULL,?,?)",
            (op_id, tenant_id, job_id, request.action, str(request.idempotency_key), now, now),
        )
        row = store._connection.execute(
            "SELECT * FROM evaluation_operations WHERE operation_id=?", (op_id,)
        ).fetchone()
    background_tasks.add_task(_executor.submit, run_operation, op_id)
    return _operation(row)


@router.get("/api/v1/admin/tenants/{tenant_id}/evaluation-operations/{operation_id}")
def operation_status(tenant_id: str, operation_id: str,
                     x_admin_key: str | None = Header(default=None)):
    verify_admin_access(_api().get_settings(), x_admin_key)
    store = _store()
    with store._lock:
        row = store._connection.execute(
            "SELECT * FROM evaluation_operations WHERE tenant_id=? AND operation_id=?",
            (tenant_id, operation_id),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "操作不存在")
    return _operation(row)


def run_operation(operation_id):
    api = _api()
    store = _store()
    with store._lock, store._connection:
        row = store._connection.execute(
            "SELECT * FROM evaluation_operations WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if row is None or row["status"] != "queued":
            return
        store._connection.execute(
            "UPDATE evaluation_operations SET status='running', updated_at=? WHERE operation_id=?",
            (datetime.now(UTC).isoformat(), operation_id),
        )
    result, error, status = {}, None, "succeeded"
    try:
        tenant_id, job_id = row["tenant_id"], row["job_id"]
        key = _tenant_access(tenant_id)
        if row["action"] == "retry_writeback":
            value = api.retry_evaluation_writeback(job_id, tenant_id, tenant_id, key)
            result = {"job_id": job_id, "writeback_status": value["writeback"]["status"]}
            if result["writeback_status"] != "succeeded":
                status, error = "failed", "回写仍未成功，请刷新任务查看处理建议"
        else:
            original = PostEvaluationRequest.model_validate(
                store.get_evaluation(tenant_id, job_id)["request"])
            target = original.writeback_target
            event = JiandaoyunSubmittedEvent(
                tenant_id=tenant_id, app_id=target.app_id, entry_id=target.entry_id,
                data_id=target.data_id, user_id=original.context.user_id,
                request_id=f"recovery_{operation_id}",
            )
            tasks = BackgroundTasks()
            accepted = api.submit_jiandaoyun_record_event(event, tasks, tenant_id, key)
            result = {"job_id": accepted.job_id, "status": accepted.status}
            for task in tasks.tasks:
                api._background_executor.submit(task.func, *task.args, **task.kwargs)
    except HTTPException as exc:
        status, error = "failed", str(exc.detail)[:200]
    except Exception as exc:  # noqa: BLE001 - durable operation boundary
        status, error = "failed", f"操作失败（{type(exc).__name__}），请核对服务状态后重试"
    with store._lock, store._connection:
        store._connection.execute(
            "UPDATE evaluation_operations SET status=?, result_json=?, error=?, updated_at=? WHERE operation_id=?",
            (status, json.dumps(result, ensure_ascii=False), error,
             datetime.now(UTC).isoformat(), operation_id),
        )


def recover_operations():
    store = _store()
    with store._lock, store._connection:
        store._connection.execute("UPDATE evaluation_operations SET status='queued' WHERE status='running'")
        rows = store._connection.execute(
            "SELECT operation_id FROM evaluation_operations WHERE status='queued'"
        ).fetchall()
    for row in rows:
        _executor.submit(run_operation, row[0])
