from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import httpx

from .config import Settings


class JiandaoyunSchemaSyncError(RuntimeError):
    pass


def discover_jiandaoyun_authorization(
    base_url: str,
    timeout_seconds: float,
    api_key: str,
) -> list[dict[str, Any]]:
    """Return the applications and forms visible to one Jiandaoyun API key."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    applications = _fetch_paginated_items(
        f"{base_url.rstrip('/')}/v5/app/list",
        headers,
        timeout_seconds,
        "apps",
        {},
    )
    result: list[dict[str, Any]] = []
    for application in applications:
        app_id = application.get("app_id")
        app_name = application.get("name")
        if not isinstance(app_id, str) or not app_id:
            continue
        forms = _fetch_paginated_items(
            f"{base_url.rstrip('/')}/v5/app/entry/list",
            headers,
            timeout_seconds,
            "forms",
            {"app_id": app_id},
        )
        normalized_forms = [
            {
                "app_id": app_id,
                "entry_id": form["entry_id"],
                "name": form.get("name") or form["entry_id"],
            }
            for form in forms
            if isinstance(form.get("entry_id"), str) and form["entry_id"]
        ]
        result.append(
            {
                "app_id": app_id,
                "name": app_name if isinstance(app_name, str) and app_name else app_id,
                "forms": normalized_forms,
            }
        )
    if not result:
        raise JiandaoyunSchemaSyncError("该API Key没有授权任何可访问应用")
    return result


def _fetch_paginated_items(
    url: str,
    headers: dict[str, str],
    timeout_seconds: float,
    collection_name: str,
    fixed_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for skip in range(0, 1000, 100):
        payload = {**fixed_payload, "limit": 100, "skip": skip}
        try:
            response = httpx.post(
                url,
                headers=headers,
                json=payload,
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise JiandaoyunSchemaSyncError(
                "简道云API Key无效、已过期或授权信息读取失败"
            ) from exc
        if isinstance(body, dict) and isinstance(body.get("data"), dict):
            body = body["data"]
        page = body.get(collection_name) if isinstance(body, dict) else None
        if not isinstance(page, list):
            raise JiandaoyunSchemaSyncError("简道云授权信息响应格式无效")
        normalized = [item for item in page if isinstance(item, dict)]
        items.extend(normalized)
        if len(page) < 100:
            return items
    raise JiandaoyunSchemaSyncError("授权应用或表单数量超过1000个，请缩小API Key授权范围")


def fetch_jiandaoyun_form_schema(
    settings: Settings,
    tenant_id: str,
    app_id: str,
    entry_id: str,
) -> dict[str, Any]:
    api_key = settings.jiandaoyun_api_key_for(tenant_id)
    if not api_key:
        raise JiandaoyunSchemaSyncError("未配置当前租户的简道云API密钥")
    return fetch_jiandaoyun_form_schema_with_key(
        settings.jiandaoyun_base_url,
        settings.jiandaoyun_timeout_seconds,
        api_key,
        app_id,
        entry_id,
    )


def fetch_jiandaoyun_form_schema_with_key(
    base_url: str,
    timeout_seconds: float,
    api_key: str,
    app_id: str,
    entry_id: str,
) -> dict[str, Any]:
    try:
        response = httpx.post(
            f"{base_url.rstrip('/')}/v5/app/entry/widget/list",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"app_id": app_id, "entry_id": entry_id},
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise JiandaoyunSchemaSyncError("简道云表单字段查询失败") from exc
    if not isinstance(payload, dict):
        raise JiandaoyunSchemaSyncError("简道云表单字段响应格式无效")
    if isinstance(payload.get("data"), dict) and "widgets" in payload["data"]:
        payload = payload["data"]
    if not isinstance(payload.get("widgets"), list):
        raise JiandaoyunSchemaSyncError("简道云表单字段响应缺少widgets")
    return payload


def synchronize_mapping(
    mapping: dict[str, Any],
    schema: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    updated = deepcopy(mapping)
    widgets = [item for item in schema.get("widgets", []) if isinstance(item, dict)]
    matched: list[dict[str, str]] = []
    unresolved: list[dict[str, str]] = []

    for section_name in ("fields", "record_fields", "output_fields"):
        section = updated.get(section_name, {})
        if not isinstance(section, dict):
            continue
        for canonical_name, spec in section.items():
            widget = _find_widget(widgets, spec)
            if widget is None:
                unresolved.append(
                    {
                        "path": f"{section_name}.{canonical_name}",
                        "field_name": _primary_label(spec) or canonical_name,
                    }
                )
                continue
            section[canonical_name] = _with_widget_metadata(spec, widget)
            matched.append(_match_record(f"{section_name}.{canonical_name}", widget))

    subforms = updated.get("subforms", {})
    if isinstance(subforms, dict):
        for canonical_name, subform in subforms.items():
            if not isinstance(subform, dict):
                continue
            parent = _find_widget(widgets, subform.get("field"))
            if parent is None:
                unresolved.append(
                    {
                        "path": f"subforms.{canonical_name}",
                        "field_name": _primary_label(subform.get("field")) or canonical_name,
                    }
                )
                continue
            subform["field"] = _with_widget_metadata(subform.get("field"), parent)
            matched.append(_match_record(f"subforms.{canonical_name}", parent))
            child_widgets = [
                item for item in parent.get("items", []) if isinstance(item, dict)
            ]
            for child_name, child_spec in subform.get("children", {}).items():
                child = _find_widget(child_widgets, child_spec)
                if child is None:
                    unresolved.append(
                        {
                            "path": f"subforms.{canonical_name}.children.{child_name}",
                            "field_name": _primary_label(child_spec) or child_name,
                        }
                    )
                    continue
                subform["children"][child_name] = _with_widget_metadata(child_spec, child)
                matched.append(
                    _match_record(f"subforms.{canonical_name}.children.{child_name}", child)
                )

    updated["status"] = (
        "copy_widget_ids_synced" if not unresolved else "copy_widget_ids_partially_synced"
    )
    updated["field_reference_mode"] = "widget_id_with_field_name_fallback"
    updated["last_schema_sync_at"] = datetime.now(UTC).isoformat()
    if schema.get("dataModifyTime") is not None:
        updated["source_data_modify_time"] = schema["dataModifyTime"]
    return updated, {
        "matched_count": len(matched),
        "unresolved_count": len(unresolved),
        "matched": matched,
        "unresolved": unresolved,
    }


def _find_widget(widgets: list[dict[str, Any]], spec: Any) -> dict[str, Any] | None:
    for label in _labels(spec):
        matches = [widget for widget in widgets if widget.get("label") == label]
        if len(matches) == 1:
            return matches[0]
    widget_id = _configured_widget_id(spec)
    if widget_id:
        matches = [widget for widget in widgets if _widget_id(widget) == widget_id]
        if len(matches) == 1:
            return matches[0]
    return None


def _labels(spec: Any) -> list[str]:
    if not isinstance(spec, dict):
        return []
    candidates = [spec.get("field_name"), *spec.get("aliases", [])]
    return [item for item in candidates if isinstance(item, str) and item]


def _primary_label(spec: Any) -> str | None:
    if isinstance(spec, dict) and isinstance(spec.get("field_name"), str):
        return spec["field_name"]
    return None


def _configured_widget_id(spec: Any) -> str | None:
    if isinstance(spec, str) and spec.startswith("_widget_"):
        return spec
    if isinstance(spec, dict):
        widget_id = spec.get("widget_id")
        if isinstance(widget_id, str) and widget_id:
            return widget_id
    return None


def _widget_id(widget: dict[str, Any]) -> str | None:
    for key in ("widgetName", "name"):
        value = widget.get(key)
        if isinstance(value, str) and value.startswith("_widget_"):
            return value
    return None


def _with_widget_metadata(spec: Any, widget: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(spec) if isinstance(spec, dict) else {}
    result["widget_id"] = _widget_id(widget)
    if isinstance(widget.get("name"), str):
        result["api_name"] = widget["name"]
    if isinstance(widget.get("type"), str):
        result["widget_type"] = widget["type"]
    return result


def _match_record(path: str, widget: dict[str, Any]) -> dict[str, str]:
    return {
        "path": path,
        "field_name": str(widget.get("label", "")),
        "widget_id": _widget_id(widget) or "",
    }
