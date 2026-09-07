"""Non-blocking, local wording repairs; never change scores or submission state."""

import re

from .experimental_business_semantic_state import build_business_state

PREVIEW_ADVICE_GUIDANCE = """
联系日期与双方共识分别表达：next_contact_at只代表填写的计划日期，不证明客户同意。
例：过程“收集信息”，计划联系日期9月24日 → “计划于9月24日再次联系”，不能写“已约定”。
反例：过程“简单沟通后约定下次拜访” → 保留已经形成的再访约定，不把它改成尚未约定。
若过程确认了再访但没有确认具体日期，分别写已约定再访和计划联系日期，不把两项合成已约定该日期。
"""
FINAL_ADVICE_GUIDANCE = """
过程质量与目标支持程度分别形成意见。R只核对过程是否含客户表达/动作，以及销售观点的事实依据；不得用目标尚未实现代替这两项判断。
例：目标“客户确认合同最终版本并承诺签署”，过程“客户申请已提交，本月内点单付款”：提取申请提交的客户事实和连续原文证据；点单付款仍为计划。合同确认/签署证据不足放在目标分析或目标核对建议，不能因此给R填写“不具体”的建议。
反例：过程“给客户网上找商品”只记录销售动作，仍可在R建议补充客户实际表达；过程有“我认为项目必能推进”但无事实支撑，仍保留R的观点依据建议。
此处只修复意见的表达及归属，不改变正式评分，不假定客户已确认合同或承诺签署。
"""

_DATE = r"(?:\d{4}年)?\d{1,2}月\d{1,2}[日号]|\d{4}-\d{2}-\d{2}"
_AGREED_DATE = re.compile(
    r"(?P<actor>销售与客户|销售和客户|双方|与客户|和客户|客户)?"
    r"(?:已经|已)?(?:约定|商定)(?:于|在)?\s*(?P<date>" + _DATE + r")"
    r"(?P<action>(?:再|再次|下次)?(?:联系|拜访|沟通|会面))"
)
_NON_ASSERTED = re.compile(r"未|没有|不曾|并非|尚未|建议|希望|计划|拟|争取|待|如果|例如|比如")


def _calendar(value):
    text = str(value or "")
    match = re.search(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})[日号]", text)
    if match:
        return tuple(int(x) if x else None for x in match.groups())
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    return tuple(map(int, match.groups())) if match else None


def _same_date(a, b):
    x, y = _calendar(a), _calendar(b)
    return bool(x and y and x[1:] == y[1:] and (x[0] is None or y[0] is None or x[0] == y[0]))


def repair_preview_text(text, snapshot):
    """One pass on already flushable sentences. Uncertain cases stay observable."""
    if not _AGREED_DATE.search(text):
        return text, []
    state = build_business_state(snapshot)
    agreements = [
        f
        for f in state["facts"]
        if f["source_field"] in {"process_description", "customer_feedback"}
        and f["fact_type"] in {"JOINT_AGREEMENT", "CUSTOMER_COMMITMENT"}
        and f["record_status"] == "recorded"
        and f["polarity"] == "positive"
        and re.search(r"约定|商定|(?:同意|确认).{0,20}(?:联系|拜访|沟通|会面|再访)", f["text"])
        and not re.search(r"尚未|没有|未约定|未同意|待确认|计划约定|希望约定", f["text"])
    ]
    changes = []

    def replace(match):
        prefix = re.split(r"[，,。！？；;\n]", text[: match.start()])[-1]
        if _NON_ASSERTED.search(prefix):
            return match.group()
        date = match.group("date")
        if any(
            _same_date(token.group(), date)
            for f in agreements
            for token in re.finditer(_DATE, f["text"])
        ):
            return match.group()
        if not _same_date(date, snapshot.get("next_contact_at")):
            return match.group()  # This repair does not guess dates.
        changes.append({"kind": "plan_date_wording", "surface": "preview"})
        if agreements:
            return "已形成再访或联系约定，计划于" + date + match.group("action")
        return "计划于" + date + match.group("action")

    return _AGREED_DATE.sub(replace, text), changes


def repair_r_recommendation(items, context):
    """Move a narrow goal-evidence reminder; preserve every feature and score."""
    process = str(context.get("process_description") or "")
    goal = str(context.get("expected_key_result") or "")
    # Opinions with unsupported confidence remain genuine R issues.
    if not re.search(r"合同|签署", goal) or re.search(
        r"我认为|我感觉|我判断|应该|估计|可能|大概|必能|必然|肯定", process
    ):
        return items, []
    state = build_business_state(context)
    facts = [
        f
        for f in state["facts"]
        if f["source_field"] == "process_description"
        and f["temporality"] == "actual"
        and f["polarity"] == "positive"
        and f["record_status"] == "recorded"
        and (
            f["fact_type"] == "CUSTOMER_ACTION"
            or f["fact_type"] == "COMPLETED_EVENT"
            and "客户" in f["text"]
        )
        and f["text"] in process
    ]
    if not facts:
        return items, []
    result = list(items)
    changes = []
    for i, item in enumerate(items):
        if item.code != "R" or item.specific is not False:
            continue
        if not re.search(
            r"(?:无法确认|尚未体现|未体现|缺少|未记录).{0,30}(?:合同|签署)", item.suggestion
        ):
            continue
        if re.search(r"观点|判断|推断|乐观|没有客户动作|未记录任何客户", item.suggestion):
            continue
        quote = facts[0]["text"]
        if len(quote) > 60 or len(goal) > 80:
            continue
        suggestion = f"过程已记录“{quote}”。请围绕本次目标“{goal}”补充对应的实际结果；申请或其他过程进展不能代替合同目标的确认依据。"
        if len(suggestion) > 160:
            continue
        result[i] = item.model_copy(update={"suggestion": suggestion})
        changes.append({"kind": "goal_reminder_relocated", "surface": "final", "code": "R"})
    return result, changes
