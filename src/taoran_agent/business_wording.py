"""Render internal assessment notation without changing model facts or scoring."""

import re

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
