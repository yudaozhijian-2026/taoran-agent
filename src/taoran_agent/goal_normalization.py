"""Deterministic boundary for the recorded key-result goal.

This module classifies only the *field value* supplied as
``expected_key_result``.  It deliberately never reads visit purpose, process
facts, feedback, or next actions, so none of those values can become a
substitute original goal.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Literal

# Keep the existing program's historical placeholder behavior (numeric-only
# values and punctuation-only placeholders), plus the explicit dash variants
# used by the form.  Matching is against the entire normalized field only.
_SYMBOLIC_PLACEHOLDER = re.compile(r"(?:\d+|[.\-_/]+)\Z")
_EXACT_PLACEHOLDERS = frozenset({"测试", "无", "待定", "待填", "暂无", "不详", "—", "n/a"})

# These are intentionally conservative whole-field values.  The existing
# semantic-state classifier already calls many other values vague; this list
# covers the known broad goal forms that can otherwise look action-like.
_BROAD_GOALS = frozenset({
    "项目顺利实施", "收集信息", "保持关系", "推进项目", "维护关系", "加强关系", "客情维护", "跟进",
})


@dataclass(frozen=True)
class GoalNormalization:
    goal_raw: str | None
    goal_normalized: str | None
    goal_state: Literal["specific", "broad", "missing_placeholder"]
    goal_assessable: bool
    goal_source: Literal["key_result"] = "key_result"
    achievement: Literal["unassessed", "unresolved"] = "unassessed"
    legacy_status: Literal["missing", "placeholder", "provided"] = "provided"

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _normalized_text(value: object) -> str | None:
    if value is None:
        return None
    return unicodedata.normalize("NFKC", str(value)).strip()


def normalize_expected_key_result(value: object) -> GoalNormalization:
    """Classify the one permitted source for goal-achievement assessment."""
    raw = None if value is None else str(value)
    normalized = _normalized_text(value)
    if not normalized:
        return GoalNormalization(
            goal_raw=raw,
            goal_normalized=None,
            goal_state="missing_placeholder",
            goal_assessable=False,
            achievement="unresolved",
            legacy_status="missing",
        )
    if normalized.casefold() in _EXACT_PLACEHOLDERS or _SYMBOLIC_PLACEHOLDER.fullmatch(normalized):
        return GoalNormalization(
            goal_raw=raw,
            goal_normalized=None,
            goal_state="missing_placeholder",
            goal_assessable=False,
            achievement="unresolved",
            legacy_status="placeholder",
        )
    if normalized in _BROAD_GOALS:
        return GoalNormalization(
            goal_raw=raw,
            goal_normalized=normalized,
            goal_state="broad",
            goal_assessable=False,
            achievement="unresolved",
        )
    return GoalNormalization(
        goal_raw=raw,
        goal_normalized=normalized,
        goal_state="specific",
        goal_assessable=True,
        achievement="unassessed",
    )
