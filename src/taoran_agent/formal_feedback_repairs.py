"""Deterministic, evidence-bound repairs for formal feedback wording.

These repairs only replace a rejected sentence.  They never alter model facts,
section verdicts, evidence, goal reviews, or scoring inputs.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from .deep_review_gates import actual_outcome_evidence

_SOFT_ADVICE = {
    "T": "可以结合当前业务阶段和实际拜访目的，核对记录是否一致。",
    "A1": "可以结合实际情况，核对预约和拜访方式的记录是否完整。",
    "O_KR": "下一步可以结合实际业务，明确希望进一步了解或确认的具体事项，并据实记录结果。",
    "R": "可以据实记录本次实际沟通事实和客户实际回应。",
    "A2": "可以根据当前记录中的实际进展，核对自评是否一致。",
    "N": "下一步可以结合实际沟通，继续确认客户希望推进的具体事项和安排，并据实记录结果。",
}


def repair_feedback_candidate(
    payload: dict[str, Any],
    failure_reason: str,
    details: dict[str, Any],
    data: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Return one scoped wording repair for a known post-feedback violation.

    The caller must run every existing validator again before accepting the
    result.  ``None`` deliberately leaves unfamiliar failures to the existing
    failure path.
    """
    supported = {
        "post_requirement_provenance_conflict",
        "post_advice_truthfulness_conflict",
        "post_achievement_boundary_conflict",
        "post_outcome_preservation_conflict",
    }
    if failure_reason not in supported or not isinstance(payload, dict):
        return None
    sections = payload.get("sections")
    facts = payload.get("facts")
    if not isinstance(sections, list) or not isinstance(facts, dict):
        return None

    targets = {
        str(hit.get("target"))
        for hit in details.get("hits", [])
        if isinstance(hit, dict) and hit.get("target")
    }
    if not targets:
        return None

    repaired = deepcopy(payload)
    changed: list[str] = []
    if failure_reason in {
        "post_requirement_provenance_conflict",
        "post_advice_truthfulness_conflict",
    }:
        for section in repaired["sections"]:
            if not isinstance(section, dict) or section.get("code") not in targets:
                continue
            code = str(section["code"])
            if code not in _SOFT_ADVICE:
                return None
            section["suggestion"] = _SOFT_ADVICE[code]
            changed.append(code)
    elif failure_reason == "post_achievement_boundary_conflict":
        if "facts.reason" in targets:
            repaired["facts"]["reason"] = _unresolved_summary(data)
            changed.append("facts.reason")
        for section in repaired["sections"]:
            if not isinstance(section, dict) or section.get("code") not in targets:
                continue
            section["reason"] = "当前目标描述较宽，现有记录不足以可靠判断是否已经完整达成。"
            section["suggestion"] = "可以根据当前记录中的实际进展，核对自评是否一致。"
            changed.append(str(section["code"]))
    else:  # post_outcome_preservation_conflict
        if "facts.reason" not in targets:
            return None
        repaired["facts"]["reason"] = _outcome_preserving_summary(data)
        changed.append("facts.reason")

    if not changed:
        return None
    return repaired, {
        "stage": "targeted_repair",
        "repair_mode": "deterministic",
        "violation_code": failure_reason,
        "targets": changed,
    }


def _unresolved_summary(data: dict[str, Any]) -> str:
    outcomes = actual_outcome_evidence(data)
    if outcomes:
        return (
            "本次已经记录了实际业务进展："
            + outcomes[0]["quote"]
            + "。当前目标描述较宽，现有记录不足以可靠判断是否已经完整达成。"
        )
    return "当前目标描述较宽，现有记录不足以可靠判断是否已经完整达成。"


def _outcome_preserving_summary(data: dict[str, Any]) -> str:
    outcomes = actual_outcome_evidence(data)
    if outcomes:
        return "本次已经记录了实际业务进展：" + outcomes[0]["quote"] + "。后续可以结合实际情况继续推进。"
    return "当前记录体现了本次实际沟通情况，后续可以结合实际情况继续推进。"
