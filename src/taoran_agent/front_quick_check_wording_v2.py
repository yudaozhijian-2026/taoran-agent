"""Read-only presentation projection for the isolated Front Quick Check.

The complete review/ledger is persisted before this view is applied. Nothing
here is an input to scoring, artifact matching, or formal semantic review.
"""
from __future__ import annotations

import re
from typing import Any
from zoneinfo import ZoneInfo

from .front_v46.experimental_business_semantic_state import build_business_state
from .record_contract import visit_contract

VERSION = "front-quick-check-wording-v2-20260921"
_VAGUE = re.compile(r"^(?:项目顺利实施|推进项目|收集信息|了解需求|保持联系|保持关系|继续跟进|后续跟进|沟通一下|了解一下)[。！!\s]*$")
_CONTACT = re.compile(r"联系|沟通|拜访|讨论|回访|见面")
_TIME = re.compile(r"\d{1,4}[-年/月]\d|\d+[日号天周]|[一二三四五六七两]+[天周]|下周|下月|明天|后天|周[一二三四五六日天]|星期[一二三四五六日天]")
_NEGATIVE = re.compile(r"尚未|还未|未曾|没有|没(?:有)?|未|待|暂不|不确定")
_LABELS = {
    "expected_key_result": "想取得的关键结果", "process_description": "过程详细描述",
    "self_assessment": "达成评价", "next_contact_at": "下一次联系时间",
    "next_action_expected_result": "下次拜访期望的关键结果",
    "next_action_purpose": "下一步行动目的", "other_purpose": "具体其他目的",
    "next_action_other_purpose": "下一次具体其他目的", "customer_type_ii": "客户类型",
    "visit_method": "拜访方式", "purpose_code": "拜访目的",
}


def enabled(settings: Any) -> bool:
    return bool(
        getattr(settings, "environment", None) == "isolated-submit-test"
        and getattr(settings, "submit_confirmation_enabled", False)
    )


def contact_state(raw: dict) -> dict:
    """Absence and intentions never become an agreement; contradictions stay unknown."""
    candidates = []
    for field in ("process_description", "customer_feedback", "next_action_expected_result",
                  "next_action_other_purpose"):
        for sentence in re.split(r"[，,。！？；;\n]+", str(raw.get(field) or "")):
            sentence = sentence.strip()
            if not sentence or not _CONTACT.search(sentence):
                continue
            state = None
            if re.search(r"(?:尚未|还未|没有|未|暂未).{0,6}(?:约定|确定|安排)|(?:时间|日期).{0,5}(?:待定|未定|未确定)", sentence):
                state = "undetermined"
            elif _TIME.search(sentence):
                if (field in {"process_description", "customer_feedback"}
                    and re.search(r"双方.{0,5}(?:约定|商定)|客户.{0,5}(?:确认|同意)|(?:与|和)客户.{0,5}(?:约定|商定)", sentence)
                    and not _NEGATIVE.search(sentence)
                    and not re.search(r"如果|希望|计划|拟|打算", sentence)):
                    state = "agreed"
                elif field.startswith("next_action_") or re.search(r"计划|打算|准备|拟|希望", sentence):
                    state = "planned"
            if state:
                candidates.append({"state": state, "field": field, "quote": sentence})
    states = {item["state"] for item in candidates}
    if "agreed" in states and "undetermined" in states:
        return {"state": "unknown", "evidence": candidates}
    state = next((s for s in ("undetermined", "agreed", "planned") if s in states), "unknown")
    return {"state": state, "evidence": [item for item in candidates if item["state"] == state]}


def _facts(raw: dict, sections: list[dict]) -> list[dict]:
    """Select whole source sentences by relevance, never crop generated prose."""
    referenced = {
        str(e.get("quote") or "") for s in sections if s.get("code") == "R"
        for e in s.get("evidence", []) if isinstance(e, dict) and e.get("quote")
    }
    choices = []
    for field in ("process_description", "customer_feedback"):
        for index, sentence in enumerate(re.split(r"[。！？；;\n]+", str(raw.get(field) or ""))):
            sentence = sentence.strip()
            sentence = re.sub(r"^【[^】]*(?:测试|验证|验收|实验|全流程|复测)[^】]*】", "", sentence).strip()
            if re.search(r"仅验证|不调用真实AI|^TAORAN流程测试$|^目的：|^(?:过程|结果|反馈)[：:]$", sentence):
                continue
            if not sentence or re.fullmatch(r"(?:无|暂无|待填|测试|\d+)", sentence):
                continue
            # Retain the entire source sentence, including negation, speaker and tense.
            rank = (
                1 if re.search(r"此前|之前|本次向客户确认", sentence) else 0,
                0 if re.search(r"客户|双方|老师|经理|主任|负责人", sentence) else 1,
                0 if re.search(r"确认|承诺|同意|提供|需要|需求|尚未|未定|待", sentence) else 1,
                0 if any(q in sentence or sentence in q for q in referenced) else 1,
                index,
            )
            choices.append((rank, {"field": field, "quote": sentence}))
    selected = []
    for _, item in sorted(choices, key=lambda x: x[0]):
        if item["quote"] not in {x["quote"] for x in selected}:
            selected.append(item)
        if len(selected) == 2:
            break
    return selected


def project(visit, *, front_review: dict | None = None, validated_analysis: str = "",
            findings: list[dict] | None = None) -> dict:
    raw = visit.model_dump(mode="json")
    presence = visit_contract(visit).get("presence", {})
    sections = (front_review or {}).get("sections") or []
    sections = list(sections)
    codes = {s.get("code") for s in sections}
    for finding in findings or []:
        if finding.get("dimension") not in codes:
            sections.append({"code": finding.get("dimension"), "verdict": finding.get("conclusion"),
                             "reason": finding.get("statement", ""), "evidence": []})
    by_code = {s.get("code"): s for s in sections}
    state = build_business_state(raw)
    goal = str(raw.get("expected_key_result") or "").strip()
    goal_problem = bool(_VAGUE.fullmatch(goal)) or (
        by_code.get("O_KR", {}).get("verdict") == "needs_revision"
        and bool(re.search(r"宽泛|笼统|不可验证|不具体|无法验证|无法衡量|不够具体|较宽|难以验证", by_code["O_KR"].get("reason", "")))
    )
    goal_problem = goal_problem or state["field_states"].get("expected_key_result") in {"missing", "placeholder"}
    alignment = dict(state["self_assessment_alignment"])
    # Advice-only workers may omit section findings. Read only explicit outcome
    # assertions from their independently validated analysis, not from advice.
    # Unknown wording stays unresolved; broad goals cannot acquire an outcome.
    if not goal_problem:
        explicit = set()
        for sentence in re.split(r"[。；;\n]+", validated_analysis):
            if re.search(r"不足|不能|无法|难以|未能|不支持|不代表|尚未|未达成", sentence):
                continue
            if re.search(r"(?:目标|实际|本次)(?:仅|已|为|只能支持|只能确认|属于|支持|可判断为|只能判断为|可以认定为)*部分(?:达成|达到|完成)", sentence):
                explicit.add("partially_achieved")
            elif re.search(r"(?:该目标|本次目标|原定目标|原目标)(?:已|已经)(?:达成|达到|完成)", sentence):
                explicit.add("achieved")
        if len(explicit) == 1:
            computed = explicit.pop()
            alignment.update(computed_goal_summary=computed,
                             alignment="aligned" if computed == raw.get("self_assessment") else "overstated")

    assessment = by_code.get("A2", {})
    if not goal_problem and assessment.get("verdict") == "met":
        if raw.get("self_assessment") in {"achieved", "partially_achieved", "not_achieved"}:
            alignment.update(computed_goal_summary=raw["self_assessment"], alignment="aligned")
    elif not goal_problem and assessment.get("verdict") == "needs_revision":
        reason = assessment.get("reason", "")
        # Use explicit outcome findings, never infer non-achievement from missing evidence.
        match = re.search(r"(?:实际|目前|现有记录更支持|事实支持)(?:仅|只能|为|是|属于|支持|显示|可判断为|能支持)*[“\"]?(部分达[到成]|未达[到成]|完全达[到成]|已达[到成])", reason)
        if match:
            outcome = match[1]
            computed = "partially_achieved" if outcome.startswith("部分") else "not_achieved" if outcome.startswith("未") else "achieved"
            if computed != raw.get("self_assessment"):
                alignment.update(computed_goal_summary=computed, alignment="overstated")
    achievement = "unresolved" if goal_problem else alignment["computed_goal_summary"]
    if achievement == "not_assessable":
        achievement = "unresolved"
    facts = _facts(raw, sections)
    analysis = "记录中的重点是：" + "；".join(f"“{x['quote']}”" for x in facts) + "。" if facts else "当前记录还没有可核对的具体过程事实。"
    if not facts and presence.get("process_description") == "not_received":
        analysis = "当前暂时无法核对本次实际进展。"
    if goal_problem:
        analysis += "当前关键结果表述比较宽，现有记录不足以判断是否已经完整实现。"
    elif achievement == "partially_achieved":
        analysis += "已有进展，但仍有目标事项待落实，目前只能确认部分完成。"

    suggestions = []
    def add(key, text, priority, source, evidence, fields):
        suggestions.append({"key": key, "text": text, "priority": priority,
                            "suggestion_basis": {"source": source, "evidence": evidence,
                                                 "fields": fields}})

    def basis(field):
        return [{"field": field, "quote": str(raw[field])}] if raw.get(field) else []

    if goal_problem:
        add("goal", "先把关键结果写成可核对的具体事项，再结合已有进展确认自评；不必补写尚未发生的结果。", 2,
            "record_fact", basis("expected_key_result"), ["expected_key_result", "self_assessment"])
    elif presence.get("expected_key_result") == "empty":
        add("goal", "请据实补充本次原本希望取得的具体结果，再结合实际进展核对自评。", 2,
            "form_completion", [], ["expected_key_result"])
    elif alignment["alignment"] in {"overstated", "understated"}:
        label = {"achieved": "达到", "partially_achieved": "部分达到", "not_achieved": "未达到"}[achievement]
        add("assessment", f"现有记录更支持“{label}”，建议结合本次实际结果重新核对自评。", 2,
            "record_fact", facts + basis("expected_key_result") + basis("self_assessment"), ["self_assessment"])

    if (not goal_problem and raw.get("self_assessment") == "achieved"
        and not any(item["key"] == "assessment" for item in suggestions)
        and re.search(r"(?:目标[^。]{0,30}(?:未取得明确结果|不足以支持|未有明确结果)|该部分已取得明确结果[^。]*。[\s\S]*?该部分已达成|具体日期尚未取得明确结果)", validated_analysis)):
        add("assessment", "本次仍有目标事项待确认，请结合记录中的已完成事项和待确认事项，重新核对自评。", 2,
            "record_fact", facts + basis("expected_key_result") + basis("self_assessment"), ["self_assessment"])

    if (not goal_problem and not any(item["key"] == "assessment" for item in suggestions)
        and re.search(r"(?:目标[^。]{0,35}(?:不足以支持|未有明确结果)|当前不足以判断该部分|记录中尚无明确事实支持)", validated_analysis)):
        analysis += "目前还不能确认目标中的所有事项都已落实。"
        add("goal_confirmation", "请核对目标中仍待确认的事项：如果实际已确认，可以据实补充；尚未确认则保留待确认状态。", 2,
            "record_fact", basis("expected_key_result") + facts, ["expected_key_result", "process_description"])
    if by_code.get("R", {}).get("verdict") == "needs_revision" and re.search(
        r"具体回应人|原话|区分事实|归属|确认方", by_code["R"].get("reason", "")
    ):
        add("attribution", "请核对过程里回应的来源：哪些是客户原话、哪些是转述或判断；无法确认的内容可保留不确定表述。", 1,
            "record_fact", facts, ["process_description"])

    contact = contact_state(raw)
    if presence.get("next_contact_at") == "empty":
        messages = {
            "agreed": "过程里已有下一次沟通约定，请把对应的具体日期同步到“下一次联系时间”；按真实约定填写。",
            "planned": "已有下一次联系计划；如果时间尚未与客户确认，可以在实际确认后据实更新具体日期。",
            "undetermined": "双方还没有确定下一次联系时间，不需要为了完成AI检查预设日期；待与客户确认后再据实更新。",
            "unknown": "下一次联系时间未填：如果已约定，请据实同步；如果尚未确定，则不需要预填。",
        }
        add("contact", messages[contact["state"]], 3,
            "confirmed_customer_commitment" if contact["state"] == "agreed" else "missing_business_decision",
            contact["evidence"], ["next_contact_at"])
    elif visit.next_contact_at is not None and visit.visit_date is not None:
        # Actual chronology only. Month/quarter cadence never overrides an agreement.
        contact_day = visit.next_contact_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
        if presence.get("visit_date") == "present" and contact_day <= visit.visit_date:
            add("contact", "下一次联系日期没有晚于本次拜访日期，请核对是否填错；按真实约定或实际计划修正，不需要预设日期。", 1,
                "record_fact", basis("next_contact_at") + basis("visit_date"), ["next_contact_at", "visit_date"])

    next_result = str(raw.get("next_action_expected_result") or "").strip()
    if (presence.get("next_action_expected_result") == "empty" or _VAGUE.fullmatch(next_result)
        or state["field_states"].get("next_action_expected_result") == "placeholder"):
        commitment = next((f for f in facts if re.search(
            r"客户.{0,12}(?:承诺|确认|表示|改为).{0,30}(?:提供|发给).{0,15}清单", f["quote"]
        ) and not re.search(r"(?:未|没有|尚未)(?:承诺|确认|同意)", f["quote"])), None)
        text = (
            "下一步可围绕客户提供清单的已有安排，写清希望确认清单是否收到、后续需核对什么；请按实际计划填写。"
            if commitment else
            "按已有计划，写清下一次要确认的信息或希望取得的结果；未确定的事项可如实说明。"
        )
        add("next_result", text, 3,
            "record_fact" if next_result or commitment else "missing_business_decision",
            basis("next_action_expected_result") + ([commitment] if commitment else basis("next_action_other_purpose")),
            ["next_action_expected_result"])

    elif by_code.get("N", {}).get("verdict") == "needs_revision" and re.search(
        r"笼统|宽泛|不具体|未明确|衔接|不匹配", by_code["N"].get("reason", "")
    ):
        add("next_result", "请核对下一步是否承接本次实际进展，并按已有销售计划明确希望取得的具体结果；还未确定的安排可以如实说明。", 3,
            "record_fact", basis("next_action_expected_result") + facts, ["next_action_expected_result"])

    if not facts and presence.get("process_description") == "present":
        add("process_fact", "过程里还没有具体业务事实；请据实说明本次沟通了什么、对方如何回应，尚未发生的结果不需要补写。", 2,
            "record_fact", basis("process_description"), ["process_description"])
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
            or raw.get("purpose_code" if field == "other_purpose" else "next_action_purpose") == "其他目的"
        ):
            add(field, f"“{label}”尚未填写，请按实际情况补充；尚未发生或未确定的内容请如实说明。", 2 if field == "process_description" else 4,
                "form_completion", [], [field])

    selected = sorted(suggestions, key=lambda x: x["priority"])[:3]
    notices = [f"系统尚未收到“{label}”，请检查字段传递。" for field, label in _LABELS.items()
               if presence.get(field) == "not_received" and (
                   field not in {"other_purpose", "next_action_other_purpose"}
                   or raw.get("purpose_code" if field == "other_purpose" else "next_action_purpose") == "其他目的"
               )]
    advice = "\n".join(f"{i}. {item['text']}" for i, item in enumerate(selected, 1))
    if not advice:
        advice = "当前没有需要优先修改的内容。"
    if notices:
        advice += "\n\n系统提示：\n" + "\n".join(notices)
    return {"version": VERSION, "analysis": analysis, "advice": advice,
            "feedback_text": f"本次拜访分析：{analysis}\n\nAI改善建议：\n{advice}",
            "achievement": achievement, "contact_state": contact["state"],
            "analysis_basis": facts, "suggestions": selected, "system_notices": notices}
