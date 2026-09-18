"""Render internal assessment notation without changing model facts or scoring."""

import re
from collections.abc import Callable
from copy import deepcopy
from datetime import date, datetime
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

# These expressions describe implementation details, not useful coaching for a
# salesperson.  Keep the scoring facts internally, but never expose this
# vocabulary in the visit analysis, advice or confirmation text.
_INTERNAL_FEEDBACK_PATTERNS = (
    ("internal_field", re.compile(
        r"(?<![A-Za-z0-9_])(?:period_met|after_visit|customer_consensus_required|"
        r"customer_consensus_met|next_action_logic_ok|authoritative_checks|advice_basis|"
        r"gap_kind|confirmed_findings)(?![A-Za-z0-9_])",
        re.IGNORECASE,
    )),
    ("internal_dimension", re.compile(
        r"(?<![A-Za-z0-9_])(?:T|A1|O_KR|R|A2|N)\s*(?:整体|项|维度|检查|判定)",
        re.IGNORECASE,
    )),
    ("threshold_wording", re.compile(r"(?:程序|评分)?(?:时间|日期|共识)?门槛")),
    ("consensus_exemption", re.compile(
        r"(?:潜力客户|目标客户).{0,28}(?:无(?:客户)?共识要求|(?:不要求|无需|不强制|不适用).{0,16}(?:客户)?共识)|"
        r"(?:客户)?共识.{0,16}(?:豁免|视为满足|自动满足|不适用)",
    )),
    ("exception_explanation", re.compile(
        r"(?:说明|解释|提供).{0,24}(?:跨(?:北京时间)?(?:自然)?(?:月|季度)|日期|时间|共识|规则)"
        r".{0,24}(?:不适用|例外|豁免).{0,12}(?:依据|原因|理由)",
    )),
)

SALESPERSON_WORDING_GUIDANCE = (
    "面向销售的本次拜访分析、改善建议和需确认事项只能描述当前记录中的业务事实、具体缺口和可执行修改方式。"
    "不得输出内部字段名、布尔值、程序判定过程、TAORAN字母检查代码或‘整体达标/不达标’；"
    "不得使用‘门槛、豁免、自动视为满足、不适用共识’等内部规则表述。"
    "某项规则对当前客户类型不适用时直接不提，不要求销售解释例外或提供规则不适用依据。"
    "联系日期有问题时只描述真实状态，例如未填写、不晚于本次拜访日期、仍处于同一自然月或同一自然季度。"
)


def salesperson_feedback_hits(value: str) -> list[dict[str, str]]:
    """Return implementation-language leaks found in salesperson-visible text."""
    text = value or ""
    hits = []
    for code, pattern in _INTERNAL_FEEDBACK_PATTERNS:
        for match in pattern.finditer(text):
            hits.append({"code": code, "quote": match.group(0)})
    return hits


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


def _contact_fact_wording(context: dict[str, Any] | None) -> str:
    """Describe the recorded date state without exposing the policy machinery."""
    context = context or {}
    policy = context.get("next_contact_policy")
    if not isinstance(policy, dict):
        policy = (context.get("_authoritative_checks") or {}).get("next_contact_policy")
    if isinstance(policy, dict):
        if policy.get("date_state") == "missing":
            return "下一次联系客户时间安排尚未填写"
        if policy.get("after_visit") is False:
            return "填写的下一次联系日期不晚于本次拜访日期"
        if policy.get("period_met") is False:
            if policy.get("period") == "month":
                return "填写的下一次联系日期与本次拜访仍在同一自然月"
            if policy.get("period") == "quarter":
                return "填写的下一次联系日期与本次拜访仍在同一自然季度"

    current = _as_date(context.get("visit_date"))
    following = _as_date(context.get("next_contact_at"))
    customer_type = str(context.get("customer_type_ii") or "")
    if following is None:
        presence = (context.get("_record_contract") or {}).get("presence", {})
        if "next_contact_at" in context or presence.get("next_contact_at") == "empty":
            return "下一次联系客户时间安排尚未填写"
        return "下一次联系客户时间安排需要结合当前记录进一步核对"
    if current is not None and following <= current:
        return "填写的下一次联系日期不晚于本次拜访日期"
    if (
        current is not None
        and customer_type in {"target", "目标客户"}
        and (following.year, following.month) == (current.year, current.month)
    ):
        return "填写的下一次联系日期与本次拜访仍在同一自然月"
    if (
        current is not None
        and customer_type in {"potential", "潜力客户", "潜在客户"}
        and (following.year, (following.month - 1) // 3)
        == (current.year, (current.month - 1) // 3)
    ):
        return "填写的下一次联系日期与本次拜访仍在同一自然季度"
    return "下一次联系客户时间安排需要结合当前记录进一步核对"


def salesperson_wording(value: str, context: dict[str, Any] | None = None) -> str:
    """Remove internal policy narration while preserving actual business findings."""
    text = business_wording(value or "")
    contact_fact = _contact_fact_wording(context)
    text = re.sub(
        r"(?:程序|系统)?判定\s*[`\"']?period_met[`\"']?\s*(?:为|=|是)?\s*(?:否|false)",
        contact_fact,
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?<![A-Za-z0-9_])N\s*整体(?:仍)?(?:未|不)(?:满足|通过|达标)?(?:程序)?(?:时间|日期)?门槛",
        contact_fact,
        text,
        flags=re.IGNORECASE,
    )
    # Applicability decisions stay in audit/scoring.  They are not a finding or
    # an action for the salesperson, so remove only the matching clause.
    text = re.sub(
        r"(?:潜力客户|目标客户)[^。；\n]{0,40}(?:无(?:客户)?共识要求|"
        r"(?:不要求|无需|不强制|不适用)[^。；\n]{0,20}(?:客户)?共识)"
        r"[^。；\n]*(?:[。；]|$)",
        "",
        text,
    )
    text = re.sub(
        r"(?:客户)?共识[^。；\n]{0,20}(?:豁免|视为满足|自动满足|不适用)[^。；\n]*(?:[。；]|$)",
        "",
        text,
    )
    text = re.sub(
        r"[^。；\n]{0,18}(?:说明|解释|提供)[^。；\n]{0,35}"
        r"(?:跨(?:北京时间)?(?:自然)?(?:月|季度)|日期|时间|共识|规则)[^。；\n]{0,24}"
        r"(?:不适用|例外|豁免)[^。；\n]{0,12}(?:依据|原因|理由)[^。；\n]*(?:[。；]|$)",
        "",
        text,
    )
    text = re.sub(r"[；;]\s*[；;]", "；", text)
    text = re.sub(r"。\s*。", "。", text)
    text = re.sub(r"(?m)^\s*\d+[.、]\s*$", "", text)
    text = re.sub(r"(?m)^AI改善建议：\s*\Z", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(" \n；;")


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
                    item[key] = salesperson_wording(
                        normalize_generated_business_terms(item[key], context), context,
                    )
    if isinstance(result.get("suggestion_reason"), str):
        result["suggestion_reason"] = salesperson_wording(
            normalize_generated_business_terms(result["suggestion_reason"], context), context,
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
