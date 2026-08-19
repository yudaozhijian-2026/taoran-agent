from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx

from .config import Settings
from .connector import load_jiandaoyun_mapping
from .models import EvaluationResponse, PostEvaluationRequest, WritebackResult


class JiandaoyunWritebackError(RuntimeError):
    pass


def evaluation_writeback_values(response: EvaluationResponse) -> dict[str, Any]:
    return {
        "evaluation_status": response.status,
        "q33_score": response.q33_score,
        "q34_score": response.q34_score,
        "total_score": response.total_score,
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


def writeback_evaluation(
    settings: Settings,
    request: PostEvaluationRequest,
    response: EvaluationResponse,
) -> WritebackResult:
    target = request.writeback_target
    if target is None:
        return WritebackResult(status="skipped")
    attempted_at = datetime.now(UTC)
    api_key = settings.jiandaoyun_api_keys.get(request.context.tenant_id)
    if not api_key:
        return WritebackResult(
            status="failed",
            target_data_id=target.data_id,
            error_message="未配置当前租户的简道云API密钥",
            attempted_at=attempted_at,
        )
    mapping = load_jiandaoyun_mapping(settings.jiandaoyun_mapping_path)
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
    values = {
        widget_id: _format_widget_value(canonical_values[name], output_fields[name])
        for name, widget_id in resolved_output_fields.items()
        if name in canonical_values and widget_id
    }
    if not values:
        return WritebackResult(
            status="failed",
            target_data_id=target.data_id,
            error_message="评价结果与回写字段没有可用映射",
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
    except httpx.HTTPError as exc:
        raise JiandaoyunWritebackError("简道云评价回写请求失败") from exc
    return WritebackResult(
        status="succeeded",
        target_data_id=target.data_id,
        written_fields=sorted(values),
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
