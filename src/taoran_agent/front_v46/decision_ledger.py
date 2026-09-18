"""Shared deterministic judgement ledger for the isolated submit flow.

The ledger never scores a visit and never calls a model.  It records only
high-confidence field facts that both front-end wording stages must share.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from ..models import VisitDraftInput
from ..record_contract import visit_contract

VERSION = "front-decision-ledger-v1-20260918"

_FIELD_CODES = {
    "customer_type_ii": "T",
    "visit_method": "A1",
    "is_appointment": "A1",
    "purpose_code": "O_KR",
    "other_purpose": "O_KR",
    "expected_key_result": "O_KR",
    "process_description": "R",
    "self_assessment": "A2",
    "next_action_purpose": "N",
    "next_action_other_purpose": "N",
    "next_action_expected_result": "N",
    "next_contact_at": "N",
}

_OBVIOUS_VAGUE = {
    "了解一下", "沟通一下", "保持联系", "保持关系", "项目顺利实施",
    "推进项目", "后续跟进", "继续跟进", "收集信息", "了解需求",
}


def _compact(value: Any) -> str:
    return re.sub(r"[\s\u3000，,。；;：:]", "", str(value or "")).strip()


def _value(raw: dict[str, Any], field: str) -> Any:
    value = raw.get(field)
    return value.value if hasattr(value, "value") else value


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            return None
    return None


def build(visit: VisitDraftInput) -> dict[str, Any]:
    """Build the model-independent facts shared by analysis and advice."""
    raw = visit.model_dump(mode="python")
    contract = visit_contract(visit)
    presence = contract.get("presence", {})
    states: dict[str, dict[str, Any]] = {}
    required: dict[tuple[str, str], dict[str, str]] = {}

    def state(field: str, status: str, reason: str) -> None:
        states[field] = {"status": status, "reason": reason}

    def require(field: str, reason: str) -> None:
        code = _FIELD_CODES[field]
        required[(code, field)] = {"code": code, "field": field, "reason": reason}
        state(field, "needs_revision", reason)

    for field in _FIELD_CODES:
        current = presence.get(field)
        if current == "not_received":
            state(field, "not_received", "interface_not_received")
        elif current == "empty":
            state(field, "empty", "not_filled")
        else:
            state(field, "recorded", "value_received")

    for field in (
        "customer_type_ii", "visit_method", "purpose_code", "expected_key_result",
        "process_description", "self_assessment", "next_action_purpose",
        "next_action_expected_result", "next_contact_at",
    ):
        if presence.get(field) == "empty":
            require(field, "not_filled")

    purpose = str(_value(raw, "purpose_code") or "").strip()
    if purpose == "其他目的" and presence.get("other_purpose") == "empty":
        require("other_purpose", "other_purpose_detail_missing")
    next_purpose = str(_value(raw, "next_action_purpose") or "").strip()
    if next_purpose == "其他目的" and presence.get("next_action_other_purpose") == "empty":
        require("next_action_other_purpose", "other_purpose_detail_missing")

    for field in ("expected_key_result", "next_action_expected_result"):
        if presence.get(field) == "present" and _compact(_value(raw, field)) in _OBVIOUS_VAGUE:
            require(field, "obviously_not_specific")

    visit_day = _as_date(_value(raw, "visit_date"))
    contact_day = _as_date(_value(raw, "next_contact_at"))
    customer_type = str(_value(raw, "customer_type_ii") or "")
    contact_standard = "customer_consensus"
    if customer_type in {"target", "目标客户"}:
        contact_standard = "different_calendar_month"
    elif customer_type in {"potential", "潜力客户"}:
        contact_standard = "different_calendar_quarter"
    if contact_day is not None and visit_day is not None:
        invalid = contact_day <= visit_day
        if contact_standard == "different_calendar_month":
            invalid = invalid or (contact_day.year, contact_day.month) == (
                visit_day.year, visit_day.month,
            )
        elif contact_standard == "different_calendar_quarter":
            invalid = invalid or (contact_day.year, (contact_day.month - 1) // 3) == (
                visit_day.year, (visit_day.month - 1) // 3,
            )
        if invalid:
            require("next_contact_at", "customer_type_date_standard_not_met")

    return {
        "version": VERSION,
        "field_states": states,
        "required_advice": list(required.values()),
        "contact_time_standard": contact_standard,
        "rules": {
            "analysis_must_not_contain_advice": True,
            "advice_only_for_actual_gaps": True,
            "validated_analysis_is_fixed": True,
        },
    }


def with_validated_analysis(
    ledger: dict[str, Any], analysis: str, *, repair_errors: list[str] | None = None,
) -> dict[str, Any]:
    result = {**ledger, "validated_analysis": str(analysis or "").strip()}
    if repair_errors:
        result["joint_repair_errors"] = list(dict.fromkeys(repair_errors))[:8]
    return result
