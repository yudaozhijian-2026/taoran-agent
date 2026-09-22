"""Lossless source registry and conservative, shared candidate boundaries.

Hints describe explicit wording, never establish goal attainment. Unknown actor,
time and outcome stay unknown; no customer-specific rules or acceptance lists.
"""
import re

from ..goal_normalization import normalize_expected_key_result
from .experimental_attribution import speaker_spans
from .experimental_business_semantic_state import (
    build_business_state,
    classify_field_state,
)
from .experimental_semantic_invariants import validate_invariants

VERSION = "record-state-v37-goal-boundary"
FIELDS = {
    "expected_key_result": "goal", "purpose_code": "purpose",
    "other_purpose": "purpose", "process_description": "reported_event",
    "customer_feedback": "reported_event", "self_assessment": "self_report",
    "next_action_purpose": "future_purpose", "next_action_other_purpose": "future_purpose",
    "next_action_expected_result": "future_goal", "next_contact_at": "planned_date",
}
GUIDANCE = """\nrecord_state是程序按原字段建立的来源索引，不是额外业务事实。
sources的id/field/start/end/quote不能改写；actor_hint、modality_hint仅来自明确措辞，不确定时保持unknown。
field_states=not_recorded只代表未记录，不代表没有发生或未约定。允许自然使用“记录未体现”“未填写”。
goal.parts是目标文字分项，不是实际已达成项；逐项比较对应事实。暂无采购计划属于已获得的需求现状信息，不等于存在积极采购意向。
信息确认类目标中，明确的否定回答也是确认结果：询问现阶段需求得到暂无计划，已经获得该需求状态；不得再写尚未确认需求。只有目标明确要求积极意向或采购承诺时，否定回答才不能满足该要求。复合目标需分别说已确认当前状态、下一步承诺在记录中尚未体现，不用后者否认前者。
客户表示会转告的已发生事实是“作出表态”，转告是否完成仍未知。self_report只描述销售自评，不作为AI已核实结论。
目标达成判断的唯一来源是expected_key_result。goal_source=key_result时，拜访目的、过程、客户反馈和下一步都不得替代原始目标。
goal_state=missing_placeholder时，目标达成必须保持unresolved；只说明没有填写可用于判断达成情况的具体关键结果，实际过程另述。
goal_state=broad时，目标达成同样保持unresolved；可说明关键结果表述较宽，但不得用拜访目的或过程自行定义验收目标。
复合目标和部分达成必须分别保留已有信息与尚未确认部分；不能从某项未证实推导整体自评错误。
后续建议允许确认新信息，但不得暗示这些信息已发生。保留正确事实，不强行凑自评差距。
"""


def build(context):
    sources, states = [], {}
    for field, role in FIELDS.items():
        value = context.get(field)
        states[field] = "not_recorded" if classify_field_state(field, value) == "missing" else "recorded"
        if states[field] == "not_recorded":
            sources.append({"id": f"s{len(sources)}", "field": field, "start": None,
                "end": None, "quote": "", "role": role, "actor_hint": "record",
                "modality_hint": "not_recorded", "negative_statement": False})
        if not isinstance(value, str) or not value.strip():
            continue
        for match in re.finditer(r"[^。！？；;\n]{1,400}[。！？；;]?", value):
            quote = match.group()
            actor = "unknown"
            speakers = speaker_spans(quote) if role == "reported_event" else []
            roles = {item["role"] for item in speakers}
            if len(roles) == 1 and speakers[0]["quote"].rstrip("。；;") == quote.rstrip("。；;"):
                actor = next(iter(roles))
            elif re.fullmatch(r"(?:向|给)客户介绍[^。；]*[。；]?|催款[。；]?", quote):
                actor = "sales"
            modality = "unknown"
            if role.startswith("future_") or role == "planned_date":
                modality = "planned"
            elif role == "self_report":
                modality = "self_reported"
            elif role == "reported_event":
                modality = "mixed_or_planned" if re.search(r"计划|将|会|后续|下次|明天|先将|待", quote) else "reported"
            sources.append({"id": f"s{len(sources)}", "field": field, "start": match.start(),
                "end": match.end(), "quote": quote, "role": role,
                "actor_hint": actor, "modality_hint": modality,
                "negative_statement": bool(re.search(r"没有|暂无|不再|不会|未能|暂不", quote))})
    normalized_goal = normalize_expected_key_result(context.get("expected_key_result"))
    goal = normalized_goal.goal_normalized or ""
    parts = [] if normalized_goal.goal_state == "missing_placeholder" else [part for part in re.split(r"[，,；;。]|并且|以及|并(?=同意|承诺|确认)", goal) if part.strip()]
    from .experimental_rendering_guidance import rendering_input
    rendering = rendering_input(build_business_state(context))
    return {**rendering, "version": VERSION, "field_states": states, "sources": sources,
        "BUSINESS_SEMANTIC_STATE": build_business_state(context),
        "goal": {"status": normalized_goal.legacy_status, "text": goal,
            **normalized_goal.as_dict(), "parts": [
            {"id": f"g{i}", "text": part, "attainment": "unassessed"} for i, part in enumerate(parts)]}}


def boundary_issues(text, context):
    """Bounded shared checks, not a general Chinese inference engine."""
    state = build(context)
    issues = validate_invariants(text, state["BUSINESS_SEMANTIC_STATE"])
    for sentence in re.split(r"[，,。！？；;\n]", text):
        if not sentence.strip():
            continue
        for field, label in (("next_contact_at", r"(?:下次)?联系(?:时间|日期)"),
                             ("expected_key_result", r"(?:本次)?(?:目标|关键结果)"),
                             ("process_description", r"过程(?:描述|记录)")):
            if state["field_states"][field] == "recorded" and (
                re.search(label + r"(?:尚)?(?:未填写|未提供|为空|空白)", sentence)
                or re.search(r"(?:未填写|未提供)(?:具体)?" + label, sentence)
            ):
                issues.append({"error_type": "recorded_as_missing", "field": field, "text": sentence})
        recorded_no_agreement = any(
            fact.get("source_field") in {"process_description", "customer_feedback"}
            and fact.get("record_status") == "negative_fact"
            and re.search(
                r"(?:未|没有|尚未)(?:约定|安排|确定)"
                r"[^，,。；;]{0,8}(?:联系|拜访)(?:时间|日期)",
                str(fact.get("text") or ""),
            )
            for fact in state["BUSINESS_SEMANTIC_STATE"].get("facts", [])
        )
        if (state["field_states"]["next_contact_at"] == "not_recorded"
                and not recorded_no_agreement
                and re.search(
                    r"(?:未|没有|尚未)(?:约定|安排|确定)"
                    r"[^\uff0c,。；;]{0,8}(?:联系|拜访)(?:时间|日期)",
                    sentence,
                )
                and not re.search(r"(?:记录|填写|表单).{0,8}(?:未|没有|尚未)(?:体现|记录|显示)", sentence)):
            issues.append({"error_type": "missing_as_absent", "field": "next_contact_at", "text": sentence})
        if state["goal"]["goal_state"] == "missing_placeholder":
            # An explicitly unknown target cannot become any named business goal.
            named = [str(context.get("purpose_code") or ""), str(context.get("process_description") or "")]
            inferred = any(value and len(value) <= 16 and re.search(re.escape(value) + r"(?:这一|的)?目标", sentence) for value in named)
            substituted = re.search(r"(?:本次|拜访)?目标(?:是|为|就是)[^，,。；]+", sentence)
            assessment = "目标" in sentence and re.search(r"(?:不足以证明|未能|尚未|已经|已)(?:实现|达成)|不足以证明.{0,24}目标.{0,10}(?:实现|达成)", sentence) and not re.search(r"自评(?:为|是)?(?:达到|已|部分|未)", sentence)
            disclaimer = re.search(r"(?:不能|不得|不应|不要|不是|并非).{0,16}(?:当作|替代|代替|视为)|无法.{0,8}(?:确定|判断|解释)|目标.{0,8}(?:不清楚|不明确|占位)", sentence)
            if (inferred or substituted or assessment) and not disclaimer:
                issues.append({"error_type": "unknown_goal_assessed", "field": "expected_key_result", "text": sentence})
        if state["goal"]["goal_state"] == "broad":
            assessment = "目标" in sentence and re.search(
                r"(?:目标.{0,12}(?:已经|已|尚未|未能|不足以证明).{0,8}(?:实现|达成)|"
                r"(?:已经|已|尚未|未能|不足以证明).{0,12}目标.{0,8}(?:实现|达成))",
                sentence,
            )
            disclaimer = re.search(r"无法(?:确定|判断)|关键结果.{0,8}(?:较宽|不明确|不具体)", sentence)
            if assessment and not disclaimer:
                issues.append({"error_type": "broad_goal_assessed", "field": "expected_key_result", "text": sentence})
    return issues
