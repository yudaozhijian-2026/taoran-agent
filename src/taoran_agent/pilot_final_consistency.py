"""Shared, read-only consistency helpers for pilot-facing AI wording.

The helpers in this module only classify facts already present in a visit
record.  They do not alter Q33/Q34 scoring, semantic facts, source evidence,
or any persisted artifact.  Front Quick Check and the post-submit wording
projection use the same classifications so that their user-visible wording
cannot disagree about a customer response, a contact plan, or goal status.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .goal_normalization import normalize_expected_key_result

_SENTENCE_SPLIT = re.compile(r"[。！？；;\n]+")
_CONTACT_ACTION = re.compile(r"(?:联系|沟通|拜访|回访|见面|讨论|约定|约好|商定)")
_EXPLICIT_DATE = re.compile(
    r"(?:20\d{2}[年/-])?\d{1,2}月\d{1,2}[日号]?|"
    r"20\d{2}[-/]\d{1,2}[-/]\d{1,2}"
)
_RELATIVE_TIME = re.compile(
    r"(?:下(?:周|月|季度)[一二三四五六日天]?|本周[一二三四五六日天]?|"
    r"(?:一|两|三|四|五|六|七|\d+)(?:天|周)后|明天|后天|届时|随后)"
)
_EVENT_TRIGGER = re.compile(r"(?:完成后|交付后|到货后|安装后|验收后|审批后|资料(?:准备|确认)后)")
_NO_RESPONSE = re.compile(
    r"(?:客户|对方|老师|负责人)[^。；\n]{0,16}(?:暂未|尚未|未|没有|暂无)[^。；\n]{0,8}"
    r"(?:回复|回应|反馈|答复|表态|确认)|"
    r"(?:暂未|尚未|未|没有|暂无)[^。；\n]{0,12}(?:收到|取得)[^。；\n]{0,10}"
    r"(?:客户|对方)[^。；\n]{0,8}(?:回复|回应|反馈|答复|表态)"
)
_RESPONSE = re.compile(
    r"(?:客户|对方|老师|负责人)[^。；\n]{0,28}"
    r"(?:确认|同意|表示|反馈|回复|回应|承诺|提出|拒绝|要求|说明|决定|接受|不接受)"
)
_VISIBLE_CADENCE = re.compile(r"跨(?:北京时间)?(?:自然)?(?:月|季度)")
_UNRESOLVED_AS_FAILED = re.compile(r"(?:目标|关键结果|本次)[^。；\n]{0,12}(?:未达成|未达到|没有完成)")
_ALIGNED_AS_CONFLICT = re.compile(r"自评[^。；\n]{0,16}(?:存在差异|不一致|不相符|重新核对)")
_NO_CONTACT_CLAIM = re.compile(r"(?:未看到|没有|尚未)明确(?:的)?后续?(?:联系|沟通|拜访)?安排")


@dataclass(frozen=True)
class CustomerResponse:
    state: str
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class ContactPlan:
    state: str
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class GoalPresentation:
    quality: str
    achievement: str
    assessment_alignment: str


def source_text(raw: Mapping[str, Any]) -> str:
    """Return the two fact-bearing free-text fields without adding content."""
    return "\n".join(
        str(raw.get(field) or "").strip()
        for field in ("process_description", "customer_feedback")
        if str(raw.get(field) or "").strip()
    )


def sentences(value: Any) -> list[str]:
    return [
        item.strip(" ，,；;")
        for item in _SENTENCE_SPLIT.split(str(value or ""))
        if item.strip(" ，,；;")
    ]


def customer_response_state(value: Any) -> CustomerResponse:
    """Classify only an explicit customer response in supplied source text."""
    no_response = []
    response = []
    for sentence in sentences(value):
        if _NO_RESPONSE.search(sentence):
            no_response.append(sentence)
            first_clause = re.split(r"(?:但|但是|不过|；|;)", sentence, maxsplit=1)[0]
            if _RESPONSE.search(first_clause) and not _NO_RESPONSE.search(first_clause):
                response.append(first_clause)
            continue
        if _RESPONSE.search(sentence):
            response.append(sentence)
    if no_response and response:
        return CustomerResponse("partial_response", tuple(no_response + response))
    if no_response:
        return CustomerResponse("no_response", tuple(no_response))
    if response:
        return CustomerResponse("confirmed_response", tuple(response))
    return CustomerResponse("unclear", ())


def contact_plan_state(raw: Mapping[str, Any]) -> ContactPlan:
    """Identify an actual contact plan; a customer event alone is not one."""
    if raw.get("next_contact_at"):
        return ContactPlan("explicit_date", (str(raw["next_contact_at"]),))

    candidates: list[str] = []
    for field in (
        "process_description",
        "customer_feedback",
        "next_action_expected_result",
        "next_action_other_purpose",
    ):
        candidates.extend(sentences(raw.get(field)))

    explicit = [
        item for item in candidates
        if _CONTACT_ACTION.search(item) and _EXPLICIT_DATE.search(item)
    ]
    if explicit:
        return ContactPlan("explicit_date", tuple(explicit))
    event = [
        item for item in candidates
        if _CONTACT_ACTION.search(item) and _EVENT_TRIGGER.search(item)
    ]
    if event:
        return ContactPlan("event_trigger", tuple(event))
    relative = [
        item for item in candidates
        if _CONTACT_ACTION.search(item) and _RELATIVE_TIME.search(item)
    ]
    if relative:
        return ContactPlan("relative_time", tuple(relative))
    return ContactPlan("no_schedule", ())


def goal_presentation_state(
    goal: Any,
    achievement: Any,
    self_assessment: Any,
    *,
    goal_quality: str | None = None,
) -> GoalPresentation:
    """Keep goal quality, achievement, and self-assessment separate."""
    quality = goal_quality or normalize_expected_key_result(goal).goal_state
    if quality not in {"specific", "broad", "missing_placeholder"}:
        quality = "specific"
    normalized = str(achievement or "unresolved")
    if quality != "specific" or normalized not in {
        "achieved", "partially_achieved", "not_achieved", "unresolved",
    }:
        normalized = "unresolved"
    if normalized == "unresolved":
        alignment = "unresolved"
    elif str(self_assessment or "") == normalized:
        alignment = "aligned"
    elif self_assessment:
        alignment = "mismatch"
    else:
        alignment = "unclear"
    return GoalPresentation(quality, normalized, alignment)


def complete_sentences(value: Any, *, limit: int = 2) -> list[str]:
    """Pick complete source sentences; never cut a sentence by character count."""
    selected = []
    for sentence in sentences(value):
        if sentence.startswith(("不过", "但是", "然而")):
            sentence = sentence.removeprefix("不过").removeprefix("但是").removeprefix("然而").lstrip("，, ")
        if sentence:
            selected.append(sentence + "。")
        if len(selected) == limit:
            break
    return selected


def visible_consistency_errors(
    visible: str,
    *,
    response: CustomerResponse,
    contact: ContactPlan,
    goal: GoalPresentation,
) -> list[str]:
    """Return wording-only consistency violations for tests and safe fallback."""
    errors = []
    if response.state == "no_response" and "客户已经对相关事项作出回应" in visible:
        errors.append("no_response_became_responded")
    if goal.achievement == "unresolved" and _UNRESOLVED_AS_FAILED.search(visible):
        errors.append("unresolved_became_not_achieved")
    if goal.assessment_alignment == "aligned" and _ALIGNED_AS_CONFLICT.search(visible):
        errors.append("aligned_assessment_became_mismatch")
    if contact.state != "no_schedule" and _NO_CONTACT_CLAIM.search(visible):
        errors.append("contact_plan_became_no_schedule")
    if _VISIBLE_CADENCE.search(visible):
        errors.append("hard_cadence_visible")
    if any(
        sentence.lstrip().startswith(("不过", "但是", "然而"))
        for sentence in re.split(r"[\n。！？]", visible)
        if sentence.strip()
    ):
        errors.append("summary_starts_with_contrast")
    return errors


def protected_tokens(value: Any) -> set[str]:
    """Expose numbers, dates, amounts and product codes for regression checks."""
    text = str(value or "")
    patterns = (
        r"\d{4}[年/-]\d{1,2}[月/-]\d{1,2}[日号]?",
        r"\d+(?:\.\d+)?(?:万元|元|%|台|套|张|件|mm|毫米|天|周|月)",
        r"(?<![A-Z])[A-Z]{1,12}(?:[-_][A-Za-z0-9]+)+(?![A-Za-z0-9])",
        r"(?<![A-Z])[A-Z]{1,12}\d+(?![A-Za-z0-9])",
        r"[A-Za-z]{1,12}(?:[-_]?[A-Za-z0-9]+){1,4}",
    )
    return {match.group(0) for pattern in patterns for match in re.finditer(pattern, text)}
