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

VERSION = "front-decision-ledger-v3-20260921"

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


def deterministic_advice(
    visit_snapshot: dict[str, Any], ledger: dict[str, Any] | None,
) -> list[dict[str, str]]:
    """Render only high-confidence gaps locally.

    The model still handles semantic judgement.  These short suggestions cover
    facts already fixed by the decision ledger, so the second model stage does
    not spend tokens restating an empty field or an obvious placeholder.
    """
    if not isinstance(ledger, dict):
        return []
    advice: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    customer_type = str(visit_snapshot.get("customer_type_ii") or "")
    standard = str(ledger.get("contact_time_standard") or "")

    for item in ledger.get("required_advice", []):
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "")
        field = str(item.get("field") or "")
        reason = str(item.get("reason") or "")
        if not code or not field or (code, field) in seen:
            continue
        text = ""
        value = str(visit_snapshot.get(field) or "").strip()
        if field == "expected_key_result":
            if reason == "not_filled":
                text = "请补充想取得的关键结果，写明本次希望客户确认、提供或完成的具体事项。"
            elif reason in {"obviously_not_specific", "not_specific"}:
                text = (
                    f"想取得的关键结果“{value}”较笼统，请写明希望客户本次确认、提供或完成什么。"
                    if value else "请写明希望客户本次确认、提供或完成的具体事项。"
                )
        elif field == "next_action_expected_result":
            if reason == "not_filled":
                text = "请补充下次拜访期望的关键结果，写明希望客户下一步确认、提供或完成的具体事项。"
            elif reason in {"obviously_not_specific", "not_specific"}:
                text = (
                    f"下次拜访期望的关键结果“{value}”较笼统，请结合本次事实写明客户下一步将确认、提供或完成什么。"
                    if value else "请结合本次事实，写明客户下一步将确认、提供或完成的具体事项。"
                )
        elif field == "next_contact_at":
            if reason == "not_filled":
                if standard == "different_calendar_month":
                    text = "请补充下一次联系客户的具体日期，并按目标客户要求安排在不同自然月。"
                elif standard == "different_calendar_quarter":
                    text = "请补充下一次联系客户的具体日期，并按潜力客户要求安排在不同自然季度。"
                else:
                    text = "请补充下一次联系客户的具体日期；商机客户建议与客户达成下一次拜访时间共识。"
            elif reason == "customer_type_date_standard_not_met":
                if standard == "different_calendar_month":
                    text = "当前联系日期不符合目标客户的时间安排，请调整到不同自然月。"
                elif standard == "different_calendar_quarter":
                    text = "当前联系日期不符合潜力客户的时间安排，请调整到不同自然季度。"
                elif "商机" in customer_type or customer_type == "opportunity":
                    text = "请结合本次拜访事实，确认当前联系日期是否已与客户形成下一步共识。"
        elif field in {"other_purpose", "next_action_other_purpose"}:
            label = "具体其他目的" if field == "other_purpose" else "下一步具体其他目的"
            text = f"已选择其他目的，请补充{label}，说明本次要解决或推进的具体事项。"
        if text:
            seen.add((code, field))
            advice.append({
                "code": code,
                "field": field,
                "reason": reason,
                "text": text,
            })
    return advice[:6]
