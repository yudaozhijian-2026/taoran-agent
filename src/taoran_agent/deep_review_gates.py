"""Evidence-bound gates for post-submit salesperson-facing feedback.

These helpers never score a visit.  They reject or conservatively normalize
wording that is incomplete, invents a completed fact, or promotes a semantic
recommendation into a company requirement.
"""
from __future__ import annotations

import re
from typing import Any

_UNFINISHED_END = re.compile(
    r"(?:[，；：、(（]\s*|(?:因此|并且|同时|其中|例如|包括|需要|建议|因为)\s*)$"
)
_STRONG_REQUIREMENT = re.compile(
    r"(?:必须|应当|不允许|严禁|要求|须要|应调整为|应改为|只能|不得)"
)
_SUPPLEMENT_COMPLETED = re.compile(
    r"(?:补充|补写|写明|记录|完善).{0,30}(?:已|已经|明确|同意|承诺|完成|确定)"
)
_SAFE_CONDITIONAL = re.compile(
    r"(?:如|若|如果)(?:实际|确实)?.{0,24}(?:已|已经|明确|同意|承诺|完成|确定)"
    r".{0,48}(?:据实|若尚未|如果尚未|保持真实|继续跟进)"
)
_NEGATIVE_COMPLETION = re.compile(
    r"(?:尚未|未|没有|尚无|无法|不能).{0,18}(?:确认|明确|同意|承诺|完成|确定)"
)
_POSITIVE_COMPLETION = re.compile(
    r"(?:已|已经|明确)(?:.{0,14})?(?:确认|同意|承诺|完成|确定)"
    r"|(?:确认|同意|承诺|完成|确定)(?:.{0,8})(?:完成|成功|妥当)"
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
_COMMITMENT = re.compile(
    r"客户.{0,16}(?:承诺|同意|确认|约定).{0,36}(?:将|会|后续|下一步|下周|下次|提供|完成|安排)"
)
_COMMITMENT_AS_COMPLETE = re.compile(
    r"(?:客户.{0,18}(?:承诺|同意|确认|约定).{0,30})?"
    r"(?:已经|已)(?:提供|交付|完成|提交|安排|实施|确认完毕)"
)
_FUTURE_OUTCOME = re.compile(
    r"(?:承诺|约定|计划|预计|拟于|将|会|后续|下一步|下周|下次|待)"
)
_COMPLETED_OUTCOME = re.compile(
    r"(?:已|已经|完成|取得|收到|发来|补发|签署|核对无遗漏)"
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
    hits = []
    for match in re.finditer(r"[^。；\n]+", str(text or "")):
        clause = match.group().strip()
        if not _SUPPLEMENT_COMPLETED.search(clause) or _SAFE_CONDITIONAL.search(clause):
            continue
        # A request to record a completed fact is allowed only when the formal
        # record itself contains positive evidence and no conflicting negation.
        if _NEGATIVE_COMPLETION.search(source_text) or not _POSITIVE_COMPLETION.search(source_text):
            hits.append({
                "rule": "advice_requests_unproven_completed_fact",
                "target": target,
                "quote": clause,
                "start": match.start(),
                "end": match.end(),
                "scanned_text": text,
            })
    return hits


def unsupported_requirement_hits(
    text: str, provenance: str, target: str,
) -> list[dict[str, Any]]:
    if provenance in {"deterministic_rule", "knowledge_policy"}:
        return []
    hits = []
    for match in re.finditer(r"[^。；\n]+", str(text or "")):
        clause = match.group().strip()
        found = _STRONG_REQUIREMENT.search(clause)
        if found:
            hits.append({
                "rule": "semantic_recommendation_presented_as_requirement",
                "target": target,
                "quote": clause,
                "provenance": provenance,
                "start": match.start(),
                "end": match.end(),
                "scanned_text": text,
            })
    return hits


def formal_achievement_status(goal_reviews: list[Any], fallback: str) -> str:
    states = {str(getattr(item, "status", "")) for item in goal_reviews}
    if states and states <= {"not_assessable", "insufficient_evidence"}:
        return "unresolved"
    return fallback


def achievement_boundary_hits(
    text: str, *, achievement_status: str, target: str,
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
            detailed = bool(re.search(
                r"\d|[一二三四五六七八九十]+(?:个|张|台|套|份|项)|"
                r"(?:规格|型号|预算|数量|尺寸|负责人|流程|清单|时间|条件|异议|承诺|同意|确认|约定)",
                text,
            ))
            if detailed:
                selected.append({"field": field, "quote": text[:160]})
    return selected[:3]


def outcome_preservation_hits(
    text: str, outcomes: list[dict[str, str]], target: str,
) -> list[dict[str, Any]]:
    if not outcomes or not _OUTCOME_DENIAL.search(str(text or "")):
        return []
    return [{
        "rule": "explicit_outcome_erased",
        "target": target,
        "quote": _OUTCOME_DENIAL.search(str(text)).group(),
        "evidence": outcomes,
        "scanned_text": text,
    }]


def commitment_boundary_hits(
    text: str, source_text: str, target: str,
) -> list[dict[str, Any]]:
    if not _COMMITMENT.search(source_text) or not _COMMITMENT_AS_COMPLETE.search(str(text or "")):
        return []
    return [{
        "rule": "future_commitment_presented_as_completed",
        "target": target,
        "quote": _COMMITMENT_AS_COMPLETE.search(str(text)).group(),
        "scanned_text": text,
    }]


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
            result += f"本次已记录客户的后续承诺“{quote}”，这是有效进展，但承诺事项尚待后续实际完成。"
        else:
            result += f"同时，本次已明确记录“{quote}”这一具体成果。"
    return result
