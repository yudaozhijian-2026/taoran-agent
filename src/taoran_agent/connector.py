from __future__ import annotations

import json
import re
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .models import (
    EvidenceInput,
    JiandaoyunCheckRequest,
    JiandaoyunEvaluationRequest,
    PostEvaluationRequest,
    PrecheckRequest,
    VisitDraftInput,
)


class FieldTransferError(ValueError):
    """Safe configuration error, containing no input values or credentials."""


def load_jiandaoyun_mapping(path: str | None = None) -> dict[str, Any]:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    resource = files("taoran_agent.data").joinpath("jiandaoyun_mapping_example.json")
    return json.loads(resource.read_text(encoding="utf-8"))


def adapt_jiandaoyun_request(
    request: JiandaoyunCheckRequest,
    mapping: dict[str, Any],
    *,
    allow_precheck_defaults: bool = True,
) -> PrecheckRequest:
    values: dict[str, Any] = {}
    supplied_fields: set[str] = set()
    fields = mapping["fields"]
    value_maps = mapping.get("value_maps", {})
    for canonical_field, source_spec in fields.items():
        found, raw_value = _mapped_value(request.form_data, source_spec, canonical_field)
        if not found:
            continue
        supplied_fields.add(canonical_field)
        value = _unwrap(raw_value)
        field_value_map = value_maps.get(canonical_field)
        if (
            field_value_map
            and isinstance(value, (str, int, float, bool))
            and value in field_value_map
        ):
            value = field_value_map[value]
        if canonical_field == "duration_minutes":
            value = _duration_minutes(value)
        if canonical_field == "evidence_ids":
            value = _evidence_ids(value)
        if canonical_field == "visit_date":
            value = _visit_date(value)
        if canonical_field == "employee_id":
            value = _user_identifier(value)
        values[canonical_field] = value
    for canonical_field, source_spec in mapping.get("system_fields", {}).items():
        found, raw_value = _mapped_value(request.form_data, source_spec, canonical_field)
        if canonical_field not in VisitDraftInput.model_fields or not found:
            continue
        supplied_fields.add(canonical_field)
        values[canonical_field] = _unwrap(raw_value)
    for canonical_field, subform in mapping.get("subforms", {}).items():
        found, raw_rows = _mapped_value(
            request.form_data,
            subform.get("field"),
            canonical_field,
        )
        if not found:
            continue
        rows = _unwrap(raw_rows)
        if isinstance(rows, str):
            try:
                rows = json.loads(rows)
            except json.JSONDecodeError:
                raise FieldTransferError("子表字段传递格式异常，请核对字段绑定。") from None
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            raise FieldTransferError("子表字段应传递完整行数组，请核对字段绑定。")
        supplied_fields.add(canonical_field)
        children = subform.get("children", {})
        normalized_rows = []
        for row in rows:
            if not isinstance(row, dict):
                raise FieldTransferError("子表行传递格式异常，请核对字段绑定。")
            item = {}
            recognized = False
            for canonical, child_spec in children.items():
                child_found, raw_child = _mapped_value(row, child_spec, canonical)
                recognized = recognized or child_found
                child_value = _unwrap(raw_child) if child_found else None
                if child_value not in (None, "", []):
                    item[canonical] = child_value
            if row and not recognized:
                raise FieldTransferError("子表行未包含已配置的子字段，请核对字段绑定。")
            if item:
                normalized_rows.append(item)
        values[canonical_field] = normalized_rows
    defaulted_fields: list[str] = []
    if allow_precheck_defaults:
        if values.get("visit_date") is None:
            values["visit_date"] = datetime.now(ZoneInfo("Asia/Shanghai")).date()
            defaulted_fields.append("visit_date")
        if not values.get("employee_id"):
            values["employee_id"] = request.context.user_id or "jiandaoyun-user"
            defaulted_fields.append("employee_id")
    field_provenance = {
        field: (
            "jiandaoyun_subform"
            if field in mapping.get("subforms", {})
            else "jiandaoyun_system_field"
            if field in mapping.get("system_fields", {})
            else "jiandaoyun_form_field"
        )
        for field in supplied_fields
    }
    values["metadata"] = {
        "connector_source": "jiandaoyun",
        "source_supplied_fields": sorted(supplied_fields),
        "precheck_defaulted_fields": defaulted_fields,
        "field_provenance": field_provenance,
        "field_mapping_version": mapping.get("mapping_version"),
        "field_mapping_status": mapping.get("status"),
    }
    return PrecheckRequest(
        context=request.context,
        visit=VisitDraftInput.model_validate(values),
        feedback_mode=request.feedback_mode,
    )


def adapt_jiandaoyun_evaluation_request(
    request: JiandaoyunEvaluationRequest, mapping: dict[str, Any]
) -> PostEvaluationRequest:
    precheck = adapt_jiandaoyun_request(
        JiandaoyunCheckRequest(context=request.context, form_data=request.form_data),
        mapping,
        allow_precheck_defaults=False,
    )
    _, mapped_manager_comment = _mapped_value(
        request.form_data,
        mapping.get("record_fields", {}).get("manager_comment"),
        "manager_comment",
    )
    evidence = request.evidence or [
        EvidenceInput(
            evidence_id=evidence_id,
            source_object="VisitRecord",
            source_record_id=request.context.source_record_id,
            field_path="evidence_ids",
        )
        for evidence_id in precheck.visit.evidence_ids
    ]
    return PostEvaluationRequest(
        context=request.context,
        visit_record_code=request.visit_record_code,
        visit=precheck.visit,
        previous_visit_summary=request.previous_visit_summary,
        evidence=evidence,
        information_collection_updated=request.information_collection_updated,
        opportunity_updated=request.opportunity_updated,
        manager_comment=request.manager_comment or _unwrap(mapped_manager_comment),
        writeback_target=request.writeback_target,
    )


def mapped_jiandaoyun_value(
    data: dict[str, Any],
    source_spec: str | dict[str, Any] | None,
    canonical_field: str,
) -> Any:
    """Resolve a configured Jiandaoyun widget/field/alias and unwrap its value."""
    found, raw_value = _mapped_value(data, source_spec, canonical_field)
    return _unwrap(raw_value) if found else None


def _unwrap(value: Any) -> Any:
    if isinstance(value, dict) and "value" in value:
        return _unwrap(value["value"])
    if isinstance(value, str) and value.strip().lower() in {"", "null", "undefined"}:
        return None
    return value


def _mapped_value(
    data: dict[str, Any],
    source_spec: str | dict[str, Any] | None,
    canonical_field: str,
) -> tuple[bool, Any]:
    candidates: list[str] = []
    if isinstance(source_spec, str):
        candidates.append(source_spec)
    elif isinstance(source_spec, dict):
        widget_id = source_spec.get("widget_id")
        field_name = source_spec.get("field_name")
        aliases = source_spec.get("aliases", [])
        if isinstance(widget_id, str) and widget_id:
            candidates.append(widget_id)
        if isinstance(field_name, str) and field_name:
            candidates.append(field_name)
        if isinstance(aliases, list):
            candidates.extend(item for item in aliases if isinstance(item, str) and item)
    candidates.append(canonical_field)
    for candidate in dict.fromkeys(candidates):
        if candidate in data:
            return True, data[candidate]
    return False, None


def _duration_minutes(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return round(float(value))
    text = str(value).strip()
    try:
        return round(float(text))
    except ValueError:
        pass
    hours = re.search(r"(\d+(?:\.\d+)?)\s*(?:小时|h)", text, re.IGNORECASE)
    minutes = re.search(r"(\d+(?:\.\d+)?)\s*(?:分钟|min)", text, re.IGNORECASE)
    if not hours and not minutes:
        return None
    return round(
        (float(hours.group(1)) * 60 if hours else 0) + (float(minutes.group(1)) if minutes else 0)
    )


def _evidence_ids(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    items = value if isinstance(value, list) else [value]
    result = []
    for item in items:
        if isinstance(item, dict):
            candidate = item.get("_id") or item.get("key") or item.get("name") or item.get("url")
        else:
            candidate = item
        if candidate:
            result.append(str(candidate))
    return list(dict.fromkeys(result))


def _visit_date(value: Any) -> Any:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and "T" in value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return value
    else:
        return value
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(ZoneInfo("Asia/Shanghai"))
    return parsed.date()


def _user_identifier(value: Any) -> Any:
    if isinstance(value, list):
        value = value[0] if value else None
    if not isinstance(value, dict):
        return value
    for key in ("username", "user_id", "id", "_id", "name"):
        candidate = value.get(key)
        if candidate not in (None, ""):
            return str(candidate)
    return None
