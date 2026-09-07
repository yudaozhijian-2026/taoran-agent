from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx

from .config import Settings
from .connector import load_jiandaoyun_mapping
from .jiandaoyun_api import JiandaoyunReadError, get_jiandaoyun_record
from .knowledge import load_taoran_knowledge_snapshot
from .models import EvaluationResponse, PostEvaluationRequest, WritebackResult
from .post_review_policy import POLICY_VERSION, knowledge_manifest
from .rules import canonical_hash
from .scoring_contract import TOTAL_RULE_VERSION
from .source_revision import business_revision, source_lock


class JiandaoyunWritebackError(RuntimeError):
    pass


def evaluation_writeback_values(response: EvaluationResponse) -> dict[str, Any]:
    return {
        "evaluation_status": response.status,
        "q33_score": response.q33_score,
        "q34_score": response.q34_score,
        "total_score": response.total_score,
        "total_max_score": response.total_max_score,
        "overall_percentage": response.overall_percentage,
        "effectiveness_level": response.effectiveness_level,
        "effective_visit_recommendation": response.count_as_effective_visit_recommendation,
        "ai_opinion": response.ai_opinion,
        "ai_suggestions": "\n".join(response.manager_coaching_suggestions),
        "rule_version": response.rule_version,
        "agent_version": response.agent_version,
        "trace_id": response.trace_id,
        "evaluated_at": response.completed_at.isoformat(),
        "score_detail_json": json.dumps(
            [item.model_dump(mode="json") for item in response.question_scores],
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }


def _blocked_rule_feedback(response: EvaluationResponse) -> str:
    reason = response.semantic_facts.failure_reason or "model_review_incomplete"
    reason_text = {
        "timeout": "提交后大模型复核超时",
        "queue_timeout": "提交后大模型排队超时",
        "rate_limited": "大模型账号并发额度暂时受限",
        "authentication_failed": "大模型账号认证失败",
        "access_denied": "大模型账号无调用权限",
        "invalid_contract": "大模型返回结构未通过校验",
        "ungrounded_evidence": "大模型引用未通过原始记录证据校验",
        "missing_evidence": "大模型结论缺少原始记录证据",
    }.get(reason, f"提交后大模型复核未完成（{reason}）")
    return (
        f"AI调用异常：{reason_text}。本次正式评分未更新，"
        "已保留本次规则与知识库综合检查；请稍后重试AI检查。"
    )


def writeback_evaluation(
    settings: Settings,
    request: PostEvaluationRequest,
    response: EvaluationResponse,
    *,
    store=None,
) -> WritebackResult:
    """Fail closed for changed sources, superseded tasks and obsolete policies.

    The lock serializes this application's writes and acceptance of new jobs.
    Jiandaoyun has no assumed atomic compare-and-swap: a human edit during the
    remote read/update interval remains an external race, documented in QA.
    """
    target = request.writeback_target
    if target is None:
        return WritebackResult(status="skipped")

    def blocked(code):
        return WritebackResult(status="failed", target_data_id=target.data_id,
                               error_message=code, attempted_at=datetime.now(UTC))

    with source_lock(request.context.tenant_id, target):
        tenant = settings.tenant_config(request.context.tenant_id)
        if tenant is not None and not tenant.enabled:
            return blocked("WRITEBACK_TENANT_DISABLED")
        if store is None:
            return blocked("WRITEBACK_GUARD_STORE_REQUIRED")
        mapping_path = settings.jiandaoyun_mapping_path_for(request.context.tenant_id)
        mapping = load_jiandaoyun_mapping(mapping_path)
        if (target.app_id != mapping.get("source_application_id")
                or target.entry_id != mapping.get("source_entry_id")):
            return blocked("WRITEBACK_TARGET_CHANGED")
        if not store.is_latest_source_job(request, response.job_id):
            return blocked("WRITEBACK_SUPERSEDED")
        version = response.knowledge_version_audit.get("post_policy", {}).get("version")
        if version != POLICY_VERSION:
            return blocked("WRITEBACK_POLICY_CHANGED")
        current_manifest = knowledge_manifest(load_taoran_knowledge_snapshot(settings.knowledge_snapshot_path))
        if response.knowledge_version_audit.get("actual_records_hash") != current_manifest["actual_records_hash"]:
            return blocked("WRITEBACK_KNOWLEDGE_CHANGED")
        if response.knowledge_version_audit.get("post_policy", {}).get("hash") != current_manifest["post_policy"]["hash"]:
            return blocked("WRITEBACK_POLICY_CHANGED")
        if request.visit.metadata.get("source_mapping_hash") != canonical_hash(mapping):
            return blocked("WRITEBACK_MAPPING_CHANGED")
        if not request.context.form_revision:
            return blocked("WRITEBACK_SOURCE_REVISION_MISSING")
        try:
            current = get_jiandaoyun_record(settings, request.context.tenant_id,
                                          target.app_id, target.entry_id, target.data_id)
        except JiandaoyunReadError:
            return blocked("WRITEBACK_SOURCE_READ_FAILED")
        if business_revision(current, mapping) != request.context.form_revision:
            return blocked("WRITEBACK_SOURCE_CHANGED")
        return _writeback_current_evaluation(settings, request, response)


def _writeback_current_evaluation(settings, request, response) -> WritebackResult:
    target = request.writeback_target
    if target is None:
        return WritebackResult(status="skipped")
    attempted_at = datetime.now(UTC)
    if response.rule_version != TOTAL_RULE_VERSION or response.total_max_score != 100:
        return WritebackResult(
            status="failed", target_data_id=target.data_id, attempted_at=attempted_at,
            error_message="历史评分采用旧量纲，禁止直接回写；请使用新请求ID按100分制重新评价。",
        )
    official_writeback_blocked = (
        response.semantic_facts.provider.startswith("llm-")
        and response.semantic_facts.status != "completed"
    )
    tenant_id = request.context.tenant_id
    api_key = settings.jiandaoyun_api_key_for(tenant_id)
    if not api_key:
        return WritebackResult(
            status="failed",
            target_data_id=target.data_id,
            error_message="未配置当前租户的简道云API密钥",
            attempted_at=attempted_at,
        )
    mapping_path = settings.jiandaoyun_mapping_path_for(tenant_id)
    if settings.tenant_config(tenant_id) is not None and not mapping_path:
        return WritebackResult(
            status="failed",
            target_data_id=target.data_id,
            error_message="未配置当前租户的简道云字段映射",
            attempted_at=attempted_at,
        )
    mapping = load_jiandaoyun_mapping(mapping_path)
    output_fields = mapping.get("output_fields", {})
    if not output_fields:
        return WritebackResult(
            status="failed",
            target_data_id=target.data_id,
            error_message="未配置简道云评价回写字段",
            attempted_at=attempted_at,
        )
    resolved_output_fields = {
        name: _output_widget_id(spec) for name, spec in output_fields.items()
    }
    unresolved_fields = [name for name, widget_id in resolved_output_fields.items() if not widget_id]
    if unresolved_fields:
        return WritebackResult(
            status="failed",
            target_data_id=target.data_id,
            error_message=(
                "简道云副本的AI输出widget ID尚未配置：" + "、".join(unresolved_fields)
            ),
            attempted_at=attempted_at,
        )
    placeholder_fields = [
        widget_id
        for widget_id in resolved_output_fields.values()
        if widget_id and "replace" in widget_id.lower()
    ]
    if placeholder_fields:
        return WritebackResult(
            status="failed",
            target_data_id=target.data_id,
            error_message="简道云AI输出字段仍为占位ID，已拒绝回写",
            attempted_at=attempted_at,
        )
    canonical_values = evaluation_writeback_values(response)
    if official_writeback_blocked:
        # 不回写分数；唯一反馈字段保留明确异常和当次综合检查，避免展示过期结论。
        canonical_values = {
            "ai_opinion": _blocked_rule_feedback(response) + "\n\n" + response.ai_opinion,
        }
    values = {
        widget_id: _format_widget_value(canonical_values[name], output_fields[name])
        for name, widget_id in resolved_output_fields.items()
        if name in canonical_values
        and widget_id
        and canonical_values[name] not in (None, "")
    }
    if not values:
        return WritebackResult(
            status="failed",
            target_data_id=target.data_id,
            error_message=(
                "大模型复核未完成，本次没有可回写的综合反馈或异常说明。"
                if official_writeback_blocked
                else "评价结果与回写字段没有可用映射"
            ),
            attempted_at=attempted_at,
        )
    try:
        http_response = httpx.post(
            f"{settings.jiandaoyun_base_url.rstrip('/')}/v5/app/entry/data/update",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "app_id": target.app_id,
                "entry_id": target.entry_id,
                "data_id": target.data_id,
                "data": {field: {"value": value} for field, value in values.items()},
                "is_start_trigger": False,
                "transaction_id": response.evaluation_id,
            },
            timeout=settings.jiandaoyun_timeout_seconds,
        )
        http_response.raise_for_status()
        payload = http_response.json()
        returned = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(returned, dict) or str(returned.get("_id", "")) != target.data_id:
            raise JiandaoyunWritebackError("简道云回写响应未确认目标记录，请核对后重试")
    except (httpx.HTTPError, ValueError) as exc:
        raise JiandaoyunWritebackError("简道云评价回写请求失败") from exc
    return WritebackResult(
        status="failed" if official_writeback_blocked else "succeeded",
        target_data_id=target.data_id,
        written_fields=sorted(values),
        error_message=(
            "大模型复核未完成：未更新正式评分，已回写当次AI异常说明和知识库填写反馈。"
            if official_writeback_blocked
            else None
        ),
        attempted_at=attempted_at,
    )


def _output_widget_id(spec: Any) -> str | None:
    if isinstance(spec, str):
        return spec or None
    if isinstance(spec, dict):
        widget_id = spec.get("widget_id")
        return widget_id if isinstance(widget_id, str) and widget_id else None
    return None


def _format_widget_value(value: Any, spec: Any) -> Any:
    """Serialize canonical values using the target Jiandaoyun widget type."""
    if not isinstance(spec, dict):
        return value
    if spec.get("widget_type") not in {"text", "textarea", "sn"}:
        return value
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
