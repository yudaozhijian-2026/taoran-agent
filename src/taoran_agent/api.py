from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
from collections.abc import AsyncIterator, Callable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import asynccontextmanager, nullcontext
from datetime import UTC, date, datetime
from importlib.resources import files
from pathlib import Path
from queue import Empty, Queue
from threading import Lock, RLock
from time import monotonic
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from pydantic import ValidationError

from . import __version__
from .agent import TaoranAgent
from .client_timings import ClientTimings
from .config import Settings, get_settings
from .connector import (
    FieldTransferError,
    adapt_jiandaoyun_evaluation_request,
    adapt_jiandaoyun_request,
    load_jiandaoyun_mapping,
    mapped_jiandaoyun_value,
)
from .evaluation_operations import router as evaluation_operations_router
from .experimental_attribution import attribution_hints
from .experimental_final_consistency import pending_finding_codes
from .experimental_quick_check_transport import ExperimentalQuickCheckNoStore
from .experimental_rendering_binding import experimental_retry_allowed
from .experimental_semantic_streaming import stream_semantic_preview
from .experimental_semantic_streaming_v21 import stream_semantic_preview_v21
from .experimental_semantic_streaming_v22 import stream_semantic_preview_v22
from .feedback import (
    build_front_ai_suggestions,
    build_front_ai_suggestions_with_model,
    merge_evaluation_with_knowledge,
)
from .field_labels import display_field_name, use_field_mapping
from .gateway import verify_admin_access, verify_q40_service_access, verify_tenant_access
from .jiandaoyun_api import (
    JiandaoyunReadError,
    find_jiandaoyun_record_by_field,
    get_jiandaoyun_record,
)
from .knowledge import KnowledgeApiClient, TaoranKnowledgeSnapshot, load_taoran_knowledge_snapshot
from .llm import (
    KNOWLEDGE_WORDING_PROMPT_VERSION,
    PROMPT_VERSION,
    ChatModelReviewer,
    _fallback_front_analysis_sections,
    _wording_format_failure,
)
from .models import (
    EvaluationAccepted,
    EvaluationResponse,
    FeedbackMode,
    FrontVisitAnalysisSection,
    Issue,
    JiandaoyunCheckRequest,
    JiandaoyunEvaluationRequest,
    JiandaoyunSubmittedEvent,
    KnowledgeWordingResult,
    PostEvaluationRequest,
    PrecheckRequest,
    PrecheckResponse,
    Q40BatchAccepted,
    Q40BatchEvaluationRequest,
    Q40BatchItemResult,
    Q40BatchResult,
    Q40PeriodFactsResponse,
    RuleCompatibilityResponse,
    SemanticReview,
    Severity,
    UnifiedButtonPrecheckResponse,
    WritebackResult,
)
from .post_review_policy import POLICY_VERSION
from .precheck_engine import TaoranPrecheckEngine
from .purpose_mapping import (
    PurposeMappingError,
    purpose_mapping_record,
    purpose_policy_for_visit,
    structure_purpose_mapping,
)
from .q40_integration import build_period_facts, rule_compatibility
from .rules import canonical_hash, normalized_text
from .runtime import build_agent
from .scoring_contract import TOTAL_RULE_VERSION
from .semantic import SemanticReviewer
from .source_revision import analysis_input_revision, business_revision, source_lock
from .storage import AgentStore, IdempotencyConflictError
from .tenant_admin import (
    JiandaoyunAuthorizationRequest,
    JiandaoyunAuthorizationResponse,
    JiandaoyunSchemaSyncError,
    TenantAccessKeyRotationResult,
    TenantFieldConfirmationRequest,
    TenantOnboardingRequest,
    TenantOnboardingResult,
    TenantWebhookSecretRotationResult,
    confirm_tenant_fields,
    discover_authorized_forms,
    discover_tenant_authorized_forms,
    list_tenants,
    onboard_tenant,
    rotate_tenant_access_key,
    rotate_tenant_webhook_secret,
)
from .writeback import JiandaoyunWritebackError, writeback_evaluation


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    prewarm_runtime()
    recover_background_jobs()
    from .evaluation_operations import recover_operations
    recover_operations()
    yield

app = FastAPI(
    title="DSM TAORAN 拜访智能体",
    version=__version__,
    description="提交前支持当前表单快照的交互式Quick Check；提交后保持Q33/Q34各50分合计100分评价及简道云单字段回写服务。",
    lifespan=lifespan,
)
_stores: dict[str, AgentStore] = {}
_agents: dict[str, TaoranAgent] = {}
_precheck_locks = [Lock() for _ in range(64)]
app.add_middleware(ExperimentalQuickCheckNoStore)

_live_knowledge_cache_lock = RLock()
_live_knowledge_cache: dict[str, tuple[float, TaoranKnowledgeSnapshot]] = {}
_live_knowledge_refresh_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="taoran-knowledge-refresh",
)
_live_knowledge_inflight: dict[str, Future[TaoranKnowledgeSnapshot]] = {}
_knowledge_semantic_cache_lock = Lock()
_knowledge_semantic_cache: dict[str, tuple[float, KnowledgeWordingResult]] = {}
_FRONT_WORDING_ARTIFACT_TYPE = "front_ai_hybrid_semantic_v2"

# 0.27.0 formal interactive Quick Check.  This state intentionally does not
# share the experimental V2 task namespace or its launch credentials.  The
# input draft lives only in the worker closure until completion; task metadata
# keeps hashes and final text, never the full unsaved form snapshot.
_quick_check_lock = RLock()
_quick_check_tasks: dict[str, dict[str, Any]] = {}
_quick_check_idempotency: dict[tuple[str, str, str, str], str] = {}
_quick_check_executor = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="taoran-quick-check",
)
_quick_check_final_executor = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="taoran-quick-check-final",
)
_quick_check_preview_executor = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="taoran-quick-check-preview",
)

# Experimental Streaming PoC only. This state never participates in the
# formal button endpoint, submitted evaluation, or Jiandaoyun writeback.
_EXPERIMENTAL_STREAMING_TTL_SECONDS = 120.0
_EXPERIMENTAL_JDY_RECORD_LAUNCH_TTL_SECONDS = 60.0
_EXPERIMENTAL_JDY_PLUGIN_ORIGIN = "https://www.jiandaoyun.com"
_experimental_streaming_lock = Lock()
_experimental_streaming_tasks: dict[str, dict[str, Any]] = {}
_experimental_jdy_record_launches: dict[str, dict[str, Any]] = {}
_experimental_streaming_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="taoran-experimental-streaming",
)
# Semantic Streaming V2 is intentionally a separate experimental task space.
# It never shares a token, route or task object with the frozen Stage Streaming
# PoC above, so V1 remains a reproducible control group.
_EXPERIMENTAL_SEMANTIC_STREAMING_TTL_SECONDS = 180.0
_experimental_semantic_streaming_lock = Lock()
_experimental_semantic_streaming_tasks: dict[str, dict[str, Any]] = {}
_experimental_semantic_record_launches: dict[str, dict[str, Any]] = {}
_experimental_semantic_streaming_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="taoran-experimental-semantic-streaming",
)
# V2.1 is a separate, frozen-from-V2 experiment.  Its model supplies only
# analysis and hints; evidence remains program-owned.
_EXPERIMENTAL_SEMANTIC_V21_TTL_SECONDS = 180.0
_experimental_semantic_v21_lock = Lock()
_experimental_semantic_v21_tasks: dict[str, dict[str, Any]] = {}
_experimental_semantic_v21_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="taoran-experimental-semantic-v21",
)
_EXPERIMENTAL_SEMANTIC_V22_TTL_SECONDS = 180.0
_EXPERIMENTAL_SEMANTIC_V22_REVIEW_LAUNCH_TTL_SECONDS = 600.0
# This is deliberately isolated from the formal mapping.  The V2.2 review
# button lives on the AI test form and uses its own current-record identifier.
_EXPERIMENTAL_SEMANTIC_V22_JDY_VISIT_CODE_WIDGET_ID = "_widget_1604739499351"
_EXPERIMENTAL_SEMANTIC_V22_JDY_VISIT_CODE_WIDGET_TYPE = "text"
_EXPERIMENTAL_SEMANTIC_V22_PUBLIC_BASE_URL = "https://dsm.yudaozhijian.top"
_experimental_semantic_v22_lock = Lock()
_experimental_semantic_v22_tasks: dict[str, dict[str, Any]] = {}
_experimental_semantic_v22_review_launches: dict[str, dict[str, Any]] = {}
_experimental_semantic_v22_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="taoran-experimental-semantic-v22",
)
_experimental_semantic_v22_review_lock = Lock()
_EXPERIMENTAL_GOLDEN_CASES = {
    "case_1": ("BFJL2026082500007", "6a8d4ef65a7c15a975c59f38"),
    "case_2": ("BFJL2026082500010", "6a8d4ef65a7c15a975c59f3b"),
    "case_3": ("BFJL2026082500015", "6a8d4ef65a7c15a975c59f42"),
}
_EXPERIMENTAL_SEMANTIC_V22_EXPANSION_CASES = {
    "expansion_01": ("BFJL2026081900001", "6a851bd917936e24c254f9f5"),
    "expansion_02": ("BFJL2026081900002", "6a856b76ed684b3f7b436998"),
    "expansion_03": ("BFJL2026082500003", "6a8d4ef65a7c15a975c59f34"),
    "expansion_04": ("BFJL2026082500004", "6a8d4ef65a7c15a975c59f35"),
    "expansion_05": ("BFJL2026082500005", "6a8d4ef65a7c15a975c59f36"),
    "expansion_06": ("BFJL2026082500007", "6a8d4ef65a7c15a975c59f38"),
    "expansion_07": ("BFJL2026082500010", "6a8d4ef65a7c15a975c59f3b"),
    "expansion_08": ("BFJL2026082500015", "6a8d4ef65a7c15a975c59f42"),
    "expansion_09": ("BFJL2026082500016", "6a8d4ef65a7c15a975c59f43"),
    "expansion_10": ("BFJL2026082500017", "6a8d4ef65a7c15a975c59f44"),
    "expansion_11": ("BFJL2026082500018", "6a8d4ef65a7c15a975c59f45"),
    "expansion_12": ("BFJL2026082500019", "6a8d4ef65a7c15a975c59f46"),
    "expansion_13": ("BFJL2026082500023", "6a8d4ef65a7c15a975c59f4a"),
    "expansion_14": ("BFJL2026082500026", "6a8d4ef65a7c15a975c59f4d"),
    "expansion_15": ("BFJL2026082500028", "6a8d4ef65a7c15a975c59f4f"),
}

_FRONT_OBVIOUS_VAGUE = {
    "了解一下", "了解需求", "沟通一下", "客户有兴趣", "客户认可方案",
    "继续跟进", "保持联系", "维护关系", "推进项目", "待定", "不知道", "无内容",
}
_FRONT_ACTORS = ("客户", "院方", "校方", "对方", "负责人", "主任", "经理", "采购", "技术", "决策人")
_FRONT_ACTIONS = ("确认", "同意", "认可", "提供", "决定", "承诺", "完成", "提出", "要求", "拒绝")
_FRONT_DETAILS = (
    "日期", "时间", "预算", "数量", "名单", "方案", "清单", "范围", "角色", "材料",
    "条件", "异议", "交付", "会议", "验证", "合同", "报价",
)
_FRONT_JUDGMENTS = ("我认为", "我感觉", "应该", "估计", "可能", "大概", "沟通顺利", "非常满意")

_FRONT_SPECIFICITY_ITEM_RULES = (
    (
        "O_KR",
        "想取得的关键结果",
        "expected_key_result",
        {"TAORAN_KR_MISSING", "TAORAN_KR_NOT_VERIFIABLE", "KR_SEMANTICALLY_VAGUE"},
        {"TAORAN_KR_MISSING"},
    ),
    (
        "R",
        "过程详细描述",
        "process_description",
        {
            "TAORAN_RESULT_MISSING",
            "TAORAN_RESULT_NOT_FACT_BASED",
            "TAORAN_FACT_JUDGMENT_MIXED",
            "RESULT_LACKS_CUSTOMER_FACTS",
        },
        {"TAORAN_RESULT_MISSING"},
    ),
    (
        "N",
        "下次拜访期望的关键结果",
        "next_action_expected_result",
        {
            "TAORAN_NSA_RESULT_MISSING",
            "TAORAN_NSA_RESULT_NOT_ACTIONABLE",
            "NEXT_ACTION_NOT_QUALIFIED",
        },
        {"TAORAN_NSA_RESULT_MISSING"},
    ),
)
_background_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="taoran-background")
_recovery_lock = Lock()
_recovery_started = False
_monitoring_lock = Lock()
_pipeline_metrics: dict[str, dict[str, int]] = {}
_knowledge_snapshot_state: dict[str, Any] = {
    "status": "not_loaded",
    "source": None,
    "snapshot_hash": None,
    "age_ms": None,
    "last_refresh_at": None,
    "last_error": None,
}
_prewarm_state_lock = Lock()
_prewarm_state: dict[str, Any] = {
    "status": "pending",
    "latency_ms": 0,
    "components": {},
    "failure_reasons": [],
}
_logger = logging.getLogger("taoran_agent.prewarm")
_ADMIN_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


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


def _set_prewarm_state(state: dict[str, Any]) -> dict[str, Any]:
    with _prewarm_state_lock:
        _prewarm_state.clear()
        _prewarm_state.update(state)
        return dict(_prewarm_state)


def _reset_prewarm_state() -> None:
    _set_prewarm_state({
        "status": "pending",
        "latency_ms": 0,
        "components": {},
        "failure_reasons": [],
    })


def _observe_pipeline(name: str, phases: dict[str, int], *, success: bool) -> None:
    with _monitoring_lock:
        metric = _pipeline_metrics.setdefault(
            name,
            {"count": 0, "success_count": 0, "failure_count": 0},
        )
        metric["count"] += 1
        metric["success_count" if success else "failure_count"] += 1
        for phase, latency in phases.items():
            metric[f"{phase}_total_ms"] = metric.get(f"{phase}_total_ms", 0) + max(0, latency)
            metric[f"{phase}_max_ms"] = max(metric.get(f"{phase}_max_ms", 0), max(0, latency))


def _monitoring_snapshot() -> dict[str, Any]:
    with _monitoring_lock:
        pipelines = {name: dict(values) for name, values in _pipeline_metrics.items()}
    with _live_knowledge_cache_lock:
        knowledge = dict(_knowledge_snapshot_state)
        cached = next(iter(_live_knowledge_cache.values()), None)
        if cached:
            knowledge["age_ms"] = int((monotonic() - cached[0]) * 1000)
            knowledge["fresh"] = knowledge["age_ms"] <= get_settings().knowledge_snapshot_cache_seconds * 1000
            knowledge["usable"] = knowledge["age_ms"] <= get_settings().knowledge_snapshot_stale_seconds * 1000
        else:
            knowledge.update({"fresh": False, "usable": False})
    return {"pipelines": pipelines, "knowledge_snapshot": knowledge}


def _prewarm_component(
    name: str,
    action: Callable[[], Any],
    components: dict[str, dict[str, Any]],
    failure_reasons: list[str],
) -> Any | None:
    started = monotonic()
    try:
        value = action()
        components[name] = {
            "status": "ready",
            "latency_ms": round((monotonic() - started) * 1000),
        }
        return value
    except Exception as exc:  # noqa: BLE001 - startup must remain available
        reason = f"{name}:{type(exc).__name__}"
        components[name] = {
            "status": "failed",
            "latency_ms": round((monotonic() - started) * 1000),
            "reason": reason,
        }
        failure_reasons.append(reason)
        return None


def prewarm_runtime(settings: Settings | None = None) -> dict[str, Any]:
    """Warm reusable runtime resources before the service accepts user traffic."""
    settings = settings or get_settings()
    if not settings.startup_prewarm_enabled:
        return _set_prewarm_state({
            "status": "disabled",
            "latency_ms": 0,
            "components": {},
            "failure_reasons": [],
        })

    started = monotonic()
    components: dict[str, dict[str, Any]] = {}
    failure_reasons: list[str] = []
    _set_prewarm_state({
        "status": "running",
        "latency_ms": 0,
        "components": {},
        "failure_reasons": [],
    })

    _prewarm_component("store", lambda: get_store(settings), components, failure_reasons)
    agent = _prewarm_component(
        "rule_engine",
        lambda: get_agent(settings),
        components,
        failure_reasons,
    )
    if settings.llm_enabled:
        model_ready = bool(
            agent is not None and isinstance(agent.semantic_reviewer, ChatModelReviewer)
        )
        if model_ready:
            components["model_client"] = {"status": "ready", "latency_ms": 0}
        else:
            reason = "model_client:InitializationError"
            components["model_client"] = {
                "status": "failed",
                "latency_ms": 0,
                "reason": reason,
            }
            failure_reasons.append(reason)
    else:
        components["model_client"] = {"status": "skipped", "latency_ms": 0}

    mapping_paths = {settings.jiandaoyun_mapping_path}
    mapping_paths.update(
        tenant.jiandaoyun.mapping_path
        for tenant in settings.tenant_registry.tenants.values()
        if tenant.enabled
    )
    _prewarm_component(
        "field_mappings",
        lambda: [load_jiandaoyun_mapping(path) for path in mapping_paths],
        components,
        failure_reasons,
    )

    if settings.knowledge_api_key is None:
        components["knowledge_snapshot"] = {"status": "skipped", "latency_ms": 0}
    else:
        _prewarm_component(
            "knowledge_snapshot",
            lambda: _fetch_live_knowledge_snapshot(
                settings,
                settings.startup_prewarm_timeout_seconds,
            ),
            components,
            failure_reasons,
        )

    state = _set_prewarm_state({
        "status": "degraded" if failure_reasons else "ready",
        "latency_ms": round((monotonic() - started) * 1000),
        "components": components,
        "failure_reasons": failure_reasons,
    })
    _logger.info(
        "startup_prewarm status=%s latency_ms=%s components=%s failures=%s",
        state["status"],
        state["latency_ms"],
        ",".join(f"{name}:{value['status']}" for name, value in components.items()),
        ",".join(failure_reasons) or "none",
    )
    return state


def _fetch_live_knowledge_snapshot(
    settings: Settings,
    timeout_seconds: float,
) -> TaoranKnowledgeSnapshot:
    """短时缓存并合并同时取数，避免多人点击对知识API发起重复请求。"""
    from .content_cache import knowledge_basis, pinned_snapshot
    if knowledge_basis.get() is not None:
        return pinned_snapshot('live')
    secret = settings.knowledge_api_key
    if secret is None:
        raise ValueError("knowledge_api_not_configured")
    cache_key = hashlib.sha256(
        (
            settings.knowledge_api_base_url
            + "\0"
            + secret.get_secret_value()
        ).encode()
    ).hexdigest()
    now = monotonic()
    with _live_knowledge_cache_lock:
        cached = _live_knowledge_cache.get(cache_key)
        if cached and now - cached[0] <= settings.knowledge_snapshot_cache_seconds:
            _knowledge_snapshot_state.update({
                "status": "ready",
                "source": "cache",
                "snapshot_hash": cached[1].snapshot_hash,
                "last_error": None,
            })
            return cached[1]
        future = _live_knowledge_inflight.get(cache_key)
        # A stale last-known-good snapshot remains valid while one background
        # refresh runs. User requests never wait behind the refresh lock.
        stale = (
            cached
            if cached and now - cached[0] <= settings.knowledge_snapshot_stale_seconds
            else None
        )
        if future is None:
            refresh_timeout = (
                settings.knowledge_timeout_seconds if stale is not None else timeout_seconds
            )

            def refresh() -> TaoranKnowledgeSnapshot:
                try:
                    snapshot = KnowledgeApiClient(
                        settings.knowledge_api_base_url,
                        secret.get_secret_value(),
                        refresh_timeout,
                    ).fetch_taoran_snapshot()
                    with _live_knowledge_cache_lock:
                        _live_knowledge_cache[cache_key] = (monotonic(), snapshot)
                        _knowledge_snapshot_state.update({
                            "status": "ready",
                            "source": "remote_api",
                            "snapshot_hash": snapshot.snapshot_hash,
                            "last_refresh_at": datetime.now(UTC).isoformat(),
                            "last_error": None,
                        })
                    return snapshot
                except Exception as exc:
                    with _live_knowledge_cache_lock:
                        _knowledge_snapshot_state.update({
                            "status": "stale" if stale is not None else "unavailable",
                            "source": "stale_cache" if stale is not None else "remote_api",
                            "last_error": type(exc).__name__,
                        })
                    raise

            future = _live_knowledge_refresh_executor.submit(refresh)
            _live_knowledge_inflight[cache_key] = future

            def clear_inflight(done: Future[TaoranKnowledgeSnapshot]) -> None:
                with _live_knowledge_cache_lock:
                    if _live_knowledge_inflight.get(cache_key) is done:
                        _live_knowledge_inflight.pop(cache_key, None)

            future.add_done_callback(clear_inflight)
        if stale is not None:
            _knowledge_snapshot_state.update({
                "status": "refreshing",
                "source": "stale_cache",
                "snapshot_hash": stale[1].snapshot_hash,
            })
            return stale[1]
    try:
        return future.result(timeout=timeout_seconds)
    except FutureTimeout as exc:
        raise TimeoutError("knowledge_snapshot_timeout") from exc


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
    started = monotonic()
    phases: dict[str, int] = {}
    persisted = store.get_evaluation(request.context.tenant_id, job_id) or {}
    store.mark_evaluation_running(request.context.tenant_id, job_id)
    try:
        saved = persisted.get("response") or {}
        if persisted.get("status") in {"queued", "running"} and (saved.get("semantic_facts") or {}).get("status") == "completed":
            response = EvaluationResponse.model_validate(saved)
            phases.update(response.phase_latency_ms)
        else:
            mapping_path = get_settings().jiandaoyun_mapping_path_for(request.context.tenant_id)
            evaluation_started = monotonic()
            with use_field_mapping(mapping_path):
                response = get_agent().evaluate(request, job_id)
            phases["formal_evaluation"] = int((monotonic() - evaluation_started) * 1000)
            phases["model"] = response.semantic_facts.latency_ms
            post_feedback_request = PrecheckRequest(
                context=request.context.model_copy(
                    update={"request_id": f"{request.context.request_id}__post_feedback"}
                ),
                visit=request.visit,
                feedback_mode=FeedbackMode.RULE,
            )
            knowledge_started = monotonic()
            unified_feedback = _execute_post_submit_rule_enrichment(
                post_feedback_request,
                get_settings(),
            )
            phases["knowledge_and_rules"] = int((monotonic() - knowledge_started) * 1000)
            response = response.model_copy(
                update={
                    "ai_opinion": merge_evaluation_with_knowledge(
                        request.visit,
                        response.q33_score,
                        response.q34_score,
                        response.total_score,
                        response.issues,
                        response.semantic_facts,
                        unified_feedback,
                    ),
                }
            )
        if response.semantic_facts.status == "completed":
            response = response.model_copy(update={"phase_latency_ms": phases})
            store.checkpoint_evaluation(response)
        writeback_started = monotonic()
        try:
            writeback = writeback_evaluation(get_settings(), request, response, store=store)
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
        phases["writeback"] = int((monotonic() - writeback_started) * 1000)
        phases["total"] = int((monotonic() - started) * 1000)
        response = response.model_copy(
            update={"writeback": writeback, "phase_latency_ms": phases}
        )
        store.complete_evaluation(response)
        _observe_pipeline("post_submit", phases, success=(
            response.semantic_facts.status == "completed" and writeback.status == "succeeded"
        ))
        _observe_pipeline("post_generation", phases, success=response.semantic_facts.status == "completed")
        _observe_pipeline("post_writeback", {"writeback": phases["writeback"]}, success=writeback.status == "succeeded")
    except Exception as exc:  # noqa: BLE001  # pragma: no cover - job boundary
        from .post_quality import PostFeedbackConflict
        from .post_review_policy import PostInputNotReceived
        error = str(exc) if isinstance(exc, PostInputNotReceived) else type(exc).__name__
        if isinstance(exc, PostFeedbackConflict):
            from .model_failure_evidence import save_failure_evidence
            evidence_id = save_failure_evidence(get_settings(), stage="post_final", candidate=None,
                details={**exc.details, "job_id":job_id, "tenant_id":request.context.tenant_id})
            error = "POST_FEEDBACK_CONFLICT:" + (evidence_id or "diagnostic_save_failed")
        store.fail_evaluation(request.context.tenant_id, job_id, error)
        phases["total"] = int((monotonic() - started) * 1000)
        _observe_pipeline("post_submit", phases, success=False)


def _execute_post_submit_rule_enrichment(
    request: PrecheckRequest,
    settings: Settings,
) -> PrecheckResponse:
    """Reuse structured knowledge and rules after submit without a second model call."""
    live_request, live_snapshot = _with_live_purpose_policy(
        request,
        settings,
        fetch_timeout_seconds=settings.knowledge_fetch_budget_seconds,
    )
    if live_snapshot is not None:
        return _execute_live_enrichment(live_request, settings, live_snapshot)
    local_snapshot = load_taoran_knowledge_snapshot(settings.knowledge_snapshot_path)
    local_request, _ = _with_live_purpose_policy(request, settings, snapshot=local_snapshot)
    return _execute_precheck_with_agent(
        local_request,
        TaoranAgent(precheck_engine=TaoranPrecheckEngine(local_snapshot)),
    )


def _submit_evaluation_job(job_id: str, request: PostEvaluationRequest) -> None:
    _background_executor.submit(execute_evaluation, job_id, request)


def recover_background_jobs(settings: Settings | None = None) -> dict[str, int]:
    """Resume queued or interrupted durable jobs after service restart."""
    global _recovery_started
    with _recovery_lock:
        if _recovery_started:
            return {"evaluations": 0, "q40_batches": 0}
        _recovery_started = True
    store = get_store(settings)
    evaluations = store.recoverable_evaluations()
    batches = store.recoverable_q40_batches()
    for record in evaluations:
        request = PostEvaluationRequest.model_validate(record["request"])
        _submit_evaluation_job(record["job_id"], request)
    for record in batches:
        request = Q40BatchEvaluationRequest.model_validate(record["request"])
        _background_executor.submit(execute_q40_batch, record["batch_job_id"], request)
    return {"evaluations": len(evaluations), "q40_batches": len(batches)}


def execute_q40_batch(batch_job_id: str, request: Q40BatchEvaluationRequest) -> None:
    store = get_store()
    store.mark_q40_batch_running(request.tenant_id, batch_job_id)
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
def health() -> dict[str, Any]:
    agent = get_agent()
    with _prewarm_state_lock:
        prewarm = {
            **_prewarm_state,
            "components": dict(_prewarm_state["components"]),
            "failure_reasons": list(_prewarm_state["failure_reasons"]),
        }
    monitoring = _monitoring_snapshot()
    if isinstance(agent.semantic_reviewer, ChatModelReviewer):
        monitoring["model_capacity"] = agent.semantic_reviewer.model_capacity.snapshot()
    return {
        "status": "ok",
        "agent": agent.catalog["agent_code"],
        "version": agent.catalog["agent_version"],
        "release_version": __version__,
        "prewarm": prewarm,
        "monitoring": monitoring,
    }


@app.get("/api/v1/agent")
def metadata() -> dict:
    return get_agent().catalog


@app.get("/admin/tenants", response_class=HTMLResponse, include_in_schema=False)
def tenant_admin_page() -> HTMLResponse:
    settings = get_settings()
    if not settings.admin_enabled:
        raise HTTPException(status_code=404, detail="Not Found")
    resource = files("taoran_agent.admin_ui").joinpath("index.html")
    return HTMLResponse(resource.read_text(encoding="utf-8"), headers=_ADMIN_SECURITY_HEADERS)


@app.get("/admin/assets/{asset_name}", include_in_schema=False)
def tenant_admin_asset(asset_name: str) -> FileResponse:
    settings = get_settings()
    if not settings.admin_enabled or asset_name not in {"admin.css", "admin.js"}:
        raise HTTPException(status_code=404, detail="Not Found")
    resource = files("taoran_agent.admin_ui").joinpath(asset_name)
    return FileResponse(str(resource), headers=_ADMIN_SECURITY_HEADERS)


@app.get("/api/v1/admin/status")
def tenant_admin_status(
    x_admin_key: str | None = Header(default=None),
) -> dict[str, Any]:
    settings = get_settings()
    verify_admin_access(settings, x_admin_key)
    return {
        "status": "ok",
        "version": app.version,
        "tenant_count": len(list_tenants(settings)),
    }


@app.get("/api/v1/admin/tenants")
def tenant_admin_list(
    x_admin_key: str | None = Header(default=None),
) -> dict[str, Any]:
    settings = get_settings()
    verify_admin_access(settings, x_admin_key)
    store = get_store(settings)
    tenants = []
    for tenant in list_tenants(settings):
        activity = store.tenant_runtime_activity(tenant["tenant_id"])
        tenants.append(
            {
                **tenant,
                "deployment_state": _deployment_state(tenant, activity),
                "activity": activity,
            }
        )
    return {"tenants": tenants}


@app.get("/api/v1/admin/runtime-status")
def tenant_admin_runtime_status(
    x_admin_key: str | None = Header(default=None),
) -> dict[str, Any]:
    settings = get_settings()
    verify_admin_access(settings, x_admin_key)
    store = get_store(settings)
    tenants = []
    for tenant in list_tenants(settings):
        activity = store.tenant_runtime_activity(tenant["tenant_id"])
        tenants.append(
            {
                **tenant,
                "deployment_state": _deployment_state(tenant, activity),
                "activity": activity,
            }
        )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "system": {
            "service_status": "ok",
            "version": app.version,
            "llm_enabled": settings.llm_enabled,
            "llm_model": settings.llm_model if settings.llm_enabled else None,
            "knowledge_api_configured": bool(settings.knowledge_api_key),
            "monitoring": _monitoring_snapshot(),
        },
        "tenants": tenants,
    }


def _deployment_state(tenant: dict[str, Any], activity: dict[str, Any]) -> str:
    if not tenant["enabled"]:
        return "configuration_pending"
    if activity["precheck"]["total_count"] == 0:
        return "awaiting_plugin_test"
    evaluation = activity["evaluation"]
    if evaluation["total_count"] == 0:
        return "awaiting_submission_test"
    latest = evaluation["latest"] or {}
    if latest.get("status") not in {"completed", "failed"}:
        return "evaluation_running"
    if latest.get("status") == "failed":
        return "evaluation_failed"
    if latest.get("writeback_status") != "succeeded":
        return "writeback_attention"
    return "operational"


@app.post(
    "/api/v1/admin/jiandaoyun/authorization",
    response_model=JiandaoyunAuthorizationResponse,
)
def tenant_admin_discover_jiandaoyun(
    request: JiandaoyunAuthorizationRequest,
    x_admin_key: str | None = Header(default=None),
) -> JiandaoyunAuthorizationResponse:
    settings = get_settings()
    verify_admin_access(settings, x_admin_key)
    try:
        return discover_authorized_forms(settings, request)
    except JiandaoyunSchemaSyncError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"简道云连接或授权信息读取失败：{exc}",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get(
    "/api/v1/admin/tenants/{tenant_id}/jiandaoyun/authorization",
    response_model=JiandaoyunAuthorizationResponse,
)
def tenant_admin_discover_existing_jiandaoyun(
    tenant_id: str,
    x_admin_key: str | None = Header(default=None),
) -> JiandaoyunAuthorizationResponse:
    settings = get_settings()
    verify_admin_access(settings, x_admin_key)
    try:
        return discover_tenant_authorized_forms(settings, tenant_id)
    except JiandaoyunSchemaSyncError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"简道云连接或授权信息读取失败：{exc}",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/v1/admin/tenants", response_model=TenantOnboardingResult)
def tenant_admin_save(
    request: TenantOnboardingRequest,
    x_admin_key: str | None = Header(default=None),
) -> TenantOnboardingResult:
    settings = get_settings()
    verify_admin_access(settings, x_admin_key)
    try:
        return onboard_tenant(settings, request)
    except JiandaoyunSchemaSyncError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"简道云连接或字段读取失败：{exc}",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post(
    "/api/v1/admin/tenants/{tenant_id}/access-key/rotate",
    response_model=TenantAccessKeyRotationResult,
)
def tenant_admin_rotate_access_key(
    tenant_id: str,
    x_admin_key: str | None = Header(default=None),
) -> TenantAccessKeyRotationResult:
    settings = get_settings()
    verify_admin_access(settings, x_admin_key)
    try:
        return rotate_tenant_access_key(settings, tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post(
    "/api/v1/admin/tenants/{tenant_id}/webhook-secret/rotate",
    response_model=TenantWebhookSecretRotationResult,
)
def tenant_admin_rotate_webhook_secret(
    tenant_id: str,
    x_admin_key: str | None = Header(default=None),
) -> TenantWebhookSecretRotationResult:
    settings = get_settings()
    verify_admin_access(settings, x_admin_key)
    try:
        return rotate_tenant_webhook_secret(settings, tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post(
    "/api/v1/admin/tenants/{tenant_id}/field-confirmation",
    response_model=TenantOnboardingResult,
)
def tenant_admin_confirm_fields(
    tenant_id: str,
    request: TenantFieldConfirmationRequest,
    x_admin_key: str | None = Header(default=None),
) -> TenantOnboardingResult:
    settings = get_settings()
    verify_admin_access(settings, x_admin_key)
    try:
        return confirm_tenant_fields(settings, tenant_id, request)
    except JiandaoyunSchemaSyncError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"简道云连接或字段读取失败：{exc}",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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
        settings = get_settings()
        request, live_snapshot = _with_live_purpose_policy(request, settings)
        mapping_path = settings.jiandaoyun_mapping_path_for(request.context.tenant_id)
        with use_field_mapping(mapping_path):
            return _execute_precheck_with_agent(
                request,
                _agent_with_snapshot(live_snapshot),
            )


def _execute_precheck(request: PrecheckRequest) -> PrecheckResponse:
    return _execute_precheck_with_agent(request, get_agent())


def _agent_with_snapshot(snapshot: TaoranKnowledgeSnapshot | None) -> TaoranAgent:
    if snapshot is None:
        return get_agent()
    try:
        return TaoranAgent(get_agent().semantic_reviewer, TaoranPrecheckEngine(snapshot))
    except ValueError:
        # 通用TAORAN核心知识缺失时不得把不完整实时快照冒充规则基线。
        return get_agent()


def _with_live_purpose_policy(
    request: PrecheckRequest,
    settings: Settings,
    snapshot: TaoranKnowledgeSnapshot | None = None,
    fetch_timeout_seconds: float | None = None,
) -> tuple[PrecheckRequest, TaoranKnowledgeSnapshot | None]:
    """精确读取DSM-BS-01-06并生成当前记录的T-03确定性策略。"""
    if request.visit.purpose_policy is not None:
        return request, snapshot
    metadata = dict(request.visit.metadata)
    if request.visit.customer_type_ii is None:
        metadata["purpose_mapping_status"] = "not_evaluated"
        return request.model_copy(
            update={"visit": request.visit.model_copy(update={"metadata": metadata})}
        ), snapshot
    if snapshot is None and not settings.knowledge_api_key:
        metadata["purpose_mapping_status"] = "unavailable"
        metadata["purpose_mapping_failure_reason"] = "knowledge_api_not_configured"
        return request.model_copy(
            update={"visit": request.visit.model_copy(update={"metadata": metadata})}
        ), snapshot
    if snapshot is None:
        try:
            snapshot = _fetch_live_knowledge_snapshot(
                settings,
                fetch_timeout_seconds or settings.knowledge_timeout_seconds,
            )
        except Exception:  # noqa: BLE001 - 不暴露知识服务响应或密钥
            metadata["purpose_mapping_status"] = "unavailable"
            metadata["purpose_mapping_failure_reason"] = "knowledge_api_unavailable"
            return request.model_copy(
                update={"visit": request.visit.model_copy(update={"metadata": metadata})}
            ), None
    record = purpose_mapping_record(snapshot)
    if record is None:
        metadata["purpose_mapping_status"] = "unavailable"
        metadata["purpose_mapping_failure_reason"] = "knowledge_record_missing"
        return request.model_copy(
            update={"visit": request.visit.model_copy(update={"metadata": metadata})}
        ), snapshot
    try:
        mapping = structure_purpose_mapping(record)
        policy = purpose_policy_for_visit(mapping, request.visit)
    except PurposeMappingError:
        metadata["purpose_mapping_status"] = "unavailable"
        metadata["purpose_mapping_failure_reason"] = "knowledge_mapping_invalid"
        return request.model_copy(
            update={"visit": request.visit.model_copy(update={"metadata": metadata})}
        ), snapshot
    if policy is None:
        metadata["purpose_mapping_status"] = "not_evaluated"
        return request.model_copy(
            update={"visit": request.visit.model_copy(update={"metadata": metadata})}
        ), snapshot
    metadata.update(
        {
            "purpose_mapping_status": "applied",
            "purpose_mapping_knowledge_id": mapping.knowledge_id,
            "purpose_mapping_knowledge_version": mapping.knowledge_version,
            "purpose_mapping_content_hash": mapping.content_hash,
            "purpose_mapping_policy_version": policy.policy_version,
        }
    )
    visit = request.visit.model_copy(
        update={"purpose_policy": policy, "metadata": metadata}
    )
    return request.model_copy(update={"visit": visit}), snapshot


def _execute_precheck_with_agent(
    request: PrecheckRequest,
    agent: TaoranAgent,
) -> PrecheckResponse:
    store = get_store()
    existing = store.get_precheck_by_request(request.context.tenant_id, request.context.request_id)
    if existing:
        snapshot_payload = {
            "form_revision": request.context.form_revision,
            "source_record_id": request.context.source_record_id,
            "visit": request.visit.model_dump(mode="json"),
        }
        if request.feedback_mode.value != "rule":
            snapshot_payload["feedback_mode"] = request.feedback_mode.value
        current_hash = canonical_hash(snapshot_payload)
        if existing["input_snapshot_hash"] != current_hash:
            raise HTTPException(status_code=409, detail="idempotency key reused with new input")
        return PrecheckResponse.model_validate(existing["response"])
    response = agent.precheck(request)
    try:
        store.save_precheck(request, response)
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return response


class _FixedSemanticReviewer(SemanticReviewer):
    def __init__(self, review: SemanticReview) -> None:
        self._review = review

    def review(self, visit):
        del visit
        return self._review.model_copy(deep=True)


def _live_enrichment_unavailable(
    request: PrecheckRequest,
    *,
    code: str,
    message: str,
    suggestion: str,
) -> PrecheckResponse:
    review = SemanticReview(
        status="unavailable",
        provider="knowledge-api",
        failure_reason=code.lower(),
        issues=[
            Issue(
                code=code,
                dimension="SYSTEM",
                severity=Severity.INFO,
                field_paths=[],
                message=message,
                suggestion=suggestion,
                source="system",
            )
        ],
    )
    response = _execute_precheck_with_agent(
        request,
        TaoranAgent(
            _FixedSemanticReviewer(review),
            TaoranPrecheckEngine(),
        ),
    )
    return response.model_copy(
        update={
            "knowledge_snapshot_hash": "",
            "knowledge_references": [],
            "taoran_sections": [
                item.model_copy(update={"knowledge_ids": []})
                for item in response.taoran_sections
            ],
        }
    )


def _execute_live_enrichment(
    request: PrecheckRequest,
    settings: Settings,
    snapshot: TaoranKnowledgeSnapshot | None = None,
) -> PrecheckResponse:
    if not settings.knowledge_api_key:
        return _live_enrichment_unavailable(
            request,
            code="KNOWLEDGE_API_NOT_CONFIGURED",
            message="知识库接口尚未配置，本次使用本地已发布标准生成建议。",
            suggestion="请管理员配置TAORAN专用知识库API Key后重试。",
        )
    if (
        snapshot is None
        and request.visit.metadata.get("purpose_mapping_failure_reason")
        == "knowledge_api_unavailable"
    ):
        return _live_enrichment_unavailable(
            request,
            code="KNOWLEDGE_API_UNAVAILABLE",
            message="本次未能从知识库API取得可用内容，未生成知识库结论。",
            suggestion="请稍后重新点击AI检测；持续失败请联系管理员。",
        )
    # The front button has an 8-second total target. Knowledge retrieval is cached;
    # a cache miss gets at most 1.5 seconds before the deterministic fallback returns.
    fetch_budget = min(settings.knowledge_timeout_seconds, 1.5)
    if snapshot is None:
        try:
            snapshot = _fetch_live_knowledge_snapshot(settings, fetch_budget)
        except Exception:  # noqa: BLE001 - 只暴露安全失败分类，不泄露供应方响应
            return _live_enrichment_unavailable(
                request,
                code="KNOWLEDGE_API_UNAVAILABLE",
                message="本次未能从知识库API取得可用内容，未生成知识库结论。",
                suggestion="请稍后重新点击AI检测；持续失败请联系管理员。",
            )
    request = request.model_copy(
        update={
            "visit": request.visit.model_copy(
                update={
                    "metadata": {
                        **request.visit.metadata,
                        "runtime_knowledge_snapshot_hash": snapshot.snapshot_hash,
                    }
                }
            )
        }
    )
    return _execute_precheck_with_agent(
        request,
        TaoranAgent(precheck_engine=TaoranPrecheckEngine(snapshot)),
    )


def _structured_unmet_items(response: PrecheckResponse) -> list[dict[str, Any]]:
    items: list[tuple[int, int, dict[str, Any]]] = []
    for order, section in enumerate(response.taoran_sections):
        if section.status != "needs_revision":
            continue
        paths = set(section.evaluated_fields)
        issues = [
            issue for issue in response.issues
            if issue.severity != Severity.INFO
            and (
                issue.dimension == section.display_code
                or bool(paths.intersection(issue.field_paths))
            )
        ]
        evidence = [
            {
                "field": display_field_name(item.field_path),
                "quote": item.quote[:80],
                "category": item.category,
            }
            for item in section.classified_evidence[:2]
        ]
        priority = sum(
            2 if issue.severity == Severity.ERROR else 1
            for issue in issues
        )
        items.append(
            (
                -priority,
                order,
                {
                "code": section.code,
                "name": section.name,
                "problems": [
                    value[:120]
                    for value in list(dict.fromkeys(issue.message for issue in issues))[:2]
                ],
                "rule_suggestion": next(
                    (issue.suggestion[:120] for issue in issues if issue.suggestion),
                    "请补充对应字段的可核验客户事实。",
                ),
                "input_evidence": evidence,
                },
            )
        )
    return [item for _, _, item in sorted(items)[:3]]


def _front_local_specificity(field: str, sources: dict[str, Any]) -> str:
    """Return obvious local outcomes and reserve semantic edge cases for the model."""
    text = str(sources.get(field) or "")
    compact = normalized_text(text)
    if not compact:
        return "unfilled"
    if compact in _FRONT_OBVIOUS_VAGUE or len(compact) < 5:
        return "not_specific"
    actor = any(token in text for token in _FRONT_ACTORS)
    action = any(token in text for token in _FRONT_ACTIONS)
    detail = any(token in text for token in _FRONT_DETAILS) or bool(
        re.search(r"\d{1,4}\s*(?:年|月|日|号|点|时|个|份|台|万|元|%)", text)
    )
    if field == "expected_key_result":
        return "specific" if len(compact) >= 12 and actor and action and detail else "ambiguous"
    if field == "process_description":
        judgment = any(token in text for token in _FRONT_JUDGMENTS)
        separated = not judgment or any(token in text for token in ("事实：", "判断：", "假设：", "\n"))
        return (
            "specific"
            if len(compact) >= 18 and actor and action and detail and separated
            else "ambiguous"
        )
    context = " ".join(
        str(sources.get(name) or "")
        for name in ("next_action_purpose", "process_description")
    )
    stop = {"客户", "下一步", "进行", "完成", "安排", "相关", "本次", "继续"}
    linked = any(
        compact[index:index + 2] in normalized_text(context)
        and compact[index:index + 2] not in stop
        for index in range(max(0, len(compact) - 1))
    )
    return (
        "specific"
        if len(compact) >= 12 and actor and action and detail and linked
        else "ambiguous"
    )


def _front_specificity_items(
    response: PrecheckResponse,
    visit_snapshot: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Send only semantic edge cases to the model for evidence extraction."""
    items: list[dict[str, Any]] = []
    visit_snapshot = visit_snapshot or {}
    sections = {section.code: section for section in response.taoran_sections}
    required_features = {
        "O_KR": ["relationship", "customer_info", "blocker"],
        "R": ["customer_expression_action", "opinion_grounded"],
        "N": ["relationship", "customer_info", "blocker"],
    }
    for code, name, field, _issue_codes, _missing_codes in _FRONT_SPECIFICITY_ITEM_RULES:
        section = sections.get(code)
        if section is not None and any(
            path == field for path in section.unreceived_fields
        ):
            # A field-transfer problem is shown as a system notice.  It must not be
            # sent to the model as though the salesperson left the field blank.
            continue
        physically_completed = response.field_completion.get(field)
        if physically_completed is False:
            continue
        allowed_sources = {
            "O_KR": ("expected_key_result",),
            "R": ("process_description",),
            "N": ("next_action_expected_result",),
        }[code]
        source_fields = {
            source: visit_snapshot.get(source)
            for source in allowed_sources
            if visit_snapshot.get(source) not in (None, "")
        }
        item = {
            "code": code,
            "name": name,
            "field": field,
            "required_features": required_features[code],
            "source_fields": source_fields,
        }
        if code == "N":
            item["reference_context"] = {
                key: visit_snapshot.get(key)
                for key in ("next_action_purpose", "process_description")
                if visit_snapshot.get(key) not in (None, "")
            }
        items.append(item)
    return items[:3]


def _knowledge_model_context(
    request: PrecheckRequest,
    snapshot: TaoranKnowledgeSnapshot,
    *,
    experimental: bool = False,
) -> dict[str, Any]:
    """生成不含完整知识正文的最小TAORAN拜访快照。"""
    visit = request.visit.model_dump(mode="json")
    text_limits = {
        "other_purpose": 120,
        "expected_key_result": 160,
        "process_description": 300,
        "customer_feedback": 160,
        "deviation_reason": 120,
        "next_action_purpose": 120,
        "next_action_other_purpose": 120,
        "next_action_expected_result": 160,
    }
    visit_fields = [
        "visit_date",
        "customer_type_ii",
        "opportunity_stage",
        "visit_method",
        "is_appointment",
        "purpose_code",
        "other_purpose",
        "expected_key_result",
        "process_description",
        "customer_feedback",
        "self_assessment",
        "deviation_reason",
        "next_action_purpose",
        "next_action_other_purpose",
        "next_action_expected_result",
        "next_contact_at",
    ]
    from .record_contract import visit_contract
    visit_snapshot: dict[str, Any] = {"_record_contract": visit_contract(request.visit)}
    for field in visit_fields:
        if visit_snapshot["_record_contract"]["presence"].get(field) == "not_received":
            continue
        value = visit.get(field)
        if not experimental and isinstance(value, str) and field in text_limits:
            value = value[: text_limits[field]]
        visit_snapshot[field] = value
    visit_snapshot["opportunity_stages"] = [
        item.get("current_stage")
        for item in visit.get("opportunities", [])
        if item.get("current_stage")
    ][:3]

    standards: list[dict[str, Any]] = []
    for record in snapshot.records:
        item: dict[str, Any] = {
            "id": record.id,
            "version": record.version,
            "content_hash": record.content_hash,
        }
        if record.id == "DSM-BS-01-06":
            policy = request.visit.purpose_policy
            item["structured_mapping"] = {
                "customer_type": (
                    policy.customer_type.value
                    if policy is not None and policy.customer_type is not None
                    else None
                ),
                "opportunity_stages": policy.opportunity_stages if policy is not None else [],
                "allowed_purposes": policy.allowed_purposes if policy is not None else [],
                "excluded_purposes": ["P6", "争取客户满意"],
            }
        standards.append(item)
    return {
        "snapshot_version": "TAORAN-LIGHT-SNAPSHOT-V2",
        "visit_snapshot": visit_snapshot,
        "standard_provenance": standards,
    }

def _v46_knowledge_model_context(
    request: PrecheckRequest,
    snapshot: TaoranKnowledgeSnapshot,
    *,
    experimental: bool = False,
) -> dict[str, Any]:
    """生成不含完整知识正文的最小TAORAN拜访快照。"""
    visit = request.visit.model_dump(mode="json")
    text_limits = {
        "other_purpose": 120,
        "expected_key_result": 160,
        "process_description": 300,
        "customer_feedback": 160,
        "deviation_reason": 120,
        "next_action_purpose": 120,
        "next_action_other_purpose": 120,
        "next_action_expected_result": 160,
    }
    visit_fields = [
        "visit_date",
        "customer_type_ii",
        "opportunity_stage",
        "visit_method",
        "is_appointment",
        "purpose_code",
        "other_purpose",
        "expected_key_result",
        "process_description",
        "customer_feedback",
        "self_assessment",
        "deviation_reason",
        "next_action_purpose",
        "next_action_other_purpose",
        "next_action_expected_result",
        "next_contact_at",
    ]
    visit_snapshot: dict[str, Any] = {}
    for field in visit_fields:
        value = visit.get(field)
        if not experimental and isinstance(value, str) and field in text_limits:
            value = value[: text_limits[field]]
        visit_snapshot[field] = value
    visit_snapshot["opportunity_stages"] = [
        item.get("current_stage")
        for item in visit.get("opportunities", [])
        if item.get("current_stage")
    ][:3]

    standards: list[dict[str, Any]] = []
    for record in snapshot.records:
        item: dict[str, Any] = {
            "id": record.id,
            "version": record.version,
            "content_hash": record.content_hash,
        }
        if record.id == "DSM-BS-01-06":
            policy = request.visit.purpose_policy
            item["structured_mapping"] = {
                "customer_type": (
                    policy.customer_type.value
                    if policy is not None and policy.customer_type is not None
                    else None
                ),
                "opportunity_stages": policy.opportunity_stages if policy is not None else [],
                "allowed_purposes": policy.allowed_purposes if policy is not None else [],
                "excluded_purposes": ["P6", "争取客户满意"],
            }
        standards.append(item)
    return {
        "snapshot_version": "TAORAN-LIGHT-SNAPSHOT-V2",
        "visit_snapshot": visit_snapshot,
        "standard_provenance": standards,
    }


_KNOWLEDGE_CONTEXT_FIELDS = {
    "T": {"customer_type_ii", "purpose_code", "other_purpose", "opportunity_stages"},
    "A1": {"customer_type_ii", "visit_method", "is_appointment", "purpose_code"},
    "O_KR": {"purpose_code", "other_purpose", "expected_key_result"},
    "R": {"process_description", "customer_feedback"},
    "A2": {
        "self_assessment",
        "expected_key_result",
        "process_description",
        "deviation_reason",
    },
    "N": {
        "visit_date",
        "customer_type_ii",
        "process_description",
        "customer_feedback",
        "next_action_purpose",
        "next_action_other_purpose",
        "next_action_expected_result",
        "next_contact_at",
        "opportunity_stages",
    },
}


def _prune_knowledge_model_context(
    context: dict[str, Any],
    unmet: list[dict[str, Any]],
) -> dict[str, Any]:
    """只保留当前待改进六项需要的表单字段，减少模型输入和无关分析。"""
    codes = {str(item.get("code", "")) for item in unmet}
    allowed = set().union(*(_KNOWLEDGE_CONTEXT_FIELDS.get(code, set()) for code in codes))
    visit_snapshot = context.get("visit_snapshot")
    if not allowed or not isinstance(visit_snapshot, dict):
        return context
    return {
        **context,
        "visit_snapshot": {
            field: value for field, value in visit_snapshot.items() if field in allowed or field == "_record_contract"
        },
    }


def _cached_knowledge_wording(
    key: str,
) -> KnowledgeWordingResult | None:
    now = monotonic()
    with _knowledge_semantic_cache_lock:
        cached = _knowledge_semantic_cache.get(key)
        if cached and cached[0] > now:
            return cached[1].model_copy(
                update={
                    "cache_hit": True,
                    "latency_ms": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "cached_input_tokens": 0,
                    "model_queue_ms": 0,
                    "model_first_byte_ms": 0,
                    "model_complete_ms": 0,
                    "model_request_id": None,
                    "model_attempts": [],
                }
            )
        if cached:
            _knowledge_semantic_cache.pop(key, None)
        expired = [item for item, value in _knowledge_semantic_cache.items() if value[0] <= now]
        for item in expired:
            _knowledge_semantic_cache.pop(item, None)
        if len(_knowledge_semantic_cache) > 512:
            oldest = min(_knowledge_semantic_cache, key=lambda item: _knowledge_semantic_cache[item][0])
            _knowledge_semantic_cache.pop(oldest, None)
    return None


def _enhance_front_suggestions(
    response: PrecheckResponse,
    reviewer: ChatModelReviewer,
    settings: Settings,
    timeout_seconds: float,
    taoran_context: dict[str, Any],
    *,
    experimental: bool = False,
) -> PrecheckResponse:
    if not response.knowledge_snapshot_hash or not response.knowledge_references:
        return response
    field_checks = _front_specificity_items(
        response,
        taoran_context.get("visit_snapshot")
        if isinstance(taoran_context.get("visit_snapshot"), dict)
        else None,
    )
    visit_snapshot = (
        taoran_context.get("visit_snapshot")
        if isinstance(taoran_context.get("visit_snapshot"), dict)
        else {}
    )
    analysis_fields = (
        "customer_type_ii", "opportunity_stage", "visit_method", "is_appointment",
        "opportunity_stages", "visit_date", "_record_contract",
        "purpose_code", "other_purpose",
        "expected_key_result", "process_description", "customer_feedback",
        "self_assessment", "deviation_reason", "next_action_purpose",
        "next_action_other_purpose", "next_action_expected_result", "next_contact_at",
    )
    visit_analysis_context = {
        field: visit_snapshot[field]
        for field in analysis_fields
        if visit_snapshot.get(field) not in (None, "", [])
    }
    if experimental and visit_analysis_context.get("next_contact_at"):
        contact = datetime.fromisoformat(str(visit_analysis_context["next_contact_at"]))
        if contact.tzinfo is not None:
            contact = contact.astimezone(ZoneInfo("Asia/Shanghai"))
        visit_analysis_context["next_contact_at"] = contact.date().isoformat()
    for stage_field in ("opportunity_stage", "opportunity_stages"):
        stage_value = visit_analysis_context.get(stage_field)
        if "对应商机阶段" in str(stage_value):
            visit_analysis_context.pop(stage_field, None)
    confirmed_findings = []
    provisional_codes = pending_finding_codes(field_checks) if experimental else set()
    for issue in response.issues:
        if issue.source == "system" or issue.severity == Severity.INFO:
            continue
        if issue.code in provisional_codes:
            continue
        finding = str(issue.message).strip()
        if finding and finding not in confirmed_findings:
            confirmed_findings.append(finding)
    if confirmed_findings:
        visit_analysis_context["confirmed_findings"] = "；".join(
            confirmed_findings[:8]
        )[:700]
    # 同一次轻量请求同时完成本次拜访概括和三个具体性判断。
    taoran_snapshot = {
        "visit_analysis_context": visit_analysis_context,
        "field_specificity_checks": field_checks,
    }
    if experimental:
        taoran_snapshot["experimental_speaker_hints"] = attribution_hints(str(visit_snapshot.get("process_description") or ""))
    # Keep an exact wording for an unchanged form under the same judgment basis.
    # Request IDs and timestamps are deliberately excluded.
    cache_key = canonical_hash(
        {
            "tenant_id": response.tenant_id,
            "input_snapshot_hash": response.input_snapshot_hash,
            "knowledge_snapshot_hash": response.knowledge_snapshot_hash,
            "prompt_version": KNOWLEDGE_WORDING_PROMPT_VERSION,
            "opinion_policy": "semantic-observe-20260908",
            "model": settings.llm_model,
            "field_specificity_checks": field_checks,
            "taoran_snapshot": taoran_snapshot,
        }
    )
    store = get_store(settings)
    if experimental:
        cache_key = canonical_hash({"experimental_final_version": "front-v46-suggestion-contract-20260908", "key": cache_key})
    persisted = store.get_feedback_artifact(
        response.tenant_id,
        _FRONT_WORDING_ARTIFACT_TYPE,
        cache_key,
    )
    wording = (
        KnowledgeWordingResult.model_validate(persisted).model_copy(
            update={
                "cache_hit": True,
                "latency_ms": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cached_input_tokens": 0,
                "model_queue_ms": 0,
                "model_first_byte_ms": 0,
                "model_complete_ms": 0,
                "model_request_id": None,
                "model_attempts": [],
            }
        )
        if persisted is not None
        else _cached_knowledge_wording(cache_key)
    )
    reused_wording = wording is not None and wording.cache_hit
    if wording is None:
        wording = reviewer.verbalize_knowledge_issues(
            field_checks,
            timeout_seconds,
            taoran_snapshot=taoran_snapshot,
            **({"experimental": True} if experimental else {}),
        )
        if (
            settings.frontend_model_format_retries > 0
            and wording.status == "unavailable"
            and _wording_format_failure(wording.failure_reason)
            and (not experimental or experimental_retry_allowed(wording._experimental_repair_context))
            and not (experimental and wording.failure_reason in {
                "wording_experimental_audit_contract", "wording_experimental_audit_comparison",
                "wording_experimental_audit_truncated", "wording_experimental_audit_upstream",
            })
        ):
            first = wording
            retry = reviewer.verbalize_knowledge_issues(
                field_checks,
                timeout_seconds,
                taoran_snapshot=taoran_snapshot,
                **({"experimental": True, "repair_reason": first.failure_reason} if experimental else {}),
                **({"repair_context": first._experimental_repair_context}
                   if experimental and first._experimental_repair_context else {}),
            )
            wording = retry.model_copy(
                update={
                    "attempt_count": 2,
                    "recovered_after_retry": retry.status == "completed",
                    "latency_ms": first.latency_ms + retry.latency_ms,
                    "input_tokens": first.input_tokens + retry.input_tokens,
                    "output_tokens": first.output_tokens + retry.output_tokens,
                    "total_tokens": first.total_tokens + retry.total_tokens,
                    "cached_input_tokens": (
                        first.cached_input_tokens + retry.cached_input_tokens
                    ),
                    "model_queue_ms": first.model_queue_ms + retry.model_queue_ms,
                    "validation_errors": [
                        *first.validation_errors,
                        *retry.validation_errors,
                    ][:20],
                    "model_first_byte_ms": retry.model_first_byte_ms,
                    "model_complete_ms": retry.model_complete_ms,
                    "model_request_id": retry.model_request_id,
                    "model_attempts": [
                        *first.model_attempts,
                        *[
                            {**attempt, "attempt": 2}
                            for attempt in retry.model_attempts
                        ],
                    ][:2],
                }
            )
        if not experimental and not wording.visit_analysis_sections:
            fallback_sections = _fallback_front_analysis_sections(
                visit_analysis_context
            )
            wording = wording.model_copy(
                update={
                    "visit_analysis_sections": [
                        FrontVisitAnalysisSection(kind=kind, text=text)
                        for kind, text in fallback_sections.items()
                        if text
                    ],
                }
            )
        if settings.knowledge_semantic_cache_seconds > 0:
            # Successful wording follows the normal cache period. A transient
            # timeout/failure is cached only briefly so repeated clicks return
            # immediately without suppressing a later recovery for five minutes.
            ttl = settings.knowledge_semantic_cache_seconds if wording.status == "completed" else (
                0.0 if _wording_format_failure(wording.failure_reason)
                else min(15.0, settings.knowledge_semantic_cache_seconds)
            )
            if ttl > 0:
                with _knowledge_semantic_cache_lock:
                    _knowledge_semantic_cache[cache_key] = (
                        monotonic() + ttl,
                        wording.model_copy(deep=True),
                    )
    # Keep the first successful model wording beyond process lifetime.  The key
    # includes the form snapshot and all judgment basis, so a changed standard
    # intentionally produces feedback under the new standard.
    if wording.status == "completed":
        canonical = store.save_feedback_artifact(
            response.tenant_id,
            _FRONT_WORDING_ARTIFACT_TYPE,
            cache_key,
            wording.model_dump(mode="json"),
        )
        updates: dict[str, Any] = {"cache_hit": reused_wording}
        if reused_wording:
            updates.update(
                latency_ms=0,
                input_tokens=0,
                output_tokens=0,
                total_tokens=0,
                cached_input_tokens=0,
                model_queue_ms=0,
                model_first_byte_ms=0,
                model_complete_ms=0,
                model_request_id=None,
                model_attempts=[],
            )
        wording = KnowledgeWordingResult.model_validate(canonical).model_copy(update=updates)
    return _apply_knowledge_wording(response, wording, settings, experimental=experimental)


def _apply_knowledge_wording(
    response: PrecheckResponse,
    wording: KnowledgeWordingResult,
    settings: Settings,
    *,
    experimental: bool = False,
) -> PrecheckResponse:
    """合并AI轻量建议；失败时无感回退到结构化建议。"""
    review = response.semantic_review.model_copy(
        update={
            "status": wording.status,
            "provider": wording.provider,
            "model": wording.model,
            "prompt_version": wording.prompt_version,
            "latency_ms": wording.latency_ms,
            "input_tokens": wording.input_tokens,
            "output_tokens": wording.output_tokens,
            "total_tokens": wording.total_tokens,
            "cached_input_tokens": wording.cached_input_tokens,
            "cache_hit": wording.cache_hit,
            "model_queue_ms": wording.model_queue_ms,
            "failure_reason": wording.failure_reason,
            "attempt_count": wording.attempt_count,
            "recovered_after_retry": wording.recovered_after_retry,
            "validation_errors": wording.validation_errors,
            "model_attempts": wording.model_attempts,
            "semantic_observations": wording.semantic_observations,
            "confirmation_items": wording.confirmation_items,
            "suggestion_status": wording.suggestion_status,
            "suggestion_count": sum(bool(i.suggestion.strip()) for i in wording.items),
            "model_first_byte_ms": wording.model_first_byte_ms,
            "model_complete_ms": wording.model_complete_ms,
            "model_request_id": wording.model_request_id,
        }
    )
    audit = response.standard_audit
    if audit is not None:
        audit = audit.model_copy(update={"model_prompt_version": wording.prompt_version})
    enhanced = response.model_copy(
        update={
            "feedback_text": build_front_ai_suggestions_with_model(response, wording, experimental=experimental),
            "semantic_review": review,
            "engine_version": (
                "TAORAN-PRECHECK-HYBRID-SEMANTIC-V5"
                if wording.status == "completed"
                else "TAORAN-PRECHECK-HYBRID-SEMANTIC-FALLBACK-V5"
            ),
            "standard_audit": audit,
        }
    )
    get_store(settings).update_precheck_response(enhanced)
    return enhanced


def _execute_two_feedback(
    canonical_request: PrecheckRequest,
    settings: Settings,
) -> tuple[PrecheckResponse, PrecheckResponse, int, int]:
    """兼容内部旧调用；新前端只使用统一反馈链。"""
    def run_rule() -> tuple[PrecheckResponse, int]:
        started = monotonic()
        result = _execute_rule_button_feedback(canonical_request, settings)
        return result, int((monotonic() - started) * 1000)

    def run_knowledge() -> tuple[PrecheckResponse, int]:
        started = monotonic()
        result = _execute_knowledge_button_feedback(canonical_request, settings)
        return result, int((monotonic() - started) * 1000)

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="taoran-feedback") as executor:
        rule_future = executor.submit(run_rule)
        knowledge_future = executor.submit(run_knowledge)
        rule, rule_latency_ms = rule_future.result()
        knowledge, knowledge_latency_ms = knowledge_future.result()
    return rule, knowledge, rule_latency_ms, knowledge_latency_ms


def _execute_rule_button_feedback(
    canonical_request: PrecheckRequest,
    settings: Settings,
) -> PrecheckResponse:
    """执行可独立返回的规则分支，不访问实时知识API或大模型。"""
    mapping_path = settings.jiandaoyun_mapping_path_for(
        canonical_request.context.tenant_id
    )
    from .content_cache import knowledge_basis, pinned_snapshot
    local_snapshot = (pinned_snapshot('local') if knowledge_basis.get() is not None
                      else load_taoran_knowledge_snapshot(settings.knowledge_snapshot_path))
    local_rule_request, _ = _with_live_purpose_policy(
        canonical_request,
        settings,
        local_snapshot,
    )
    rule_request = local_rule_request.model_copy(update={"feedback_mode": FeedbackMode.RULE})
    with use_field_mapping(mapping_path):
        return _execute_precheck_with_agent(
            rule_request,
            _agent_with_snapshot(local_snapshot),
        )


def _execute_knowledge_button_feedback(
    canonical_request: PrecheckRequest,
    settings: Settings,
    *,
    experimental: bool = False,
) -> PrecheckResponse:
    """执行可独立返回的实时知识库分支和受控AI表达。"""
    started = monotonic()
    base_context = canonical_request.context
    mapping_path = settings.jiandaoyun_mapping_path_for(base_context.tenant_id)
    reviewer = get_agent().semantic_reviewer
    if isinstance(reviewer, ChatModelReviewer):
        from .front_v46 import bind
        reviewer = bind(reviewer)
        reviewer.observation_identity = {"tenant_id": base_context.tenant_id,
            "request_id": base_context.request_id, "record_version": base_context.form_revision}
    # Current product setting prioritizes data-grounded AI wording. There is no
    # short UI cutoff; only the provider connection safety limit remains.
    phase_latency_ms: dict[str, int] = {}
    knowledge_request = canonical_request.model_copy(
        update={
            "context": base_context.model_copy(
                update={"request_id": f"{base_context.request_id}__enrichment"}
            ),
            "feedback_mode": FeedbackMode.RULE,
        }
    )
    fetch_started = monotonic()
    live_request, live_snapshot = _with_live_purpose_policy(
        knowledge_request,
        settings,
        fetch_timeout_seconds=settings.knowledge_fetch_budget_seconds,
    )
    phase_latency_ms["knowledge_fetch_and_mapping"] = int(
        (monotonic() - fetch_started) * 1000
    )
    structured_started = monotonic()
    with use_field_mapping(mapping_path):
        result = _execute_live_enrichment(
            live_request,
            settings,
            live_snapshot,
        )
    phase_latency_ms["structured_feedback"] = int(
        (monotonic() - structured_started) * 1000
    )

    queue_started = monotonic()
    lease = (
        reviewer.button_feedback_scheduler.lease(
            settings.frontend_model_timeout_seconds
        )
        if isinstance(reviewer, ChatModelReviewer)
        else nullcontext("acquired")
    )
    with lease as lease_status:
        phase_latency_ms["model_queue"] = int((monotonic() - queue_started) * 1000)
        if (
            lease_status == "acquired"
            and isinstance(reviewer, ChatModelReviewer)
            and live_snapshot is not None
        ):
            # Do not apply the former 4/7/10-second semantic cutoff. Wait for
            # the data-grounded AI result within the connection safety limit.
            knowledge_wording_budget = settings.frontend_model_timeout_seconds
            with use_field_mapping(mapping_path):
                result = _enhance_front_suggestions(
                    result,
                    reviewer,
                    settings,
                    knowledge_wording_budget,
                    _knowledge_model_context(live_request, live_snapshot, experimental=experimental),
                    experimental=experimental,
                )
        else:
            if isinstance(reviewer, ChatModelReviewer):
                reason = (
                    lease_status
                    if lease_status != "acquired"
                    else "knowledge_snapshot_unavailable"
                )
                provider = "llm-chat-light-suggestion"
            else:
                reason = "model_not_configured"
                provider = "model-not-configured"
            with use_field_mapping(mapping_path):
                result = _apply_knowledge_wording(
                    result,
                    KnowledgeWordingResult(
                        status="timeout" if reason in {"queue_timeout", "queue_full"} else "unavailable",
                        provider=provider,
                        model=settings.llm_model,
                        prompt_version=KNOWLEDGE_WORDING_PROMPT_VERSION,
                        failure_reason=reason,
                    ),
                    settings,
                )
    hidden_queue_ms = result.semantic_review.model_queue_ms
    phase_latency_ms["model_queue"] += hidden_queue_ms
    phase_latency_ms["model"] = max(
        0,
        result.semantic_review.latency_ms - hidden_queue_ms,
    )
    if result.semantic_review.attempt_count > 1:
        phase_latency_ms["model_retry_overhead"] = max(
            0,
            result.semantic_review.latency_ms
            - hidden_queue_ms
            - (result.semantic_review.model_complete_ms or 0),
        )
    phase_latency_ms["total"] = int((monotonic() - started) * 1000)
    result = result.model_copy(update={"phase_latency_ms": phase_latency_ms})
    # 结构化结果和AI措辞会在阶段计时完成前先写入。必须用最终响应再次更新，
    # 否则前端能看到阶段耗时，历史检查记录却仍是空字典。
    get_store(settings).update_precheck_response(result)
    return result


def _execute_unified_button_feedback(
    canonical_request: PrecheckRequest,
    settings: Settings,
    *,
    experimental: bool = False,
) -> PrecheckResponse:
    """唯一反馈链：确定性规则为底座，实时知识与轻量AI只做增强。"""
    enhanced = _execute_knowledge_button_feedback(canonical_request, settings, experimental=experimental)
    if enhanced.knowledge_snapshot_hash:
        return enhanced.model_copy(
            update={
                "feedback_mode": FeedbackMode.RULE,
                "rule_feedback_text": enhanced.feedback_text,
            }
        )

    # 实时知识不可用时仍返回完整本地规则结果，不让可选增强拖垮AI检测。
    local = _execute_rule_button_feedback(canonical_request, settings)
    reason = next(
        (
            issue.message
            for issue in enhanced.issues
            if issue.source == "system" and issue.code.startswith("KNOWLEDGE_API_")
        ),
        "本次未能取得最新知识内容，已使用已发布的本地内容生成建议。",
    )
    with use_field_mapping(
        settings.jiandaoyun_mapping_path_for(canonical_request.context.tenant_id)
    ):
        front_feedback = build_front_ai_suggestions(local, system_notice=reason)
    return local.model_copy(
        update={
            "feedback_text": front_feedback,
            "rule_feedback_text": front_feedback,
            "semantic_review": enhanced.semantic_review,
            "phase_latency_ms": enhanced.phase_latency_ms,
            "latency_ms": enhanced.latency_ms,
        }
    )


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
            detail="AI调用异常。异常原因：字段传递格式异常。处理建议：请核对本次字段值及子表绑定后重试。",
        ) from None
    return create_precheck(canonical_request, x_tenant_id, x_api_key)


@app.post(
    "/api/v1/connectors/jiandaoyun/visit/button-check",
    response_model=UnifiedButtonPrecheckResponse,
)
def jiandaoyun_button_precheck(
    request: dict[str, Any],
    x_tenant_id: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> UnifiedButtonPrecheckResponse:
    """简道云“AI检测”按钮：返回唯一的规则＋知识库综合建议。"""
    started = monotonic()
    canonical_request, settings = _canonicalize_button_request(
        request,
        x_tenant_id,
        x_api_key,
    )
    result = _execute_unified_button_feedback(canonical_request, settings)
    elapsed_ms = int((monotonic() - started) * 1000)
    _observe_pipeline(
        "front_button",
        {**result.phase_latency_ms, "request_total": elapsed_ms},
        success=True,
    )
    return UnifiedButtonPrecheckResponse.from_precheck(
        result,
        latency_ms=elapsed_ms,
    )


def _canonicalize_button_request(
    request: dict[str, Any],
    x_tenant_id: str | None,
    x_api_key: str | None,
) -> tuple[PrecheckRequest, Settings]:
    """只做一次简道云草稿解析与租户授权，供联合和独立分支端点共用。"""
    if "context" in request and "form_data" in request:
        structured_request = JiandaoyunCheckRequest.model_validate(request)
    else:
        flat_request = dict(request)
        tenant_id = str(flat_request.pop("tenant_id", "")).strip()
        if not tenant_id:
            raise HTTPException(status_code=422, detail="tenant_id is required")
        user_id = str(flat_request.pop("user_id", "jiandaoyun-user")).strip()
        if "feedback_mode" in flat_request:
            raise HTTPException(status_code=422, detail="前端检测接口不再接受反馈模式参数")
        flat_request.pop("request_id", None)
        form_revision = flat_request.pop("form_revision", None)
        source_record_id = flat_request.pop("source_record_id", None)
        # 简道云会在日期字段为空时省略整个按钮参数。下一次联系日期允许暂时
        # 未填写，因此将这种省略规范化为空值，让规则给出具体补充建议。
        flat_request.setdefault("next_contact_at", None)
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
                "feedback_mode": "rule",
            }
        )
    authorize(structured_request.context.tenant_id, x_tenant_id, x_api_key)
    settings = get_settings()
    mapping = tenant_mapping(settings, structured_request.context.tenant_id)
    try:
        canonical_request = adapt_jiandaoyun_request(structured_request, mapping)
    except (FieldTransferError, ValidationError):
        raise HTTPException(
            status_code=422,
            detail="AI调用异常。异常原因：字段传递格式异常。处理建议：请核对本次字段值及子表绑定后重试。",
        ) from None
    return canonical_request, settings


@app.post(
    "/api/v1/connectors/jiandaoyun/visit/button-check/rule",
    response_model=UnifiedButtonPrecheckResponse,
)
def jiandaoyun_button_rule_precheck(
    request: dict[str, Any],
    x_tenant_id: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> UnifiedButtonPrecheckResponse:
    """唯一前端反馈请求：规则底座＋实时知识库＋可选轻量AI。"""
    started = monotonic()
    canonical_request, settings = _canonicalize_button_request(
        request,
        x_tenant_id,
        x_api_key,
    )
    result = _execute_unified_button_feedback(canonical_request, settings)
    elapsed_ms = int((monotonic() - started) * 1000)
    _observe_pipeline(
        "front_button",
        {**result.phase_latency_ms, "request_total": elapsed_ms},
        success=True,
    )
    return UnifiedButtonPrecheckResponse.from_precheck(
        result,
        latency_ms=elapsed_ms,
    )


def _require_interactive_quick_check(settings: Settings) -> None:
    if not settings.quick_check_interactive_enabled:
        raise HTTPException(status_code=404, detail="Not Found")


def _quick_check_sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _quick_check_snapshot_hash(
    canonical_request: PrecheckRequest,
    record_code: str,
) -> str:
    """Hash the parsed current form, never a saved Jiandaoyun record."""
    return canonical_hash(
        {
            "record_code": record_code,
            "source_record_id": canonical_request.context.source_record_id,
            "visit": canonical_request.visit.model_dump(mode="json"),
        }
    )


def _canonicalize_interactive_quick_check(
    request: dict[str, Any],
    x_tenant_id: str | None,
    x_api_key: str | None,
) -> tuple[PrecheckRequest, Settings, str, str, str, bool, dict]:
    """Validate a browser's unsaved snapshot without reading Jiandaoyun."""
    if not x_tenant_id or not x_tenant_id.strip() or not x_api_key or not x_api_key.strip():
        raise HTTPException(status_code=401, detail="Missing tenant credentials")
    raw = dict(request)
    record_code = str(raw.pop("record_code", "")).strip()
    data_id = str(raw.pop("data_id", "")).strip() or None
    user_id = str(raw.pop("user_id", "jiandaoyun-user")).strip() or "jiandaoyun-user"
    force = bool(raw.pop("force", False))
    client_hash = raw.pop("input_hash", None)
    snapshot = raw.pop("form_snapshot", None)
    if raw or not isinstance(snapshot, dict):
        raise HTTPException(status_code=422, detail="当前表单快照格式不正确，请重新打开AI检测。")
    canonical_request, settings = _canonicalize_button_request(
        {
            "context": {
                "tenant_id": x_tenant_id or "",
                "request_id": f"quick_check_{uuid4().hex}",
                "user_id": user_id,
                # Reuse the established Jiandaoyun source enum.  The task
                # contract, not a new source label, identifies this as a
                # current-form interactive check.
                "source": "jiandaoyun",
                "source_record_id": data_id,
            },
            "form_data": snapshot,
        },
        x_tenant_id,
        x_api_key,
    )
    from .content_cache import fingerprint, implementation_digest
    local = load_taoran_knowledge_snapshot(settings.knowledge_snapshot_path)
    live = None
    try:
        live = _fetch_live_knowledge_snapshot(settings, settings.knowledge_fetch_budget_seconds)
        live_hash = live.snapshot_hash
    except Exception:  # noqa: BLE001 - unavailable knowledge is isolated, never cached as success
        # Unknown current knowledge must never hit a prior success cache.
        live_hash = 'unavailable'
        force = True
    config = settings.model_dump(mode='json')
    config['model_credential_digest'] = hashlib.sha256(
        (settings.llm_api_key.get_secret_value() if settings.llm_api_key else '').encode()
    ).hexdigest()
    input_hash = fingerprint(
        snapshot, canonical_request.visit.model_dump(mode='json'),
        tenant=canonical_request.context.tenant_id, credential=x_api_key.strip(), user=user_id,
        settings=config, mapping=tenant_mapping(settings, canonical_request.context.tenant_id),
        local_knowledge_hash=local.snapshot_hash, live_knowledge_hash=live_hash,
    )
    # Internal content namespace is not a field, business ID, or writeback target.
    record_code = 'content:' + input_hash
    canonical_request = canonical_request.model_copy(update={
        'context': canonical_request.context.model_copy(update={
            'form_revision': 'content-v1:' + input_hash,
        }),
    })
    if client_hash is not None and (
        not isinstance(client_hash, str)
        or not hmac.compare_digest(client_hash, input_hash)
    ):
        raise HTTPException(status_code=422, detail="当前表单快照校验不一致，请重新点击AI检测。")
    basis = {'local': local.model_dump(mode='json'),
             'live': live.model_dump(mode='json') if live is not None else None,
             'implementation': implementation_digest()}
    return canonical_request, settings, record_code, input_hash, user_id, force, basis


def _quick_check_persist(task):
    if not task.get('request_snapshot'):
        return
    from .quick_check_recovery import save
    save(task, get_store(task.get('_settings')))


def _quick_check_schedule(task, canonical_request, settings):
    task['_settings'] = settings
    task['request_snapshot'] = canonical_request.model_dump(mode='json')
    task['attempt'] = task.get('attempt', 0) + 1
    generation = task['attempt']
    task['status'] = 'processing'
    task['phase_timings'] = {}
    task.pop('outcome', None)
    task.pop('completed_at', None)
    from .async_opinion import basic_feedback
    task['basic_feedback'] = basic_feedback(canonical_request.visit)
    from .front_v46 import POLICY_VERSION
    task['front_policy'] = POLICY_VERSION
    task['preview_snapshot'] = {'text':'','status':'processing','kind':'ai'}
    task['events'] = Queue()
    queued = monotonic()
    _quick_check_persist(task)  # Durable source precedes dispatch.
    def work():
        task['phase_timings']['worker_queue_ms'] = int((monotonic()-queued)*1000)
        _quick_check_persist(task)
        return _quick_check_run(canonical_request, settings, task['events'], task.get('knowledge_basis'))
    task['future'] = _quick_check_executor.submit(work)
    def completed(_future):
        with _quick_check_lock:
            if task.get('attempt') != generation:
                return
            outcome = _quick_check_resolve(task)
            _quick_check_preview_snapshot(task)
            _quick_check_persist(task)
            preview = (outcome or {}).get('preview_future')
            if preview is not None:
                def preview_done(_):
                    with _quick_check_lock:
                        if task.get('attempt') == generation:
                            _quick_check_preview_snapshot(task)
                            _quick_check_persist(task)
                preview.add_done_callback(preview_done)
    task['future'].add_done_callback(completed)


def _quick_check_cleanup(now: float) -> None:
    expired = [
        check_id
        for check_id, task in _quick_check_tasks.items()
        if float(task["expires_at"]) <= now and task["future"].done()
    ]
    for check_id in expired:
        task = _quick_check_tasks.pop(check_id)
        key = task["idempotency_key"]
        if _quick_check_idempotency.get(key) == check_id:
            _quick_check_idempotency.pop(key, None)


def _quick_check_run_final(canonical_request, settings):
    """Keep bounded transient retries around the restored V4.6 policy."""
    from time import sleep
    attempts = []
    for index in range(3):
        result = _quick_check_run_final_once(canonical_request, settings)
        attempts.append(result.get("phase_timings", {}))
        reason = (result.get("diagnostics") or {}).get("failure_reason")
        if result.get("status") == "completed" or reason not in {
            "timeout", "queue_timeout", "queue_full", "rate_limited",
            "invalid_response_or_network_error", "wording_experimental_audit_upstream",
        } or index == 2:
            result["transport_attempt_count"] = index + 1
            result["recoverable"] = result.get("status") != "completed"
            result["phase_timings"] = {
                **result.get("phase_timings", {}),
                "attempts": [item for timing in attempts for item in timing.get("attempts", [])],
            }
            return result
        sleep(index + 1)


def _quick_check_run_final_once(
    canonical_request: PrecheckRequest,
    settings: Settings,
) -> dict[str, Any]:
    """Run candidate-only Final presentation on validated model wording."""
    from .front_v46.experimental_final_diagnostics import audit as experimental_final_audit
    from .front_v46.experimental_final_diagnostics import category as experimental_failure_category
    from .quick_check_recovery import phase_timings
    started = monotonic()
    request_id = canonical_request.context.request_id
    try:
        result = _execute_unified_button_feedback(canonical_request, settings, experimental=True)
        final = UnifiedButtonPrecheckResponse.from_precheck(
            result,
            latency_ms=int((monotonic() - started) * 1000),
        )
        if final.semantic_review.status != "completed" or "本次拜访分析：" not in final.feedback_text:
            diagnostics = experimental_final_audit(final.semantic_review)
            _logger.warning("experimental_final_failed request_ref=%s diagnostics=%s", hashlib.sha256(request_id.encode()).hexdigest()[:16], json.dumps(diagnostics))
            return {"status": "failed", "failure_category": experimental_failure_category(diagnostics["failure_reason"]),
                    "full_feedback_ms": int((monotonic() - started) * 1000), "diagnostics": diagnostics,
                    "phase_timings": phase_timings(result.semantic_review, int((monotonic()-started)*1000))}
        return {
            "status": "completed",
            "generated_at": datetime.now(UTC).isoformat(),
            "phase_timings": phase_timings(result.semantic_review, int((monotonic()-started)*1000)),
            "feedback_text": final.feedback_text,
            "final_feedback_hash": hashlib.sha256(final.feedback_text.encode()).hexdigest(),
            "provider_first_byte_ms": result.semantic_review.model_first_byte_ms,
            "provider_complete_ms": result.semantic_review.model_complete_ms,
            "full_feedback_ms": int((monotonic() - started) * 1000),
            "knowledge_cache_hit": result.semantic_review.cache_hit,
            "model_attempt_count": result.semantic_review.attempt_count,
            "diagnostics": experimental_final_audit(result.semantic_review),
            "recovered_after_retry": result.semantic_review.recovered_after_retry,
        }
    except Exception as exc:  # noqa: BLE001 - report only the safe exception class
        _logger.warning("interactive_quick_check_final_failed class=%s", type(exc).__name__)
        return {"status": "failed", "failure_category": "final_service_error"}
    finally:
        # _execute_unified_button_feedback uses normal precheck infrastructure.
        # Remove its transient request rows so unsaved browser text is not kept.
        get_store(settings).delete_prechecks_by_request_ids(
            canonical_request.context.tenant_id,
            [request_id, f"{request_id}__enrichment"],
        )


def _quick_check_run_preview(visit, settings, events):
    """Shared live wording channel; failures never discard the formal result."""
    from .front_v46.experimental_semantic_streaming_v22 import stream_semantic_preview_v22

    lease = None
    try:
        reviewer = get_agent(settings).semantic_reviewer
        if isinstance(reviewer, ChatModelReviewer):
            lease = reviewer.model_capacity.acquire("frontend", settings.frontend_model_timeout_seconds)
            if lease is None:
                raise TimeoutError("preview_queue_timeout")
        preview = stream_semantic_preview_v22(
            settings, visit,
            lambda text: events.put({"type": "preview_delta", "text": text}),
            interactive=True, live=True,
            reset=lambda: events.put({"type": "preview_reset"}),
        )
    except Exception:  # noqa: BLE001 - auxiliary failures must not discard Final
        preview = {"status": "failed", "failure_category": "preview_service_error"}
    finally:
        if lease is not None:
            lease.release()
    events.put({"type": "preview_complete", **preview})
    return preview


def _quick_check_run(
    canonical_request: PrecheckRequest,
    settings: Settings,
    events: Queue[dict[str, Any]],
    knowledge_basis: dict | None = None,
) -> dict[str, Any]:
    from .content_cache import run_with_knowledge_basis
    final_future = _quick_check_final_executor.submit(
        run_with_knowledge_basis, _quick_check_run_final, canonical_request, settings, knowledge_basis,
    )
    preview_future = _quick_check_preview_executor.submit(
        _quick_check_run_preview, canonical_request.visit, settings, events,
    )
    try:
        final = final_future.result()
    except Exception:  # noqa: BLE001 - worker failures become a traceable Final state
        final = {"status": "failed", "failure_category": "final_service_error"}
    # Final remains available immediately; Preview keeps running independently.
    preview = preview_future.result() if preview_future.done() else {"status": "processing"}
    return {"preview": preview, "final": final, "preview_future": preview_future}


def _quick_check_preview_snapshot(task: dict[str, Any]) -> dict[str, Any]:
    """Task-scoped memory only; all readers receive the same retained snapshot."""
    with _quick_check_lock:
        state = task.setdefault("preview_snapshot", {"text": "", "status": "processing"})
        outcome = task.get("outcome", {})
        future = outcome.get("preview_future")
        if future is not None and future.done():
            try:
                outcome["preview"] = future.result()
            except Exception:  # noqa: BLE001 - auxiliary Preview failure is isolated
                outcome["preview"] = {"status": "failed"}
        while True:
            try:
                event = task["events"].get_nowait()
            except Empty:
                break
            if event["type"] == "preview_delta" and state["status"] == "processing":
                state["text"] += event["text"]
            elif event["type"] == "preview_reset":
                state.update(text="", status="processing")
            elif event["type"] == "preview_complete":
                state["status"] = "completed" if event.get("status") == "completed" else "unavailable"
        preview = outcome.get("preview", {})
        if state["status"] == "processing" and preview.get("status") not in (None, "processing"):
            state["status"] = "completed" if preview["status"] == "completed" else "unavailable"
        return dict(state)


def _quick_check_resolve(task: dict[str, Any]) -> dict[str, Any] | None:
    future: Future[dict[str, Any]] = task["future"]
    if not future.done():
        return None
    if "outcome" not in task:
        try:
            outcome = future.result()
        except Exception:  # noqa: BLE001 - worker failures are rendered safely
            outcome = {"preview": {"status": "failed"}, "final": {"status": "failed", "failure_category": "final_service_error"}}
        final = outcome["final"]
        if final.get("status") == "completed" and (
            not isinstance(final.get("feedback_text"), str)
            or not final["feedback_text"].strip()
        ):
            outcome["final"] = final = {
                "status": "failed", "failure_category": "empty_final_feedback",
            }
        task["outcome"] = outcome
        task["status"] = "completed" if final.get("status") == "completed" else "failed"
        task["completed_at"] = datetime.now(UTC).isoformat()
    return task["outcome"]


def _quick_check_task(check_id: str, stream_token: str) -> dict[str, Any]:
    with _quick_check_lock:
        _quick_check_cleanup(monotonic())
        task = _quick_check_tasks.get(check_id)
        if task is None:
            from .quick_check_recovery import load
            task = load(check_id, get_store())
            if task is not None:
                _quick_check_tasks[check_id] = task
        launch_valid = task is not None and (
            monotonic() <= float(task["stream_token_expires_at"])
            and hmac.compare_digest(str(task["stream_token"]), stream_token)
        )
        session_valid = task is not None and isinstance(task.get("session_token"), str) and (
            monotonic() < float(task["expires_at"])
            and hmac.compare_digest(task["session_token"], stream_token)
        )
        if not (launch_valid or session_valid):
            raise HTTPException(status_code=404, detail="Not Found")
        return task


def _quick_check_task_response(task: dict[str, Any]) -> dict[str, Any]:
    outcome = _quick_check_resolve(task)
    result = {
        "check_id": task["check_id"],
        "status": task["status"],
        "check_sequence": task["check_sequence"],
        "input_hash": task["input_hash"],
        "created_at": task["created_at"],
        "completed_at": task.get("completed_at"),
        "acknowledged_at": task.get("acknowledged_at"),
    }
    if outcome is not None:
        preview, final = outcome["preview"], outcome["final"]
        result.update(
            preview_status=preview.get("status", "failed"),
            preview_hash=preview.get("feedback_hash"),
            final_status=final.get("status", "failed"),
            final_feedback_hash=final.get("final_feedback_hash"),
            failure_category=final.get("failure_category"),
            full_feedback_ms=final.get("full_feedback_ms"),
            diagnostics=final.get("diagnostics"),
        )
        if final.get("status") == "completed":
            result["final_feedback_text"] = final["feedback_text"]
    preview_snapshot = _quick_check_preview_snapshot(task)
    result["preview_status"] = preview_snapshot["status"]
    result["preview_feedback_text"] = preview_snapshot["text"]
    content_incomplete = task['status'] == 'completed' and (
        preview_snapshot['status'] in {'unavailable', 'failed'}
        or (result.get('diagnostics') or {}).get('suggestion_status') == 'incomplete'
    )
    result['content_complete'] = task['status'] == 'completed' and not content_incomplete and preview_snapshot['status'] == 'completed'
    result['recoverable'] = (task['status'] == 'failed' or content_incomplete) and bool(task.get('request_snapshot'))
    result['attempt'] = task.get('attempt',1)
    result['phase_timings'] = {**task.get('phase_timings',{}), **((outcome or {}).get('final',{}).get('phase_timings',{}))}
    preview_timing = (outcome or {}).get('preview', {})
    first, total = preview_timing.get('first_real_ai_text_ms'), preview_timing.get('semantic_complete_ms')
    result['phase_timings']['preview'] = {'first_byte_wait_ms':first,
        'generation_ms':max(0,total-first) if isinstance(first,int) and isinstance(total,int) else None,
        'total_ms':total, 'status':preview_timing.get('status','processing')}
    result['basic_feedback'] = task.get('basic_feedback','')
    result['front_policy'] = task.get('front_policy')
    result['preview_kind'] = 'ai' if task.get('front_policy') else ('basic' if task.get('basic_feedback') else 'ai')
    result['generated_at'] = (outcome or {}).get('final',{}).get('generated_at')
    # Shared content computations are not record revisions. The plugin guards
    # explicit writeback using a per-opening nonce, task ID and content hash.
    latest=get_store(task.get('_settings')).latest_quick_check(task['tenant_id'],task['record_code']) if task.get('request_snapshot') and not task.get('knowledge_basis') else None
    result['superseded'] = bool(latest and latest.get('input_hash') != task['input_hash'] and latest.get('created_at','') > task.get('created_at',''))
    result['retention_until'] = task.get('retention_until')
    _quick_check_persist(task)
    return result


@app.post("/api/v1/quick-check/tasks", status_code=status.HTTP_202_ACCEPTED)
def create_interactive_quick_check_task(
    request: dict[str, Any],
    x_tenant_id: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    if request.get("saved_record_check") is True:
        from .saved_record_check import launch
        return launch(request, x_tenant_id, x_api_key)
    canonical_request, settings, record_code, input_hash, user_id, force, basis = (
        _canonicalize_interactive_quick_check(request, x_tenant_id, x_api_key)
    )
    _require_interactive_quick_check(settings)
    from .front_v46 import POLICY_VERSION
    key = (canonical_request.context.tenant_id, user_id, record_code, input_hash)
    now = monotonic()
    with _quick_check_lock:
        _quick_check_cleanup(now)
        existing_id = _quick_check_idempotency.get(key)
        if not existing_id:
            saved=get_store(settings).find_quick_check(key[0],key[1],record_code,input_hash)
            if saved and saved.get('front_policy') == POLICY_VERSION:
                from .quick_check_recovery import load
                restored=load(saved['check_id'],get_store(settings))
                if restored:
                    restored['_settings'] = settings
                    existing_id=restored['check_id'];_quick_check_tasks[existing_id]=restored
                    _quick_check_idempotency[key]=existing_id
        if existing_id:
            existing = _quick_check_tasks.get(existing_id)
            if existing is not None:
                from .content_cache import reuse_allowed
                existing_response = _quick_check_task_response(existing)
            if existing is not None and not reuse_allowed(existing, existing_response, now):
                # An explicit click on the form can start again after failure.
                # Active/successful tasks still deduplicate; keep the failed audit.
                existing = None
                force = True
            if existing is not None and existing.get('front_policy') == POLICY_VERSION:
                if now > float(existing["stream_token_expires_at"]):
                    existing["stream_token"] = secrets.token_urlsafe(32)
                    existing["stream_token_until"] = datetime.now(UTC).timestamp() + settings.quick_check_stream_token_ttl_seconds
                    existing["stream_token_expires_at"] = (
                        now + settings.quick_check_stream_token_ttl_seconds
                    )
                    _quick_check_persist(existing)
                return {
                    **_quick_check_task_response(existing),
                    "opening_id": secrets.token_urlsafe(24),
                    "stream_token": existing["stream_token"],
                    "reused": True,
                    "expires_in_seconds": max(0, int(existing["stream_token_expires_at"] - now)),
                }
        check_id = f"qc_{uuid4().hex}"
        stream_token = secrets.token_urlsafe(32)
        events: Queue[dict[str, Any]] = Queue()
        if force:
            # A deliberate fresh check must not silently replay a persisted
            # wording artifact. Only the cache namespace changes; model input,
            # formal Prompt, business facts, and input_hash remain unchanged.
            canonical_request = canonical_request.model_copy(update={
                "context": canonical_request.context.model_copy(update={
                    "form_revision": f"experimental-027-fresh-{check_id}",
                }),
            })
        sequence_key = (canonical_request.context.tenant_id, record_code)
        previous = [
            item["check_sequence"]
            for item in _quick_check_tasks.values()
            if (item["tenant_id"], item["record_code"]) == sequence_key
        ]
        task = {
            "check_id": check_id,
            "tenant_id": canonical_request.context.tenant_id,
            "user_id": user_id,
            "record_code": record_code,
            "input_hash": input_hash,
            "knowledge_basis": basis,
            "idempotency_key": key,
            "check_sequence": max(previous, default=0) + 1,
            "status": "processing",
            "created_at": datetime.now(UTC).isoformat(),
            "expires_at": now + settings.quick_check_recovery_ttl_seconds,
            "cache_until": datetime.now(UTC).timestamp() + settings.quick_check_task_ttl_seconds,
            "retention_until": datetime.now(UTC).timestamp() + settings.quick_check_recovery_ttl_seconds,
            "stream_token_until": datetime.now(UTC).timestamp() + settings.quick_check_stream_token_ttl_seconds,
            "stream_token": stream_token,
            "stream_token_expires_at": now + settings.quick_check_stream_token_ttl_seconds,
            "events": events,
        }
        _quick_check_schedule(task, canonical_request, settings)
        _quick_check_tasks[check_id] = task
        _quick_check_idempotency[key] = check_id
    return {
        **_quick_check_task_response(task),
        "opening_id": secrets.token_urlsafe(24),
        "stream_token": stream_token,
        "reused": False,
        "expires_in_seconds": settings.quick_check_stream_token_ttl_seconds,
    }


@app.post(
    "/api/v1/experimental/quick-check-interactive/tasks/current-record",
    status_code=status.HTTP_202_ACCEPTED,
)
def create_experimental_interactive_quick_check_current_record_task(
    request: dict[str, Any],
    x_tenant_id: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    """Experimental saved-record launcher with an opt-in complete-page contract.

    Page mode uses the saved lookup for identity only, never business fallback.
    Existing callers stay in saved mode; no formal routes or mappings change.
    """
    tenant_id = str(x_tenant_id or "").strip()
    visit_record_code = str(request.get("visit_record_code") or "").strip()
    user_id = str(request.get("user_id") or "jiandaoyun-user").strip()
    if not tenant_id or not visit_record_code:
        raise HTTPException(status_code=422, detail="当前记录识别信息不完整，请重新打开候选检测。")
    authorize(tenant_id, x_tenant_id, x_api_key)
    settings = get_settings()
    _require_interactive_quick_check(settings)
    mapping = tenant_mapping(settings, tenant_id)
    page_snapshot = None
    if "page_snapshot" in request or "snapshot_mode" in request:
        from .experimental_page_snapshot import PageSnapshotError, current_page_snapshot

        if request.get("snapshot_mode") != "experimental_current_page_v1":
            raise HTTPException(status_code=422, detail="页面快照协议不匹配，请检查候选按钮配置。")
        try:
            page_snapshot = current_page_snapshot(request.get("page_snapshot"), mapping)
        except PageSnapshotError:
            raise HTTPException(
                status_code=422,
                detail="页面字段传递不完整或子表格式错误，请检查候选按钮绑定；未使用已保存内容替代。",
            ) from None
    app_id = str(mapping.get("source_application_id", "")).strip()
    entry_id = str(mapping.get("source_entry_id", "")).strip()
    # The candidate launcher uses the configured "拜访记录编码" as its sole
    # record identifier.  Unlike the separate Semantic V2.2 experiment, it
    # must not reuse the test form's "拜访记录签到识别" field.  Reading this
    # specification preserves the formal mapping unchanged while keeping the
    # candidate aligned with the same unique record code used by Quick Check.
    record_field = mapping.get("record_fields", {}).get("visit_record_code", {})
    field_id = str(record_field.get("widget_id", "")).strip()
    field_type = str(record_field.get("widget_type", "")).strip()
    if not all((app_id, entry_id, field_id, field_type)):
        raise HTTPException(status_code=503, detail="候选检测服务配置未完成。")
    try:
        form_snapshot = find_jiandaoyun_record_by_field(
            settings,
            tenant_id,
            app_id,
            entry_id,
            field_id,
            field_type,
            visit_record_code,
        )
    except JiandaoyunReadError:
        raise HTTPException(status_code=404, detail="未找到当前拜访记录，请保存后重新检测。") from None
    data_id = str(form_snapshot.get("_id") or "").strip()
    if not data_id:
        raise HTTPException(status_code=404, detail="未找到当前拜访记录，请保存后重新检测。")
    response = create_interactive_quick_check_task(
        {
            "record_code": visit_record_code,
            "data_id": data_id,
            "user_id": user_id or "jiandaoyun-user",
            "form_snapshot": page_snapshot if page_snapshot is not None else form_snapshot,
        },
        x_tenant_id=x_tenant_id,
        x_api_key=x_api_key,
    )
    source = "current_page_snapshot" if page_snapshot is not None else "saved_current_record"
    with _quick_check_lock:
        if response["check_id"] in _quick_check_tasks:
            _quick_check_tasks[response["check_id"]]["source"] = source
    return {**response, "experimental": True, "source": source}


async def _interactive_quick_check_events(
    request: Request,
    task: dict[str, Any],
) -> AsyncIterator[str]:
    started = monotonic()
    yield _quick_check_sse("started", {"check_id": task["check_id"], "status": "processing"})
    yield _quick_check_sse("stage", {"text": "正在进行TAORAN分析"})
    last_snapshot = None
    final_sent = False
    while True:
        if await request.is_disconnected():
            return
        if monotonic() >= task.get("expires_at", float("inf")):
            yield _quick_check_sse("error", {"code": "task_expired"})
            return
        outcome = _quick_check_resolve(task)
        snapshot = _quick_check_preview_snapshot(task)
        if snapshot != last_snapshot:
            yield _quick_check_sse("preview_snapshot", {"check_id": task["check_id"], **snapshot})
            last_snapshot = snapshot
        if outcome is not None and not final_sent:
            final = outcome["final"]
            final_sent = True
            if final.get("status") != "completed":
                yield _quick_check_sse("final_failed", {
                    "check_id": task["check_id"],
                    "code": final.get("failure_category", "final_service_error"),
                    "recoverable": bool(task.get("request_snapshot")),
                    "phase_timings": final.get("phase_timings",{}),
                })
            else:
                yield _quick_check_sse("final_completed", {
                    "check_id": task["check_id"],
                    "feedback_text": final["feedback_text"],
                    "final_feedback_hash": final["final_feedback_hash"],
                    "full_feedback_ms": final["full_feedback_ms"],
                    "phase_timings": final.get("phase_timings",{}),
                    "stream_elapsed_ms": int((monotonic() - started) * 1000),
                    "knowledge_cache_hit": final.get("knowledge_cache_hit"),
                    "model_attempt_count": final.get("model_attempt_count"),
                    "diagnostics": final.get("diagnostics"),
                    "recovered_after_retry": final.get("recovered_after_retry"),
                })
        if final_sent and snapshot["status"] != "processing":
            return
        yield ": keepalive\n\n"
        await asyncio.sleep(0.12)


@app.get("/api/v1/quick-check/tasks/{check_id}/events")
async def interactive_quick_check_events(
    request: Request,
    check_id: str,
    stream_token: str = Query(min_length=32, max_length=256),
) -> StreamingResponse:
    task = _quick_check_task(check_id, stream_token)
    return StreamingResponse(
        _interactive_quick_check_events(request, task),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.get("/api/v1/quick-check/tasks/{check_id}")
def get_interactive_quick_check_task(
    check_id: str,
    stream_token: str = Query(min_length=32, max_length=256),
) -> dict[str, Any]:
    return _quick_check_task_response(_quick_check_task(check_id, stream_token))


@app.post("/api/v1/quick-check/tasks/{check_id}/resume", status_code=202)
def resume_interactive_quick_check_task(check_id: str, stream_token: str = Query(min_length=32,max_length=256)):
    task = _quick_check_task(check_id, stream_token)
    with _quick_check_lock:
        result = _quick_check_task_response(task)
        if result.get('superseded'):
            raise HTTPException(status_code=409,detail='该任务属于旧记录版本，请分析最新记录')
        if not result.get('recoverable'):
            return result  # Idempotent for active and already-completed tasks.
        if task.get('knowledge_basis'):
            raise HTTPException(status_code=409, detail='请关闭弹窗后重新点击AI检测，将按当前内容及生效配置重新分析。')
        if not task.get('request_snapshot'):
            raise HTTPException(status_code=409, detail='原任务没有可恢复的输入快照')
        task.setdefault('attempt_history',[]).append({'attempt':task.get('attempt',1),
            'completed_at':task.get('completed_at'),'outcome':{k:v for k,v in task.get('outcome',{}).items() if k != 'preview_future'},
            'phase_timings':result['phase_timings']})
        original = PrecheckRequest.model_validate(task['request_snapshot'])
        original.context.form_revision = f"v47-resume-{check_id}-{task.get('attempt',1)+1}"
        _quick_check_schedule(task, original, get_settings())
        return _quick_check_task_response(task)


@app.post("/api/v1/quick-check/tasks/{check_id}/acknowledge")
def acknowledge_interactive_quick_check_task(
    check_id: str,
    stream_token: str = Query(min_length=32, max_length=256),
    client_timings: ClientTimings | None = None,
) -> dict[str, Any]:
    task = _quick_check_task(check_id, stream_token)
    result = _quick_check_task_response(task)
    if result.get("superseded"):
        raise HTTPException(status_code=409,detail="该意见属于旧记录版本，请打开最新版本的分析")
    if result["status"] != "completed" or "final_feedback_text" not in result:
        raise HTTPException(status_code=409, detail="AI检测尚未完成")
    if client_timings is not None:
        task["client_timings"] = {"basis": "popup_open_relative_ms", "reported_by": "browser",
                                  **client_timings.model_dump(exclude_none=True)}
        _quick_check_persist(task)
    if task.get("acknowledged_at") is None:
        task["acknowledged_at"] = datetime.now(UTC).isoformat()
        _quick_check_persist(task)
    # Deliberately no Jiandaoyun writeback here.  The parent page owns the
    # current unsaved form and writes the field only after origin validation.
    return {
        "check_id": check_id,
        "status": "acknowledged",
        "acknowledged_at": task["acknowledged_at"],
        "input_hash":task["input_hash"],"generated_at":result.get("generated_at"),
        "final_feedback_text": result["final_feedback_text"],
    }


@app.get("/quick-check/interactive", response_class=HTMLResponse)
def interactive_quick_check_page(
    request: Request,
    check_id: str = Query(min_length=3, max_length=100),
    stream_token: str = Query(min_length=32, max_length=256),
) -> HTMLResponse:
    """Minimal iframe page; the Jiandaoyun parent owns all form writes."""
    settings = get_settings()
    _require_interactive_quick_check(settings)
    # Validate before returning a page, without exposing the task body in HTML.
    try:
        task = _quick_check_task(check_id, stream_token)
    except HTTPException as exc:
        if exc.status_code not in {404, 410}:
            raise
        return HTMLResponse(
            '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>TAORAN AI检测</title><body><main><h1>TAORAN AI检测</h1>'
            '<p>检测链接已失效或任务已过期。请关闭当前弹窗，返回拜访记录界面重新点击“AI检测”。</p>'
            '</main></body></html>',
            status_code=exc.status_code,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
    # Redeem the short launch capability into a task-scoped, memory-only
    # session. Reading/reviewing a slow result must not be cut off at 120s.
    with _quick_check_lock:
        if "session_token" not in task:
            task["session_token"] = secrets.token_urlsafe(32)
        _quick_check_persist(task)
    public_path = (
        request.headers.get("X-Forwarded-Prefix")
        or settings.quick_check_public_path
    ).strip().rstrip("/")
    if public_path and not public_path.startswith("/"):
        raise HTTPException(status_code=500, detail="Invalid Quick Check public path")
    public_path_json = json.dumps(public_path).replace("<", "\\u003c")
    html = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>TAORAN AI检测</title>
<style>body{font:15px -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;margin:0;color:#172033;background:#fff}main{padding:22px;max-width:760px;margin:auto}h1{font-size:20px;margin:0 0 12px}.status{color:#15803d;font-weight:700;margin:8px 0 16px}.panel{background:#f5f8fa;border-radius:10px;padding:14px;white-space:pre-wrap;line-height:1.65;min-height:68px}.label{font-weight:600;margin:16px 0 8px}button{margin-top:18px;background:#0b9e95;color:#fff;border:0;border-radius:7px;padding:10px 20px;font-size:15px;cursor:pointer}button[disabled]{opacity:.55;cursor:default}.error{color:#b42318}</style></head><body><main>
<h1>TAORAN AI检测（experimental）</h1><p id="sourceNote">__TAORAN_SOURCE_NOTE__</p><div id="status" class="status">正在连接检测任务…</div>
<p id="returnNotice" role="note" style="background:#fff7e6;padding:12px;border-radius:7px;line-height:1.6">分析完成后，请点击“已读并返回”，可将 AI 最终反馈带回拜访记录填写页面。直接关闭弹窗不会同步反馈。</p>
<button id="resume" hidden type="button">恢复本次分析</button>
<div id="previewLabel" class="label">AI实时分析</div><div id="content" class="panel">AI正在分析，请稍候；可关闭后重新打开查看进度。</div>
<section id="finalPanel" hidden><div class="label">AI反馈意见</div><div id="finalContent" class="panel"></div></section>
<button id="ack" hidden disabled>已读并返回</button></main><script>
const publicPath=__TAORAN_PUBLIC_PATH__;
const sessionToken=__TAORAN_SESSION_TOKEN__;
const taskVersion=__TAORAN_TASK_VERSION__;
const frontPolicy=__TAORAN_FRONT_POLICY__;
__TAORAN_INTERACTIVE_SCRIPT__
</script></body></html>"""
    replacements={
        '__TAORAN_FRONT_POLICY__':json.dumps(task.get('front_policy')),
        '__TAORAN_TASK_VERSION__':json.dumps(task['input_hash']),
        '__TAORAN_PUBLIC_PATH__':public_path_json,
        '__TAORAN_SESSION_TOKEN__':json.dumps(task['session_token']),
        '__TAORAN_INTERACTIVE_SCRIPT__':files('taoran_agent').joinpath('interactive_quick_check.js').read_text(encoding='utf-8'),
        '__TAORAN_SOURCE_NOTE__':('本次检测已保存记录。页面上尚未保存的修改不会被读取。'
            if task.get('source')=='saved_current_record' else '本次检测按钮传入的当前页面数据；不会自动保存记录。'),
    }
    # One template pass: source text containing template markers stays data.
    import re
    html=re.sub('|'.join(map(re.escape,replacements)),lambda m:replacements[m.group()],html)
    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                "connect-src 'self'; base-uri 'none'; form-action 'none'; "
                "frame-ancestors https://www.jiandaoyun.com https://jiandaoyun.com"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _require_experimental_streaming(settings: Settings) -> None:
    """Keep the PoC unreachable from production and ordinary candidates."""
    if (
        settings.environment != "experimental"
        or not settings.experimental_streaming_poc_enabled
    ):
        raise HTTPException(status_code=404, detail="Not Found")


def _require_experimental_launch_token(settings: Settings, launch_token: str) -> None:
    """Authorize only the fixed, three-case iframe launcher."""
    configured = settings.experimental_streaming_poc_launch_token
    if (
        configured is None
        or len(launch_token) < 32
        or not hmac.compare_digest(configured.get_secret_value(), launch_token)
    ):
        raise HTTPException(status_code=404, detail="Not Found")


def _experimental_sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _experimental_cleanup_tasks(now: float) -> None:
    expired = [
        check_id
        for check_id, task in _experimental_streaming_tasks.items()
        if float(task["expires_at"]) <= now
    ]
    for check_id in expired:
        _experimental_streaming_tasks.pop(check_id, None)
    expired_record_launches = [
        record_launch_token
        for record_launch_token, launch in _experimental_jdy_record_launches.items()
        if float(launch["expires_at"]) <= now
    ]
    for record_launch_token in expired_record_launches:
        _experimental_jdy_record_launches.pop(record_launch_token, None)


def _experimental_create_jdy_record_launch(data_id: str) -> str:
    """Mint a short-lived, record-scoped iframe capability for the Jdy plugin."""
    record_launch_token = secrets.token_urlsafe(32)
    now = monotonic()
    with _experimental_streaming_lock:
        _experimental_cleanup_tasks(now)
        _experimental_jdy_record_launches[record_launch_token] = {
            "data_id": data_id,
            "expires_at": now + _EXPERIMENTAL_JDY_RECORD_LAUNCH_TTL_SECONDS,
        }
    return record_launch_token


def _require_experimental_jdy_record_launch(
    data_id: str,
    record_launch_token: str,
    *,
    consume: bool,
) -> None:
    """Verify the one-time Jdy extension launch without exposing a static secret."""
    with _experimental_streaming_lock:
        _experimental_cleanup_tasks(monotonic())
        launch = _experimental_jdy_record_launches.get(record_launch_token)
        if (
            launch is None
            or not hmac.compare_digest(str(launch["data_id"]), data_id)
        ):
            raise HTTPException(status_code=404, detail="Not Found")
        if consume:
            _experimental_jdy_record_launches.pop(record_launch_token, None)


def _experimental_run_quick_check(
    canonical_request: PrecheckRequest,
    settings: Settings,
    started_at: float,
) -> dict[str, Any]:
    """Run the unchanged 0.26.3 Quick Check on the isolated PoC service."""
    try:
        result = _execute_unified_button_feedback(canonical_request, settings)
        elapsed_ms = int((monotonic() - started_at) * 1000)
        final = UnifiedButtonPrecheckResponse.from_precheck(
            result,
            latency_ms=elapsed_ms,
        )
        return {
            "status": "completed",
            "feedback_text": final.feedback_text,
            "full_feedback_ms": elapsed_ms,
            "provider_first_byte_ms": result.semantic_review.model_first_byte_ms,
            "provider_complete_ms": result.semantic_review.model_complete_ms,
            # Telemetry only: this value describes the isolated experimental
            # service's wording cache.  It never changes the 0.26.3 result.
            "knowledge_cache_hit": result.semantic_review.cache_hit,
        }
    except Exception:  # The browser receives no provider, record, or stack data.
        _logger.exception("experimental_streaming_quick_check_failed")
        return {"status": "error"}


def _experimental_task_for_events(check_id: str, stream_token: str) -> dict[str, Any]:
    with _experimental_streaming_lock:
        _experimental_cleanup_tasks(monotonic())
        task = _experimental_streaming_tasks.get(check_id)
        if (
            task is None
            or not hmac.compare_digest(str(task["stream_token"]), stream_token)
        ):
            raise HTTPException(status_code=404, detail="experimental check not found")
        return task


def _experimental_enqueue_quick_check(
    canonical_request: PrecheckRequest,
    settings: Settings,
) -> dict[str, Any]:
    check_id = f"exp_{uuid4().hex}"
    stream_token = secrets.token_urlsafe(32)
    started_at = monotonic()
    future = _experimental_streaming_executor.submit(
        _experimental_run_quick_check,
        canonical_request,
        settings,
        started_at,
    )
    with _experimental_streaming_lock:
        _experimental_cleanup_tasks(started_at)
        _experimental_streaming_tasks[check_id] = {
            "tenant_id": canonical_request.context.tenant_id,
            "stream_token": stream_token,
            "created_at": started_at,
            "expires_at": started_at + _EXPERIMENTAL_STREAMING_TTL_SECONDS,
            "future": future,
        }
    return {
        "experimental": True,
        "check_id": check_id,
        "stream_token": stream_token,
        "status": "processing",
        "expires_in_seconds": int(_EXPERIMENTAL_STREAMING_TTL_SECONDS),
    }


@app.post("/api/v1/experimental/quick-check", status_code=status.HTTP_202_ACCEPTED)
def create_experimental_quick_check(
    request: dict[str, Any],
    x_tenant_id: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    """Create an isolated, read-only Stage Streaming PoC task.

    The input contract intentionally matches the formal button request, but this
    route is a different namespace, has a separate in-memory task/token store,
    and never invokes a submitted-evaluation or writeback function.
    """
    settings = get_settings()
    _require_experimental_streaming(settings)
    canonical_request, _ = _canonicalize_button_request(
        request,
        x_tenant_id,
        x_api_key,
    )
    return _experimental_enqueue_quick_check(canonical_request, settings)


@app.post("/api/v1/experimental/quick-check/golden/{case_id}", status_code=status.HTTP_202_ACCEPTED)
def create_experimental_golden_quick_check(
    case_id: str,
    launch_token: str = Query(min_length=32, max_length=256),
) -> dict[str, Any]:
    """Read and run one pre-approved Golden Case for the iframe PoC only."""
    settings = get_settings()
    _require_experimental_streaming(settings)
    _require_experimental_launch_token(settings, launch_token)
    record = _EXPERIMENTAL_GOLDEN_CASES.get(case_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Not Found")
    expected_code, data_id = record
    tenant_id = "tenant_demo"
    mapping = tenant_mapping(settings, tenant_id)
    form_data = get_jiandaoyun_record(
        settings,
        tenant_id,
        str(mapping["source_application_id"]),
        str(mapping["source_entry_id"]),
        data_id,
    )
    actual_code = str(
        mapped_jiandaoyun_value(
            form_data,
            mapping.get("record_fields", {}).get("visit_record_code"),
            "visit_record_code",
        )
        or data_id
    )
    if actual_code != expected_code:
        raise HTTPException(status_code=409, detail="experimental Golden Case changed")
    access_keys = settings.tenant_access_keys_for(tenant_id)
    if not access_keys:
        raise HTTPException(status_code=503, detail="experimental service unavailable")
    canonical_request, _ = _canonicalize_button_request(
        {
            "context": {
                "tenant_id": tenant_id,
                "request_id": f"experimental_iframe_{case_id}_{uuid4().hex}",
                "user_id": "experimental-streaming-poc",
                "source": "test",
                "source_record_id": data_id,
            },
            "form_data": form_data,
        },
        tenant_id,
        access_keys[0],
    )
    return _experimental_enqueue_quick_check(canonical_request, settings)


@app.post(
    "/api/v1/experimental/quick-check/current-record/{data_id}",
    status_code=status.HTTP_202_ACCEPTED,
)
def create_experimental_current_record_quick_check(
    data_id: str,
    launch_token: str | None = Query(default=None, min_length=32, max_length=256),
    record_launch_token: str | None = Query(default=None, min_length=32, max_length=256),
) -> dict[str, Any]:
    """Read the current record from the configured test form and run the PoC.

    This is deliberately separate from the formal button route.  The supplied
    identifier is useful only for a record in the configured test form, is read
    through the existing read-only Jiandaoyun API, and never reaches a formal
    scoring or writeback path.
    """
    settings = get_settings()
    _require_experimental_streaming(settings)
    data_id = data_id.strip()
    if not data_id or len(data_id) > 128:
        raise HTTPException(status_code=404, detail="Not Found")
    if launch_token is not None:
        _require_experimental_launch_token(settings, launch_token)
    elif record_launch_token is not None:
        _require_experimental_jdy_record_launch(
            data_id,
            record_launch_token,
            consume=True,
        )
    else:
        raise HTTPException(status_code=404, detail="Not Found")
    tenant_id = "tenant_demo"
    mapping = tenant_mapping(settings, tenant_id)
    app_id = str(mapping.get("source_application_id", "")).strip()
    entry_id = str(mapping.get("source_entry_id", "")).strip()
    if not app_id or not entry_id:
        raise HTTPException(status_code=503, detail="experimental service unavailable")
    try:
        form_data = get_jiandaoyun_record(
            settings,
            tenant_id,
            app_id,
            entry_id,
            data_id,
        )
    except JiandaoyunReadError:
        # Do not reveal whether a particular record exists to an iframe caller.
        raise HTTPException(status_code=404, detail="Not Found") from None
    access_keys = settings.tenant_access_keys_for(tenant_id)
    if not access_keys:
        raise HTTPException(status_code=503, detail="experimental service unavailable")
    canonical_request, _ = _canonicalize_button_request(
        {
            "context": {
                "tenant_id": tenant_id,
                "request_id": f"experimental_current_record_{uuid4().hex}",
                "user_id": "experimental-streaming-poc",
                # The canonical contract intentionally keeps the established
                # source enum. The request ID/route still mark this as PoC.
                "source": "test",
                "source_record_id": data_id,
            },
            "form_data": form_data,
        },
        tenant_id,
        access_keys[0],
    )
    return _experimental_enqueue_quick_check(canonical_request, settings)


@app.get("/api/v1/experimental/quick-check/jdy-current-record-launch")
def create_experimental_jdy_current_record_launch(
    data_id: str | None = None,
    visit_record_code: str | None = None,
    origin: str | None = Header(default=None),
) -> JSONResponse:
    """Create a record-scoped iframe URL for the Jdy front-end extension only.

    This endpoint deliberately returns no business data and does not run a
    check.  Its only role is to prevent a static experimental capability from
    being stored in the Jdy plugin source.
    """
    settings = get_settings()
    _require_experimental_streaming(settings)
    if origin != _EXPERIMENTAL_JDY_PLUGIN_ORIGIN:
        raise HTTPException(status_code=404, detail="Not Found")
    data_id = data_id.strip() if data_id else ""
    visit_record_code = visit_record_code.strip() if visit_record_code else ""
    if len(data_id) > 128 or len(visit_record_code) > 256:
        raise HTTPException(status_code=404, detail="Not Found")
    if bool(data_id) == bool(visit_record_code):
        raise HTTPException(status_code=404, detail="Not Found")
    if visit_record_code:
        tenant_id = "tenant_demo"
        mapping = tenant_mapping(settings, tenant_id)
        record_field = mapping.get("record_fields", {}).get("visit_record_code", {})
        field_id = str(record_field.get("widget_id", "")).strip()
        field_type = str(record_field.get("widget_type", "")).strip()
        app_id = str(mapping.get("source_application_id", "")).strip()
        entry_id = str(mapping.get("source_entry_id", "")).strip()
        if not all((field_id, field_type, app_id, entry_id)):
            raise HTTPException(status_code=503, detail="experimental service unavailable")
        try:
            record = find_jiandaoyun_record_by_field(
                settings,
                tenant_id,
                app_id,
                entry_id,
                field_id,
                field_type,
                visit_record_code,
            )
        except JiandaoyunReadError:
            # Do not reveal whether a given visit-record code exists.
            raise HTTPException(status_code=404, detail="Not Found") from None
        data_id = str(record["_id"])
    record_launch_token = _experimental_create_jdy_record_launch(data_id)
    launch_url = (
        "/experimental/quick-check-current-record?data_id="
        f"{quote(data_id, safe='')}&record_launch_token="
        f"{quote(record_launch_token, safe='')}"
    )
    return JSONResponse(
        {
            "experimental": True,
            "launch_url": launch_url,
            "expires_in_seconds": int(_EXPERIMENTAL_JDY_RECORD_LAUNCH_TTL_SECONDS),
        },
        headers={
            "Access-Control-Allow-Origin": _EXPERIMENTAL_JDY_PLUGIN_ORIGIN,
            "Vary": "Origin",
            "Cache-Control": "no-store",
            "X-TAORAN-Experimental": "streaming-poc",
        },
    )


async def _experimental_event_stream(
    request: Request,
    check_id: str,
    task: dict[str, Any],
) -> AsyncIterator[str]:
    started_at = float(task["created_at"])
    first_event_ms = int((monotonic() - started_at) * 1000)
    yield _experimental_sse("started", {
        "experimental": True,
        "check_id": check_id,
        "first_event_ms": first_event_ms,
    })
    yield _experimental_sse("delta", {
        "experimental": True,
        "mode": "stage",
        "text": "正在读取拜访记录并执行规则检查…",
    })
    await asyncio.sleep(0)
    yield _experimental_sse("delta", {
        "experimental": True,
        "mode": "stage",
        "text": "正在等待与 0.26.3 相同的 AI 分析完成…",
    })
    future: Future[dict[str, Any]] = task["future"]
    while not future.done():
        if await request.is_disconnected():
            return
        yield ": experimental keepalive\n\n"
        await asyncio.sleep(0.25)
    outcome = future.result()
    if outcome["status"] != "completed":
        yield _experimental_sse("error", {
            "experimental": True,
            "code": "EXPERIMENTAL_AI_UNAVAILABLE",
            "message": "AI分析暂时未完成，请关闭后重新发起实验检测。",
        })
        return
    # Stage mode deliberately exposes no unvalidated provider JSON. The final
    # user-visible text is exactly the existing 0.26.3 formatter output.
    yield _experimental_sse("completed", {
        "experimental": True,
        "check_id": check_id,
        "feedback_text": outcome["feedback_text"],
        "first_event_ms": first_event_ms,
        "provider_first_byte_ms": outcome["provider_first_byte_ms"],
        "provider_complete_ms": outcome["provider_complete_ms"],
        "first_real_ai_text_visible_ms": outcome["full_feedback_ms"],
        "full_feedback_ms": outcome["full_feedback_ms"],
        "raw_streaming_exposed": False,
    })


@app.get("/api/v1/experimental/quick-check/{check_id}/events")
async def experimental_quick_check_events(
    request: Request,
    check_id: str,
    stream_token: str = Query(min_length=32, max_length=256),
) -> StreamingResponse:
    settings = get_settings()
    _require_experimental_streaming(settings)
    task = _experimental_task_for_events(check_id, stream_token)
    return StreamingResponse(
        _experimental_event_stream(request, check_id, task),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "X-TAORAN-Experimental": "streaming-poc",
        },
    )


def _experimental_semantic_cleanup_tasks(now: float) -> None:
    expired = [
        check_id
        for check_id, task in _experimental_semantic_streaming_tasks.items()
        if float(task["expires_at"]) <= now
    ]
    for check_id in expired:
        _experimental_semantic_streaming_tasks.pop(check_id, None)
    expired_launches = [
        token
        for token, launch in _experimental_semantic_record_launches.items()
        if float(launch["expires_at"]) <= now
    ]
    for token in expired_launches:
        _experimental_semantic_record_launches.pop(token, None)


def _experimental_semantic_create_record_launch(data_id: str) -> str:
    token = secrets.token_urlsafe(32)
    now = monotonic()
    with _experimental_semantic_streaming_lock:
        _experimental_semantic_cleanup_tasks(now)
        _experimental_semantic_record_launches[token] = {
            "data_id": data_id,
            "expires_at": now + _EXPERIMENTAL_JDY_RECORD_LAUNCH_TTL_SECONDS,
        }
    return token


def _require_experimental_semantic_record_launch(
    data_id: str,
    token: str,
    *,
    consume: bool,
) -> None:
    with _experimental_semantic_streaming_lock:
        _experimental_semantic_cleanup_tasks(monotonic())
        launch = _experimental_semantic_record_launches.get(token)
        if (
            launch is None
            or not hmac.compare_digest(str(launch["data_id"]), data_id)
        ):
            raise HTTPException(status_code=404, detail="Not Found")
        if consume:
            _experimental_semantic_record_launches.pop(token, None)


def _experimental_run_semantic_quick_check(
    canonical_request: PrecheckRequest,
    settings: Settings,
    started_at: float,
    deltas: Queue[str],
) -> dict[str, Any]:
    """Run V2 preview beside the unmodified authoritative 0.26.3 final path.

    The preview is always marked provisional.  It cannot alter the final text,
    and a protocol failure merely discards it instead of manufacturing success.
    """
    formal_future = _experimental_streaming_executor.submit(
        _experimental_run_quick_check,
        canonical_request,
        settings,
        started_at,
    )
    semantic = stream_semantic_preview(settings, canonical_request.visit, deltas.put)
    authoritative = formal_future.result()
    return {"authoritative": authoritative, "semantic": semantic}


def _experimental_enqueue_semantic_quick_check(
    canonical_request: PrecheckRequest,
    settings: Settings,
) -> dict[str, Any]:
    check_id = f"sem_v2_{uuid4().hex}"
    stream_token = secrets.token_urlsafe(32)
    started_at = monotonic()
    deltas: Queue[str] = Queue()
    future = _experimental_semantic_streaming_executor.submit(
        _experimental_run_semantic_quick_check,
        canonical_request,
        settings,
        started_at,
        deltas,
    )
    with _experimental_semantic_streaming_lock:
        _experimental_semantic_cleanup_tasks(started_at)
        _experimental_semantic_streaming_tasks[check_id] = {
            "tenant_id": canonical_request.context.tenant_id,
            "stream_token": stream_token,
            "created_at": started_at,
            "expires_at": started_at + _EXPERIMENTAL_SEMANTIC_STREAMING_TTL_SECONDS,
            "deltas": deltas,
            "future": future,
        }
    return {
        "experimental": True,
        "semantic_streaming_v2": True,
        "check_id": check_id,
        "stream_token": stream_token,
        "status": "processing",
        "expires_in_seconds": int(_EXPERIMENTAL_SEMANTIC_STREAMING_TTL_SECONDS),
    }


def _experimental_semantic_task_for_events(check_id: str, stream_token: str) -> dict[str, Any]:
    with _experimental_semantic_streaming_lock:
        _experimental_semantic_cleanup_tasks(monotonic())
        task = _experimental_semantic_streaming_tasks.get(check_id)
        if (
            task is None
            or not hmac.compare_digest(str(task["stream_token"]), stream_token)
        ):
            raise HTTPException(status_code=404, detail="experimental check not found")
        return task


@app.post(
    "/api/v1/experimental/semantic-quick-check",
    status_code=status.HTTP_202_ACCEPTED,
)
def create_experimental_semantic_quick_check(
    request: dict[str, Any],
    x_tenant_id: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    """Authenticated V2 harness route; separate from every formal endpoint."""
    settings = get_settings()
    _require_experimental_streaming(settings)
    canonical_request, _ = _canonicalize_button_request(request, x_tenant_id, x_api_key)
    return _experimental_enqueue_semantic_quick_check(canonical_request, settings)


@app.post(
    "/api/v1/experimental/semantic-quick-check/golden/{case_id}",
    status_code=status.HTTP_202_ACCEPTED,
)
def create_experimental_semantic_golden_quick_check(
    case_id: str,
    launch_token: str = Query(min_length=32, max_length=256),
) -> dict[str, Any]:
    """Read a pre-approved Golden Case for V2 only; no writeback is possible."""
    settings = get_settings()
    _require_experimental_streaming(settings)
    _require_experimental_launch_token(settings, launch_token)
    record = _EXPERIMENTAL_GOLDEN_CASES.get(case_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Not Found")
    expected_code, data_id = record
    tenant_id = "tenant_demo"
    mapping = tenant_mapping(settings, tenant_id)
    form_data = get_jiandaoyun_record(
        settings, tenant_id, str(mapping["source_application_id"]),
        str(mapping["source_entry_id"]), data_id,
    )
    actual_code = str(
        mapped_jiandaoyun_value(
            form_data,
            mapping.get("record_fields", {}).get("visit_record_code"),
            "visit_record_code",
        ) or data_id
    )
    if actual_code != expected_code:
        raise HTTPException(status_code=409, detail="experimental Golden Case changed")
    access_keys = settings.tenant_access_keys_for(tenant_id)
    if not access_keys:
        raise HTTPException(status_code=503, detail="experimental service unavailable")
    canonical_request, _ = _canonicalize_button_request(
        {
            "context": {
                "tenant_id": tenant_id,
                "request_id": f"experimental_semantic_v2_{case_id}_{uuid4().hex}",
                "user_id": "experimental-semantic-streaming-v2",
                "source": "test",
                "source_record_id": data_id,
            },
            "form_data": form_data,
        }, tenant_id, access_keys[0],
    )
    return _experimental_enqueue_semantic_quick_check(canonical_request, settings)


@app.post(
    "/api/v1/experimental/semantic-quick-check/current-record/{data_id}",
    status_code=status.HTTP_202_ACCEPTED,
)
def create_experimental_semantic_current_record_quick_check(
    data_id: str,
    launch_token: str | None = Query(default=None, min_length=32, max_length=256),
    record_launch_token: str | None = Query(default=None, min_length=32, max_length=256),
) -> dict[str, Any]:
    settings = get_settings()
    _require_experimental_streaming(settings)
    data_id = data_id.strip()
    if not data_id or len(data_id) > 128:
        raise HTTPException(status_code=404, detail="Not Found")
    if launch_token is not None:
        _require_experimental_launch_token(settings, launch_token)
    elif record_launch_token is not None:
        _require_experimental_semantic_record_launch(data_id, record_launch_token, consume=True)
    else:
        raise HTTPException(status_code=404, detail="Not Found")
    tenant_id = "tenant_demo"
    mapping = tenant_mapping(settings, tenant_id)
    app_id = str(mapping.get("source_application_id", "")).strip()
    entry_id = str(mapping.get("source_entry_id", "")).strip()
    if not app_id or not entry_id:
        raise HTTPException(status_code=503, detail="experimental service unavailable")
    try:
        form_data = get_jiandaoyun_record(settings, tenant_id, app_id, entry_id, data_id)
    except JiandaoyunReadError:
        raise HTTPException(status_code=404, detail="Not Found") from None
    access_keys = settings.tenant_access_keys_for(tenant_id)
    if not access_keys:
        raise HTTPException(status_code=503, detail="experimental service unavailable")
    canonical_request, _ = _canonicalize_button_request(
        {
            "context": {
                "tenant_id": tenant_id,
                "request_id": f"experimental_semantic_v2_current_{uuid4().hex}",
                "user_id": "experimental-semantic-streaming-v2",
                "source": "test",
                "source_record_id": data_id,
            },
            "form_data": form_data,
        }, tenant_id, access_keys[0],
    )
    return _experimental_enqueue_semantic_quick_check(canonical_request, settings)


@app.get("/api/v1/experimental/semantic-quick-check/jdy-current-record-launch")
def create_experimental_semantic_jdy_current_record_launch(
    visit_record_code: str = Query(min_length=1, max_length=256),
    origin: str | None = Header(default=None),
) -> JSONResponse:
    """Mint a V2-only, short-lived iframe capability for the current test record."""
    settings = get_settings()
    _require_experimental_streaming(settings)
    if origin != _EXPERIMENTAL_JDY_PLUGIN_ORIGIN:
        raise HTTPException(status_code=404, detail="Not Found")
    tenant_id = "tenant_demo"
    mapping = tenant_mapping(settings, tenant_id)
    field = mapping.get("record_fields", {}).get("visit_record_code", {})
    app_id = str(mapping.get("source_application_id", "")).strip()
    entry_id = str(mapping.get("source_entry_id", "")).strip()
    field_id = str(field.get("widget_id", "")).strip()
    field_type = str(field.get("widget_type", "")).strip()
    if not all((app_id, entry_id, field_id, field_type)):
        raise HTTPException(status_code=503, detail="experimental service unavailable")
    try:
        record = find_jiandaoyun_record_by_field(
            settings, tenant_id, app_id, entry_id, field_id, field_type, visit_record_code.strip(),
        )
    except JiandaoyunReadError:
        raise HTTPException(status_code=404, detail="Not Found") from None
    data_id = str(record["_id"])
    token = _experimental_semantic_create_record_launch(data_id)
    launch_url = (
        "/experimental/semantic-quick-check-current-record?data_id="
        f"{quote(data_id, safe='')}&record_launch_token={quote(token, safe='')}"
    )
    return JSONResponse(
        {"experimental": True, "semantic_streaming_v2": True, "launch_url": launch_url,
         "expires_in_seconds": int(_EXPERIMENTAL_JDY_RECORD_LAUNCH_TTL_SECONDS)},
        headers={"Access-Control-Allow-Origin": _EXPERIMENTAL_JDY_PLUGIN_ORIGIN,
                 "Vary": "Origin", "Cache-Control": "no-store",
                 "X-TAORAN-Experimental": "semantic-streaming-v2"},
    )


async def _experimental_semantic_event_stream(
    request: Request, check_id: str, task: dict[str, Any],
) -> AsyncIterator[str]:
    started_at = float(task["created_at"])
    first_event_ms = int((monotonic() - started_at) * 1000)
    yield _experimental_sse("started", {"experimental": True, "semantic_streaming_v2": True,
                                        "check_id": check_id, "first_event_ms": first_event_ms})
    yield _experimental_sse("stage", {"experimental": True, "text": "正在读取当前记录并生成实验性语义分析…"})
    future: Future[dict[str, Any]] = task["future"]
    deltas: Queue[str] = task["deltas"]
    while not future.done():
        if await request.is_disconnected():
            return
        while True:
            try:
                delta = deltas.get_nowait()
            except Empty:
                break
            yield _experimental_sse("feedback_delta", {"experimental": True,
                                                        "provisional": True, "text": delta})
        yield ": experimental keepalive\n\n"
        await asyncio.sleep(0.12)
    while True:
        try:
            delta = deltas.get_nowait()
        except Empty:
            break
        yield _experimental_sse("feedback_delta", {"experimental": True,
                                                    "provisional": True, "text": delta})
    outcome = future.result()
    authoritative = outcome["authoritative"]
    semantic = outcome["semantic"]
    if authoritative.get("status") != "completed":
        yield _experimental_sse("error", {"experimental": True,
                                           "code": "EXPERIMENTAL_AI_UNAVAILABLE",
                                           "message": "AI分析暂时未完成，请关闭后重新发起实验检测。"})
        return
    if semantic.get("status") != "completed":
        yield _experimental_sse("provisional_discarded", {
            "experimental": True, "failure_category": semantic.get("failure_category", "upstream_service_error"),
        })
    yield _experimental_sse("completed", {
        "experimental": True,
        "semantic_streaming_v2": True,
        "check_id": check_id,
        "feedback_text": authoritative["feedback_text"],
        "first_event_ms": first_event_ms,
        "first_real_ai_text_ms": semantic.get("first_real_ai_text_ms"),
        "semantic_complete_ms": semantic.get("semantic_complete_ms"),
        "full_feedback_ms": authoritative["full_feedback_ms"],
        "provider_first_byte_ms": authoritative["provider_first_byte_ms"],
        "provider_complete_ms": authoritative["provider_complete_ms"],
        "preview_validated": semantic.get("status") == "completed",
        "preview_evidence_count": semantic.get("evidence_count", 0),
    })


@app.get("/api/v1/experimental/semantic-quick-check/{check_id}/events")
async def experimental_semantic_quick_check_events(
    request: Request,
    check_id: str,
    stream_token: str = Query(min_length=32, max_length=256),
) -> StreamingResponse:
    settings = get_settings()
    _require_experimental_streaming(settings)
    task = _experimental_semantic_task_for_events(check_id, stream_token)
    return StreamingResponse(
        _experimental_semantic_event_stream(request, check_id, task),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no",
                 "X-TAORAN-Experimental": "semantic-streaming-v2"},
    )


def _experimental_semantic_v21_cleanup(now: float) -> None:
    expired = [
        check_id for check_id, task in _experimental_semantic_v21_tasks.items()
        if float(task["expires_at"]) <= now
    ]
    for check_id in expired:
        _experimental_semantic_v21_tasks.pop(check_id, None)


def _experimental_run_semantic_v21(
    canonical_request: PrecheckRequest,
    settings: Settings,
    started_at: float,
    deltas: Queue[str],
) -> dict[str, Any]:
    # The authoritative 0.26.3 result remains unchanged and independent from
    # the preview.  V2.1 only changes the experimental evidence ownership.
    formal_future = _experimental_streaming_executor.submit(
        _experimental_run_quick_check, canonical_request, settings, started_at,
    )
    semantic = stream_semantic_preview_v21(settings, canonical_request.visit, deltas.put)
    return {"authoritative": formal_future.result(), "semantic": semantic}


def _experimental_enqueue_semantic_v21(
    canonical_request: PrecheckRequest, settings: Settings,
) -> dict[str, Any]:
    check_id = f"sem_v21_{uuid4().hex}"
    stream_token = secrets.token_urlsafe(32)
    started_at = monotonic()
    deltas: Queue[str] = Queue()
    future = _experimental_semantic_v21_executor.submit(
        _experimental_run_semantic_v21, canonical_request, settings, started_at, deltas,
    )
    with _experimental_semantic_v21_lock:
        _experimental_semantic_v21_cleanup(started_at)
        _experimental_semantic_v21_tasks[check_id] = {
            "tenant_id": canonical_request.context.tenant_id,
            "stream_token": stream_token,
            "created_at": started_at,
            "expires_at": started_at + _EXPERIMENTAL_SEMANTIC_V21_TTL_SECONDS,
            "deltas": deltas,
            "future": future,
        }
    return {
        "experimental": True, "semantic_streaming_v21": True, "check_id": check_id,
        "stream_token": stream_token, "status": "processing",
        "expires_in_seconds": int(_EXPERIMENTAL_SEMANTIC_V21_TTL_SECONDS),
    }


def _experimental_semantic_v21_task(check_id: str, stream_token: str) -> dict[str, Any]:
    with _experimental_semantic_v21_lock:
        _experimental_semantic_v21_cleanup(monotonic())
        task = _experimental_semantic_v21_tasks.get(check_id)
        if task is None or not hmac.compare_digest(str(task["stream_token"]), stream_token):
            raise HTTPException(status_code=404, detail="experimental check not found")
        return task


@app.post(
    "/api/v1/experimental/semantic-quick-check-v21",
    status_code=status.HTTP_202_ACCEPTED,
)
def create_experimental_semantic_v21(
    request: dict[str, Any],
    x_tenant_id: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    settings = get_settings()
    _require_experimental_streaming(settings)
    canonical_request, _ = _canonicalize_button_request(request, x_tenant_id, x_api_key)
    return _experimental_enqueue_semantic_v21(canonical_request, settings)


@app.post(
    "/api/v1/experimental/semantic-quick-check-v21/golden/{case_id}",
    status_code=status.HTTP_202_ACCEPTED,
)
def create_experimental_semantic_v21_golden(
    case_id: str,
    launch_token: str = Query(min_length=32, max_length=256),
) -> dict[str, Any]:
    settings = get_settings()
    _require_experimental_streaming(settings)
    _require_experimental_launch_token(settings, launch_token)
    record = _EXPERIMENTAL_GOLDEN_CASES.get(case_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Not Found")
    expected_code, data_id = record
    tenant_id = "tenant_demo"
    mapping = tenant_mapping(settings, tenant_id)
    form_data = get_jiandaoyun_record(
        settings, tenant_id, str(mapping["source_application_id"]),
        str(mapping["source_entry_id"]), data_id,
    )
    actual_code = str(mapped_jiandaoyun_value(
        form_data, mapping.get("record_fields", {}).get("visit_record_code"), "visit_record_code",
    ) or data_id)
    if actual_code != expected_code:
        raise HTTPException(status_code=409, detail="experimental Golden Case changed")
    access_keys = settings.tenant_access_keys_for(tenant_id)
    if not access_keys:
        raise HTTPException(status_code=503, detail="experimental service unavailable")
    canonical_request, _ = _canonicalize_button_request(
        {
            "context": {
                "tenant_id": tenant_id,
                "request_id": f"experimental_semantic_v21_{case_id}_{uuid4().hex}",
                "user_id": "experimental-semantic-v21",
                "source": "test",
                "source_record_id": data_id,
            },
            "form_data": form_data,
        }, tenant_id, access_keys[0],
    )
    return _experimental_enqueue_semantic_v21(canonical_request, settings)


async def _experimental_semantic_v21_events(
    request: Request, check_id: str, task: dict[str, Any],
) -> AsyncIterator[str]:
    started_at = float(task["created_at"])
    first_event_ms = int((monotonic() - started_at) * 1000)
    yield _experimental_sse("started", {
        "experimental": True, "semantic_streaming_v21": True,
        "check_id": check_id, "first_event_ms": first_event_ms,
    })
    yield _experimental_sse("stage", {
        "experimental": True, "text": "正在生成分析并由系统核对原始记录证据…",
    })
    future: Future[dict[str, Any]] = task["future"]
    deltas: Queue[str] = task["deltas"]
    while not future.done():
        if await request.is_disconnected():
            return
        while True:
            try:
                delta = deltas.get_nowait()
            except Empty:
                break
            yield _experimental_sse("feedback_delta", {
                "experimental": True, "provisional": True, "text": delta,
            })
        yield ": experimental keepalive\n\n"
        await asyncio.sleep(0.12)
    while True:
        try:
            delta = deltas.get_nowait()
        except Empty:
            break
        yield _experimental_sse("feedback_delta", {
            "experimental": True, "provisional": True, "text": delta,
        })
    outcome = future.result()
    authoritative = outcome["authoritative"]
    semantic = outcome["semantic"]
    if authoritative.get("status") != "completed":
        yield _experimental_sse("error", {
            "experimental": True, "code": "EXPERIMENTAL_AI_UNAVAILABLE",
            "message": "AI分析暂时未完成，请关闭后重新发起实验检测。",
        })
        return
    validation_started = monotonic()
    if semantic.get("status") == "completed":
        yield _experimental_sse("provisional_confirmed", {
            "experimental": True, "evidence_count": len(semantic.get("evidence", [])),
            "matched_fields": semantic.get("matched_fields", []),
        })
    else:
        yield _experimental_sse("provisional_discarded", {
            "experimental": True,
            "failure_category": semantic.get("failure_category", "upstream_service_error"),
        })
    yield _experimental_sse("completed", {
        "experimental": True, "semantic_streaming_v21": True, "check_id": check_id,
        "feedback_text": authoritative["feedback_text"],
        "first_event_ms": first_event_ms,
        "first_real_ai_text_ms": semantic.get("first_real_ai_text_ms"),
        "semantic_complete_ms": semantic.get("semantic_complete_ms"),
        "full_feedback_ms": authoritative["full_feedback_ms"],
        "preview_validated": semantic.get("status") == "completed",
        "evidence_builder_status": semantic.get("evidence_builder_status", "failed"),
        "exact_match_count": semantic.get("exact_match_count", 0),
        "normalized_match_count": semantic.get("normalized_match_count", 0),
        "unmatched_hint_count": semantic.get("unmatched_hint_count", 0),
        "matched_fields": semantic.get("matched_fields", []),
        "provisional_validation_ms": int((monotonic() - validation_started) * 1000),
    })


@app.get("/api/v1/experimental/semantic-quick-check-v21/{check_id}/events")
async def experimental_semantic_v21_events(
    request: Request,
    check_id: str,
    stream_token: str = Query(min_length=32, max_length=256),
) -> StreamingResponse:
    settings = get_settings()
    _require_experimental_streaming(settings)
    return StreamingResponse(
        _experimental_semantic_v21_events(
            request, check_id, _experimental_semantic_v21_task(check_id, stream_token),
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no",
                 "X-TAORAN-Experimental": "semantic-streaming-v21"},
    )


def _experimental_semantic_v22_cleanup(now: float) -> None:
    for check_id in [
        item for item, task in _experimental_semantic_v22_tasks.items()
        if float(task["expires_at"]) <= now
    ]:
        _experimental_semantic_v22_tasks.pop(check_id, None)
    for token in [
        item for item, launch in _experimental_semantic_v22_review_launches.items()
        if float(launch["expires_at"]) <= now
    ]:
        _experimental_semantic_v22_review_launches.pop(token, None)


def _experimental_semantic_v22_create_review_launch(data_id: str | None = None) -> str:
    """Mint a short-lived capability for the isolated human-review page.

    This is deliberately not a formal credential and contains no business
    data.  It avoids putting the static experimental launcher secret in the
    Jiandaoyun extension source.
    """
    token = secrets.token_urlsafe(32)
    now = monotonic()
    with _experimental_semantic_v22_lock:
        _experimental_semantic_v22_cleanup(now)
        _experimental_semantic_v22_review_launches[token] = {
            "expires_at": now + _EXPERIMENTAL_SEMANTIC_V22_REVIEW_LAUNCH_TTL_SECONDS,
            # A current-record launch is bound to one configured test-form
            # record.  Generic launches intentionally remain Golden-Case only.
            "data_id": data_id,
        }
    return token


def _require_experimental_semantic_v22_review_launch(token: str) -> dict[str, Any]:
    with _experimental_semantic_v22_lock:
        _experimental_semantic_v22_cleanup(monotonic())
        launch = _experimental_semantic_v22_review_launches.get(token)
        if launch is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return launch


def _require_experimental_semantic_v22_review_access(
    settings: Settings,
    launch_token: str | None,
    review_launch_token: str | None,
) -> None:
    """Accept the administrator launcher or the Jdy-minted review launcher."""
    if bool(launch_token) == bool(review_launch_token):
        raise HTTPException(status_code=404, detail="Not Found")
    if launch_token is not None:
        _require_experimental_launch_token(settings, launch_token)
    else:
        _require_experimental_semantic_v22_review_launch(review_launch_token or "")


def _experimental_run_semantic_v22(
    canonical_request: PrecheckRequest, settings: Settings, started_at: float, deltas: Queue[str],
) -> dict[str, Any]:
    formal_future = _experimental_streaming_executor.submit(
        _experimental_run_quick_check, canonical_request, settings, started_at,
    )
    preview = stream_semantic_preview_v22(settings, canonical_request.visit, deltas.put)
    return {"authoritative": formal_future.result(), "preview": preview}


def _experimental_enqueue_semantic_v22(
    canonical_request: PrecheckRequest, settings: Settings,
) -> dict[str, Any]:
    check_id = f"sem_v22_{uuid4().hex}"
    stream_token = secrets.token_urlsafe(32)
    started_at = monotonic()
    deltas: Queue[str] = Queue()
    future = _experimental_semantic_v22_executor.submit(
        _experimental_run_semantic_v22, canonical_request, settings, started_at, deltas,
    )
    with _experimental_semantic_v22_lock:
        _experimental_semantic_v22_cleanup(started_at)
        _experimental_semantic_v22_tasks[check_id] = {
            "stream_token": stream_token, "created_at": started_at,
            "expires_at": started_at + _EXPERIMENTAL_SEMANTIC_V22_TTL_SECONDS,
            "deltas": deltas, "future": future,
        }
    return {
        "experimental": True, "semantic_streaming_v22": True, "check_id": check_id,
        "stream_token": stream_token, "status": "processing",
        "expires_in_seconds": int(_EXPERIMENTAL_SEMANTIC_V22_TTL_SECONDS),
    }


def _experimental_semantic_v22_task(check_id: str, stream_token: str) -> dict[str, Any]:
    with _experimental_semantic_v22_lock:
        _experimental_semantic_v22_cleanup(monotonic())
        task = _experimental_semantic_v22_tasks.get(check_id)
        if task is None or not hmac.compare_digest(str(task["stream_token"]), stream_token):
            raise HTTPException(status_code=404, detail="experimental check not found")
        return task


@app.post(
    "/api/v1/experimental/semantic-quick-check-v22",
    status_code=status.HTTP_202_ACCEPTED,
)
def create_experimental_semantic_v22(
    request: dict[str, Any],
    x_tenant_id: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    settings = get_settings()
    _require_experimental_streaming(settings)
    canonical_request, _ = _canonicalize_button_request(request, x_tenant_id, x_api_key)
    return _experimental_enqueue_semantic_v22(canonical_request, settings)


async def _experimental_semantic_v22_events(
    request: Request, check_id: str, task: dict[str, Any],
) -> AsyncIterator[str]:
    started_at = float(task["created_at"])
    first_event_ms = int((monotonic() - started_at) * 1000)
    yield _experimental_sse("started", {
        "experimental": True, "semantic_streaming_v22": True,
        "check_id": check_id, "first_event_ms": first_event_ms,
    })
    yield _experimental_sse("stage", {
        "experimental": True, "text": "AI正在分析当前拜访记录…",
    })
    future: Future[dict[str, Any]] = task["future"]
    deltas: Queue[str] = task["deltas"]
    while not future.done():
        if await request.is_disconnected():
            return
        while True:
            try:
                delta = deltas.get_nowait()
            except Empty:
                break
            yield _experimental_sse("feedback_delta", {
                "experimental": True, "assistive": True, "text": delta,
            })
        yield ": experimental keepalive\n\n"
        await asyncio.sleep(0.12)
    while True:
        try:
            delta = deltas.get_nowait()
        except Empty:
            break
        yield _experimental_sse("feedback_delta", {
            "experimental": True, "assistive": True, "text": delta,
        })
    outcome = future.result()
    authoritative = outcome["authoritative"]
    preview = outcome["preview"]
    if authoritative.get("status") != "completed":
        yield _experimental_sse("error", {
            "experimental": True, "code": "EXPERIMENTAL_AI_UNAVAILABLE",
            "message": "AI分析暂时未完成，请关闭后重新发起实验检测。",
        })
        return
    if preview.get("status") != "completed":
        yield _experimental_sse("provisional_discarded", {
            "experimental": True,
            "failure_category": preview.get("failure_category", "upstream_service_error"),
        })
    yield _experimental_sse("completed", {
        "experimental": True, "semantic_streaming_v22": True, "check_id": check_id,
        "feedback_text": authoritative["feedback_text"],
        "first_event_ms": first_event_ms,
        "first_real_ai_text_ms": preview.get("first_real_ai_text_ms"),
        "semantic_complete_ms": preview.get("semantic_complete_ms"),
        "full_feedback_ms": authoritative["full_feedback_ms"],
        "provider_first_byte_ms": authoritative["provider_first_byte_ms"],
        "provider_complete_ms": authoritative["provider_complete_ms"],
        "knowledge_cache_hit": authoritative.get("knowledge_cache_hit", False),
        # The authoritative final is already fully formatted and validated by
        # the unchanged 0.26.3 path when this event is emitted.
        "final_validation_complete_ms": authoritative["full_feedback_ms"],
        "preview_displayed": preview.get("status") == "completed",
        "preview_length": preview.get("feedback_length", 0),
        "preview_hash": preview.get("feedback_hash"),
        "specific_fact_claim_count": preview.get("specific_fact_claim_count", 0),
        "unsupported_specific_fact_count": preview.get("unsupported_specific_fact_count", 0),
        "evidence_builder_status": preview.get("evidence_builder_status", "not_available"),
        # The final replaces, rather than merges with, the preview; no preview
        # text is persisted or presented as a formal conclusion.
        "serious_early_final_ui_conflict": False,
    })


@app.get("/api/v1/experimental/semantic-quick-check-v22/{check_id}/events")
async def experimental_semantic_v22_events(
    request: Request,
    check_id: str,
    stream_token: str = Query(min_length=32, max_length=256),
) -> StreamingResponse:
    settings = get_settings()
    _require_experimental_streaming(settings)
    return StreamingResponse(
        _experimental_semantic_v22_events(
            request, check_id, _experimental_semantic_v22_task(check_id, stream_token),
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no",
                 "X-TAORAN-Experimental": "semantic-streaming-v22"},
    )


def _experimental_semantic_v22_tag_review_task(
    check_id: str,
    *,
    case_id: str | None = None,
    data_id: str | None = None,
) -> None:
    with _experimental_semantic_v22_lock:
        task = _experimental_semantic_v22_tasks.get(check_id)
        if task is not None:
            if case_id is not None:
                task["review_case_id"] = case_id
            if data_id is not None:
                task["review_data_id"] = data_id


def _experimental_semantic_v22_review_path(settings: Settings) -> Path:
    return Path(settings.database_path).parent / "semantic_streaming_v22_review.jsonl"


def _experimental_semantic_v22_review_was_saved(path: Path, check_id: str) -> bool:
    """Return whether this isolated review task already has one saved decision."""
    if not path.is_file():
        return False
    with _experimental_semantic_v22_review_lock, path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("run_id") == check_id:
                return True
    return False


@app.post(
    "/api/v1/experimental/semantic-quick-check-v22/review/golden/{case_id}",
    status_code=status.HTTP_202_ACCEPTED,
)
def create_experimental_semantic_v22_review_golden(
    case_id: str,
    launch_token: str | None = Query(default=None, min_length=32, max_length=256),
    review_launch_token: str | None = Query(default=None, min_length=32, max_length=256),
) -> dict[str, Any]:
    """Start one approved Golden Case for browser-only human review."""
    settings = get_settings()
    _require_experimental_streaming(settings)
    _require_experimental_semantic_v22_review_access(
        settings, launch_token, review_launch_token,
    )
    record = _EXPERIMENTAL_GOLDEN_CASES.get(case_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return _experimental_semantic_v22_start_review_case(case_id, record, settings)


@app.post(
    "/api/v1/experimental/semantic-quick-check-v22/review/expansion/{case_id}",
    status_code=status.HTTP_202_ACCEPTED,
)
def create_experimental_semantic_v22_review_expansion(
    case_id: str,
    launch_token: str | None = Query(default=None, min_length=32, max_length=256),
    review_launch_token: str | None = Query(default=None, min_length=32, max_length=256),
) -> dict[str, Any]:
    """Run one approved 15-case expansion record in the isolated review lane."""
    settings = get_settings()
    _require_experimental_streaming(settings)
    _require_experimental_semantic_v22_review_access(
        settings, launch_token, review_launch_token,
    )
    record = _EXPERIMENTAL_SEMANTIC_V22_EXPANSION_CASES.get(case_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return _experimental_semantic_v22_start_review_case(case_id, record, settings)


def _experimental_semantic_v22_start_review_case(
    case_id: str,
    record: tuple[str, str],
    settings: Settings,
) -> dict[str, Any]:
    """Build one whitelisted browser-only case without formal writeback."""
    expected_code, data_id = record
    tenant_id = "tenant_demo"
    mapping = tenant_mapping(settings, tenant_id)
    form_data = get_jiandaoyun_record(
        settings, tenant_id, str(mapping["source_application_id"]),
        str(mapping["source_entry_id"]), data_id,
    )
    actual_code = str(mapped_jiandaoyun_value(
        form_data, mapping.get("record_fields", {}).get("visit_record_code"), "visit_record_code",
    ) or data_id)
    if actual_code != expected_code:
        raise HTTPException(status_code=409, detail="experimental Golden Case changed")
    access_keys = settings.tenant_access_keys_for(tenant_id)
    if not access_keys:
        raise HTTPException(status_code=503, detail="experimental service unavailable")
    canonical_request, _ = _canonicalize_button_request(
        {
            "context": {
                "tenant_id": tenant_id,
                "request_id": f"experimental_semantic_v22_review_{case_id}_{uuid4().hex}",
                "user_id": "experimental-semantic-v22-review",
                "source": "test",
                "source_record_id": data_id,
            },
            "form_data": form_data,
        }, tenant_id, access_keys[0],
    )
    task = _experimental_enqueue_semantic_v22(canonical_request, settings)
    _experimental_semantic_v22_tag_review_task(task["check_id"], case_id=case_id)
    return task


@app.post(
    "/api/v1/experimental/semantic-quick-check-v22/review/current-record",
    status_code=status.HTTP_202_ACCEPTED,
)
def create_experimental_semantic_v22_review_current_record(
    review_launch_token: str = Query(min_length=32, max_length=256),
) -> dict[str, Any]:
    """Run V2.2 for exactly the record bound to a Jdy review-page launch.

    This remains separate from the production button endpoint.  The only
    possible write is deferred until a human completes the browser review.
    """
    settings = get_settings()
    _require_experimental_streaming(settings)
    launch = _require_experimental_semantic_v22_review_launch(review_launch_token)
    data_id = str(launch.get("data_id") or "").strip()
    if not data_id:
        raise HTTPException(status_code=404, detail="Not Found")
    tenant_id = "tenant_demo"
    mapping = tenant_mapping(settings, tenant_id)
    app_id = str(mapping.get("source_application_id", "")).strip()
    entry_id = str(mapping.get("source_entry_id", "")).strip()
    if not app_id or not entry_id:
        raise HTTPException(status_code=503, detail="experimental service unavailable")
    try:
        form_data = get_jiandaoyun_record(settings, tenant_id, app_id, entry_id, data_id)
    except JiandaoyunReadError:
        raise HTTPException(status_code=404, detail="Not Found") from None
    access_keys = settings.tenant_access_keys_for(tenant_id)
    if not access_keys:
        raise HTTPException(status_code=503, detail="experimental service unavailable")
    canonical_request, _ = _canonicalize_button_request(
        {
            "context": {
                "tenant_id": tenant_id,
                "request_id": f"experimental_semantic_v22_review_current_{uuid4().hex}",
                "user_id": "experimental-semantic-v22-review",
                "source": "test",
                "source_record_id": data_id,
            },
            "form_data": form_data,
        }, tenant_id, access_keys[0],
    )
    task = _experimental_enqueue_semantic_v22(canonical_request, settings)
    _experimental_semantic_v22_tag_review_task(task["check_id"], data_id=data_id)
    return task


@app.get("/api/v1/experimental/semantic-quick-check-v22/review-launch")
def create_experimental_semantic_v22_review_launch(
    origin: str | None = Header(default=None),
) -> JSONResponse:
    """Create a Jiandaoyun-only link to the isolated V2.2 review page."""
    settings = get_settings()
    _require_experimental_streaming(settings)
    if origin != _EXPERIMENTAL_JDY_PLUGIN_ORIGIN:
        raise HTTPException(status_code=404, detail="Not Found")
    token = _experimental_semantic_v22_create_review_launch()
    launch_url = (
        "/experimental/semantic-quick-check-v22-review?review_launch_token="
        f"{quote(token, safe='')}"
    )
    return JSONResponse(
        {
            "experimental": True,
            "semantic_streaming_v22_review": True,
            "launch_url": launch_url,
            "expires_in_seconds": int(_EXPERIMENTAL_SEMANTIC_V22_REVIEW_LAUNCH_TTL_SECONDS),
        },
        headers={
            "Access-Control-Allow-Origin": _EXPERIMENTAL_JDY_PLUGIN_ORIGIN,
            "Vary": "Origin",
            "Cache-Control": "no-store",
            "X-TAORAN-Experimental": "semantic-streaming-v22-review",
        },
    )


@app.get(
    "/experimental/semantic-quick-check-v22-review-jdy-launch",
    include_in_schema=False,
)
def experimental_semantic_v22_review_jdy_launch(
    referer: str | None = Header(default=None),
) -> RedirectResponse:
    """Bridge the approved Jiandaoyun page-popup plugin to review mode.

    The configured popup URL is deliberately secret-free.  A request must be
    framed from Jiandaoyun, then receives one short-lived experimental review
    capability.  Generic launches remain Golden-Case only.
    """
    settings = get_settings()
    _require_experimental_streaming(settings)
    parsed = urlsplit(referer or "")
    if parsed.scheme != "https" or parsed.netloc != "www.jiandaoyun.com":
        raise HTTPException(status_code=404, detail="Not Found")
    token = _experimental_semantic_v22_create_review_launch()
    return RedirectResponse(
        url=(
            "/experimental/semantic-quick-check-v22-review?review_launch_token="
            f"{quote(token, safe='')}"
        ),
        status_code=status.HTTP_303_SEE_OTHER,
        headers={
            "Cache-Control": "no-store",
            "X-TAORAN-Experimental": "semantic-streaming-v22-review",
        },
    )


@app.get(
    "/api/v1/experimental/semantic-quick-check-v22/review/jdy-current-record-launch",
    response_model=None,
)
def create_experimental_semantic_v22_jdy_current_record_review_launch(
    visit_record_code: str = Query(min_length=1, max_length=256),
    open_page: bool = Query(default=False, alias="open"),
    origin: str | None = Header(default=None),
    referer: str | None = Header(default=None),
) -> JSONResponse | RedirectResponse:
    """Mint a short-lived, one-record business-review page for Jdy only."""
    settings = get_settings()
    _require_experimental_streaming(settings)
    jdy_navigation = (
        open_page is True
        and isinstance(referer, str)
        and urlsplit(referer).netloc == "www.jiandaoyun.com"
    )
    if origin != _EXPERIMENTAL_JDY_PLUGIN_ORIGIN and not jdy_navigation:
        raise HTTPException(status_code=404, detail="Not Found")
    tenant_id = "tenant_demo"
    mapping = tenant_mapping(settings, tenant_id)
    app_id = str(mapping.get("source_application_id", "")).strip()
    entry_id = str(mapping.get("source_entry_id", "")).strip()
    field_id = _EXPERIMENTAL_SEMANTIC_V22_JDY_VISIT_CODE_WIDGET_ID
    field_type = _EXPERIMENTAL_SEMANTIC_V22_JDY_VISIT_CODE_WIDGET_TYPE
    if not all((app_id, entry_id, field_id, field_type)):
        raise HTTPException(status_code=503, detail="experimental service unavailable")
    try:
        record = find_jiandaoyun_record_by_field(
            settings, tenant_id, app_id, entry_id, field_id, field_type,
            visit_record_code.strip(),
        )
    except JiandaoyunReadError:
        raise HTTPException(status_code=404, detail="Not Found") from None
    data_id = str(record.get("_id") or "").strip()
    if not data_id:
        raise HTTPException(status_code=404, detail="Not Found")
    token = _experimental_semantic_v22_create_review_launch(data_id)
    # A full URL is intentional here.  The Jdy plugin hosts its own navigation
    # context; a relative redirect would otherwise be resolved as a Jdy page.
    launch_url = (
        f"{_EXPERIMENTAL_SEMANTIC_V22_PUBLIC_BASE_URL}"
        "/experimental/semantic-quick-check-v22-review?mode=current-record&review_launch_token="
        f"{quote(token, safe='')}"
    )
    if jdy_navigation:
        return RedirectResponse(
            url=launch_url,
            status_code=status.HTTP_303_SEE_OTHER,
            headers={
                "Cache-Control": "no-store",
                "X-TAORAN-Experimental": "semantic-streaming-v22-review",
            },
        )
    return JSONResponse(
        {"experimental": True, "semantic_streaming_v22_review": True,
         "launch_url": launch_url,
         "expires_in_seconds": int(_EXPERIMENTAL_SEMANTIC_V22_REVIEW_LAUNCH_TTL_SECONDS)},
        headers={"Access-Control-Allow-Origin": _EXPERIMENTAL_JDY_PLUGIN_ORIGIN,
                 "Vary": "Origin", "Cache-Control": "no-store",
                 "X-TAORAN-Experimental": "semantic-streaming-v22-review"},
    )


def _experimental_semantic_v22_writeback_feedback(
    settings: Settings,
    *,
    data_id: str,
    feedback_text: str,
    transaction_id: str,
) -> dict[str, Any]:
    """Write only reviewed final feedback to the configured test form.

    This deliberately bypasses the formal evaluation writeback.  No score,
    Q33/Q34 value, status, or other formal field can be changed here.
    """
    tenant_id = "tenant_demo"
    api_key = settings.jiandaoyun_api_key_for(tenant_id)
    mapping = tenant_mapping(settings, tenant_id)
    app_id = str(mapping.get("source_application_id", "")).strip()
    entry_id = str(mapping.get("source_entry_id", "")).strip()
    output = mapping.get("output_fields", {}).get("ai_opinion", {})
    widget_id = str(
        output.get("widget_id", "") if isinstance(output, dict) else output
    ).strip()
    text = feedback_text.strip()
    if not api_key or not app_id or not entry_id or not widget_id or not text:
        return {"status": "failed", "reason": "experimental_writeback_not_configured"}
    if "replace" in widget_id.lower() or len(text) > 20_000:
        return {"status": "failed", "reason": "experimental_writeback_not_allowed"}
    try:
        response = httpx.post(
            f"{settings.jiandaoyun_base_url.rstrip('/')}/v5/app/entry/data/update",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "app_id": app_id,
                "entry_id": entry_id,
                "data_id": data_id,
                "data": {widget_id: {"value": text}},
                "is_start_trigger": False,
                "transaction_id": transaction_id,
            },
            timeout=settings.jiandaoyun_timeout_seconds,
        )
        response.raise_for_status()
    except httpx.HTTPError:
        return {"status": "failed", "reason": "jiandaoyun_writeback_failed"}
    return {"status": "succeeded", "written_field": "AI反馈意见"}


@app.post("/api/v1/experimental/semantic-quick-check-v22/review/{check_id}")
def save_experimental_semantic_v22_review(
    check_id: str,
    review: dict[str, Any],
    stream_token: str = Query(min_length=32, max_length=256),
) -> dict[str, Any]:
    """Save only reviewer decisions and safe telemetry in the isolated volume."""
    settings = get_settings()
    _require_experimental_streaming(settings)
    task = _experimental_semantic_v22_task(check_id, stream_token)
    future: Future[dict[str, Any]] = task["future"]
    review_case_id = str(task.get("review_case_id") or "").strip() or None
    review_data_id = str(task.get("review_data_id") or "").strip() or None
    if not future.done() or not (review_case_id or review_data_id):
        raise HTTPException(status_code=409, detail="experimental review is not ready")
    allowed = {
        "relevant", "specific", "actionable", "consistent_with_record",
        "reviewer_note", "reviewer_type",
    }
    if set(review) - allowed or not {"relevant", "specific", "actionable", "consistent_with_record"} <= set(review):
        raise HTTPException(status_code=422, detail="invalid experimental review")
    scores = {
        key: str(review.get(key, "")).upper()
        for key in ("relevant", "specific", "actionable", "consistent_with_record")
    }
    if any(value not in {"PASS", "FAIL"} for value in scores.values()):
        raise HTTPException(status_code=422, detail="invalid experimental review")
    reviewer_type = str(review.get("reviewer_type", "human")).strip()
    if reviewer_type not in {"human", "ai_sales_expert"}:
        raise HTTPException(status_code=422, detail="invalid experimental reviewer type")
    note = str(review.get("reviewer_note", "")).strip().replace("\n", " ")[:240]
    path = _experimental_semantic_v22_review_path(settings)
    # A review decision is evidence, not an editable draft.  Claim it before
    # writeback so duplicate clicks or parallel tabs cannot overwrite scores
    # or issue a second experimental writeback.
    if _experimental_semantic_v22_review_was_saved(path, check_id):
        raise HTTPException(status_code=409, detail="experimental review has already been submitted")
    with _experimental_semantic_v22_lock:
        current = _experimental_semantic_v22_tasks.get(check_id)
        if current is None or not hmac.compare_digest(str(current["stream_token"]), stream_token):
            raise HTTPException(status_code=404, detail="experimental check not found")
        if current.get("review_submitted") is True:
            raise HTTPException(status_code=409, detail="experimental review has already been submitted")
        current["review_submitted"] = True
    outcome = future.result()
    authoritative = outcome["authoritative"]
    preview = outcome["preview"]
    useful = (
        preview.get("status") == "completed"
        and scores["relevant"] == "PASS"
        and scores["consistent_with_record"] == "PASS"
        and (scores["specific"] == "PASS" or scores["actionable"] == "PASS")
    )
    approved_for_writeback = review_data_id is not None and all(
        value == "PASS" for value in scores.values()
    )
    if approved_for_writeback:
        writeback = _experimental_semantic_v22_writeback_feedback(
            settings,
            data_id=review_data_id,
            feedback_text=str(authoritative.get("feedback_text") or ""),
            transaction_id=f"experimental-v22-review-{check_id}",
        )
    elif review_data_id is not None:
        writeback = {"status": "skipped", "reason": "review_not_approved"}
    else:
        writeback = {"status": "skipped", "reason": "golden_case_review"}
    entry = {
        "experimental": True,
        "schema_version": "semantic-streaming-v22-business-review-v1",
        "case_id": review_case_id,
        "data_id": review_data_id,
        "run_id": check_id,
        "preview_hash": preview.get("feedback_hash"),
        "preview_length": preview.get("feedback_length", 0),
        "final_hash": hashlib.sha256(str(authoritative.get("feedback_text", "")).encode()).hexdigest(),
        "relevant": scores["relevant"],
        "specific": scores["specific"],
        "actionable": scores["actionable"],
        "consistent_with_record": scores["consistent_with_record"],
        "business_useful": "PASS" if useful else "FAIL",
        "reviewer_type": reviewer_type,
        "reviewer_note": note or None,
        "timing": {
            "first_real_ai_text_ms": preview.get("first_real_ai_text_ms"),
            "preview_complete_ms": preview.get("semantic_complete_ms"),
            "final_feedback_ms": authoritative.get("full_feedback_ms"),
        },
        "safety": {
            "specific_fact_claim_count": preview.get("specific_fact_claim_count", 0),
            "unsupported_specific_fact_count": preview.get("unsupported_specific_fact_count", 0),
            "final_success": authoritative.get("status") == "completed",
        },
        "writeback": writeback,
    }
    with _experimental_semantic_v22_review_lock, path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
    return {
        "experimental": True,
        "saved": True,
        "business_useful": entry["business_useful"],
        "writeback": writeback,
    }


_EXPERIMENTAL_STREAMING_UI = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TAORAN AI检测（实验）</title><style>
body{margin:0;background:#f5f7fa;color:#172b4d;font:15px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:760px;margin:32px auto;padding:24px;background:#fff;border-radius:10px;box-shadow:0 8px 28px #091e4220}
h1{font-size:20px;margin:0 0 18px}.tag{color:#8a5b00;background:#fff2cc;border-radius:4px;padding:2px 7px;font-size:12px}
#status{padding:12px;background:#e9f2ff;border-radius:6px;margin-bottom:16px}.feedback{white-space:pre-wrap;line-height:1.7;min-height:100px}
button{margin-top:18px;padding:8px 14px;border:0;border-radius:5px;background:#1f4e78;color:#fff;cursor:pointer}</style></head>
<body><main><h1>TAORAN AI检测 <span class="tag">experimental</span></h1><div id="status">正在建立实验流式连接…</div>
<div id="feedback" class="feedback"></div><button onclick="window.close()">关闭</button></main>
<script>
const qs=new URLSearchParams(location.search), checkId=qs.get('check_id'), token=qs.get('stream_token');
const status=document.querySelector('#status'), feedback=document.querySelector('#feedback');
if(!checkId||!token){status.textContent='缺少实验检测凭据。';}else{
 const source=new EventSource(`/api/v1/experimental/quick-check/${encodeURIComponent(checkId)}/events?stream_token=${encodeURIComponent(token)}`);
 source.addEventListener('started',()=>status.textContent='实验连接已建立，正在开始检测…');
 source.addEventListener('delta',(event)=>{const data=JSON.parse(event.data);status.textContent=data.text;});
 source.addEventListener('completed',(event)=>{const data=JSON.parse(event.data);status.textContent=`已完成（完整反馈 ${data.full_feedback_ms}ms）`;feedback.textContent=data.feedback_text;source.close();});
 source.addEventListener('error',(event)=>{try{status.textContent=JSON.parse(event.data).message;}catch{status.textContent='实验流式连接中断。'}source.close();});
}
</script></body></html>"""


_EXPERIMENTAL_GOLDEN_LAUNCHER_UI = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TAORAN 流式检测（实验）</title><style>
body{margin:0;background:#f5f7fa;color:#172b4d;font:15px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:760px;margin:32px auto;padding:24px;background:#fff;border-radius:10px;box-shadow:0 8px 28px #091e4220}
h1{font-size:20px;margin:0 0 12px}.tag{color:#8a5b00;background:#fff2cc;border-radius:4px;padding:2px 7px;font-size:12px}
p{line-height:1.6}button{margin:6px 6px 6px 0;padding:9px 13px;border:0;border-radius:5px;background:#1f4e78;color:#fff;cursor:pointer}button:disabled{opacity:.6;cursor:wait}#status{padding:12px;background:#e9f2ff;border-radius:6px;margin:14px 0}.feedback{white-space:pre-wrap;line-height:1.7;min-height:80px}</style></head>
<body><main><h1>TAORAN AI 流式检测 <span class="tag">experimental</span></h1>
<p>仅用于三个已批准 Golden Case 的技术验证；不会写回简道云字段，也不会执行提交后评分。</p>
<div id="cases"></div><div id="status">请选择一个实验案例。</div><div id="feedback" class="feedback"></div></main>
<script>
const token=new URLSearchParams(location.search).get('launch_token'),cases=['case_1','case_2','case_3'];
const list=document.querySelector('#cases'),status=document.querySelector('#status'),feedback=document.querySelector('#feedback');
function disable(v){document.querySelectorAll('button').forEach(b=>b.disabled=v)}
for(const id of cases){const b=document.createElement('button');b.textContent=id.replace('_',' ')+' Golden Case';b.onclick=async()=>{disable(true);feedback.textContent='';status.textContent='正在创建隔离实验检测…';try{const res=await fetch(`/api/v1/experimental/quick-check/golden/${id}?launch_token=${encodeURIComponent(token)}`,{method:'POST'});if(!res.ok)throw new Error();const task=await res.json();const s=new EventSource(`/api/v1/experimental/quick-check/${encodeURIComponent(task.check_id)}/events?stream_token=${encodeURIComponent(task.stream_token)}`);s.addEventListener('started',()=>status.textContent='实验连接已建立，正在开始检测…');s.addEventListener('delta',e=>status.textContent=JSON.parse(e.data).text);s.addEventListener('completed',e=>{const d=JSON.parse(e.data);status.textContent=`已完成（完整反馈 ${d.full_feedback_ms}ms）`;feedback.textContent=d.feedback_text;s.close();disable(false)});s.addEventListener('error',()=>{status.textContent='实验流式连接中断，请重新发起。';s.close();disable(false)});}catch{status.textContent='实验任务未能创建，请联系管理员。';disable(false)}};list.appendChild(b)}
if(!token){list.innerHTML='';status.textContent='缺少实验访问凭据。'}
</script></body></html>"""


_EXPERIMENTAL_CURRENT_RECORD_LAUNCHER_UI = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TAORAN 当前记录流式检测（实验）</title><style>
body{margin:0;background:#f5f7fa;color:#172b4d;font:15px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:760px;margin:32px auto;padding:24px;background:#fff;border-radius:10px;box-shadow:0 8px 28px #091e4220}
h1{font-size:20px;margin:0 0 12px}.tag{color:#8a5b00;background:#fff2cc;border-radius:4px;padding:2px 7px;font-size:12px}
p{line-height:1.6}#status{padding:12px;background:#e9f2ff;border-radius:6px;margin:14px 0}.feedback{white-space:pre-wrap;line-height:1.7;min-height:80px}
button{margin-top:10px;padding:8px 14px;border:0;border-radius:5px;background:#1f4e78;color:#fff;cursor:pointer}</style></head>
<body><main><h1>TAORAN 当前记录流式检测 <span class="tag">experimental</span></h1>
<p>仅读取当前这条测试表记录，执行与 0.26.3 相同的 Quick Check；不会写回简道云字段，也不会执行提交后评分。</p>
<div id="status">正在创建当前记录的隔离实验检测…</div><div id="feedback" class="feedback"></div><button onclick="window.close()">关闭</button></main>
<script>
const qs=new URLSearchParams(location.search),token=qs.get('launch_token'),recordLaunchToken=qs.get('record_launch_token'),dataId=qs.get('data_id');
const status=document.querySelector('#status'),feedback=document.querySelector('#feedback');
async function start(){
 if((!token&&!recordLaunchToken)||!dataId){status.textContent='缺少当前记录的实验检测凭据。';return;}
 try{const credential=token ? `launch_token=${encodeURIComponent(token)}` : `record_launch_token=${encodeURIComponent(recordLaunchToken)}`;
  const res=await fetch(`/api/v1/experimental/quick-check/current-record/${encodeURIComponent(dataId)}?${credential}`,{method:'POST'});
  if(!res.ok)throw new Error();const task=await res.json();
  const source=new EventSource(`/api/v1/experimental/quick-check/${encodeURIComponent(task.check_id)}/events?stream_token=${encodeURIComponent(task.stream_token)}`);
  source.addEventListener('started',()=>status.textContent='实验连接已建立，正在读取当前拜访记录…');
  source.addEventListener('delta',event=>status.textContent=JSON.parse(event.data).text);
  source.addEventListener('completed',event=>{const data=JSON.parse(event.data);status.textContent=`已完成（完整反馈 ${data.full_feedback_ms}ms）`;feedback.textContent=data.feedback_text;source.close();});
  source.addEventListener('error',()=>{status.textContent='实验流式连接中断，请关闭后重新发起。';source.close();});
 }catch{status.textContent='当前记录的实验任务未能创建，请关闭后重新发起。';}
}
start();
</script></body></html>"""


_EXPERIMENTAL_SEMANTIC_CURRENT_RECORD_UI = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TAORAN 语义流式检测（实验 V2）</title><style>
body{margin:0;background:#f5f7fa;color:#172b4d;font:15px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:760px;margin:32px auto;padding:24px;background:#fff;border-radius:10px;box-shadow:0 8px 28px #091e4220}
h1{font-size:20px;margin:0 0 12px}.tag{color:#8a5b00;background:#fff2cc;border-radius:4px;padding:2px 7px;font-size:12px}
p{line-height:1.6}#status{padding:12px;background:#e9f2ff;border-radius:6px;margin:14px 0}.preview,.feedback{white-space:pre-wrap;line-height:1.7;min-height:48px}.preview{background:#fff9e8;padding:10px;border-radius:6px}.label{font-size:13px;color:#6b7280;margin:14px 0 6px}button{margin-top:10px;padding:8px 14px;border:0;border-radius:5px;background:#1f4e78;color:#fff;cursor:pointer}</style></head>
<body><main><h1>TAORAN 语义流式检测 <span class="tag">experimental V2</span></h1>
<p>仅读取当前测试记录。上方文字为实验性临时分析；完成后将由未改动的 0.26.3 校验结果替换为最终反馈。不会写回简道云，也不会执行提交后评分。</p>
<div id="status">正在创建语义流式实验检测…</div><div class="label">临时语义分析（实验）</div><div id="preview" class="preview"></div><div class="label">最终 AI 反馈意见（0.26.3）</div><div id="feedback" class="feedback"></div><button onclick="window.close()">关闭</button></main>
<script>
const qs=new URLSearchParams(location.search),dataId=qs.get('data_id'),token=qs.get('launch_token'),recordLaunchToken=qs.get('record_launch_token');
const status=document.querySelector('#status'),preview=document.querySelector('#preview'),feedback=document.querySelector('#feedback');
async function start(){
 if((!token&&!recordLaunchToken)||!dataId){status.textContent='缺少当前记录的实验检测凭据。';return;}
 try{const credential=token?`launch_token=${encodeURIComponent(token)}`:`record_launch_token=${encodeURIComponent(recordLaunchToken)}`;
  const res=await fetch(`/api/v1/experimental/semantic-quick-check/current-record/${encodeURIComponent(dataId)}?${credential}`,{method:'POST'});if(!res.ok)throw new Error();const task=await res.json();
  const source=new EventSource(`/api/v1/experimental/semantic-quick-check/${encodeURIComponent(task.check_id)}/events?stream_token=${encodeURIComponent(task.stream_token)}`);
  source.addEventListener('started',()=>status.textContent='实验连接已建立，正在生成分析…');
  source.addEventListener('stage',e=>status.textContent=JSON.parse(e.data).text);
  source.addEventListener('feedback_delta',e=>{preview.textContent+=JSON.parse(e.data).text;status.textContent='正在生成实验性语义分析…';});
  source.addEventListener('provisional_discarded',()=>{preview.textContent='临时语义分析未通过证据校验，已丢弃。';});
  source.addEventListener('completed',e=>{const d=JSON.parse(e.data);feedback.textContent=d.feedback_text;status.textContent=`已完成（最终反馈 ${d.full_feedback_ms}ms）`;if(!d.preview_validated)preview.textContent='临时语义分析未通过校验，已丢弃。';source.close();});
  source.addEventListener('error',()=>{status.textContent='实验流式连接中断，请关闭后重新发起。';preview.textContent='';source.close();});
 }catch{status.textContent='当前记录的实验任务未能创建，请关闭后重新发起。';}}
start();
</script></body></html>"""


_EXPERIMENTAL_SEMANTIC_V22_REVIEW_UI = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TAORAN V2.2 业务审核（实验）</title><style>
body{margin:0;background:#f5f7fa;color:#172b4d;font:15px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}main{max-width:840px;margin:24px auto;padding:24px;background:#fff;border-radius:10px;box-shadow:0 8px 28px #091e4220}h1{font-size:20px;margin:0 0 12px}.tag{color:#8a5b00;background:#fff2cc;border-radius:4px;padding:2px 7px;font-size:12px}p{line-height:1.6}button{margin:5px 5px 5px 0;padding:8px 12px;border:0;border-radius:5px;background:#1f4e78;color:#fff;cursor:pointer}button:disabled{opacity:.6;cursor:wait}#status{padding:12px;background:#e9f2ff;border-radius:6px;margin:14px 0}.box{white-space:pre-wrap;line-height:1.7;min-height:76px;padding:12px;border-radius:6px}.preview{background:#fff9e8}.final{background:#eef7ff}.label{font-weight:600;margin:16px 0 6px}.review{display:grid;grid-template-columns:1fr 120px;gap:8px;max-width:460px;margin-top:18px}.review textarea{grid-column:1/3;min-height:54px}.hint{font-size:13px;color:#667085}</style></head>
<body><main><h1>TAORAN V2.2 业务审核 <span class="tag">experimental</span></h1><p id="intro">每次仅展示批准的 Golden Case。Preview 只在当前页面显示；提交时只保存评分、短备注、哈希和耗时，不保存 Preview 正文，也不会写回简道云。</p><div id="cases"></div><div id="status">请选择案例后开始审核。</div><div class="label">AI实时分析（Preview）</div><div id="preview" class="box preview"></div><div class="label">AI检测最终结果（Final）</div><div id="final" class="box final"></div><form id="review" hidden><div class="review"><label>审核来源</label><select name="reviewer_type"><option value="human">人工业务审核</option><option value="ai_sales_expert">AI销售专家代理审核</option></select><label>Relevant（是否针对本条记录）</label><select name="relevant"><option>PASS</option><option>FAIL</option></select><label>Specific（是否结合实际情况）</label><select name="specific"><option>PASS</option><option>FAIL</option></select><label>Actionable（是否知道怎么改）</label><select name="actionable"><option>PASS</option><option>FAIL</option></select><label>Consistent with record（是否与记录一致）</label><select name="consistent_with_record"><option>PASS</option><option>FAIL</option></select><textarea name="reviewer_note" maxlength="240" placeholder="可选短备注（不保存 Preview 正文）"></textarea></div><button type="submit">提交审核并回写反馈</button><span id="saved" class="hint"></span></form></main>
<script>
const qs=new URLSearchParams(location.search),launchToken=qs.get('launch_token'),reviewLaunchToken=qs.get('review_launch_token'),currentRecord=qs.get('mode')==='current-record',expansionMode=qs.get('mode')==='expansion',credential=launchToken?`launch_token=${encodeURIComponent(launchToken)}`:reviewLaunchToken?`review_launch_token=${encodeURIComponent(reviewLaunchToken)}`:'',cases=expansionMode?Array.from({length:15},(_,i)=>`expansion_${String(i+1).padStart(2,'0')}`):['case_1','case_2','case_3'];let active=null;const list=document.querySelector('#cases'),status=document.querySelector('#status'),preview=document.querySelector('#preview'),final=document.querySelector('#final'),form=document.querySelector('#review'),saved=document.querySelector('#saved'),intro=document.querySelector('#intro');function disable(v){document.querySelectorAll('#cases button').forEach(b=>b.disabled=v)}
async function start(url){disable(true);active=null;preview.textContent='';final.textContent='';form.hidden=true;saved.textContent='';form.querySelector('button[type="submit"]').disabled=false;status.textContent='AI正在分析当前拜访记录…';try{const res=await fetch(url,{method:'POST'});if(!res.ok)throw new Error();const task=await res.json();active=task;const source=new EventSource(`/api/v1/experimental/semantic-quick-check-v22/${encodeURIComponent(task.check_id)}/events?stream_token=${encodeURIComponent(task.stream_token)}`);source.addEventListener('feedback_delta',e=>{preview.textContent+=JSON.parse(e.data).text;});source.addEventListener('provisional_discarded',()=>{preview.textContent='本次实时建议未通过基础安全检查，已不展示。';});source.addEventListener('completed',e=>{const data=JSON.parse(e.data);final.textContent=data.feedback_text;status.textContent=currentRecord?'AI检测完成。四项均确认通过后，提交将回写真实AI反馈意见。':'AI检测完成，请根据两段内容完成审核。';form.hidden=false;source.close();disable(false);});source.addEventListener('error',()=>{status.textContent='本次实验检测未完成，请重新选择案例。';source.close();disable(false);});}catch{status.textContent='实验任务未能创建，请重新选择案例。';disable(false);}}
if(!credential){list.innerHTML='';status.textContent='缺少实验审核凭据。';}else if(currentRecord){intro.textContent='当前仅审核从简道云按钮打开的这一条测试记录。Preview 只在本页显示；最终反馈只有在四项审核均为 PASS 后，才回写到该记录的“AI反馈意见”。';list.innerHTML='';start(`/api/v1/experimental/semantic-quick-check-v22/review/current-record?${credential}`);}else{if(expansionMode)intro.textContent='当前为 15 条扩展验证。每条仅来自预先批准的测试样本；AI代理审核不会写回简道云。';for(const id of cases){const b=document.createElement('button');b.textContent=expansionMode?`扩展案例 ${Number(id.slice(-2))}`:id.replace('_',' ')+' Golden Case';b.onclick=()=>start(`/api/v1/experimental/semantic-quick-check-v22/review/${expansionMode?'expansion':'golden'}/${id}?${credential}`);list.appendChild(b)}}
form.addEventListener('submit',async e=>{e.preventDefault();if(!active)return;const submit=form.querySelector('button[type="submit"]');submit.disabled=true;saved.textContent='正在保存审核结果…';const data=Object.fromEntries(new FormData(form));const res=await fetch(`/api/v1/experimental/semantic-quick-check-v22/review/${encodeURIComponent(active.check_id)}?stream_token=${encodeURIComponent(active.stream_token)}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});if(!res.ok){if(res.status===409){saved.textContent='审核已提交，不能再次修改。';return;}saved.textContent='保存失败，请重试。';submit.disabled=false;return;}const result=await res.json(),writeback=result.writeback||{};saved.textContent=writeback.status==='succeeded'?'已保存审核，真实AI反馈已写入AI反馈意见。':writeback.status==='skipped'?'已保存审核；本次未回写（四项审核需均为PASS）。':writeback.status==='failed'?'审核已保存，但AI反馈意见回写失败，请联系管理员重试。':`已保存：Business Useful ${result.business_useful}`;});
</script></body></html>"""


@app.get("/experimental/quick-check-ui", response_class=HTMLResponse, include_in_schema=False)
def experimental_quick_check_ui() -> HTMLResponse:
    settings = get_settings()
    _require_experimental_streaming(settings)
    return HTMLResponse(
        _EXPERIMENTAL_STREAMING_UI,
        headers={"Cache-Control": "no-store", "X-TAORAN-Experimental": "streaming-poc"},
    )


@app.get("/experimental/quick-check-golden", response_class=HTMLResponse, include_in_schema=False)
def experimental_golden_launcher_ui(
    launch_token: str = Query(min_length=32, max_length=256),
) -> HTMLResponse:
    settings = get_settings()
    _require_experimental_streaming(settings)
    _require_experimental_launch_token(settings, launch_token)
    return HTMLResponse(
        _EXPERIMENTAL_GOLDEN_LAUNCHER_UI,
        headers={"Cache-Control": "no-store", "X-TAORAN-Experimental": "streaming-poc"},
    )


@app.get(
    "/experimental/quick-check-current-record",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def experimental_current_record_launcher_ui(
    data_id: str = Query(min_length=1, max_length=128),
    launch_token: str | None = Query(default=None, min_length=32, max_length=256),
    record_launch_token: str | None = Query(default=None, min_length=32, max_length=256),
) -> HTMLResponse:
    """Experimental iframe page for the record whose button opened the modal."""
    settings = get_settings()
    _require_experimental_streaming(settings)
    if not data_id.strip():
        raise HTTPException(status_code=404, detail="Not Found")
    if launch_token is not None:
        _require_experimental_launch_token(settings, launch_token)
    elif record_launch_token is not None:
        _require_experimental_jdy_record_launch(
            data_id,
            record_launch_token,
            consume=False,
        )
    else:
        raise HTTPException(status_code=404, detail="Not Found")
    return HTMLResponse(
        _EXPERIMENTAL_CURRENT_RECORD_LAUNCHER_UI,
        headers={"Cache-Control": "no-store", "X-TAORAN-Experimental": "streaming-poc"},
    )


@app.get(
    "/experimental/semantic-quick-check-current-record",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def experimental_semantic_current_record_launcher_ui(
    data_id: str = Query(min_length=1, max_length=128),
    launch_token: str | None = Query(default=None, min_length=32, max_length=256),
    record_launch_token: str | None = Query(default=None, min_length=32, max_length=256),
) -> HTMLResponse:
    settings = get_settings()
    _require_experimental_streaming(settings)
    if launch_token is not None:
        _require_experimental_launch_token(settings, launch_token)
    elif record_launch_token is not None:
        _require_experimental_semantic_record_launch(data_id, record_launch_token, consume=False)
    else:
        raise HTTPException(status_code=404, detail="Not Found")
    return HTMLResponse(
        _EXPERIMENTAL_SEMANTIC_CURRENT_RECORD_UI,
        headers={"Cache-Control": "no-store", "X-TAORAN-Experimental": "semantic-streaming-v2"},
    )


@app.get(
    "/experimental/semantic-quick-check-v22-review",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def experimental_semantic_v22_review_ui(
    launch_token: str | None = Query(default=None, min_length=32, max_length=256),
    review_launch_token: str | None = Query(default=None, min_length=32, max_length=256),
) -> HTMLResponse:
    settings = get_settings()
    _require_experimental_streaming(settings)
    _require_experimental_semantic_v22_review_access(
        settings, launch_token, review_launch_token,
    )
    return HTMLResponse(
        _EXPERIMENTAL_SEMANTIC_V22_REVIEW_UI,
        headers={"Cache-Control": "no-store", "X-TAORAN-Experimental": "semantic-streaming-v22-review"},
    )


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
    elif record["status"] == "failed":
        raise HTTPException(status_code=409, detail={
            "message": "同一请求此前评价失败，请修正数据后补取重试，或对返回的失败任务发起重试。",
            "job_id": job_id,
            "error": record.get("error_message"),
        })
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
    revision = business_revision(record, mapping)
    request_id = event.request_id or f"jdy_submit_{event.data_id}_{revision[:16]}"
    if not event.request_id:
        request_id += f"_{TOTAL_RULE_VERSION}"
    if event.request_id and event.request_id.startswith("refetch_job_"):
        request_id += f"_{revision[:16]}_{POLICY_VERSION}"
    settings = get_settings()
    if settings.llm_enabled and not event.request_id:
        analysis_revision = canonical_hash({
            "post_policy": POLICY_VERSION,
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
    canonical = adapt_jiandaoyun_evaluation_request(request, mapping)
    if event.request_id and event.request_id.startswith("manual_saved_"):
        return _submit_saved_analysis(canonical, background_tasks, x_tenant_id, x_api_key)
    return submit_evaluation(canonical, background_tasks, x_tenant_id, x_api_key)


def _submit_saved_analysis(request, background_tasks, x_tenant_id, x_api_key):
    """Compare before analysis only. Failed attempts never count as a success."""
    authorize(request.context.tenant_id, x_tenant_id, x_api_key)
    with source_lock(request.context.tenant_id, request.writeback_target):
        latest = get_store().latest_source_job(request)
        if latest and analysis_input_revision(PostEvaluationRequest.model_validate(
            latest["request"]
        )) == analysis_input_revision(request):
            response = latest.get("response") or {}
            usable = latest["status"] in {"queued", "running"} or (
                latest["status"] == "completed"
                and (response.get("semantic_facts") or {}).get("status") == "completed"
                and (response.get("writeback") or {}).get("status") == "succeeded"
            )
            if usable:
                return EvaluationAccepted(
                    job_id=latest["job_id"], trace_id=response.get("trace_id", "reused"),
                    status="completed" if latest["status"] == "completed" else "queued",
                    input_snapshot_hash=latest["input_snapshot_hash"],
                )
        return submit_evaluation(request, background_tasks, x_tenant_id, x_api_key)


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
    if operation != "data_create" or not isinstance(record, dict):
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


@app.post("/api/v1/visit/evaluations/{job_id}/refetch-retry", response_model=EvaluationAccepted)
def refetch_retry_evaluation(
    job_id: str,
    background_tasks: BackgroundTasks,
    tenant_id: str = Query(min_length=1),
    x_tenant_id: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> EvaluationAccepted:
    """Re-read the configured source for a failed job, retaining its original audit."""
    authorize(tenant_id, x_tenant_id, x_api_key)
    job = get_store().get_evaluation(tenant_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="evaluation not found")
    result = job.get("response") or {}
    if job["status"] != "failed" and not (
        job["status"] == "completed" and
        (result.get("semantic_facts", {}).get("status") != "completed"
         or result.get("writeback", {}).get("status") == "failed")
    ):
        raise HTTPException(status_code=409, detail="仅异常任务可补取重试")
    original = PostEvaluationRequest.model_validate(job["request"])
    target = original.writeback_target
    if target is None:
        raise HTTPException(status_code=409, detail="缺少简道云来源，请补齐输入后重新评价")
    event = JiandaoyunSubmittedEvent(
        tenant_id=tenant_id, data_id=target.data_id, app_id=target.app_id,
        entry_id=target.entry_id, user_id=original.context.user_id,
        request_id=f"refetch_{job_id}",
    )
    return submit_jiandaoyun_record_event(event, background_tasks, x_tenant_id, x_api_key)


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
    if evaluation.writeback.status == "succeeded":
        return evaluation.model_dump(mode="json")
    if evaluation.semantic_facts.status != "completed":
        raise HTTPException(status_code=409, detail="模型分析未通过，请补取最新记录重新分析")
    # Retry only delivery of the persisted, validated report. Never call a model here.
    try:
        writeback = writeback_evaluation(get_settings(), request, evaluation, store=store)
    except JiandaoyunWritebackError:
        writeback = WritebackResult(status="failed", error_message="WRITEBACK_DELIVERY_FAILED",
                                   attempted_at=datetime.now(UTC))
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


app.include_router(evaluation_operations_router)
