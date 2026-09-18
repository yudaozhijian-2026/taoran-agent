"""High-confidence cross-module checks for validated front-end wording."""
from __future__ import annotations

import re
from typing import Any

VERSION = "front-joint-consistency-v1-20260918"

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
