"""Evidence-bound gates for post-submit salesperson-facing feedback.

These helpers never score a visit.  They reject or conservatively normalize
wording that is incomplete, invents a completed fact, or promotes a semantic
recommendation into a company requirement.
"""

from __future__ import annotations

import re
from typing import Any

from .semantic_roles import SemanticRole, semantic_scope, semantic_segments

_UNFINISHED_END = re.compile(
    r"(?:[，；：、(（]\s*|(?:因此|并且|同时|其中|例如|包括|需要|建议|因为)\s*)$"
)
_STRONG_REQUIREMENT = re.compile(r"(?:必须|应当|不允许|严禁|要求|须要|应调整为|应改为|只能|不得)")
_COMPLETED_CLAIM = re.compile(
    r"(?:已|已经)(?:接受|同意|承诺|确认|完成|取得|付款|支付|下单|采购|提供|交付|发货|收货|提交|"
    r"签约|安排|实施|回复|反馈|对接|形成|收到)"
    r"|(?:明确|确认).{0,8}(?:接受|同意|承诺|完成|付款|支付|下单|采购|提供|交付|发货|收货|"
    r"提交|签约|安排|实施|回复|反馈|对接|形成|收到)"
)
_ADVICE_ACTION = re.compile(r"(?:补充|补写|写明|完善|改写|补入|记录)")
_ADVICE_DIRECTIVE_PREFIX = re.compile(
    r"^(?:请|建议|需(?:要)?|应(?:当)?|必须|可以|可|务必)(?:据实|再|进一步|完整|详细)?"
)
_UNRESOLVED_AS_FAILED = re.compile(
    r"(?:目标|关键结果|原定事项)[^。；\n]{0,120}"
    r"(?:故|因此|所以)?(?:判定|判断|认定)?(?:为)?"
    r"(?:未达到|没有达到|未达成|没有达成|失败)"
    r"|(?:调整|改为|校准为).{0,10}(?:未达到|没有达到|未达成)"
)
_OUTCOME_DENIAL = re.compile(
    r"(?:没有|未|尚未)(?:取得|形成|获得).{0,12}(?:成果|结果|进展|信息)"
    r"|(?:本次|此次)拜访.{0,12}(?:没有|无)(?:成果|进展|价值)"
)
_SOURCE_SENTENCE = re.compile(r"[^。；\n]+")
_WEAK_INTENT = re.compile(r"(?:考虑|意向|可能|希望|倾向|(?<!模)拟(?:于|在|采购|付款|测试|提供))")
_FIRM_CUSTOMER_COMMITMENT = re.compile(r"客户[^，。；\n]{0,8}?(?:承诺|保证|同意|确认|约定)")
_FUTURE_DIRECTION = re.compile(
    r"(?:将|(?<!展)会|后续|下一步|下周|下次|本周|周[一二三四五六日天]|月底|月末|之后|再|待|拟|预计|一定)"
)
_SOURCE_CONDITIONAL_RESPONSE = re.compile(
    r"(?:客户|对方|他|她)[^。；\n]{0,24}?(?:可以|可|将|(?<!展)会|承诺|同意|答应|后续|下周|下次|待|预计|拟)"
)
_COMPLETED_ACTION = re.compile(
    r"(?:已经|已)(?:接受|确认|同意|付款|支付|下单|采购|提供|交付|发货|完成|提交|安排|实施|发送|回复|测试|沟通|反馈|对接|签署|配合)"
)
_ACTION = re.compile(
    r"(?:接受|确认|付款|支付|下单|采购|提供|交付|发货|收货|提交|完成|安排|实施|测试|回复|反馈|沟通|对接|通知|签署|配合)"
)
_ACTION_NOISE = re.compile(
    r"(?:将|会|后续|下一步|下周|下次|本周|月底|月末|之后|再|待|拟|预计|一定|已经|已|客户|承诺|保证|同意|确认|约定|明确|于|在)"
)
_FUTURE_OUTCOME = re.compile(r"(?:承诺|约定|计划|预计|拟于|将|会|后续|下一步|下周|下次|待)")
_COMPLETED_OUTCOME = re.compile(r"(?:已|已经|完成|取得|收到|发来|补发|签署|核对无遗漏)")
_EVENT_TIME = re.compile(
    r"(?:\d{4}[-年]\d{1,2}[-月]\d{1,2}(?:日|号)?|\d{1,2}月\d{1,2}(?:日|号)?|"
    r"下周[一二三四五六日天]|本周[一二三四五六日天]|周[一二三四五六日天]|下周|本周|"
    r"明天|今天|月底|月末)"
)
_EVENT_CONDITION = re.compile(
    r"(?:审批通过后|收到[^，,。；;]{0,16}后|(?:如|如果|若)[^，,。；;]{0,24}(?:后|再|才|则|考虑|推进))"
)


class FinalFeedbackIncomplete(ValueError):
    def __init__(self, issues: list[str]):
        super().__init__("FINAL_FEEDBACK_INCOMPLETE")
        self.issues = issues


def text_completeness_issues(text: str) -> list[str]:
    value = str(text or "").strip()
    if not value:
        return ["empty_text"]
    issues = []
    if _UNFINISHED_END.search(value):
        issues.append("unfinished_ending")
    # An opening bracket at the tail is another reliable truncation signal.
    if value.count("（") > value.count("）") or value.count("(") > value.count(")"):
        issues.append("unclosed_parenthesis")
    return issues


def final_feedback_issues(text: str, *, needs_advice: bool) -> list[str]:
    value = str(text or "").strip()
    issues = text_completeness_issues(value)
    if "本次拜访分析：" not in value:
        issues.append("analysis_module_missing")
    analysis = value.split("本次拜访分析：", 1)[-1]
    advice = ""
    if "AI改善建议：" in analysis:
        analysis, advice = analysis.split("AI改善建议：", 1)
    issues.extend(f"analysis_{item}" for item in text_completeness_issues(analysis))
    if needs_advice:
        if "AI改善建议：" not in value:
            issues.append("advice_module_missing")
        else:
            issues.extend(f"advice_{item}" for item in text_completeness_issues(advice))
    return list(dict.fromkeys(issues))


def require_complete_feedback(text: str, *, needs_advice: bool) -> str:
    issues = final_feedback_issues(text, needs_advice=needs_advice)
    if issues:
        raise FinalFeedbackIncomplete(issues)
    return text


def requirement_provenance(section: Any) -> str:
    basis = getattr(section, "advice_basis", None)
    gap_kind = getattr(basis, "gap_kind", None)
    if gap_kind == "policy_mismatch":
        return "deterministic_rule"
    if gap_kind in {"inconsistency", "fact_source_unclear"}:
        return "record_evidence"
    return "semantic_recommendation"


def advice_truthfulness_hits(text: str, source_text: str, target: str) -> list[dict[str, Any]]:
    """Reject only an instruction to add an unsupported completed fact.

    A completed fact can legitimately appear in a suggestion as a factual
    recap.  In particular, the noun ``记录`` in ``本次记录中`` must not be
    combined with a later ``已`` phrase.  Inspect directive fragments instead:
    the writing verb and requested completed claim must share the same local
    comma-bounded fragment.
    """
    hits = []
    source_completed = _completed_actions(source_text)
    source_future = _future_customer_commitments(source_text)
    source_future += _conditional_source_future_actions(source_text)
    for segment in semantic_segments(text):
        for fragment in re.finditer(r"[^、：:]+", segment.text):
            requested = fragment.group().strip()
            if not requested:
                continue
            directive = _completion_writing_directive(requested)
            if directive is None:
                continue
            requested_content = requested[directive.end() :]
            future = _future_customer_commitments(requested_content)
            claim = _COMPLETED_CLAIM.search(requested_content)
            unsupported_future = [
                event for event in future
                if not any(_same_future_event(event, source) for source in source_future)
            ]
            if unsupported_future:
                start = segment.start + fragment.start()
                hits.append({
                    "rule": (
                        "advice_requests_unproven_completed_fact"
                        if claim else "advice_requests_unproven_future_commitment"
                    ),
                    "target": target,
                    "quote": requested,
                    "start": start,
                    "end": start + len(fragment.group()),
                    "scanned_text": text,
                })
                continue
            if claim is None:
                continue
            if future:
                # "已承诺下周付款" confirms a future promise; it does not say
                # that the payment has already happened.
                continue
            role = semantic_scope(requested, directive.end() + claim.start(), directive.end() + claim.end())
            if role in {
                SemanticRole.EXAMPLE_OR_HYPOTHETICAL,
                SemanticRole.INSUFFICIENT_EVIDENCE,
                SemanticRole.QUESTION_OR_PENDING_CONFIRMATION,
                SemanticRole.NEGATED_FACT,
                SemanticRole.CONDITIONAL_FUTURE,
            }:
                continue
            # Evidence must describe the same actor/action/object.  A separate
            # uncompleted payment cannot negate a documented material receipt.
            requested_completed = _completed_actions(requested_content)
            if not requested_completed or any(
                not any(
                    item["subject"] == evidence["subject"]
                    and _same_action(item["action"], evidence["action"])
                    for evidence in source_completed
                )
                for item in requested_completed
            ):
                start = segment.start + fragment.start()
                hits.append(
                    {
                        "rule": "advice_requests_unproven_completed_fact",
                        "target": target,
                        "quote": requested,
                        "start": start,
                        "end": start + len(fragment.group()),
                        "scanned_text": text,
                    }
                )
    return hits


def _completion_writing_directive(fragment: str) -> re.Match[str] | None:
    """Find a real writing instruction, never the noun use of ``记录``."""
    value = fragment.strip()
    direct = re.match(r"(?:补充|补写|写明|完善|改写|补入)", value)
    if direct:
        return direct
    prefix = _ADVICE_DIRECTIVE_PREFIX.match(value)
    if prefix is None:
        return None
    action = _ADVICE_ACTION.search(value, prefix.end())
    if action is None:
        return None
    # ``建议核对记录中…`` and ``请说明记录显示…`` are not requests to
    # write a fact.  The other writing verbs remain unambiguous here.
    if action.group() == "记录" and value[action.end() :].lstrip().startswith(("中", "显示", "表明")):
        return None
    return action


def unsupported_requirement_hits(
    text: str,
    provenance: str,
    target: str,
) -> list[dict[str, Any]]:
    if provenance in {"deterministic_rule", "knowledge_policy"}:
        return []
    hits = []
    for match in re.finditer(r"[^。；\n]+", str(text or "")):
        clause = match.group().strip()
        found = _STRONG_REQUIREMENT.search(clause)
        if found:
            hits.append(
                {
                    "rule": "semantic_recommendation_presented_as_requirement",
                    "target": target,
                    "quote": clause,
                    "provenance": provenance,
                    "start": match.start(),
                    "end": match.end(),
                    "scanned_text": text,
                }
            )
    return hits


def formal_achievement_status(goal_reviews: list[Any], fallback: str) -> str:
    states = {str(getattr(item, "status", "")) for item in goal_reviews}
    if states and states <= {"not_assessable", "insufficient_evidence"}:
        return "unresolved"
    return fallback


def achievement_boundary_hits(
    text: str,
    *,
    achievement_status: str,
    target: str,
) -> list[dict[str, Any]]:
    if achievement_status != "unresolved":
        return []
    return [
        {
            "rule": "unresolved_presented_as_not_achieved",
            "target": target,
            "quote": match.group().strip(),
            "start": match.start(),
            "end": match.end(),
            "scanned_text": text,
        }
        for match in _UNRESOLVED_AS_FAILED.finditer(str(text or ""))
    ]


def actual_outcome_evidence(data: dict[str, Any]) -> list[dict[str, str]]:
    """Select source-grounded progress worth preserving in final wording."""
    selected = []
    for field in ("process_description", "customer_feedback"):
        value = str(data.get(field) or "").strip()
        for part in re.split(r"[。；\n]+", value):
            text = part.strip(" ，,;；")
            if not text:
                continue
            detailed = bool(
                re.search(
                    r"\d|[一二三四五六七八九十]+(?:个|张|台|套|份|项)|"
                    r"(?:规格|型号|预算|数量|尺寸|负责人|流程|清单|时间|条件|异议|承诺|同意|确认|约定)",
                    text,
                )
            )
            if detailed:
                selected.append({"field": field, "quote": text[:160]})
    return selected[:3]


def outcome_preservation_hits(
    text: str,
    outcomes: list[dict[str, str]],
    target: str,
) -> list[dict[str, Any]]:
    if not outcomes or not _OUTCOME_DENIAL.search(str(text or "")):
        return []
    return [
        {
            "rule": "explicit_outcome_erased",
            "target": target,
            "quote": _OUTCOME_DENIAL.search(str(text)).group(),
            "evidence": outcomes,
            "scanned_text": text,
        }
    ]


def commitment_boundary_hits(
    text: str,
    source_text: str,
    target: str,
) -> list[dict[str, Any]]:
    """Protect future customer commitments without rejecting completed facts.

    A customer saying that a state *has already happened* is evidence, not a
    commitment.  The previous gate paired any source ``客户确认 ... 完成`` with
    any candidate ``已完成`` and therefore rejected faithful summaries of the
    record.  This gate instead compares events: a firm, explicit future
    commitment must be present in the source for that same action; a source
    future action may not be promoted to a completed action without separate
    completion evidence.
    """
    source_future = _future_customer_commitments(source_text)
    source_future_evidence = source_future + _conditional_source_future_actions(source_text)
    source_completed = _completed_actions(source_text)
    candidate_future = _future_customer_commitments(text)
    candidate_completed = _completed_actions(text)
    hits = []

    for event in candidate_future:
        if any(
            _same_future_event(event, source) for source in source_future_evidence
        ):
            continue
        hits.append(
            {
                "rule": "unsupported_future_customer_commitment",
                "target": target,
                "quote": event["clause"],
                "scanned_text": text,
                "candidate_event": event,
                "source_future_commitments": [
                    {
                        key: item.get(key)
                        for key in (
                            "clause",
                            "event_window",
                            "subject",
                            "action",
                            "state",
                            "usage",
                            "time_or_condition",
                        )
                    }
                    for item in source_future_evidence
                ],
            }
        )

    for event in candidate_completed:
        matching_future = [
            source for source in source_future if _same_action(event["action"], source["action"])
        ]
        if not matching_future:
            continue
        if any(_same_action(event["action"], source["action"]) for source in source_completed):
            continue
        hits.append(
            {
                "rule": "future_commitment_presented_as_completed",
                "target": target,
                "quote": event["clause"],
                "scanned_text": text,
                "candidate_event": event,
                "source_future_commitments": [
                    {
                        key: item.get(key)
                        for key in (
                            "clause",
                            "event_window",
                            "subject",
                            "action",
                            "state",
                            "usage",
                            "time_or_condition",
                        )
                    }
                    for item in matching_future
                ],
            }
        )
    return hits


def _future_customer_commitments(text: str) -> list[dict[str, str]]:
    """Return unsupported-prone customer future events, one event at a time.

    The gate deliberately parses a small punctuation-bounded event window.  It
    does not let a later ``待回复`` or ``下一步`` turn an earlier completed fact
    into a future promise, and it does not treat a salesperson's instruction
    to confirm something with a customer as the customer's confirmation.
    """
    result = []
    for segment in semantic_segments(text):
        for firm in _FIRM_CUSTOMER_COMMITMENT.finditer(segment.text):
            event = _customer_commitment_event(segment.text, firm)
            if event["state"] != "future_commitment":
                continue
            result.append(event)
    return result


def _customer_commitment_event(clause: str, firm: re.Match[str]) -> dict[str, str]:
    """Classify one candidate customer commitment without inferring proof."""
    start = (
        max(clause.rfind(mark, 0, firm.start()) for mark in ("，", ",", "、", "；", ";", "。")) + 1
    )
    ends = [clause.find(mark, firm.end()) for mark in ("，", ",", "、", "；", ";", "。")]
    end = min((value for value in ends if value >= 0), default=len(clause))
    window = clause[start:end].strip()
    tail = clause[firm.end() : end]
    action = _action_signature(clause, firm.end(), end)
    role = semantic_scope(clause, firm.start(), firm.end())
    completed = bool(_COMPLETED_ACTION.search(window))
    # A promise to perform an action is future-facing even without a date:
    # "客户同意采购" asserts agreement now, while procurement remains future.
    # A separately completed tail ("确认安装已完成") is a past fact instead.
    future = bool(_FUTURE_DIRECTION.search(tail)) or bool(
        _ACTION.search(tail) and not _COMPLETED_ACTION.search(tail)
    )
    usage = role.value.lower() if role != SemanticRole.ASSERTED_FACT else "fact"
    if role != SemanticRole.ASSERTED_FACT:
        state = usage
    elif completed and not future:
        state = "completed_fact"
        usage = "completed_fact"
    else:
        state = "future_commitment" if future else "asserted_fact"
    return {
        "clause": clause,
        "event_window": window,
        "subject": "customer",
        "action": action,
        "state": state,
        "usage": usage,
        "time_or_condition": tail.strip(),
    }


def _conditional_source_future_actions(text: str) -> list[dict[str, str]]:
    """Return source-only customer-response evidence for a future action.

    A source record can use a pronoun or a conditional response (for example,
    ``他…可以提了就通知我``). It supports the same future action but does not
    prove completion, so callers may use it only to support a candidate future
    expression; completed-action protection remains tied to firm commitments.
    """
    result = []
    for sentence in _SOURCE_SENTENCE.finditer(str(text or "")):
        segments = semantic_segments(sentence.group())
        for index, segment in enumerate(segments):
            clause = segment.text
            # Keep a source answer local.  An earlier promise in the same
            # sentence must not lend its certainty to another action.
            if any(
                _customer_commitment_event(clause, match)["state"] == "future_commitment"
                for match in _FIRM_CUSTOMER_COMMITMENT.finditer(clause)
            ) or _WEAK_INTENT.search(clause):
                continue
            starts = [match.start() for match in _SOURCE_CONDITIONAL_RESPONSE.finditer(clause)]
            # Chinese often leaves the actor in the preceding comma fragment:
            # "他让物资关注情况，可以提了就通知我".
            if (
                not starts
                and index
                and re.match(r"(?:可以|可|待|后续|下周|下次|预计)", clause)
                and re.search(r"(?:客户|对方|他|她)", segments[index - 1].text)
            ):
                starts = [0]
            for start in starts:
                action = _action_signature(clause, start)
                if action.partition(":")[0]:
                    result.append({
                        "clause": clause,
                        "action": action,
                        "time_or_condition": _event_time_or_condition(clause),
                    })
    return result


def _completed_actions(text: str) -> list[dict[str, str]]:
    """Return candidate/source actions explicitly stated as already completed."""
    result = []
    for segment in semantic_segments(text):
        clause = segment.text
        for completed in _COMPLETED_ACTION.finditer(clause):
            if semantic_scope(clause, completed.start(), completed.end()) != SemanticRole.ASSERTED_FACT:
                continue
            actors = list(re.finditer(r"客户|我方|销售|业务员", clause[: completed.start()]))
            actor = actors[-1].group() if actors else ""
            result.append(
                {
                    "clause": clause,
                    "event_window": clause,
                    "subject": "customer" if actor == "客户" else "sales" if actor else "unknown",
                    "action": _action_signature(clause, completed.start()),
                    "state": "completed_fact",
                    "usage": "fact",
                    "time_or_condition": "",
                }
            )
    return result


def classify_semantic_roles(text: str, source_text: str = "") -> list[dict[str, str]]:
    """Expose event-local roles for deterministic boundary and stage replay."""
    source_future = _future_customer_commitments(source_text)
    source_future += _conditional_source_future_actions(source_text)
    result = []
    for segment in semantic_segments(text):
        matches = list(_FIRM_CUSTOMER_COMMITMENT.finditer(segment.text))
        if not matches:
            role = semantic_scope(segment.text)
            result.append({"clause": segment.text, "role": role.value, "subject": "customer" if "客户" in segment.text else "sales" if "我方" in segment.text or "销售" in segment.text else "unknown", "action": _action_signature(segment.text, 0)})
            continue
        for firm in matches:
            event = _customer_commitment_event(segment.text, firm)
            if event["state"] == "future_commitment":
                role = (
                    SemanticRole.SUPPORTED_CUSTOMER_COMMITMENT
                    if any(_same_future_event(event, source) for source in source_future)
                    else SemanticRole.UNSUPPORTED_CUSTOMER_COMMITMENT
                )
            elif event["state"] == "completed_fact":
                role = SemanticRole.ASSERTED_FACT
            else:
                role = SemanticRole.__members__.get(event["state"].upper(), SemanticRole.ASSERTED_FACT)
            result.append({"clause": segment.text, "role": role.value, "subject": event["subject"], "action": event["action"]})
    return result


def _action_signature(clause: str, start: int, end: int | None = None) -> str:
    """Create a small, event-local action signature for evidence comparison."""
    window = clause[start:end]
    action = _ACTION.search(window)
    if action:
        verb = action.group()
        tail = re.split(r"(?:但|然而|不过|可是|而|并且|同时|，|,|；|;)", window[action.end() :], maxsplit=1)[0][:16]
    else:
        verb = ""
        tail = window[:24]
    compact = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", _ACTION_NOISE.sub("", tail))
    return f"{verb}:{compact}".rstrip(":")


def _same_action(left: str, right: str) -> bool:
    """Require the action verb and, when available, its object to agree."""
    left_verb, _, left_object = left.partition(":")
    right_verb, _, right_object = right.partition(":")
    if not left_verb or left_verb != right_verb:
        return False
    if not left_object or not right_object:
        return True
    return left_object in right_object or right_object in left_object


def _same_future_event(candidate: dict[str, str], source: dict[str, str]) -> bool:
    """Match a future promise by action/object plus explicit time and condition.

    A source condition cannot support an unconditional promise.  A candidate
    may summarize a dated source without repeating its date, but it may not
    introduce or change an explicit date.
    """
    if not _same_action(candidate.get("action", ""), source.get("action", "")):
        return False
    candidate_window = str(candidate.get("time_or_condition") or candidate.get("clause") or "")
    source_window = str(source.get("time_or_condition") or source.get("clause") or "")
    candidate_conditions = _event_conditions(candidate_window)
    source_conditions = _event_conditions(source_window)
    if source_conditions and candidate_conditions != source_conditions:
        return False
    if candidate_conditions and not source_conditions:
        return False
    candidate_times = _event_times(candidate_window)
    source_times = _event_times(source_window)
    return not candidate_times or candidate_times == source_times


def _event_time_or_condition(text: str) -> str:
    return str(text or "")


def _event_times(text: str) -> tuple[str, ...]:
    return tuple(_normalize_event_marker(match.group()) for match in _EVENT_TIME.finditer(text))


def _event_conditions(text: str) -> tuple[str, ...]:
    return tuple(_normalize_event_marker(match.group()) for match in _EVENT_CONDITION.finditer(text))


def _normalize_event_marker(value: str) -> str:
    return re.sub(r"[\s，,。；;：:（）()]", "", value).replace("星期", "周")


def preserve_outcomes(
    analysis: str,
    *,
    achievement_status: str,
    outcomes: list[dict[str, str]],
    source_text: str,
) -> str:
    """Add grounded progress without turning a future promise into completion."""
    result = str(analysis or "").strip()
    if achievement_status == "unresolved":
        # A non-verifiable goal may not be converted into a negative outcome.
        # Remove only the unsupported terminal conclusion; retain the factual
        # explanation that made the goal non-verifiable.
        result = re.sub(
            r"[，,] *(?:故|因此|所以)?(?:判定|判断|认定)?(?:为)?"
            r"(?:目标)?(?:尚)?(?:未达成|未达到|没有达成|没有达到)(?=[，。；]|$)",
            "",
            result,
        )
        if not re.search(
            r"不足以.{0,12}(?:判断|确认).{0,12}(?:达成|达到)|"
            r"无法可靠判断|(?:无法|不能)(?:完整|准确)?评估",
            result,
        ):
            result += "当前原定关键结果较宽泛或证据不足，现有记录不足以可靠判断是否完全达成。"
    if outcomes and not any(item["quote"][:12] in result for item in outcomes):
        quote = outcomes[0]["quote"]
        is_future = bool(_FUTURE_OUTCOME.search(quote)) and not bool(
            _COMPLETED_OUTCOME.search(quote)
        )
        if is_future:
            result += (
                f"本次已记录客户的后续承诺“{quote}”，这是有效进展，但承诺事项尚待后续实际完成。"
            )
        else:
            result += f"同时，本次已明确记录“{quote}”这一具体成果。"
    return result
