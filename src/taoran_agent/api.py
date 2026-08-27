from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, date, datetime
from threading import Lock
from typing import Any
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request, status
from pydantic import ValidationError

from .agent import TaoranAgent
from .config import Settings, get_settings
from .connector import (
    FieldTransferError,
    adapt_jiandaoyun_evaluation_request,
    adapt_jiandaoyun_request,
    load_jiandaoyun_mapping,
    mapped_jiandaoyun_value,
)
from .field_labels import use_field_mapping
from .gateway import verify_q40_service_access, verify_tenant_access
from .jiandaoyun_api import JiandaoyunReadError, get_jiandaoyun_record
from .knowledge import load_taoran_knowledge_snapshot
from .llm import PROMPT_VERSION
from .models import (
    ButtonPrecheckResponse,
    EvaluationAccepted,
    EvaluationResponse,
    JiandaoyunCheckRequest,
    JiandaoyunEvaluationRequest,
    JiandaoyunSubmittedEvent,
    PostEvaluationRequest,
    PrecheckRequest,
    PrecheckResponse,
    Q40BatchAccepted,
    Q40BatchEvaluationRequest,
    Q40BatchItemResult,
    Q40BatchResult,
    Q40PeriodFactsResponse,
    RuleCompatibilityResponse,
)
from .q40_integration import build_period_facts, rule_compatibility
from .rules import canonical_hash
from .runtime import build_agent
from .scoring_contract import TOTAL_RULE_VERSION
from .storage import AgentStore, IdempotencyConflictError
from .writeback import JiandaoyunWritebackError, writeback_evaluation

app = FastAPI(
    title="DSM TAORAN 拜访智能体",
    version="0.8.0",
    description="提交前非阻断TAORAN检查、提交后Q33/Q34各50分合计100分评价及简道云回写服务。",
)
_stores: dict[str, AgentStore] = {}
_agents: dict[str, TaoranAgent] = {}
_precheck_locks = [Lock() for _ in range(64)]


def get_store(settings: Settings | None = None) -> AgentStore:
    settings = settings or get_settings()
    if settings.database_path not in _stores:
        _stores[settings.database_path] = AgentStore(settings.database_path)
    return _stores[settings.database_path]


def get_agent(settings: Settings | None = None) -> TaoranAgent:
    settings = settings or get_settings()
    snapshot = load_taoran_knowledge_snapshot(settings.knowledge_snapshot_path)
    key = canonical_hash({
        "settings": settings.model_dump(mode="json"),
        "llm_key_digest": hashlib.sha256(
            (settings.llm_api_key.get_secret_value() if settings.llm_api_key else "").encode()
        ).hexdigest(),
        "knowledge_hash": snapshot.snapshot_hash,
    })
    if key not in _agents:
        _agents[key] = build_agent(settings, snapshot)
    return _agents[key]


def authorize(
    tenant_id: str,
    x_tenant_id: str | None,
    x_api_key: str | None,
) -> None:
    verify_tenant_access(get_settings(), tenant_id, x_tenant_id, x_api_key)


def tenant_mapping(settings: Settings, tenant_id: str) -> dict[str, Any]:
    mapping_path = settings.jiandaoyun_mapping_path_for(tenant_id)
    if settings.tenant_config(tenant_id) is not None and not mapping_path:
        raise HTTPException(status_code=503, detail="tenant Jiandaoyun mapping is not configured")
    return load_jiandaoyun_mapping(mapping_path)


def execute_evaluation(job_id: str, request: PostEvaluationRequest) -> None:
    store = get_store()
    try:
        mapping_path = get_settings().jiandaoyun_mapping_path_for(request.context.tenant_id)
        with use_field_mapping(mapping_path):
            response = get_agent().evaluate(request, job_id)
        try:
            writeback = writeback_evaluation(get_settings(), request, response)
        except JiandaoyunWritebackError as exc:
            writeback = response.writeback.model_copy(
                update={
                    "status": "failed",
                    "target_data_id": (
                        request.writeback_target.data_id if request.writeback_target else None
                    ),
                    "error_message": str(exc),
                    "attempted_at": response.completed_at,
                }
            )
        response = response.model_copy(update={"writeback": writeback})
        store.complete_evaluation(response)
    except Exception as exc:  # noqa: BLE001  # pragma: no cover - job boundary
        store.fail_evaluation(request.context.tenant_id, job_id, type(exc).__name__)


def execute_q40_batch(batch_job_id: str, request: Q40BatchEvaluationRequest) -> None:
    store = get_store()
    items: list[Q40BatchItemResult] = []
    try:
        for evaluation_request in request.evaluations:
            snapshot_hash = canonical_hash(evaluation_request)
            job_id = f"job_{snapshot_hash[:20]}"
            record, created = store.create_evaluation_job(
                job_id, evaluation_request, snapshot_hash
            )
            if created:
                execute_evaluation(job_id, evaluation_request)
                record = store.get_evaluation(request.tenant_id, job_id) or record
                item_status = "completed" if record["status"] == "completed" else "failed"
            elif record["status"] == "completed":
                item_status = "reused"
            elif record["status"] == "failed":
                item_status = "failed"
            else:
                item_status = "pending"
            item_error = record.get("error_message")
            if record.get("response") and record["response"].get("rule_version") != request.required_rule_version:
                item_status = "failed"
                item_error = "旧请求ID关联不同评分量纲，请使用新请求ID重新评价。"
            items.append(
                Q40BatchItemResult(
                    request_id=evaluation_request.context.request_id,
                    visit_record_code=evaluation_request.visit_record_code,
                    job_id=job_id,
                    status=item_status,
                    error_message=item_error,
                )
            )
        failed_count = sum(item.status == "failed" for item in items)
        pending_count = sum(item.status == "pending" for item in items)
        response = Q40BatchResult(
            batch_job_id=batch_job_id,
            tenant_id=request.tenant_id,
            status=(
                "completed_with_errors" if failed_count or pending_count else "completed"
            ),
            requested_count=len(items),
            completed_count=sum(item.status == "completed" for item in items),
            reused_count=sum(item.status == "reused" for item in items),
            failed_count=failed_count,
            pending_count=pending_count,
            items=items,
            required_rule_version=request.required_rule_version,
            completed_at=datetime.now(UTC),
        )
        store.complete_q40_batch(response)
    except Exception as exc:  # noqa: BLE001  # pragma: no cover - batch boundary
        store.fail_q40_batch(request.tenant_id, batch_job_id, type(exc).__name__)


@app.get("/health")
def health() -> dict[str, str]:
    agent = get_agent()
    return {
        "status": "ok",
        "agent": agent.catalog["agent_code"],
        "version": agent.catalog["agent_version"],
    }


@app.get("/api/v1/agent")
def metadata() -> dict:
    return get_agent().catalog


@app.get("/api/v1/connectors/jiandaoyun/mapping")
def jiandaoyun_mapping(
    tenant_id: str = Query(min_length=1),
    x_tenant_id: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> dict:
    authorize(tenant_id, x_tenant_id, x_api_key)
    return tenant_mapping(get_settings(), tenant_id)


@app.get("/api/v1/tenants/{tenant_id}/configuration")
def tenant_configuration(
    tenant_id: str,
    x_tenant_id: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    """Return a secret-free readiness summary for customer onboarding and support."""
    authorize(tenant_id, x_tenant_id, x_api_key)
    settings = get_settings()
    tenant = settings.tenant_config(tenant_id)
    mapping_path = settings.jiandaoyun_mapping_path_for(tenant_id)
    mapping = load_jiandaoyun_mapping(mapping_path) if mapping_path else {}
    return {
        "tenant_id": tenant_id,
        "enabled": tenant.enabled if tenant else True,
        "configuration_source": settings.tenant_configuration_source(tenant_id),
        "access_key_count": len(settings.tenant_access_keys_for(tenant_id)),
        "jiandaoyun": {
            "api_key_configured": bool(settings.jiandaoyun_api_key_for(tenant_id)),
            "webhook_secret_configured": bool(
                settings.jiandaoyun_webhook_secret_for(tenant_id)
            ),
            "mapping_configured": bool(mapping_path),
            "application_id": mapping.get("source_application_id"),
            "entry_id": mapping.get("source_entry_id"),
            "entry_name": mapping.get("source_entry_name"),
            "mapping_version": mapping.get("mapping_version"),
        },
    }


@app.post("/api/v1/visit/checks", response_model=PrecheckResponse)
def create_precheck(
    request: PrecheckRequest,
    x_tenant_id: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> PrecheckResponse:
    authorize(request.context.tenant_id, x_tenant_id, x_api_key)
    lock_index = hash((request.context.tenant_id, request.context.request_id)) % len(
        _precheck_locks
    )
    with _precheck_locks[lock_index]:
        mapping_path = get_settings().jiandaoyun_mapping_path_for(request.context.tenant_id)
        with use_field_mapping(mapping_path):
            return _execute_precheck(request)


def _execute_precheck(request: PrecheckRequest) -> PrecheckResponse:
    store = get_store()
    existing = store.get_precheck_by_request(request.context.tenant_id, request.context.request_id)
    if existing:
        current_hash = canonical_hash(
            {
                "form_revision": request.context.form_revision,
                "source_record_id": request.context.source_record_id,
                "visit": request.visit.model_dump(mode="json"),
            }
        )
        if existing["input_snapshot_hash"] != current_hash:
            raise HTTPException(status_code=409, detail="idempotency key reused with new input")
        return PrecheckResponse.model_validate(existing["response"])
    response = get_agent().precheck(request)
    try:
        store.save_precheck(request, response)
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return response


@app.post("/api/v1/agent/visit/check", response_model=PrecheckResponse)
def invoke_precheck(
    request: PrecheckRequest,
    x_tenant_id: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> PrecheckResponse:
    return create_precheck(request, x_tenant_id, x_api_key)


@app.post("/api/v1/connectors/jiandaoyun/visit/check", response_model=PrecheckResponse)
def jiandaoyun_precheck(
    request: JiandaoyunCheckRequest,
    x_tenant_id: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> PrecheckResponse:
    authorize(request.context.tenant_id, x_tenant_id, x_api_key)
    mapping = tenant_mapping(get_settings(), request.context.tenant_id)
    try:
        canonical_request = adapt_jiandaoyun_request(request, mapping)
    except (FieldTransferError, ValidationError):
        raise HTTPException(
            status_code=422,
            detail="本次检测未完成：字段传递格式异常，请核对本次字段值及子表绑定后重试。",
        ) from None
    return create_precheck(canonical_request, x_tenant_id, x_api_key)


@app.post(
    "/api/v1/connectors/jiandaoyun/visit/button-check",
    response_model=ButtonPrecheckResponse,
)
def jiandaoyun_button_precheck(
    request: dict[str, Any],
    x_tenant_id: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> ButtonPrecheckResponse:
    """简道云“AI检测”按钮：只返回规范建议，不生成或返回正式评分。"""
    if "context" in request and "form_data" in request:
        structured_request = JiandaoyunCheckRequest.model_validate(request)
    else:
        flat_request = dict(request)
        tenant_id = str(flat_request.pop("tenant_id", "")).strip()
        if not tenant_id:
            raise HTTPException(status_code=422, detail="tenant_id is required")
        user_id = str(flat_request.pop("user_id", "jiandaoyun-user")).strip()
        flat_request.pop("request_id", None)
        form_revision = flat_request.pop("form_revision", None)
        source_record_id = flat_request.pop("source_record_id", None)
        structured_request = JiandaoyunCheckRequest.model_validate(
            {
                "context": {
                    "tenant_id": tenant_id,
                    "request_id": f"jdy_button_{uuid4().hex}",
                    "user_id": user_id or "jiandaoyun-user",
                    "source": "jiandaoyun",
                    "form_revision": form_revision,
                    "source_record_id": source_record_id,
                },
                "form_data": flat_request,
            }
        )
    precheck = jiandaoyun_precheck(structured_request, x_tenant_id, x_api_key)
    return ButtonPrecheckResponse.from_precheck(precheck)


@app.get("/api/v1/visit/checks/{check_id}")
def get_precheck_record(
    check_id: str,
    tenant_id: str = Query(min_length=1),
    x_tenant_id: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> dict:
    authorize(tenant_id, x_tenant_id, x_api_key)
    record = get_store().get_precheck(tenant_id, check_id)
    if record is None:
        raise HTTPException(status_code=404, detail="precheck not found")
    return record


@app.post(
    "/api/v1/visit/evaluations",
    response_model=EvaluationAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def submit_evaluation(
    request: PostEvaluationRequest,
    background_tasks: BackgroundTasks,
    x_tenant_id: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> EvaluationAccepted:
    authorize(request.context.tenant_id, x_tenant_id, x_api_key)
    snapshot_hash = canonical_hash(request)
    job_id = f"job_{snapshot_hash[:20]}"
    try:
        record, created = get_store().create_evaluation_job(job_id, request, snapshot_hash)
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if created:
        background_tasks.add_task(execute_evaluation, job_id, request)
    elif record.get("response") and record["response"].get("rule_version") != TOTAL_RULE_VERSION:
        raise HTTPException(status_code=409, detail="旧请求ID关联历史评分量纲，请使用新请求ID重新评价")
    return EvaluationAccepted(
        job_id=job_id,
        trace_id=f"tr_{snapshot_hash[20:40]}",
        status="completed" if record["status"] == "completed" else "queued",
        input_snapshot_hash=snapshot_hash,
    )


@app.post(
    "/api/v1/connectors/jiandaoyun/visit/evaluations",
    response_model=EvaluationAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def submit_jiandaoyun_evaluation(
    request: JiandaoyunEvaluationRequest,
    background_tasks: BackgroundTasks,
    x_tenant_id: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> EvaluationAccepted:
    mapping = tenant_mapping(get_settings(), request.context.tenant_id)
    canonical_request = adapt_jiandaoyun_evaluation_request(request, mapping)
    return submit_evaluation(
        canonical_request,
        background_tasks,
        x_tenant_id,
        x_api_key,
    )


@app.post(
    "/api/v1/connectors/jiandaoyun/visit/submitted",
    response_model=EvaluationAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def submit_jiandaoyun_record_event(
    event: JiandaoyunSubmittedEvent,
    background_tasks: BackgroundTasks,
    x_tenant_id: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> EvaluationAccepted:
    """Read the authoritative submitted record, enqueue evaluation, and write back results."""
    authorize(event.tenant_id, x_tenant_id, x_api_key)
    settings = get_settings()
    mapping = tenant_mapping(settings, event.tenant_id)
    configured_app_id = str(mapping.get("source_application_id", "")).strip()
    configured_entry_id = str(mapping.get("source_entry_id", "")).strip()
    app_id = event.app_id or configured_app_id
    entry_id = event.entry_id or configured_entry_id
    if not app_id or not entry_id:
        raise HTTPException(status_code=500, detail="Jiandaoyun source form is not configured")
    if app_id != configured_app_id or entry_id != configured_entry_id:
        raise HTTPException(status_code=422, detail="event target is not the configured test copy")
    try:
        record = get_jiandaoyun_record(
            settings,
            event.tenant_id,
            app_id,
            entry_id,
            event.data_id,
        )
    except JiandaoyunReadError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return _enqueue_jiandaoyun_record(
        event,
        record,
        mapping,
        background_tasks,
        x_tenant_id,
        x_api_key,
    )


def _enqueue_jiandaoyun_record(
    event: JiandaoyunSubmittedEvent,
    record: dict[str, Any],
    mapping: dict[str, Any],
    background_tasks: BackgroundTasks,
    x_tenant_id: str | None,
    x_api_key: str | None,
) -> EvaluationAccepted:
    app_id = event.app_id or str(mapping.get("source_application_id", "")).strip()
    entry_id = event.entry_id or str(mapping.get("source_entry_id", "")).strip()
    record_fields = mapping.get("record_fields", {})
    visit_record_code = mapped_jiandaoyun_value(
        record,
        record_fields.get("visit_record_code"),
        "visit_record_code",
    )
    if not visit_record_code:
        visit_record_code = event.data_id
    output_widget_ids = {
        str(spec.get("widget_id"))
        for spec in mapping.get("output_fields", {}).values()
        if isinstance(spec, dict) and spec.get("widget_id")
    }
    business_record = {
        key: value
        for key, value in record.items()
        if key not in output_widget_ids and key not in {"updateTime", "updater"}
    }
    revision = canonical_hash(business_record)
    request_id = event.request_id or f"jdy_submit_{event.data_id}_{revision[:16]}"
    if not event.request_id:
        request_id += f"_{TOTAL_RULE_VERSION}"
    settings = get_settings()
    if settings.llm_enabled and not event.request_id:
        analysis_revision = canonical_hash({
            "model": settings.llm_model, "endpoint": settings.llm_api_url,
            "prompt": PROMPT_VERSION,
            "knowledge": load_taoran_knowledge_snapshot(
                settings.knowledge_snapshot_path
            ).snapshot_hash,
        })[:12]
        request_id += f"_llm_{analysis_revision}"
    creator = record.get("creator") if isinstance(record.get("creator"), dict) else {}
    user_id = str(creator.get("username") or event.user_id)
    request = JiandaoyunEvaluationRequest.model_validate(
        {
            "context": {
                "tenant_id": event.tenant_id,
                "request_id": request_id,
                "user_id": user_id,
                "source": "jiandaoyun",
                "form_revision": revision,
                "source_record_id": event.data_id,
            },
            "visit_record_code": str(visit_record_code),
            "form_data": record,
            "writeback_target": {
                "app_id": app_id,
                "entry_id": entry_id,
                "data_id": event.data_id,
            },
        }
    )
    return submit_jiandaoyun_evaluation(
        request,
        background_tasks,
        x_tenant_id,
        x_api_key,
    )


@app.post(
    "/api/v1/connectors/jiandaoyun/visit/webhook",
    status_code=status.HTTP_202_ACCEPTED,
)
async def receive_jiandaoyun_visit_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    tenant_id: str = Query(min_length=1),
    nonce: str = Query(min_length=1),
    timestamp: str = Query(min_length=1),
    x_jdy_signature: str | None = Header(default=None, alias="X-JDY-Signature"),
    x_jdy_deliver_id: str | None = Header(default=None, alias="X-JDY-DeliverId"),
) -> dict[str, Any] | EvaluationAccepted:
    settings = get_settings()
    tenant = settings.tenant_config(tenant_id)
    if tenant is not None and not tenant.enabled:
        raise HTTPException(status_code=403, detail="tenant is disabled")
    tenant_keys = settings.tenant_access_keys_for(tenant_id)
    if settings.has_tenant_access_configuration and not tenant_keys:
        raise HTTPException(status_code=401, detail="unknown tenant")
    secret = settings.jiandaoyun_webhook_secret_for(tenant_id)
    if not secret:
        raise HTTPException(status_code=503, detail="Jiandaoyun webhook secret is not configured")
    payload = await request.body()
    signature_content = b":".join(
        [
            nonce.encode("utf-8"),
            payload,
            secret.encode("utf-8"),
            timestamp.encode("utf-8"),
        ]
    )
    expected_signature = hashlib.sha1(signature_content).hexdigest()
    if not x_jdy_signature or not hmac.compare_digest(
        x_jdy_signature.lower(), expected_signature
    ):
        raise HTTPException(status_code=401, detail="invalid Jiandaoyun webhook signature")
    try:
        body = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="invalid webhook JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="invalid webhook body")
    operation = str(body.get("op", ""))
    record = body.get("data")
    if operation not in {"data_create", "data_update"} or not isinstance(record, dict):
        return {
            "status": "ignored",
            "operation": operation or "connection_test",
            "delivery_id": x_jdy_deliver_id,
        }
    mapping = tenant_mapping(settings, tenant_id)
    configured_app_id = str(mapping.get("source_application_id", "")).strip()
    configured_entry_id = str(mapping.get("source_entry_id", "")).strip()
    app_id = str(record.get("appId") or record.get("app_id") or configured_app_id)
    entry_id = str(record.get("entryId") or record.get("entry_id") or configured_entry_id)
    data_id = str(record.get("_id") or record.get("data_id") or "")
    if not data_id:
        raise HTTPException(status_code=422, detail="webhook data_id is missing")
    if app_id != configured_app_id or entry_id != configured_entry_id:
        raise HTTPException(status_code=422, detail="webhook target is not the test copy")
    event = JiandaoyunSubmittedEvent(
        tenant_id=tenant_id,
        data_id=data_id,
        app_id=app_id,
        entry_id=entry_id,
        user_id="jiandaoyun-webhook",
    )
    # The signed webhook itself authenticates Jiandaoyun; tenant authorization is
    # still enforced internally with the configured tenant key.
    if not tenant_keys:
        raise HTTPException(status_code=401, detail="unknown tenant")
    return _enqueue_jiandaoyun_record(
        event,
        record,
        mapping,
        background_tasks,
        tenant_id,
        tenant_keys[0],
    )


@app.get("/api/v1/visit/evaluations/{job_id}")
def get_evaluation(
    job_id: str,
    tenant_id: str = Query(min_length=1),
    x_tenant_id: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> dict:
    authorize(tenant_id, x_tenant_id, x_api_key)
    record = get_store().get_evaluation(tenant_id, job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="evaluation not found")
    return record


@app.post("/api/v1/visit/evaluations/{job_id}/writeback")
def retry_evaluation_writeback(
    job_id: str,
    tenant_id: str = Query(min_length=1),
    x_tenant_id: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> dict:
    authorize(tenant_id, x_tenant_id, x_api_key)
    store = get_store()
    record = store.get_evaluation(tenant_id, job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="evaluation not found")
    if record["status"] != "completed" or not record["response"]:
        raise HTTPException(status_code=409, detail="evaluation is not completed")
    request = PostEvaluationRequest.model_validate(record["request"])
    if request.writeback_target is None:
        raise HTTPException(status_code=409, detail="writeback target is not configured")
    evaluation = EvaluationResponse.model_validate(record["response"])
    if evaluation.rule_version != TOTAL_RULE_VERSION:
        raise HTTPException(status_code=409, detail="历史200分制结果不可直接回写，请用新请求ID重新评价")
    if (
        evaluation.semantic_facts.provider.startswith("llm-")
        and evaluation.semantic_facts.status != "completed"
    ):
        # A model outage cannot be bypassed by a writeback retry: re-analyze first.
        if not get_settings().llm_enabled:
            raise HTTPException(status_code=409, detail="请先恢复大模型配置，再重试模型复核与回写")
        evaluation = get_agent().evaluate(request, job_id)
    try:
        writeback = writeback_evaluation(get_settings(), request, evaluation)
    except JiandaoyunWritebackError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    evaluation = evaluation.model_copy(update={"writeback": writeback})
    store.complete_evaluation(evaluation)
    return evaluation.model_dump(mode="json")


def authorize_q40(
    tenant_id: str,
    x_service_id: str | None,
    x_service_key: str | None,
) -> None:
    verify_q40_service_access(get_settings(), tenant_id, x_service_id, x_service_key)


@app.get(
    "/api/v1/integrations/q40/compatibility",
    response_model=RuleCompatibilityResponse,
)
def q40_compatibility(
    tenant_id: str = Query(min_length=1),
    required_rule_version: str = Query(min_length=1),
    x_service_id: str | None = Header(default=None),
    x_service_key: str | None = Header(default=None),
) -> RuleCompatibilityResponse:
    authorize_q40(tenant_id, x_service_id, x_service_key)
    return rule_compatibility(required_rule_version)


@app.get(
    "/api/v1/integrations/q40/period-facts",
    response_model=Q40PeriodFactsResponse,
)
def q40_period_facts(
    period_start: date,
    period_end: date,
    tenant_id: str = Query(min_length=1),
    employee_id: str = Query(min_length=1),
    required_rule_version: str = Query(min_length=1),
    expected_visit_record_count: int | None = Query(default=None, ge=0),
    x_service_id: str | None = Header(default=None),
    x_service_key: str | None = Header(default=None),
) -> Q40PeriodFactsResponse:
    authorize_q40(tenant_id, x_service_id, x_service_key)
    if period_end < period_start:
        raise HTTPException(status_code=422, detail="period_end must not be before period_start")
    compatibility = rule_compatibility(required_rule_version)
    if not compatibility.compatible:
        raise HTTPException(status_code=409, detail=compatibility.model_dump(mode="json"))
    records = get_store().list_completed_evaluations(
        tenant_id,
        employee_id,
        period_start.isoformat(),
        period_end.isoformat(),
        required_rule_version,
    )
    return build_period_facts(
        tenant_id,
        employee_id,
        period_start,
        period_end,
        required_rule_version,
        records,
        expected_visit_record_count,
    )


@app.post(
    "/api/v1/integrations/q40/evaluations:batch",
    response_model=Q40BatchAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def submit_q40_batch(
    request: Q40BatchEvaluationRequest,
    background_tasks: BackgroundTasks,
    x_service_id: str | None = Header(default=None),
    x_service_key: str | None = Header(default=None),
) -> Q40BatchAccepted:
    authorize_q40(request.tenant_id, x_service_id, x_service_key)
    compatibility = rule_compatibility(request.required_rule_version)
    if not compatibility.compatible:
        raise HTTPException(status_code=409, detail=compatibility.model_dump(mode="json"))
    snapshot_hash = canonical_hash(request)
    batch_job_id = f"q40batch_{snapshot_hash[:20]}"
    try:
        record, created = get_store().create_q40_batch_job(
            batch_job_id, request, snapshot_hash
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if created:
        background_tasks.add_task(execute_q40_batch, batch_job_id, request)
    current_status = record["status"]
    accepted_status = (
        current_status
        if current_status in {"completed", "completed_with_errors"}
        else "queued"
    )
    return Q40BatchAccepted(
        batch_job_id=batch_job_id,
        status=accepted_status,
        evaluation_count=len(request.evaluations),
        input_snapshot_hash=snapshot_hash,
    )


@app.get("/api/v1/integrations/q40/batches/{batch_job_id}")
def get_q40_batch(
    batch_job_id: str,
    tenant_id: str = Query(min_length=1),
    x_service_id: str | None = Header(default=None),
    x_service_key: str | None = Header(default=None),
) -> dict:
    authorize_q40(tenant_id, x_service_id, x_service_key)
    record = get_store().get_q40_batch(tenant_id, batch_job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="q40 batch not found")
    return record
