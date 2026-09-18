"""High-confidence cross-module checks for validated front-end wording."""
from __future__ import annotations

import re
from typing import Any

VERSION = "front-joint-consistency-v2-20260918"

_INTERNAL = re.compile(
    r"(?:period_met|customer_consensus_met|next_action_logic_ok|authoritative_checks|"
    r"advice_basis|N整体|[A-Z]\d?_(?:met|ok)|程序判定|时间门槛|共识豁免)"
)
_CHANGE = r"(?:建议|请|需要|应当|必须|需)(?:再|进一步)?(?:补充|修改|调整|明确|细化|核实)"


def errors(analysis: str, advice: str, ledger: dict[str, Any]) -> list[str]:
    """Return only contradictions that are safe to enforce without a model."""
    analysis = str(analysis or "")
    advice = str(advice or "")
    result: list[str] = []
    if _INTERNAL.search(analysis + "\n" + advice):
        result.append("internal_rule_leak")
    if re.search(r"目标(?:已|已经)?(?:取得明确结果|达成|完成)", analysis) and re.search(
        _CHANGE + r"[^\n。；]{0,24}(?:原定目标|想取得的关键结果|关键结果)", advice,
    ):
        result.append("goal_achievement_conflict")
    if re.search(r"(?:过程|客户)事实(?:依据)?(?:充分|清楚|完整)", analysis) and re.search(
        _CHANGE + r"[^\n。；]{0,24}(?:过程事实|客户事实|过程详细描述)", advice,
    ):
        result.append("process_fact_conflict")
    if re.search(r"自评[^\n。；]{0,16}(?:一致|相符|符合)", analysis) and re.search(
        _CHANGE + r"[^\n。；]{0,20}(?:自评|评价)", advice,
    ):
        result.append("self_assessment_conflict")
    if not str(ledger.get("validated_analysis") or "").strip():
        result.append("validated_analysis_missing")
    return list(dict.fromkeys(result))


def repair_advice(analysis: str, advice: str, error_codes: list[str]) -> tuple[str, list[str]]:
    """Remove only high-confidence conflicting advice clauses locally.

    The validated analysis is immutable.  This helper deliberately handles
    only the same narrow contradictions detected by :func:`errors`; anything
    uncertain remains for the bounded advice-only model repair.
    """
    if not advice or not error_codes:
        return advice, []

    predicates = []
    requested = set(error_codes)
    if "internal_rule_leak" in requested:
        predicates.append(("internal_rule_leak", lambda text: bool(_INTERNAL.search(text))))
    if "goal_achievement_conflict" in requested:
        predicates.append(("goal_achievement_conflict", lambda text: bool(
            re.search(_CHANGE + r"[^\n。；]{0,24}(?:原定目标|想取得的关键结果|关键结果)", text)
        )))
    if "process_fact_conflict" in requested:
        predicates.append(("process_fact_conflict", lambda text: bool(
            re.search(_CHANGE + r"[^\n。；]{0,24}(?:过程事实|客户事实|过程详细描述)", text)
        )))
    if "self_assessment_conflict" in requested:
        predicates.append(("self_assessment_conflict", lambda text: bool(
            re.search(_CHANGE + r"[^\n。；]{0,20}(?:自评|评价)", text)
        )))
    if not predicates:
        return advice, []

    # Keep punctuation with each clause so removing one conflict does not
    # rewrite or reorder unrelated suggestions.
    clauses = re.split(r"(?<=[。！？；])|(?=\n)", advice)
    kept: list[str] = []
    applied: list[str] = []
    for clause in clauses:
        matched = [code for code, predicate in predicates if predicate(clause)]
        if matched:
            applied.extend(matched)
            continue
        kept.append(clause)
    repaired = "".join(kept)
    repaired = re.sub(r"\n{3,}", "\n\n", repaired).strip()
    # Renumber only line-leading numbered suggestions after a clause removal.
    counter = 0
    lines = []
    for line in repaired.splitlines():
        if re.match(r"^\s*\d+[、．.]", line):
            counter += 1
            line = re.sub(r"^\s*\d+([、．.])", rf"{counter}\1", line)
        lines.append(line)
    return "\n".join(lines).strip(), list(dict.fromkeys(applied))
