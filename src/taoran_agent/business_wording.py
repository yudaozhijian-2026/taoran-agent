"""Render internal assessment notation without changing model facts or scoring."""

import re
from collections.abc import Callable
from copy import deepcopy
from typing import Any

from .field_labels import display_form_field_name

_FLAGS = {
    "key_result_quality_ok": ("原定关键结果具体、可核对", "原定关键结果尚不够具体、可核对"),
    "process_fact_based": ("过程描述包含实际发生的事实", "过程描述的事实依据不足"),
    "customer_consensus_met": ("客户共识条件符合本次判定规则", "客户共识条件尚未满足"),
    "next_action_logic_ok": ("下一步安排符合衔接要求", "下一步安排尚不符合衔接要求"),
}
_LABELS = {
    "key_result_quality_ok": "关键结果的明确程度",
    "process_fact_based": "过程描述的事实依据",
    "customer_consensus_met": "客户共识条件",
    "next_action_logic_ok": "下一步安排的衔接情况",
    "purpose_achievement": "目标达成情况",
    "partially_achieved": "部分达成",
    "not_achieved": "尚未达成",
    "achieved": "已达成",
    "evidence_fields": "事实依据对应的记录内容",
    "needs_revision": "需改进",
    "not_evaluated": "不足以判断",
    "met": "符合要求",
    "true": "是",
    "false": "否",
}
_TOKEN = r"(?<![A-Za-z0-9_])[A-Za-z_][A-Za-z0-9_]*(?:\[\]\.[A-Za-z_][A-Za-z0-9_]*)?(?![A-Za-z0-9_])"

_FIELD_VALUE_LABELS = {
    "customer_type_ii": {
        "potential": "潜力客户",
        "target": "目标客户",
        "opportunity": "商机客户",
    },
    "visit_method": {
        "face_to_face": "面对面拜访",
        "video": "视频会议",
        "phone": "电话拜访",
        "asynchronous_message": "微信/邮件/QQ沟通",
    },
    "self_assessment": {
        "achieved": "达到目的",
        "partially_achieved": "部分达到目的",
        "not_achieved": "未达到目的",
    },
    "is_appointment": {
        True: "已预约",
        False: "未预约",
        "true": "已预约",
        "false": "未预约",
    },
}

_CUSTOMER_TYPE_ALIASES = {
    "潜在客户": "潜力客户",
    "潜在型客户": "潜力客户",
    "目标型客户": "目标客户",
    "机会客户": "商机客户",
    "商机型客户": "商机客户",
}

_METHOD_ALIASES = {
    "asynchronous_message": (
        "异步沟通", "异步交流", "异步拜访", "异步消息沟通", "线上文字沟通", "即时通讯沟通",
    ),
    "face_to_face": ("线下面访", "面对面沟通方式"),
    "video": ("视频拜访", "视频沟通方式"),
    "phone": ("电话沟通方式",),
}


def business_field_value(field: str, value: Any) -> Any:
    """Return the exact user-facing option for model-facing visit context."""
    if hasattr(value, "value"):
        value = value.value
    return _FIELD_VALUE_LABELS.get(field, {}).get(value, value)


def model_facing_visit_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Hide internal enums while retaining all original business facts."""
    result = deepcopy(snapshot)
    for field in _FIELD_VALUE_LABELS:
        if field in result:
            result[field] = business_field_value(field, result[field])
    return result


def normalize_generated_business_terms(value: str, context: dict[str, Any] | None = None) -> str:
    """Normalize model-created aliases to actual form options.

    Only model wording is passed here; source quotes remain untouched.  Method
    aliases are replaced only when the current record proves the exact method.
    """
    text = value or ""
    for alias, label in _CUSTOMER_TYPE_ALIASES.items():
        text = text.replace(alias, label)

    context = context or {}
    raw_method = context.get("visit_method")
    method_key = raw_method.value if hasattr(raw_method, "value") else raw_method
    if method_key not in _METHOD_ALIASES:
        method_key = {
            "面对面拜访": "face_to_face",
            "视频会议": "video",
            "电话拜访": "phone",
            "微信/邮件/QQ沟通": "asynchronous_message",
            "微信/QQ/邮件沟通": "asynchronous_message",
        }.get(str(method_key or ""))
    if method_key in _METHOD_ALIASES:
        exact = business_field_value("visit_method", method_key)
        for alias in _METHOD_ALIASES[method_key]:
            text = text.replace(alias, exact)
    return text


def normalize_generated_payload_wording(raw: Any, context: dict[str, Any] | None = None) -> Any:
    """Normalize user-visible model text without modifying proofs or facts."""
    if not isinstance(raw, dict):
        return raw
    result = deepcopy(raw)
    for root, keys in (
        ("analysis_points", ("text",)),
        ("items", ("suggestion",)),
        ("confirmations", ("question", "impact")),
    ):
        for item in result.get(root, []):
            if not isinstance(item, dict):
                continue
            for key in keys:
                if isinstance(item.get(key), str):
                    item[key] = normalize_generated_business_terms(item[key], context)
    if isinstance(result.get("suggestion_reason"), str):
        result["suggestion_reason"] = normalize_generated_business_terms(
            result["suggestion_reason"], context,
        )
    return result


def _active_generated_aliases(context: dict[str, Any] | None = None) -> tuple[str, ...]:
    aliases = list(_CUSTOMER_TYPE_ALIASES)
    context = context or {}
    raw_method = context.get("visit_method")
    method_key = raw_method.value if hasattr(raw_method, "value") else raw_method
    if method_key not in _METHOD_ALIASES:
        method_key = {
            "面对面拜访": "face_to_face",
            "视频会议": "video",
            "电话拜访": "phone",
            "微信/邮件/QQ沟通": "asynchronous_message",
            "微信/QQ/邮件沟通": "asynchronous_message",
        }.get(str(method_key or ""))
    aliases.extend(_METHOD_ALIASES.get(method_key, ()))
    return tuple(aliases)


class BusinessWordingStream:
    """Normalize business aliases across arbitrary model stream boundaries."""

    def __init__(self, emit: Callable[[str], None] | None, context: dict[str, Any] | None = None):
        self.emit = emit
        self.context = context or {}
        self.aliases = _active_generated_aliases(self.context)
        self.pending = ""

    def feed(self, chunk: str) -> None:
        if self.emit is None or not chunk:
            return
        self.pending += chunk
        hold = 0
        for alias in self.aliases:
            for size in range(1, min(len(alias), len(self.pending)) + 1):
                if self.pending.endswith(alias[:size]) and size < len(alias):
                    hold = max(hold, size)
        safe = self.pending[:-hold] if hold else self.pending
        self.pending = self.pending[-hold:] if hold else ""
        if safe:
            self.emit(normalize_generated_business_terms(safe, self.context))

    def flush(self) -> None:
        if self.emit is not None and self.pending:
            self.emit(normalize_generated_business_terms(self.pending, self.context))
        self.pending = ""


def business_wording(value: str) -> str:
    """Translate known internal notation only; preserve business names and layout."""
    text = value or ""
    for key, phrases in _FLAGS.items():
        pattern = (
            r"(?<![A-Za-z0-9_])(?:facts\.)?[`\"']?" + key
            + r"[`\"']?\s*(?:==|=|:|：|为|是)\s*[`\"']?(true|false)[`\"']?(?![A-Za-z0-9_])"
        )
        text = re.sub(pattern, lambda m, phrases=phrases: phrases[m[1].lower() == "false"], text,
                      flags=re.IGNORECASE)

    def translate(match: re.Match[str]) -> str:
        token = match[0]
        return _LABELS.get(token) or display_form_field_name(token) or token

    return re.sub(_TOKEN, translate, text)
