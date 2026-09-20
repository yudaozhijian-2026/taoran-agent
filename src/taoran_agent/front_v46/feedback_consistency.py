"""Shared, conservative consistency rules for experimental front feedback.

The model still interprets business language.  These checks only enforce
high-confidence boundaries that can be proved from the current form: do not
turn an information/status objective into a completion objective, do not add
optional details, and do not publish non-actionable or duplicate advice.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from ..models import VisitDraftInput

VERSION = "front-feedback-consistency-v2-20260917"

GUIDANCE = """
先确定原目标类型，再判断事实和建议：确认/了解/核实/收集某项情况、状态或信息，只要求取得明确结果，不等于相关事项必须完成；获得客户承诺不等于承诺已经履行；只有原目标明确要求完成、交付、安装或验收，才把完成事实作为达成条件。
分析和建议必须分别核对本次目标、过程事实、自评和下一步，不用下一步缺口否定本次已经达成的目标。本次目标达成与下一步仍不具体可以同时成立。
每条建议必须对应当前记录中的真实缺口和具体来源；型号、负责人、联系人、姓名、职务或对接人只有在原目标明确要求，或其缺失确实影响具体结论时才可要求补充。
已经清楚记录或已经达标的内容放在分析中肯定，不得作为“无需补充”“信息完整”“符合要求”等改善建议输出。改善建议只保留需要填写者实际修改的内容，不重复表达同一缺口。
未来承诺尚未到期时，只能说明已经取得该承诺及后续待跟进，不得提前判定未履行；没有可靠日期依据时不得推断已经逾期。
如果原文确有歧义且影响结论，使用中性需确认事项；不能确定时不强行判定，也不能凭AI推断新增完成条件。
"""


_ROLE_OR_OPTIONAL = re.compile(
    r"负责人|联系人|姓名|职务|决策人|采购人|对接人|设备型号|产品型号|具体型号"
    r"|沟通方式|交流方式|沟通渠道|联系渠道|电话|微信"
)
_DEMAND = re.compile(
    r"未(?:记录|明确|确认|取得|提供|说明|体现)|缺少|不足以判断|"
    r"(?:建议|请|需要|应当|必须|需)(?:再|进一步)?(?:补充|填写|核实|确认|说明|明确)"
)
_NEGATED_DEMAND = re.compile(r"(?:无需|不必|不要求|不强制|不需要).{0,12}(?:补充|填写|确认|提供|说明)?")
_NON_ACTIONABLE = re.compile(r"无需补充|无需修改|不需要补充|信息完整|内容完整|已足够|符合要求|已经达标|已达标")
_ACTION = re.compile(r"建议|请|需要|应当|必须|仍需|尚需|补充|填写|修改|明确|核实|确认|说明|细化")
_CONFIRM_STATE_GOAL = re.compile(
    r"(?:确认|核实|了解|掌握|收集|获取|弄清).{0,32}(?:情况|状态|信息|位置|需求|问题|现状|意见|反馈|原因|条件)"
)
_EXTRA_COMPLETION = re.compile(
    r"(?:是否|有没有|能否|有无).{0,24}(?:完成|落实|履行|执行|交付|安装完毕|准备完成)"
    r"|(?:完成|落实|履行|执行|交付)情况.{0,12}(?:尚未|未|需|需要|确认|核实)"
    r"|(?:尚未|未).{0,16}(?:完成|落实|履行|执行|交付)"
)
_WHOLE_GOAL_ACHIEVED = re.compile(r"(?:本次|原定|该)?目标(?:已经|已)?达成|本次目标已经完成")
_WHOLE_GOAL_NOT_ACHIEVED = re.compile(r"(?:本次|原定|该)?目标(?:尚未|未能|没有|未)达成|无法判断(?:本次|原定|该)?目标是否达成")
_KNOWN_CODES = {"C", "T", "A1", "O_KR", "R", "A2", "N"}
_CUSTOMER_FACT = re.compile(
    r"客户.{0,28}(?:表示|反馈|说明|确认|提供|发送|发来|同意|拒绝|提出|补充|承诺|回复)"
    r"|(?:表示|反馈|说明|确认|提供|发送|发来|同意|拒绝|提出|补充|承诺|回复).{0,28}客户"
)
_OBSERVABLE_NEXT_RESULT = re.compile(
    r"(?:确认|核实|获取|收到|约定|完成|提交|提供|反馈|安排|解决|明确).{1,40}"
)

_CONTACT_DATE_CLAIMS = (
    ("not_after_visit", re.compile(r"(?:下一次|下次)?联系(?:客户)?(?:日期|时间).{0,18}(?:不晚于|早于或等于|未晚于)(?:本次)?拜访(?:日期|时间)?")),
    ("same_month", re.compile(r"(?:下一次|下次)?联系(?:客户)?(?:日期|时间).{0,24}(?:仍|还)?(?:处于|在|属于)?同一(?:个)?(?:自然)?月")),
    ("same_quarter", re.compile(r"(?:下一次|下次)?联系(?:客户)?(?:日期|时间).{0,24}(?:仍|还)?(?:处于|在|属于)?同一(?:个)?(?:自然)?季度")),
)


def _source(context: dict, fields: tuple[str, ...]) -> str:
    return "\n".join(str(context.get(field) or "") for field in fields)


def _clauses(text: str):
    return [part.strip() for part in re.split(r"[。；;！!\n]", str(text or "")) if part.strip()]


def _as_date(value) -> date | None:
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


def contact_date_claim_error(text: str, context: dict) -> str | None:
    """Reject date claims that directly contradict the current page values."""
    visit_day = _as_date(context.get("visit_date"))
    contact_day = _as_date(context.get("next_contact_at"))
    if visit_day is None or contact_day is None:
        return None
    actual = {
        "not_after_visit": contact_day <= visit_day,
        "same_month": (contact_day.year, contact_day.month) == (
            visit_day.year, visit_day.month,
        ),
        "same_quarter": (
            contact_day.year, (contact_day.month - 1) // 3,
        ) == (
            visit_day.year, (visit_day.month - 1) // 3,
        ),
    }
    for code, pattern in _CONTACT_DATE_CLAIMS:
        if pattern.search(str(text or "")) and not actual[code]:
            return f"contact_date_{code}_contradiction"
    return None


def unsupported_optional_requirement(text: str, context: dict) -> bool:
    """Optional details cannot silently become universal completion fields."""
    relevant = _source(context, (
        "visit_purpose", "purpose_code", "other_purpose", "expected_key_result",
        "process_description", "customer_feedback", "next_action_expected_result",
    ))
    for clause in _clauses(text):
        mentioned = _ROLE_OR_OPTIONAL.search(clause)
        if not mentioned or _NEGATED_DEMAND.search(clause) or not _DEMAND.search(clause):
            continue
        if mentioned.group() not in relevant:
            return True
    return False


def non_actionable_advice(text: str) -> bool:
    """A positive observation belongs to analysis, not the advice list."""
    clauses = _clauses(text)
    if not clauses or not any(_NON_ACTIONABLE.search(part) for part in clauses):
        return False
    remaining = "。".join(_NON_ACTIONABLE.sub("", part) for part in clauses)
    return not _ACTION.search(remaining)


def information_goal_completion_expansion(text: str, context: dict, code: str = "") -> bool:
    """Do not upgrade a status/information objective into actual execution."""
    if code == "N":
        return False
    goal = str(context.get("expected_key_result") or "")
    if not _CONFIRM_STATE_GOAL.search(goal):
        return False
    return any(_EXTRA_COMPLETION.search(part) and not re.search(
        r"(?:不等于|不要求|无需|不能因|不应以).{0,20}(?:完成|落实|履行)", part,
    ) for part in _clauses(text))


def contradictory_goal_summary(texts: list[str]) -> bool:
    body = "。".join(texts)
    return bool(_WHOLE_GOAL_ACHIEVED.search(body) and _WHOLE_GOAL_NOT_ACHIEVED.search(body))


def front_rule_issue_superseded(code: str, context: dict) -> bool:
    """Suppress only high-confidence front-end false positives, never scoring rules."""
    process = str(context.get("process_description") or "")
    expected = str(context.get("expected_key_result") or "")
    next_result = str(context.get("next_action_expected_result") or "")
    has_customer_fact = bool(_CUSTOMER_FACT.search(process))
    if code == "TAORAN_RESULT_NOT_FACT_BASED" and has_customer_fact:
        return True
    if code == "TAORAN_ASSESSMENT_NOT_EVIDENCED" and has_customer_fact and expected.strip():
        return True
    return bool(
        code == "TAORAN_NSA_RESULT_NOT_ACTIONABLE"
        and _OBSERVABLE_NEXT_RESULT.search(next_result)
    )


def candidate_errors(raw: dict, context: dict) -> list[dict]:
    """Return repairable, high-confidence errors with exact local paths."""
    if not isinstance(raw, dict):
        return []
    errors: list[dict] = []
    analysis = raw.get("analysis_points") if isinstance(raw.get("analysis_points"), list) else []
    items = raw.get("items") if isinstance(raw.get("items"), list) else []
    if contradictory_goal_summary([
        str(point.get("text") or "") for point in analysis if isinstance(point, dict)
    ]):
        for index, point in enumerate(analysis):
            if isinstance(point, dict) and _WHOLE_GOAL_NOT_ACHIEVED.search(str(point.get("text") or "")):
                errors.append({"location": f"analysis_points.{index}", "code": "goal_summary_contradiction"})
    for index, point in enumerate(analysis):
        if not isinstance(point, dict):
            continue
        error = contact_date_claim_error(str(point.get("text") or ""), context)
        if error:
            errors.append({"location": f"analysis_points.{index}", "code": error})
    seen: dict[str, int] = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        suggestion = str(item.get("suggestion") or "").strip()
        code = str(item.get("code") or "")
        if not suggestion:
            continue
        normalized = re.sub(r"[\s，,。；;：:！!？?（）()【】\[\]]", "", suggestion)
        if normalized in seen:
            errors.append({"location": f"items.{index}", "code": "duplicate_suggestion"})
        else:
            seen[normalized] = index
        if non_actionable_advice(suggestion):
            errors.append({"location": f"items.{index}", "code": "non_actionable_suggestion"})
        date_error = contact_date_claim_error(suggestion, context)
        if date_error:
            errors.append({"location": f"items.{index}", "code": date_error})
        if unsupported_optional_requirement(suggestion, context):
            errors.append({"location": f"items.{index}", "code": "unsupported_optional_requirement"})
        if information_goal_completion_expansion(suggestion, context, code):
            errors.append({"location": f"items.{index}", "code": "objective_scope_expansion"})
        # Unknown codes have their own classification-only recovery. Preserve
        # that candidate first so a code error cannot erase visible advice.
        if code not in _KNOWN_CODES:
            continue
        proofs = item.get("proofs")
        if not isinstance(proofs, list) or not proofs:
            errors.append({"location": f"items.{index}", "code": "ungrounded_suggestion"})
            continue
        for proof in proofs:
            if not isinstance(proof, dict) or not isinstance(proof.get("field"), str):
                errors.append({"location": f"items.{index}", "code": "invalid_suggestion_source"})
                break
            field = proof["field"]
            quote = str(proof.get("quote") or "")
            if field not in VisitDraftInput.model_fields or field.startswith("_"):
                errors.append({"location": f"items.{index}", "code": "invalid_suggestion_source"})
                break
            value = context.get(field)
            empty = value is None or value == [] or (isinstance(value, str) and not value.strip())
            if (empty and quote) or (not empty and (not quote or not isinstance(value, str) or quote not in value)):
                errors.append({"location": f"items.{index}", "code": "unresolved_suggestion_source"})
                break
    return _unique_errors(errors)


def preview_errors(text: str, context: dict) -> list[dict]:
    errors = []
    if unsupported_optional_requirement(text, context):
        errors.append({"code": "unsupported_optional_requirement"})
    if contradictory_goal_summary([text]):
        errors.append({"code": "goal_summary_contradiction"})
    date_error = contact_date_claim_error(text, context)
    if date_error:
        errors.append({"code": date_error})
    presence = (context.get("_record_contract") or {}).get("presence", {})
    if (presence.get("next_contact_at") == "empty"
            and re.search(
                r"(?:当前|本次|整条)?记录.{0,16}(?:无需|无须|不需要)(?:再)?补充"
                r"|未发现.{0,12}(?:缺口|需要改善)"
                r"|(?:当前|本次|整条)?记录.{0,16}未(?:反映|体现|发现).{0,12}(?:需要补充|需要改善|缺口)",
                text,
            )):
        errors.append({"code": "known_gap_declared_complete"})
    return errors


def _unique_errors(errors: list[dict]) -> list[dict]:
    result = []
    seen = set()
    for error in errors:
        key = (error.get("location"), error.get("code"))
        if key not in seen:
            result.append(error)
            seen.add(key)
    return result
