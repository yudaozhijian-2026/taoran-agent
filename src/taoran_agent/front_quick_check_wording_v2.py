"""Read-only presentation projection for the isolated Front Quick Check.

The complete review/ledger is persisted before this view is applied. Nothing
here is an input to scoring, artifact matching, or formal semantic review.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

from .front_v46.experimental_business_semantic_state import classify_field_state
from .record_contract import visit_contract

VERSION = "front-quick-check-wording-v2.1-20260921"
_VAGUE = re.compile(
    r"^(?:项目顺利实施|推进项目|收集信息|了解需求|保持联系|保持关系|继续跟进|"
    r"后续跟进|沟通一下|了解一下|测试|测试测试)[。！!\s]*$"
)
_CONTACT = re.compile(r"联系|沟通|拜访|讨论|回访|见面|约定|约好|商定")
_CONTACT_ACTION = re.compile(
    r"(?:再|再次|继续|届时|随后|完成后|下次).{0,6}(?:联系|沟通|拜访|讨论)|"
    r"下次再约"
)
_TIME = re.compile(
    r"(?:20\d{2}[年/-])?\d{1,2}[月/-]\d{1,2}[日号]?|\d+[日号天周]后?|"
    r"[一二三四五六七两]+[天周]后?|本周[一二三四五六日天]?|"
    r"下周[一二三四五六日天]?|"
    r"下月|明天|后天|届时|随后|完成后|"
    r"周[一二三四五六日天]|星期[一二三四五六日天]"
)
_AGREEMENT = re.compile(
    r"双方.{0,8}(?:约定|商定|约好)|(?:销售|我们).{0,8}(?:与客户)?(?:约定|约好)|"
    r"客户.{0,8}(?:确认|同意).{0,24}(?:再|再次|继续|届时|随后|完成后|下次).{0,6}"
    r"(?:联系|沟通|拜访|讨论)|"
    r"(?:约定|约好).{0,20}(?:联系|沟通|拜访|讨论)"
)
_FUTURE_EVENT = re.compile(
    r"客户.{0,16}(?:确认|承诺|表示|改为|计划).{0,30}(?:本周|下周|下月|明天|后天|"
    r"周[一二三四五六日天]|星期[一二三四五六日天]|\d{1,2}月\d{1,2}日|\d+[天周]后)"
    r".{0,32}(?:提供|发送|发给|完成|提交|安排|排期)"
)
_LABELS = {
    "expected_key_result": "想取得的关键结果",
    "process_description": "过程详细描述",
    "self_assessment": "达成评价",
    "next_contact_at": "下一次联系时间",
    "next_action_expected_result": "下次拜访期望的关键结果",
    "next_action_purpose": "下一步行动目的",
    "other_purpose": "具体其他目的",
    "next_action_other_purpose": "下一次具体其他目的",
    "customer_type_ii": "客户类型",
    "visit_method": "拜访方式",
    "purpose_code": "拜访目的",
}


def enabled(settings: Any) -> bool:
    return bool(
        getattr(settings, "environment", None) == "isolated-submit-test"
        and getattr(settings, "submit_confirmation_enabled", False)
    )


def _sentences(value: Any) -> list[str]:
    result = []
    for sentence in re.split(r"[。！？；;\n]+", str(value or "")):
        sentence = re.sub(
            r"^【[^】]*(?:测试|验证|验收|实验|全流程|复测)[^】]*】", "", sentence
        ).strip(" ，,")
        sentence = re.sub(r"^(?:目的|过程|结果|反馈)[：:]\s*", "", sentence).strip()
        if not sentence or re.search(r"仅用于.*测试|不调用真实AI|^TAORAN流程测试$", sentence):
            continue
        if re.fullmatch(r"(?:无|暂无|待填|测试|\d+)", sentence):
            continue
        result.append(sentence)
    return result


def _time_text(text: str) -> str | None:
    match = _TIME.search(text)
    return match.group(0) if match else None


def _absolute_contact_date(text: str, visit_day: date | None) -> date | None:
    match = re.search(r"(?:(20\d{2})[年/-])?(\d{1,2})[月/-](\d{1,2})[日号]?", text)
    if not match:
        return None
    year = int(match[1]) if match[1] else (visit_day.year if visit_day else None)
    if year is None:
        return None
    try:
        return date(year, int(match[2]), int(match[3]))
    except ValueError:
        return None


def contact_state(raw: dict) -> dict:
    """Classify an actual contact agreement before considering cadence guidance."""
    candidates = []
    for field in (
        "process_description",
        "customer_feedback",
        "next_action_expected_result",
        "next_action_other_purpose",
    ):
        for sentence in _sentences(raw.get(field)):
            if not _CONTACT.search(sentence):
                continue
            negative = bool(
                re.search(
                    r"(?:尚未|还未|没有|暂未)(?:约定|确定|安排)(?:下一次)?(?:联系|沟通)?|"
                    r"(?:联系)?(?:时间|日期).{0,6}(?:待定|未定|未确定)",
                    sentence,
                )
            )
            intended = bool(
                re.search(r"(?:销售|我们|双方).{0,8}(?:计划|打算|拟|希望)", sentence)
                or re.search(r"(?:计划|打算|拟|希望).{0,12}(?:联系|沟通|拜访|讨论)", sentence)
            )
            agreed = bool(
                _AGREEMENT.search(sentence)
                or re.search(
                    r"客户.{0,8}(?:确认|同意).{0,16}(?:联系|沟通|拜访|讨论)",
                    sentence,
                )
            ) and not negative and not intended
            implicit_agreement = bool(
                (
                    _TIME.search(sentence)
                    and re.search(
                        r"(?:后|下次|届时|随后).{0,6}(?:再|继续)?"
                        r"(?:联系|沟通|拜访|讨论)",
                        sentence,
                    )
                )
                or re.search(r"下次再约.{0,6}(?:讨论|沟通|联系)", sentence)
            )
            planned = bool(
                field.startswith("next_action_")
                or (
                    re.search(r"销售|我们", sentence)
                    and re.search(r"计划|打算|准备|拟|希望", sentence)
                )
            )
            if negative:
                state = "undetermined"
            elif agreed or implicit_agreement:
                state = "agreed"
            elif planned and _TIME.search(sentence):
                state = "planned"
            else:
                continue
            timing = _time_text(sentence)
            if state == "agreed" and re.search(r"届时|随后|完成后", sentence):
                timing_kind = "after_event"
            elif state == "agreed" and re.search(
                r"(?:(?:20\d{2})[年/-])?\d{1,2}[月/-]\d{1,2}[日号]?", sentence
            ):
                timing_kind = "exact"
            elif state == "agreed" and timing:
                timing_kind = "relative"
            else:
                timing_kind = None
            candidates.append(
                {
                    "state": state,
                    "field": field,
                    "quote": sentence,
                    "timing_kind": timing_kind,
                    "timing_text": timing,
                }
            )
    states = {item["state"] for item in candidates}
    if "agreed" in states and "undetermined" in states:
        return {"state": "unknown", "evidence": candidates, "timing_kind": None}
    state = next(
        (item for item in ("agreed", "planned", "undetermined") if item in states),
        "unknown",
    )
    evidence = [item for item in candidates if item["state"] == state]
    selected = evidence[0] if evidence else {}
    return {
        "state": state,
        "evidence": evidence,
        "timing_kind": selected.get("timing_kind"),
        "timing_text": selected.get("timing_text"),
    }


def _future_customer_event(raw: dict) -> dict | None:
    for field in ("process_description", "customer_feedback"):
        for sentence in _sentences(raw.get(field)):
            if _FUTURE_EVENT.search(sentence) and not _CONTACT_ACTION.search(sentence):
                return {"field": field, "quote": sentence, "time": _time_text(sentence)}
    return None


def _facts(raw: dict, sections: list[dict]) -> list[dict]:
    """Select exact source sentences for audit while display text is paraphrased."""
    referenced = {
        str(e.get("quote") or "")
        for section in sections
        if section.get("code") == "R"
        for e in section.get("evidence", [])
        if isinstance(e, dict) and e.get("quote")
    }
    choices = []
    for field in ("process_description", "customer_feedback"):
        for index, sentence in enumerate(_sentences(raw.get(field))):
            rank = (
                0 if re.search(r"尚未|还未|未定|待确认|待完成|未准备", sentence) else 1,
                1 if re.search(r"此前|之前|本次向客户确认", sentence) else 0,
                0 if re.search(r"客户|双方|老师|经理|主任|负责人", sentence) else 1,
                0 if re.search(r"确认|承诺|同意|提供|需要|需求|尚未|未定|待", sentence) else 1,
                0 if any(q in sentence or sentence in q for q in referenced) else 1,
                index,
            )
            choices.append((rank, {"field": field, "quote": sentence}))
    selected = []
    for _, item in sorted(choices, key=lambda value: value[0]):
        if item["quote"] not in {existing["quote"] for existing in selected}:
            selected.append(item)
        if len(selected) == 2:
            break
    return selected


def _analysis_summary(raw: dict, facts: list[dict]) -> str:
    text = "。".join(
        _sentences(raw.get("process_description")) + _sentences(raw.get("customer_feedback"))
    )
    if not text:
        return "当前记录还没有可核对的具体过程事实。"
    desk = re.search(r"(?:采购|想要采购)(\d+)张", text)
    size = re.search(r"(\d{3,4})\s*(?:mm)?[×xX*]\s*(\d{3,4})\s*mm", text)
    if desk and size and "办公桌" in text:
        return (
            f"本次已经明确客户计划采购{desk[1]}张{size[1]}×{size[2]}mm组合办公桌，"
            "采购数量和规格需求已经比较清楚。"
        )
    if "培训" in text:
        if "预算" in text and "审批负责人" in text:
            return "本次已确认培训预算初步预留，但内部审批负责人仍未明确。"
        known = []
        pending = []
        if "设备运行稳定" in text:
            known.append("设备运行稳定")
        if re.search(r"培训需求|需要一次维护培训|安排维护培训", text):
            known.append("客户有维护培训需求")
        if "参训部门为" in text:
            known.append("参训部门")
        if re.search(r"(?:已发送|收到).{0,8}资料清单", text):
            known.append("资料清单")
        if re.search(
            r"(?:日期|时间).{0,8}(?:仍需|尚未|待|需等待)|"
            r"(?:尚未|待).{0,10}(?:日期|时间)|排期",
            text,
        ):
            pending.append("具体培训时间")
        if re.search(
            r"参训(?:名单|部门).{0,8}(?:尚未|未|待)|"
            r"(?:尚未|待).{0,16}参训(?:名单|部门)",
            text,
        ):
            pending.append("参训人员或部门")
        if known or pending:
            first = (
                "本次已明确" + "、".join(dict.fromkeys(known))
                if known
                else "本次已确认培训需求"
            )
            second = (
                "，但" + "、".join(dict.fromkeys(pending)) + "仍待客户内部确认"
                if pending
                else ""
            )
            return first + second + "。"
    if "设备清单" in text or re.search(r"[一二三四五六七八九十\d]+台设备.{0,20}安装位置", text):
        count = re.search(r"([一二三四五六七八九十\d]+)台设备", text)
        equipment = f"{count[1]}台设备" if count else "设备"
        if re.search(
            r"清单.{0,8}(?:尚未|未).{0,6}(?:整理|完成)|"
            r"(?:下周|周[一二三四五六日天]).{0,10}(?:提供|发给).{0,8}设备清单",
            text,
        ):
            timing = _time_text(text) or "后续"
            return (
                f"本次已确认设备清单尚待提供，客户明确将在{timing}提供{equipment}清单，"
                "属于实际阶段性安排。"
            )
        if re.search(r"两处电源.{0,8}(?:尚未|未).{0,8}(?:准备|完成)", text):
            return f"本次已取得{equipment}清单并确认安装位置，同时发现两处电源尚未准备。"
        if "安装位置" in text and re.search(r"(?:均已|已经|已).{0,8}(?:完成|确认)", text):
            return (
                f"本次已完成{equipment}清单、安装位置及电源准备状态的核对，"
                "现有记录显示相关事项已确认。"
            )
        if re.search(r"预算审批.{0,8}(?:已通过|完成)", text) and "采购负责人" in text:
            return "本次已取得设备清单，并确认预算审批已通过及采购负责人。"
        return f"本次已取得{equipment}清单，相关清单信息已经记录。"
    if "审核" in text:
        return "本次已跟进审核进度，但集团端仍未反馈，当前推进节点尚不明确。"
    compact = re.sub(r"^(?:本次|销售)(?:已经|已)?", "", facts[0]["quote"])
    return f"本次记录已明确{compact[:72].rstrip('，,；;')}。"


def _specific_next_step(raw: dict) -> tuple[str, dict] | None:
    patterns = (
        (
            r"审批负责人.{0,8}(?:尚未|未|待)",
            "下一步可以重点确认内部审批负责人；在客户尚未明确前，不需要提前填写为已确认。",
        ),
        (
            (
                r"(?:培训)?(?:日期|时间).{0,12}(?:排期|尚未|待|需等待)|"
                r"(?:尚未|待).{0,10}(?:培训)?(?:日期|时间)"
            ),
            "下一步可以重点确认培训具体日期；如果客户部门排期尚未形成，可以继续保留为待确认状态。",
        ),
        (
            r"参训(?:名单|部门).{0,10}(?:尚未|未|待)",
            "下一步可以重点确认参训人员或部门；客户内部尚未确定时，应继续保留为待确认状态。",
        ),
        (
            r"两处电源.{0,8}(?:尚未|未).{0,8}(?:准备|完成)",
            "下一步可以重点确认剩余两处电源是否已经完成准备。",
        ),
        (
            r"客户.{0,20}(?:确认|承诺|表示|改为).{0,30}(?:提供|发给).{0,12}清单",
            "下一步可以围绕设备清单交付继续推进，确认清单是否收到以及后续需要核对的事项。",
        ),
        (
            r"集团.{0,12}(?:没反应|未反馈)|审核.{0,12}(?:卡|未通过|待)",
            "下一步可以重点确认集团审核的当前状态和后续反馈节点；尚未收到回复时应如实保留为待确认。",
        ),
        (
            (
                r"办公桌.{0,40}(?:先|之后|再).{0,16}(?:了解|回复)|"
                r"(?:了解产品|产品情况).{0,20}(?:回复|反馈)"
            ),
            "下一步可以围绕已答复的产品了解事项继续推进，确认需要向客户反馈的产品信息；尚未形成的方案或报价不需要提前补写。",
        ),
        (
            r"(?:提出|需要).{0,12}(?:维护)?培训|下季度.{0,10}培训",
            "下一步可以围绕已有维护培训需求，写清计划继续确认的培训对象、时间或准备材料。",
        ),
        (
            r"(?:安装位置|电源准备).{0,24}(?:均已|已经|均).{0,12}(?:完成|确认)",
            "下一步如需继续跟进，可以围绕已确认的安装位置和电源准备状态，写清实际需要核对的事项。",
        ),
    )
    evidence = [
        {"field": field, "quote": sentence}
        for field in ("process_description", "customer_feedback")
        for sentence in _sentences(raw.get(field))
    ]
    for fact in evidence:
        for pattern, advice in patterns:
            if re.search(pattern, fact["quote"]):
                return advice, fact
    return None


def _contact_advice(raw: dict, presence: dict, visit: Any) -> tuple[dict, dict | None]:
    contact = contact_state(raw)
    future_event = _future_customer_event(raw)
    if presence.get("next_contact_at") != "empty":
        if visit.next_contact_at is None or visit.visit_date is None:
            return contact, None
        contact_day = visit.next_contact_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
        if presence.get("visit_date") == "present" and contact_day <= visit.visit_date:
            return contact, {
                "text": "下一次联系日期没有晚于本次拜访日期，请根据真实安排核对后修正。",
                "source": "record_fact",
                "evidence": [
                    {"field": "next_contact_at", "quote": str(raw["next_contact_at"])},
                    {"field": "visit_date", "quote": str(raw["visit_date"])},
                ],
            }
        agreed = next(
            (item for item in contact["evidence"] if item.get("timing_kind") == "exact"),
            None,
        )
        if agreed and "前" not in agreed["quote"]:
            stated = _absolute_contact_date(agreed["quote"], visit.visit_date)
            if stated and stated != contact_day:
                return contact, {
                    "text": "当前下一次联系时间与记录中的实际约定不一致，建议根据双方真实安排核对后统一。",
                    "source": "confirmed_next_contact",
                    "evidence": [
                        agreed,
                        {"field": "next_contact_at", "quote": str(raw["next_contact_at"])},
                    ],
                }
        return contact, None
    if contact["state"] == "agreed":
        timing = contact.get("timing_text") or "后续"
        if contact.get("timing_kind") == "exact":
            text = (
                f"记录中已经明确下一次沟通时间为{timing}，"
                "建议核对无误后同步填写到“下一次联系时间”。"
            )
        elif contact.get("timing_kind") == "after_event":
            event_time = _time_text(contact["evidence"][0]["quote"]) or "相关事项完成后"
            text = (
                f"记录中已经有{event_time}再次联系核对的安排，"
                "建议确认具体日期后同步填写到“下一次联系时间”。"
            )
        else:
            action = (
                "再次联系核对"
                if re.search(r"联系.{0,4}核对", contact["evidence"][0]["quote"])
                else "再次沟通"
            )
            text = (
                f"记录中已经提到{timing}{action}，建议根据双方实际约定确认具体日期，"
                "并同步填写到“下一次联系时间”。"
            )
        return contact, {
            "text": text,
            "source": "confirmed_next_contact",
            "evidence": contact["evidence"],
        }
    if contact["state"] == "planned":
        return contact, {
            "text": "记录中已有后续联系计划；建议按实际计划确认日期后同步填写到“下一次联系时间”。",
            "source": "business_need",
            "evidence": contact["evidence"],
        }
    if future_event:
        timing = future_event.get("time") or "后续"
        event = (
            "提供设备清单"
            if "清单" in future_event["quote"]
            else "完成后续事项"
        )
        return contact, {
            "text": (
                f"客户已经明确{timing}{event}，但当前记录还没有明确联系时间。"
                "可以结合该事项完成后的实际跟进需要规划下一次联系安排。"
            ),
            "source": "future_customer_event",
            "evidence": [future_event],
        }
    if contact["state"] == "undetermined":
        return contact, {
            "text": "记录已说明双方尚未确定下一次联系时间，建议结合下一步推进事项主动确认联系安排。",
            "source": "business_need",
            "evidence": contact["evidence"],
        }
    customer_type = str(raw.get("customer_type_ii") or "")
    cadence = {
        "potential": "按潜力客户的维护节奏，可以参考跨自然季度安排后续联系",
        "target": "按目标客户的维护节奏，可以参考跨自然月安排后续联系",
    }.get(customer_type)
    text = (
        "当前还没有填写下一次联系时间，记录中也未看到明确的后续联系安排。"
        "建议结合下一步推进事项规划联系时间"
    )
    if cadence:
        text += f"；{cadence}，具体时间以客户实际推进情况为准"
    return contact, {
        "text": text + "。",
        "source": "customer_type_cadence_reference" if cadence else "business_need",
        "evidence": [],
    }


def project(
    visit,
    *,
    front_review: dict | None = None,
    validated_analysis: str = "",
    findings: list[dict] | None = None,
) -> dict:
    raw = visit.model_dump(mode="json")
    presence = visit_contract(visit).get("presence", {})
    sections = list((front_review or {}).get("sections") or [])
    codes = {section.get("code") for section in sections}
    for finding in findings or []:
        if finding.get("dimension") not in codes:
            sections.append(
                {
                    "code": finding.get("dimension"),
                    "verdict": finding.get("conclusion"),
                    "reason": finding.get("statement", ""),
                    "evidence": [],
                }
            )
    by_code = {section.get("code"): section for section in sections}
    field_states = {
        field: classify_field_state(field, raw.get(field))
        for field in ("expected_key_result", "next_action_expected_result")
    }
    goal = str(raw.get("expected_key_result") or "").strip()
    goal_problem = bool(_VAGUE.fullmatch(goal)) or (
        by_code.get("O_KR", {}).get("verdict") == "needs_revision"
        and bool(
            re.search(
                r"宽泛|笼统|不可验证|不具体|无法验证|无法衡量|不够具体|较宽|难以验证",
                by_code["O_KR"].get("reason", ""),
            )
        )
    )
    goal_problem = goal_problem or field_states.get("expected_key_result") in {
        "missing",
        "placeholder",
    }
    alignment = {"computed_goal_summary": "unresolved", "alignment": "not_assessable"}
    if not goal_problem:
        explicit = set()
        for sentence in re.split(r"[。；;\n]+", validated_analysis):
            if re.search(r"不足|不能|无法|难以|未能|不支持|不代表|尚未|未达成", sentence):
                continue
            if re.search(r"(?:目标|实际|本次).{0,12}部分(?:达成|达到|完成)", sentence):
                explicit.add("partially_achieved")
            elif re.search(
                r"(?:该目标|本次目标|原定目标|原目标)(?:已|已经)(?:达成|达到|完成)",
                sentence,
            ):
                explicit.add("achieved")
        if len(explicit) == 1:
            computed = explicit.pop()
            alignment.update(
                computed_goal_summary=computed,
                alignment="aligned" if computed == raw.get("self_assessment") else "overstated",
            )
    assessment = by_code.get("A2", {})
    if not goal_problem and assessment.get("verdict") == "met":
        if raw.get("self_assessment") in {"achieved", "partially_achieved", "not_achieved"}:
            alignment.update(computed_goal_summary=raw["self_assessment"], alignment="aligned")
    elif not goal_problem and assessment.get("verdict") == "needs_revision":
        match = re.search(
            r"(?:实际|目前|现有记录更支持|事实支持)[^。]{0,12}?"
            r"(部分达[到成]|未达[到成]|完全达[到成]|已达[到成])",
            assessment.get("reason", ""),
        )
        if match:
            outcome = match[1]
            computed = (
                "partially_achieved"
                if outcome.startswith("部分")
                else "not_achieved"
                if outcome.startswith("未")
                else "achieved"
            )
            if computed != raw.get("self_assessment"):
                alignment.update(computed_goal_summary=computed, alignment="overstated")
    achievement = "unresolved" if goal_problem else alignment["computed_goal_summary"]
    facts = _facts(raw, sections)
    analysis = _analysis_summary(raw, facts)
    if not facts and presence.get("process_description") == "not_received":
        analysis = "当前暂时无法核对本次实际进展。"
    if goal_problem:
        analysis = analysis.rstrip("。") + "；但当前关键结果表述比较宽，现有记录不足以判断是否已经完整实现。"
    elif achievement == "partially_achieved":
        analysis += "已有进展，但仍有目标事项待落实，目前只能确认部分完成。"

    suggestions = []

    def add(key, text, priority, source, evidence, fields):
        suggestions.append(
            {
                "key": key,
                "text": text,
                "priority": priority,
                "suggestion_basis": {
                    "source": source,
                    "evidence": evidence,
                    "fields": fields,
                },
            }
        )

    def basis(field):
        return [{"field": field, "quote": str(raw[field])}] if raw.get(field) else []

    if goal_problem:
        add(
            "goal",
            "先把关键结果写成可核对的具体事项，再结合已有进展确认自评；不必补写尚未发生的结果。",
            2,
            "goal_quality",
            basis("expected_key_result"),
            ["expected_key_result", "self_assessment"],
        )
    elif alignment["alignment"] in {"overstated", "understated"}:
        label = {
            "achieved": "达到",
            "partially_achieved": "部分达到",
            "not_achieved": "未达到",
        }[achievement]
        add(
            "assessment",
            f"现有记录更支持“{label}”，建议结合本次实际结果重新核对自评。",
            2,
            "self_assessment_conflict",
            facts + basis("expected_key_result") + basis("self_assessment"),
            ["self_assessment"],
        )
    if by_code.get("R", {}).get("verdict") == "needs_revision" and re.search(
        r"具体回应人|原话|区分事实|归属|确认方", by_code["R"].get("reason", "")
    ):
        add(
            "attribution",
            "请核对过程里回应的来源：哪些是客户原话、哪些是转述或判断；无法确认的内容可保留不确定表述。",
            1,
            "uncertain_evidence",
            facts,
            ["process_description"],
        )

    contact, contact_advice = _contact_advice(raw, presence, visit)
    if contact_advice:
        add(
            "contact",
            contact_advice["text"],
            3,
            contact_advice["source"],
            contact_advice["evidence"],
            ["next_contact_at"],
        )

    next_result = str(raw.get("next_action_expected_result") or "").strip()
    if (
        presence.get("next_action_expected_result") == "empty"
        or _VAGUE.fullmatch(next_result)
        or field_states.get("next_action_expected_result") == "placeholder"
    ):
        specific = _specific_next_step(raw)
        if specific:
            text, evidence = specific
            source = (
                "customer_commitment"
                if re.search(r"承诺|确认|表示|改为", evidence["quote"])
                else "record_fact"
            )
            next_evidence = [evidence]
        else:
            text = "按已有计划，写清下一次要确认的信息或希望取得的结果；未确定的事项可如实说明。"
            source = "missing_form_field"
            next_evidence = basis("next_action_expected_result")
        add(
            "next_result",
            text,
            3,
            source,
            next_evidence,
            ["next_action_expected_result"],
        )
    elif by_code.get("N", {}).get("verdict") == "needs_revision" and re.search(
        r"不衔接|未承接|不匹配|(?:期望结果|关键结果|下一步目的).{0,15}(?:笼统|宽泛|不具体|未明确)",
        by_code["N"].get("reason", ""),
    ):
        add(
            "next_result",
            "请核对下一步是否承接本次实际进展，并按已有销售计划明确希望取得的具体结果；还未确定的安排可以如实说明。",
            3,
            "record_fact",
            basis("next_action_expected_result") + facts,
            ["next_action_expected_result"],
        )
    if not facts and presence.get("process_description") == "present":
        add(
            "process_fact",
            "过程里还没有具体业务事实；请据实说明本次沟通了什么、对方如何回应，尚未发生的结果不需要补写。",
            2,
            "missing_form_field",
            basis("process_description"),
            ["process_description"],
        )
    for code, field, text in (
        ("T", "customer_type_ii", "请结合本次已知客户情况，核对客户类型及商机阶段是否选对。"),
        ("O_KR", "purpose_code", "请核对所选拜访目的是否符合本次实际沟通事项；按系统现有选项据实选择。"),
    ):
        section = by_code.get(code, {})
        if section.get("verdict") == "needs_revision" and re.search(
            r"不匹配|不适用|选错|不符|冲突", section.get("reason", "")
        ) and not (code == "O_KR" and goal_problem):
            add(code, text, 2, "record_fact", basis(field) + facts, [field])
    for field, label in _LABELS.items():
        if field in {"expected_key_result", "next_contact_at", "next_action_expected_result"}:
            continue
        if presence.get(field) == "empty" and (
            field not in {"other_purpose", "next_action_other_purpose"}
            or raw.get(
                "purpose_code" if field == "other_purpose" else "next_action_purpose"
            )
            == "其他目的"
        ):
            add(
                field,
                f"“{label}”尚未填写，请按实际情况补充；尚未发生或未确定的内容请如实说明。",
                2 if field == "process_description" else 4,
                "missing_form_field",
                [],
                [field],
            )

    selected = sorted(suggestions, key=lambda item: item["priority"])[:3]
    notices = [
        f"系统尚未收到“{label}”，请检查字段传递。"
        for field, label in _LABELS.items()
        if presence.get(field) == "not_received"
        and (
            field not in {"other_purpose", "next_action_other_purpose"}
            or raw.get(
                "purpose_code" if field == "other_purpose" else "next_action_purpose"
            )
            == "其他目的"
        )
    ]
    advice = "\n".join(
        f"{index}. {item['text']}" for index, item in enumerate(selected, 1)
    )
    if not advice:
        advice = "当前没有需要优先修改的内容。"
    if notices:
        advice += "\n\n系统提示：\n" + "\n".join(notices)
    return {
        "version": VERSION,
        "analysis": analysis,
        "advice": advice,
        "feedback_text": f"本次拜访分析：{analysis}\n\nAI改善建议：\n{advice}",
        "achievement": achievement,
        "contact_state": contact["state"],
        "contact_timing_kind": contact.get("timing_kind"),
        "analysis_basis": facts,
        "suggestions": selected,
        "system_notices": notices,
    }
