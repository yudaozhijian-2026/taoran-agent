"""Business wording for the post-submit Deep Review result only.

This module is deliberately downstream from scoring and semantic review.  It
does not mutate the visit, model facts, findings, evidence, scores, or contact
policy.  It only renders the already-authoritative result for a salesperson.
"""

from __future__ import annotations

import re
from typing import Any

from .models import Q34SemanticFacts, VisitDraftInput

VERSION = "BACKEND-SALESPERSON-WORDING-V2-20260921"

_INTERNAL_OR_JUDGING = re.compile(
    r"(?:下一步逻辑(?:不成立|不完整|错误|校验失败)|客户共识不足|"
    r"(?:规则|逻辑|校验)(?:不通过|失败)|不合法|不合格|"
    r"符合(?:潜力客户|目标客户|商机客户|TAORAN|N-\d+)[^，。；\n]{0,12}(?:标准|要求)|"
    r"(?:N-\d+|finding_id|decision_ledger|field_paths|validator|schema|"
    r"next_action_logic_ok|customer_consensus_met|needs_revision|not_evaluated))",
    re.IGNORECASE,
)
_HARD_CADENCE = re.compile(
    r"(?:必须|确保|应当|须|需要)[^，。；\n]{0,24}跨(?:北京时间)?(?:自然)?(?:月|季度)|"
    r"(?:请|建议)[^，。；\n]{0,36}(?:联系|日期|时间)[^，。；\n]{0,24}跨(?:北京时间)?(?:自然)?(?:月|季度)|"
    r"(?:潜力客户|目标客户)[^，。；\n]{0,18}(?:必须|要求|需)[^，。；\n]{0,18}跨(?:北京时间)?(?:自然)?(?:月|季度)"
)
_PASS_ONLY = re.compile(
    r"^(?:过程(?:描述)?有客户事实(?:依据|支撑)|过程事实清楚|"
    r"自评(?:达到目的|部分达到目的|未达到目的)?与(?:事实|记录|实际达成)一致|"
    r"(?:联系日期|日期)(?:已)?跨自然(?:月|季度)[^。]*|"
    r"(?:类型|目的|预约|方式)[^。]{0,80}(?:匹配|完整|如实记录|无矛盾))。?$"
)
_PLACEHOLDER = re.compile(r"^(?:测试\s*)+$|^(?:保持联系|继续跟进|再沟通|收集信息)$")
_CONFIRMED = re.compile(r"客户[^。；\n]{0,28}(?:确认|同意|约定|承诺)|双方[^。；\n]{0,12}约定")
_CONDITIONAL = re.compile(r"(?:如果|若|待|在)[^。；\n]{0,18}(?:后|情况下|通过|批准)|有条件|条件是|前提是")
_TIME_FACT = re.compile(
    r"(?:\d{1,2}月\d{1,2}日|\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?|"
    r"下(?:周|月|季度)|本周[一二三四五六日天]?|周[一二三四五六日天]|明天|后天)"
)
_ACTION_FACT = re.compile(
    r"(?:沟通|联系|拜访|评审|培训|试用|报价|方案|清单|下单|采购|安装|验收|反馈|确认|提供|提交|完成)"
)


def render_backend_business_feedback(
    visit: VisitDraftInput,
    semantic_facts: Q34SemanticFacts,
    analysis_text: str,
    advice_items: list[str],
) -> tuple[str, list[str]]:
    """Render Deep Review facts as coaching without changing any decision."""
    analysis = _render_analysis(visit, semantic_facts, analysis_text)
    advice = _render_advice(visit, semantic_facts, advice_items)
    _assert_safe(analysis, advice)
    return analysis, advice


def render_backend_safe_feedback(
    visit: VisitDraftInput,
    semantic_facts: Q34SemanticFacts,
) -> tuple[str, list[str]]:
    """Render a conservative, deterministic fallback from accepted facts only.

    It intentionally ignores model-written analysis and suggestions.  The
    formal semantic result, evidence-derived outcomes and advice bases remain
    the only inputs, so a wording-safety failure cannot require another model
    call or expose an internal label.
    """
    audit = semantic_facts.quality_audit or {}
    status = str(audit.get("achievement_status") or semantic_facts.purpose_achievement.value)
    outcome = _safe_outcome(audit.get("actual_outcomes", []))
    if status == "unresolved":
        analysis = "当前目标描述较宽，现有记录不足以可靠判断是否已经完整达成。"
    elif status in {"partially_achieved", "partially_supported"}:
        analysis = "本次已经形成阶段性业务进展，仍有事项需要结合实际沟通继续明确。"
    elif status in {"achieved", "supported"}:
        analysis = "当前记录支持本次已经完成原定事项。"
    else:
        analysis = "当前记录尚未形成支持完成结论的充分事实，后续可以结合实际情况继续推进。"
    if outcome:
        analysis = "本次已经记录了具体业务进展：" + outcome + "。" + analysis

    advice = []
    for section in semantic_facts.sections:
        if section.verdict != "needs_revision":
            continue
        item = _SAFE_ADVICE_BY_SECTION.get(section.code)
        if item:
            advice.append(item)
    policy = _contact_policy(semantic_facts)
    contact = _contact_advice(visit, semantic_facts, policy)
    if contact:
        advice.append(contact)
    if not advice and any(section.verdict == "needs_revision" for section in semantic_facts.sections):
        advice.append("可以结合实际沟通，补充本次尚未明确的具体业务信息，并据实记录结果。")
    advice = _deduplicate_sentences(advice)
    _assert_safe(analysis, advice)
    return analysis, advice


_SAFE_ADVICE_BY_SECTION = {
    "T": "可以依据当前业务阶段和实际拜访目的，核对记录是否一致。",
    "A1": "可以结合实际情况，核对预约和拜访方式的记录是否完整。",
    "O_KR": "下一步可以明确希望确认的具体事项，并根据实际沟通结果据实记录。",
    "R": "可以据实记录本次实际沟通事实和客户实际回应。",
    "A2": "可以根据本次已经记录的实际进展，核对自评是否一致。",
    "N": "下一步可以结合实际沟通，继续确认客户希望推进的具体事项和安排，并据实记录结果。",
}


def _render_analysis(
    visit: VisitDraftInput,
    facts: Q34SemanticFacts,
    fallback: str,
) -> str:
    audit = facts.quality_audit or {}
    goal_reviews = [item for item in audit.get("goal_reviews", []) if isinstance(item, dict)]
    status = str(audit.get("achievement_status") or facts.purpose_achievement.value)
    outcomes = [item for item in audit.get("actual_outcomes", []) if isinstance(item, dict)]

    progress = ""
    if goal_reviews:
        progress = _businessize_goal_review(
            str(goal_reviews[0].get("reason") or ""),
            status=status,
            goal=str(visit.expected_key_result or "").strip(),
            outcomes=outcomes,
        )
    if not progress:
        progress = _businessize_existing_analysis(fallback, status=status)
    if not progress and outcomes:
        progress = "本次已经取得以下明确进展：" + _clean_outcome_quote(
            str(outcomes[0].get("quote") or "")
        ) + "。"

    gaps = _important_gap_sentences(visit, facts)
    parts = _deduplicate_sentences([progress, *gaps])
    return "\n\n".join(part for part in parts if part).strip()


def _businessize_goal_review(
    reason: str,
    *,
    status: str,
    goal: str,
    outcomes: list[dict[str, Any]],
) -> str:
    text = _normalize_sentence(reason)
    if not text:
        return ""
    broad = bool(re.search(r"(?:过于|比较)?宽泛|含糊|无法(?:核验|评估|判断)", text))
    if broad:
        outcome = next(
            (_clean_outcome_quote(str(item.get("quote") or "")) for item in outcomes),
            "",
        )
        pieces = []
        if outcome:
            pieces.append(f"本次已经记录了具体业务进展：{outcome}。")
        if goal:
            pieces.append(
                f"不过“{goal}”这个目标本身比较宽，现有记录不足以判断是否完成了原计划的全部内容。"
            )
        else:
            pieces.append("现有目标描述比较宽，当前记录不足以判断是否完成了原计划的全部内容。")
        return "".join(pieces)

    # Prefer the factual process/result portion over a mechanical goal opening.
    body = re.sub(
        r"^(?:原目标|原定目标|原定关键结果|原目标要求|原目标包含)(?:为|是|要求)?[^，。]{0,160}[，,。]",
        "",
        text,
        count=1,
    )
    body = re.sub(r"^过程(?:描述)?(?:中)?(?:显示|记录)?", "", body)
    body = re.sub(r"^(?:显示|记录显示)", "", body)
    body = re.sub(r"[，,](?:故|因此)?(?:判定|实际)?(?:为)?(?:部分达成|部分达到)[。]?$", "", body)
    body = re.sub(r"[，,](?:目标|本次核心目标)(?:已)?达成[。]?$", "", body)
    body = re.sub(r"[，,](?:目标|本次核心目标)(?:未)?达成[。]?$", "", body)
    body = _normalize_sentence(body)

    if status in {"partially_achieved", "partially_supported"}:
        if "但" in body:
            achieved, pending = body.split("但", 1)
            achieved = achieved.rstrip("，,。； ")
            pending = pending.rstrip("，,。； ")
            return f"本次{achieved}。目前{pending}，因此当前更接近部分完成。"
        return f"本次{body}。当前已经取得阶段性进展，但原定事项尚未全部闭环。"
    if status in {"achieved", "supported"}:
        return f"本次{body}。本次核心目标已经实现。"
    if status in {"not_achieved", "unsupported"}:
        if outcomes:
            outcome = _clean_outcome_quote(str(outcomes[0].get("quote") or ""))
            return (
                "本次还没有完成原定的最终结果，"
                + (f"但已经取得“{outcome}”这一阶段信息，为下一步继续推进提供了基础。" if outcome else
                   "现有记录体现了阶段信息，可作为下一步继续推进的基础。")
            )
        return "本次尚未完成原定的最终结果，当前记录中还没有形成足以支持完成结论的客户事实。"
    return text


def _businessize_existing_analysis(text: str, *, status: str) -> str:
    clauses = []
    for clause in re.split(r"(?<=[。！？])|[；;]", str(text or "")):
        value = _normalize_sentence(clause)
        if not value or _PASS_ONLY.match(value):
            continue
        if re.search(r"客户分类为|属于.+允许的目的|预约与方式|类型与目的匹配", value):
            continue
        value = re.sub(r"^(?:原目标为|原定目标为|原目标要求|原定关键结果为)", "本次需要", value)
        value = re.sub(r"^原目标", "当前目标", value)
        value = value.replace("下一步逻辑不成立", "下一步安排尚未形成清楚的业务闭环")
        value = value.replace("下一步逻辑不完整", "下一步安排还不够明确")
        value = re.sub(r"潜力客户需跨自然季度|目标客户需跨自然月", "", value)
        value = re.sub(r"(?:目标已达成|判定达到目的)", "本次核心目标已经实现", value)
        clauses.append(value)
        if len(clauses) == 3:
            break
    result = "".join(_ensure_sentence(item) for item in clauses)
    if status == "unresolved":
        result = result.replace("目标未达成", "现有记录不足以判断目标是否完成")
    return result


def _important_gap_sentences(visit: VisitDraftInput, facts: Q34SemanticFacts) -> list[str]:
    sections = {section.code: section for section in facts.sections}
    audit = facts.quality_audit or {}
    bases = audit.get("advice_basis", {}) if isinstance(audit.get("advice_basis"), dict) else {}
    results = []

    next_section = sections.get("N")
    next_basis = bases.get("N", {}) if isinstance(bases.get("N"), dict) else {}
    next_fields = set(next_basis.get("fields") or [])
    next_value = str(visit.next_action_expected_result or "").strip()
    if (
        next_section is not None
        and next_section.verdict == "needs_revision"
        and ("next_action_expected_result" in next_fields or _PLACEHOLDER.fullmatch(next_value))
    ):
        if next_value:
            results.append(
                f"当前下一步已经有推进方向，但“{next_value}”还不能体现下一次希望推动客户确认、提供、决定或完成什么。"
            )
        else:
            results.append("当前还没有形成可观察、可确认的下一步客户结果。")

    policy = _contact_policy(facts)
    if policy.get("date_state") == "missing" or (
        not policy and getattr(visit, "next_contact_at", None) is None
    ):
        results.append("当前还没有明确下一次联系时间。")
    elif policy.get("after_visit") is False:
        results.append("当前下一次联系时间不晚于本次拜访日期，现有安排还不能作为后续可执行时间。")

    if (
        next_section is not None
        and next_section.verdict == "needs_revision"
        and policy.get("customer_consensus_required")
        and facts.customer_consensus_met is False
    ):
        results.append(_consensus_analysis(_source_text(facts)))

    assessment = sections.get("A2")
    assessment_basis = bases.get("A2", {}) if isinstance(bases.get("A2"), dict) else {}
    if assessment is not None and assessment.verdict == "needs_revision" and assessment_basis:
        results.append("当前自评与本次已经取得的实际结果存在差异，需要依据真实进展重新核对。")
    return results


def _render_advice(
    visit: VisitDraftInput,
    facts: Q34SemanticFacts,
    advice_items: list[str],
) -> list[str]:
    audit = facts.quality_audit or {}
    bases = audit.get("advice_basis", {}) if isinstance(audit.get("advice_basis"), dict) else {}
    rendered = []
    date_gap = False

    for section in facts.sections:
        if section.verdict != "needs_revision" or not section.suggestion.strip():
            continue
        basis = bases.get(section.code, {}) if isinstance(bases.get(section.code), dict) else {}
        fields = set(basis.get("fields") or [])
        suggestion = _businessize_advice(section.suggestion)
        if section.code == "N" and "next_contact_at" in fields:
            date_gap = True
            suggestion = _remove_contact_time_clause(suggestion)
        if section.code == "N" and facts.customer_consensus_met is False:
            suggestion = _remove_consensus_judgement(suggestion)
            consensus_advice = _consensus_advice(_source_text(facts))
            if consensus_advice:
                rendered.append(consensus_advice)
        if suggestion:
            rendered.append(suggestion)

    # Keep validated knowledge/rule advice only when it was not represented by
    # a model section.  This preserves the current fallback path without adding
    # new interpretation.
    if not rendered:
        rendered.extend(_businessize_advice(item) for item in advice_items if item.strip())

    policy = _contact_policy(facts)
    date_gap = date_gap or policy.get("date_state") == "missing" or policy.get("after_visit") is False
    if date_gap or policy.get("period_met") is False:
        contact = _contact_advice(visit, facts, policy)
        if contact:
            rendered.append(contact)

    return _deduplicate_sentences([item for item in rendered if item])


def _businessize_advice(text: str) -> str:
    value = _normalize_sentence(text)
    value = re.sub(r"^(?:建议|请)将", "建议", value)
    value = re.sub(r"^(?:请|建议)补充", "建议补充", value)
    value = value.replace("下一步逻辑不成立", "下一步安排与本次尚未解决的问题承接还不够明确")
    value = value.replace("下一步逻辑不完整", "下一步安排还不够明确")
    value = value.replace("客户共识不足", "双方尚未就下一步安排形成明确约定")
    value = re.sub(r"(?:以便|从而)?符合(?:潜力客户|目标客户|商机客户)[^，。；\n]{0,18}(?:标准|要求)", "", value)
    # Remove the complete generated cadence clause. This avoids leaving a
    # sentence fragment such as “以。” after the hard requirement is removed.
    clauses = []
    for clause in re.split(r"(?<=[。；;])", value):
        if re.search(
            r"(?:联系|日期|时间)[^。；\n]{0,36}跨(?:北京时间)?(?:自然)?(?:月|季度)",
            clause,
        ):
            continue
        clauses.append(clause)
    value = "".join(clauses)
    value = _HARD_CADENCE.sub("", value)
    value = re.sub(r"[，,；;]\s*[，,；;]", "，", value)
    return _ensure_sentence(value.strip("，,；;。 ")) if value.strip("，,；;。 ") else ""


def _remove_contact_time_clause(text: str) -> str:
    kept = []
    for clause in re.split(r"(?<=[。；;])|(?=同时|并填写|且填写)", text):
        if re.search(r"下一次联系|联系客户时间|联系日期|跨自然(?:月|季度)", clause):
            continue
        clean = clause.strip("，,；;。 ")
        if clean:
            kept.append(_ensure_sentence(clean))
    return "".join(kept)


def _remove_consensus_judgement(text: str) -> str:
    return re.sub(r"[^。；\n]*(?:客户共识不足|没有共识|共识未满足)[^。；\n]*[。；]?", "", text).strip()


def _contact_advice(
    visit: VisitDraftInput,
    facts: Q34SemanticFacts,
    policy: dict[str, Any],
) -> str:
    source = _source_text(facts)
    confirmed = _customer_confirmed_schedule(source)
    customer_type = str(policy.get("customer_type") or "")

    if policy.get("date_state") == "missing":
        base = "当前还没有明确下一次联系时间，建议结合客户实际安排和下一步推进事项确定一个可执行的时间点。"
        if confirmed:
            base = "记录中已经有客户确认的后续时间和事项，建议将双方真实约定填写到下一次联系时间安排中，并按约推进。"
        return base + _cadence_reference(customer_type)
    if policy.get("after_visit") is False:
        return "当前下一次联系时间不晚于本次拜访日期，建议结合实际客户安排重新确认一个后续可执行的联系时间。"
    if policy.get("period_met") is False:
        if confirmed:
            return ""  # A real customer appointment outranks a generic cadence reference.
        return (
            "当前已经安排了下一次联系时间。如果该时间尚未与客户确认，可以进一步核实客户是否方便，"
            "并根据实际互动节奏调整。" + _cadence_reference(customer_type)
        )
    return ""


def _cadence_reference(customer_type: str) -> str:
    if customer_type == "potential":
        return "后续制定持续维护计划时，可以参考潜力客户适当拉开联系周期的管理建议。"
    if customer_type == "target":
        return "后续制定持续跟进计划时，可以参考目标客户按月规划联系节奏的管理建议。"
    return ""


def _consensus_analysis(source: str) -> str:
    if _CONDITIONAL.search(source) and re.search(r"客户|对方", source):
        return "客户已经表达了有条件的推进意向，相关条件是否满足仍需确认。"
    if re.search(r"客户|对方", source):
        return "客户已经对相关事项作出回应，但现有记录还不足以确认双方已经就下一步安排形成明确约定。"
    return "当前记录主要体现了销售侧计划，本次拜访中还没有明确看到客户对下一步安排的回应。"


def _consensus_advice(source: str) -> str:
    if _CONDITIONAL.search(source) and re.search(r"客户|对方", source):
        return "建议先确认客户提出的相关条件是否满足，再落实双方下一步的具体安排。"
    if re.search(r"客户|对方", source):
        return "建议围绕本次客户回应，进一步确认双方下一步要推进的事项和安排。"
    return "建议在下一次互动中确认客户希望继续推进的具体事项，而不只保留销售侧计划。"


def _customer_confirmed_schedule(source: str) -> bool:
    return bool(_CONFIRMED.search(source) and _TIME_FACT.search(source) and _ACTION_FACT.search(source))


def _contact_policy(facts: Q34SemanticFacts) -> dict[str, Any]:
    checks = (facts.quality_audit or {}).get("authoritative_checks", {})
    policy = checks.get("next_contact_policy", {}) if isinstance(checks, dict) else {}
    return policy if isinstance(policy, dict) else {}


def _source_text(facts: Q34SemanticFacts) -> str:
    checks = (facts.quality_audit or {}).get("authoritative_checks", {})
    return str(checks.get("source_text") or "") if isinstance(checks, dict) else ""


def _clean_outcome_quote(text: str) -> str:
    value = re.sub(r"^【[^】]{1,80}】", "", str(text or "").strip())
    return value.strip("，,；;。 ")


def _safe_outcome(values: Any) -> str:
    if not isinstance(values, list):
        return ""
    for item in values:
        if not isinstance(item, dict):
            continue
        quote = _clean_outcome_quote(str(item.get("quote") or ""))
        if quote and not _INTERNAL_OR_JUDGING.search(quote):
            return quote
    return ""


def _normalize_sentence(text: str) -> str:
    value = re.sub(r"\s+", "", str(text or ""))
    value = value.replace("符合潜力客户标准", "").replace("符合目标客户标准", "")
    value = re.sub(r"潜力客户需跨自然季度|目标客户需跨自然月", "", value)
    value = _INTERNAL_OR_JUDGING.sub("", value)
    value = re.sub(r"[，,；;]\s*[，,；;]", "，", value)
    return value.strip("，,；;。 ")


def _ensure_sentence(text: str) -> str:
    value = str(text or "").strip()
    return value if not value or value.endswith(("。", "！", "？", "!", "?")) else value + "。"


def _deduplicate_sentences(items: list[str]) -> list[str]:
    result = []
    normalized = []
    for item in items:
        value = str(item or "").strip()
        if not value:
            continue
        key = re.sub(r"[，。；：、\s]", "", value)
        if any(key in old or old in key for old in normalized if min(len(key), len(old)) >= 12):
            continue
        result.append(value)
        normalized.append(key)
    return result


def _assert_safe(analysis: str, advice: list[str]) -> None:
    visible = "\n".join([analysis, *advice])
    hit = _INTERNAL_OR_JUDGING.search(visible)
    if hit:
        raise ValueError(f"backend_salesperson_wording_v2_internal_leak:{hit.group(0)}")
    hard = _HARD_CADENCE.search(visible)
    if hard:
        raise ValueError(f"backend_salesperson_wording_v2_hard_cadence:{hard.group(0)}")
