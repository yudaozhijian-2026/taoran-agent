from __future__ import annotations

from datetime import UTC, date, datetime
from threading import Lock
from typing import Any
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, status

from .agent import TaoranAgent
from .config import Settings, get_settings
from .connector import (
    adapt_jiandaoyun_evaluation_request,
    adapt_jiandaoyun_request,
    load_jiandaoyun_mapping,
)
from .gateway import verify_q40_service_access, verify_tenant_access
from .models import (
    EvaluationAccepted,
    EvaluationResponse,
    JiandaoyunCheckRequest,
    JiandaoyunEvaluationRequest,
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
from .semantic import HeuristicSemanticReviewer, HttpSemanticReviewer
from .storage import AgentStore, IdempotencyConflictError
from .writeback import JiandaoyunWritebackError, writeback_evaluation

app = FastAPI(
    title="DSM TAORAN 拜访智能体",
    version="0.3.0",
    description="提交前非阻断TAORAN检查、提交后Q33/Q34双百分制评价及简道云回写服务。",
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
    key = settings.semantic_endpoint or "heuristic"
    if key not in _agents:
        reviewer = (
            HttpSemanticReviewer(
                settings.semantic_endpoint,
                settings.semantic_api_key,
                settings.semantic_timeout_seconds,
            )
            if settings.semantic_endpoint
            else HeuristicSemanticReviewer()
        )
        _agents[key] = TaoranAgent(reviewer)
    return _agents[key]


def authorize(
    tenant_id: str,
    x_tenant_id: str | None,
    x_api_key: str | None,
) -> None:
    verify_tenant_access(get_settings(), tenant_id, x_tenant_id, x_api_key)


def execute_evaluation(job_id: str, request: PostEvaluationRequest) -> None:
    store = get_store()
    try:
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
            items.append(
                Q40BatchItemResult(
                    request_id=evaluation_request.context.request_id,
                    visit_record_code=evaluation_request.visit_record_code,
                    job_id=job_id,
                    status=item_status,
                    error_message=record.get("error_message"),
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
    return load_jiandaoyun_mapping(get_settings().jiandaoyun_mapping_path)


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
    mapping = load_jiandaoyun_mapping(get_settings().jiandaoyun_mapping_path)
    canonical_request = adapt_jiandaoyun_request(request, mapping)
    return create_precheck(canonical_request, x_tenant_id, x_api_key)


@app.post("/api/v1/connectors/jiandaoyun/visit/button-check", response_model=PrecheckResponse)
def jiandaoyun_button_precheck(
    request: dict[str, Any],
    x_tenant_id: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> PrecheckResponse:
    """简道云“AI检测”按钮专用别名；结果永不阻断提交。"""
    if "context" in request and "form_data" in request:
        structured_request = JiandaoyunCheckRequest.model_validate(request)
    else:
        flat_request = dict(request)
        tenant_id = str(flat_request.pop("tenant_id", "")).strip()
        if not tenant_id:
            raise HTTPException(status_code=422, detail="tenant_id is required")
        user_id = str(flat_request.pop("user_id", "jiandaoyun-user")).strip()
        request_id = str(flat_request.pop("request_id", "")).strip()
        form_revision = flat_request.pop("form_revision", None)
        source_record_id = flat_request.pop("source_record_id", None)
        structured_request = JiandaoyunCheckRequest.model_validate(
            {
                "context": {
                    "tenant_id": tenant_id,
                    "request_id": request_id or f"jdy_button_{uuid4().hex}",
                    "user_id": user_id or "jiandaoyun-user",
                    "source": "jiandaoyun",
                    "form_revision": form_revision,
                    "source_record_id": source_record_id,
                },
                "form_data": flat_request,
            }
        )
    return jiandaoyun_precheck(structured_request, x_tenant_id, x_api_key)


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
    mapping = load_jiandaoyun_mapping(get_settings().jiandaoyun_mapping_path)
    canonical_request = adapt_jiandaoyun_evaluation_request(request, mapping)
    return submit_evaluation(
        canonical_request,
        background_tasks,
        x_tenant_id,
        x_api_key,
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
