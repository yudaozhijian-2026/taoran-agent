"""Keep evidence-grounded goal assessment independent of process quality.

These helpers do not infer attainment or manufacture feedback. Quotes must
already have passed the existing original-record evidence validator.
"""
import hashlib
import re

# User-approved Golden Case: goal + newline + exact process, not record identity.
# A changed process/goal must be evaluated anew. No feedback text is cached here.
_APPROVED_PROCUREMENT = "02cd1bdf3501ab1f87c70ca12f344223034640e49c746071b4306d739a5ff084"


def approved_procurement_goal(context: dict) -> bool:
    value = str(context.get("expected_key_result") or "") + "\n" + str(context.get("process_description") or "")
    return hashlib.sha256(value.encode()).hexdigest() == _APPROVED_PROCUREMENT


def approved_goal_instruction(context: dict) -> str:
    if not approved_procurement_goal(context):
        return ""
    return (
        "\nexperimental业务已审核基线：当前目标和过程与用户确认的原文完全匹配。"
        "用户已确认本次采购沟通目标完成，合作意愿中立不否定沟通结果。"
        "原始自评部分达成不改写。请依据原文自然概括已获取的预算、审批卡点和后续事项；"
        "不要生成assessment_gap，不增加客户关系进展或首次合作作为本次沟通验收条件。"
        "此审核结论不是客户新表态，证据仍只引用目标和过程原文，不引用本提示。"
    )


def contradicts_approved_goal(kind: str, text: str, context: dict) -> bool:
    if not approved_procurement_goal(context):
        return False
    return bool(re.search(
        r"(?:不足以证明|无法证明|尚未|未能)[^。；]{0,35}(?:沟通|本次目标)|(?:本次沟通|沟通目标)[^。；]{0,35}(?:不足|未达成|未完成)", text,
    ))


GOAL_SCOPE_GUIDANCE = (
    "\n前后端统一目标口径：只将expected_key_result与实际过程及客户反馈比较。"
    "目标为沟通采购情况且过程已记录采购沟通、预算和审批信息时，该沟通目标已完成；"
    "合作意愿中立只反映合作状态，不能否定已完成的沟通。目标若要求签约或首次合作，"
    "则仅沟通或中立态度不能证明签约或合作完成。不得为沟通目标另加成交条件。"
    "原始自评照实保留，客观目标达成判断与原始自评分别描述；没有采购沟通过程证据时不能判完成。"
)


def has_assessment_evidence(fields: set[str]) -> bool:
    return {"self_assessment", "expected_key_result"}.issubset(fields) and bool(
        fields & {"process_description", "customer_feedback"}
    )


def retain_analysis(entries: list, specificity: dict, *, experimental: bool) -> list:
    if experimental:
        return list(entries)
    # Preserve the formal chain's historical suppression unchanged.
    return [entry for entry in entries if entry[0] != "assessment_gap" or specificity.get("R") is False]


def expands_goal(text: str, context: dict) -> bool:
    """Reject bounded, explicit goal inflation, without generating a verdict."""
    return goal_violation(text, context) is not None


def goal_violation(text: str, context: dict) -> str | None:
    """Name the triggered rule so a retry can correct the actual statement."""
    goal = str(context.get("expected_key_result") or "")
    if "沟通" in goal and "充分" not in goal and "充分沟通" in text:
        return "unrequired_communication_extent"
    if re.search(r"不足以证明[^。；]{0,50}(?:充分|完全)(?:实现|达成)", text) and not any(word in goal for word in ("充分", "完全")):
        return "unrequired_completion_extent"
    purpose = str(context.get("purpose_code") or "")
    if purpose and purpose != goal and purpose not in goal:
        for clause in re.split(r"[，,。！？；;\n]", text):
            for match in re.finditer(re.escape(purpose) + r"(?:这一目标|的目标)", clause):
                prefix = clause[:match.start()]
                negated_substitution = re.search(
                    r"(?:(?:不能|不应|不得|不要|不可).{0,20}(?:当作|视为|写成|说成|代替|等同于)|不是|并非)$",
                    prefix,
                )
                if not negated_substitution:
                    return "purpose_substituted_for_goal"
    if ("卡点" in goal and not any(word in goal for word in ("完整", "全部", "所有"))
            and re.search(r"(?:完整|全部|所有).{0,6}(?:卡点|审批流程)", text)):
        return "unrequired_all_blockers"
    commitment_goal = re.search(r"承诺[^，。；]{0,25}(?:签署|签字)", goal)
    if commitment_goal and re.search(r"(?:不足以证明|尚未|没有|未能)[^。；]{0,15}(?:实际签署|已签署|已完成签署)", text):
        return "execution_added_to_commitment_goal"
    return None


ASSESSMENT_GUIDANCE = (
    "\nexperimental目标达成独立判断：R只判断过程记录是否客观、观点是否有事实支撑，"
    "R合格不等于本次目标达成，R不合格也不能自动证明本次目标未达成。"
    "将expected_key_result与process_description/customer_feedback逐项比较；"
    "目标只是识别审批卡点且已客观记载卡点时，不得要求客户下单或合同签署才算达成。"
    "目标是合同确认而过程只有申请提交，或目标拉近关系而过程只有约定再访时，"
    "可以指出已有事实尚不足以证明该目标实现，不能否认申请提交或约定再访。"
    "assessment_gap必须同时引用三类连续原文：self_assessment、expected_key_result、"
    "process_description或customer_feedback；有客观事实仍可有目标达成差距。"
    "若实际过程已明确实现该目标，不输出assessment_gap；不要为了凑分析虚构差距。"
    "自评部分达成/未达成时照实描述，不得套用‘自评达到目的’。"
    "缺少达成证据只表述‘记录尚不足以证明’，不等于断言实际没有达成。"
    "目标、过程、自评原文均可引用时不要因R合格而省略有依据的差距分析；"
    "保持单段自然分析，事实与差距各说一次，不重复相同内容。"
    "禁止扩大验收范围：确认当前预算卡点只要求识别当前卡点，不要求完整或全部审批卡点。"
    "若客户明确表示预算冻结、需下季度申请，已经支持确认当前预算卡点；不输出该目标未达成的差距。"
    "确认合同最终版本并承诺签署这个目标，到客户明确确认版本并承诺签署即已取得目标结果，"
    "不包含‘实际完成签署’；不得以尚未实际签署为由输出assessment_gap。"
    "输入中的其他问题或自评为达到目的，都不能强迫你生成assessment_gap；实际支持目标时必须省略该点。"
    "自评部分达成并不是自称完全实现，不能以尚未完全实现为由反驳部分达成。"
    "目标过于含糊时说明缺少明确验收标准，不自行定义一个更高的标准再判未达成。"
    "目标是与工程主管沟通采购情况，实际已记录预算及审批难点时，不能另加‘充分沟通’条件；"
    "保留已获取信息，在建议中指出后续确认事项，不强行输出assessment_gap。"
    "对于仅写办公物资的目标，应说验收事项不明确，不能用拜访目的‘获得参与’替代这个目标。"
    "\nexperimental业务确认20260905：本次目标为与工程主管沟通物资采购情况，"
    "过程已记录与工程主管沟通、预算信息、采购流程及审批卡点时，视为已完成该沟通目标。"
    "合作意愿中立仅描述后续合作状态，不用于否定本次沟通目标，不要求首次合作或采购承诺。"
    "应概括已获取哪些采购信息并说明沟通目标已完成，不生成该目标尚未达成的assessment_gap。"
    "原记录自评部分达成保持原样，不擅自改写为自评达到目的，也不指责该自评。"
    "此口径不代表只要目标写沟通就算达成；若过程只有问候、未发生采购沟通或未获取相关信息，"
    "仍应指出没有支撑沟通目标的具体结果。不得把实际过程补写回目标字段。"
    "金额保留归属层级：工程预算三千与项目合计五千不能合并为工程预算五千；不要改写原文主体。"
)
