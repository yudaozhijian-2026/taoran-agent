from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from typing import Any

from .config import get_settings
from .connector import load_jiandaoyun_mapping

_BUSINESS_LABEL_OVERRIDES = {
    "submitted_at": "提交时间",
    "source_record_id": "数据ID",
    "opportunity_id": "商机编号",
    "opportunity_stage": "最新商机阶段",
    "customer_feedback": "客户反馈",
    "deviation_reason": "偏差原因",
    "next_action_target_id": "下一次行动对象",
    "information_collection_updated": "客户信息是否已更新",
    "opportunity_updated": "商机信息是否已更新",
    "purpose_policy": "拜访目的生效策略",
    "metadata": "系统辅助信息",
}
_DEFAULT_MAPPING = object()
_ACTIVE_MAPPING_PATH: ContextVar[str | None | object] = ContextVar(
    "taoran_active_mapping_path",
    default=_DEFAULT_MAPPING,
)


def display_field_name(field_path: str) -> str:
    """Return the Jiandaoyun business label while preserving canonical paths internally."""
    active_mapping = _ACTIVE_MAPPING_PATH.get()
    mapping_path = (
        get_settings().jiandaoyun_mapping_path
        if active_mapping is _DEFAULT_MAPPING
        else active_mapping
    )
    labels = _field_labels(mapping_path)
    if field_path in labels:
        return labels[field_path]
    normalized = field_path.replace("[]", "")
    if normalized in labels:
        return labels[normalized]
    return _BUSINESS_LABEL_OVERRIDES.get(field_path, "相关字段")


@contextmanager
def use_field_mapping(mapping_path: str | None) -> Iterator[None]:
    """Use one tenant's labels while building user-visible feedback for this request."""
    token = _ACTIVE_MAPPING_PATH.set(mapping_path)
    try:
        yield
    finally:
        _ACTIVE_MAPPING_PATH.reset(token)


@lru_cache(maxsize=8)
def _field_labels(mapping_path: str | None) -> dict[str, str]:
    mapping = load_jiandaoyun_mapping(mapping_path)
    labels = dict(_BUSINESS_LABEL_OVERRIDES)
    for section_name in ("fields", "record_fields", "output_fields", "system_fields"):
        section = mapping.get(section_name, {})
        if not isinstance(section, dict):
            continue
        for canonical_name, spec in section.items():
            label = _field_name(spec)
            if label:
                labels[canonical_name] = label
    for canonical_name, subform in mapping.get("subforms", {}).items():
        if not isinstance(subform, dict):
            continue
        parent_label = _field_name(subform.get("field"))
        if parent_label:
            labels[canonical_name] = parent_label
        children = subform.get("children", {})
        if not isinstance(children, dict):
            continue
        for child_name, child_spec in children.items():
            child_label = _field_name(child_spec)
            if parent_label and child_label:
                labels[f"{canonical_name}[].{child_name}"] = f"{parent_label}.{child_label}"
                labels[f"{canonical_name}.{child_name}"] = f"{parent_label}.{child_label}"
    labels["submitted_at"] = "提交时间"
    return labels


def _field_name(spec: Any) -> str | None:
    if isinstance(spec, dict):
        value = spec.get("field_name")
        return value if isinstance(value, str) and value else None
    return spec if isinstance(spec, str) and spec else None
