"""Conservative, post-only alarms for explicit claim/source contradictions.

These checks reject candidates; they never manufacture facts or scores. They
cover recognizable wording, not unrestricted natural-language entailment.
"""
import re


def _clauses(text):
    return re.finditer(r"(?:(?!但是|但|并且|且|同时)[^。；，,\n])+", text)


def _action_state(clause, match):
    """Scope pending language to this action, retaining its exact actor/verb."""
    before = clause[:match.start()]
    if re.search(r"(?:等待|等候|待|计划|准备|拟|将|希望|预期|安排|督促|要求|请).{0,8}$", before):
        return "pending"
    if re.search(r"(?:已|已经|完成)", match.group()) or re.match(
        r"(?:动作)?(?:已|已经)?完成", clause[match.end():]
    ):
        return "completed"
    if re.search(r"(?:正在|在|开始|继续)", match.group()):
        return "ongoing"
    return "observed"


def _actor_pattern(action):
    return (r"(?<!给)(?<!替)(?<!代)(?<!帮)(?<!助)客户"
            r"(?:本人|已|已经|现场|实际|完成|明确|亲自|正在|尚在|仍在|在|开始|继续|进一步)*" + action)


def _assertion(text):
    # Suggestions, conditions and explicit uncertainty are not occurrence claims.
    # Comma splitting can separate a clarification purpose from its "补充" prefix.
    # "以明确客户签收状态" and "明确是X还是Y" ask for information.
    if re.search(r"^(?:以|为)?(?:明确|查明|核实|确认).{0,24}(?:状态|情况|是否|还是)", text):
        return False
    return not re.search(
        r"(?:未|没有|尚无|不能|无法|不足以|不代表|不等于|而非|并非|不是|是否|待确认|"
        r"请|建议|例如|比如|——如|^如|如客户|安排|缺少|缺乏|缺失|若|如果|应当|需要|核实|核验|补充|补录|"
        r"有待|尚待|须|期望|计划)|(?:依据|根据).{0,30}(?:校准|调整|核对|评估)", text
    )


def claim_hits(text, target, context):
    source = context.get("source_text", "")
    hits = []
    for match in _clauses(text):
        clause = match.group()
        rule = None
        if _assertion(clause):
            # A customer beneficiary also does not establish a salesperson actor.
            # Check direct and reversed attribution, e.g. "收货是销售动作".
            sales_receipt = (
                r"(?:销售(?:人员|方)?|我方|我)(?:本人|已经|已|现场|实际|亲自|正在|代客户|替客户|给客户|为客户|代为|帮客户|协助客户)*收货"
                r"|收货[\"“”‘’']{0,1}(?:动作)?(?:是|属于|由)(?:销售(?:人员|方)?|我方)"
            )
            if re.search(sales_receipt, clause) and not any(
                _assertion(m.group()) and re.search(sales_receipt, m.group())
                for m in _clauses(source)
            ):
                rule = "sales_actor_not_established"
            # A negative order statement still attributes a decision to an actor.
            # "本周不会再点单" alone does not establish who said/decided it.
            negative_order = (r"客户(?:表示|明确|反馈|告知|说|决定)?(?:本周|本月|今天|本次)?"
                              r"(?:不会再|不会|不再|暂不)(?:点单|下单|采购)")
            if re.search(negative_order, clause) and not any(
                re.search(negative_order, m.group()) for m in _clauses(source)
            ):
                rule = "customer_actor_not_established"
            # Omitted actors and actions performed for a customer do not identify
            # the customer as actor. Keep such source wording without inventing one.
            for action in (
                r"(?:收货|接收(?:了)?(?:货物|商品|产品)|收到(?:了)?(?:货物|商品|产品))",
                "签收", "验收", "核对", "签署", "评估", "收集",
            ):
                claimed = re.search(_actor_pattern(action), clause)
                if not claimed or _action_state(clause, claimed) == "pending":
                    continue
                source_states = [
                    _action_state(m.group(), found)
                    for m in _clauses(source) if _assertion(m.group())
                    for found in re.finditer(_actor_pattern(action), m.group())
                    if _action_state(m.group(), found) != "pending"
                ]
                if not source_states:
                    rule = "customer_actor_not_established"
                elif _action_state(clause, claimed) == "completed" and "completed" not in source_states:
                    rule = "pending_process_presented_as_completed"
            pending_contract = re.search(
                r"(?:正在|还在|仍在|在走|待).{0,6}合同(?:流程|签署)|合同(?:流程|签署).{0,5}(?:进行中|待完成|尚未完成)", source
            )
            completed_contract = any(
                _assertion(m.group()) and re.search(
                    r"合同(?:流程|签署)?(?:已经|已)?完成|(?:已经|已)完成合同(?:流程|签署)", m.group()
                ) for m in _clauses(source)
            )
            if pending_contract and not completed_contract and not re.search(
                r"(?:等待|等候|计划|准备|希望|预期).{0,12}完成", clause
            ) and (
                re.search(r"完成.{0,14}合同(?:流程|签署)", clause)
                or re.search(r"合同(?:流程|签署)(?:已经|已)?完成", clause)
            ):
                rule = "pending_process_presented_as_completed"
        # Calendar claims can affect scoring, so reject before section-only repair
        # freezes any facts. Dates come from the normalized submitted snapshot.
        calendar = context.get("calendar", {})
        if not re.search(r"日期|下次联系|联系时间|周期", clause) or re.search(
            r"无需|不必|须|需要|要求|应|建议|请|避免|不得|不能|若|如果", clause
        ):
            calendar = {}
        for period, key in (("季度", "different_quarter"), ("月", "different_month")):
            if key not in calendar:
                continue
            negative = re.search(r"(?:未|没有|不)跨(?:越)?(?:北京时间)?(?:自然)?"+period, clause)
            positive = re.search(r"跨(?:越)?(?:北京时间)?(?:自然)?"+period, clause)
            if (calendar[key] and negative) or (not calendar[key] and positive and not negative and _assertion(clause)):
                rule = "calendar_claim_contradicts_submitted_dates"
        if rule:
            hits.append({"target": target, "rule": rule, "quote": clause,
                         "start": match.start(), "end": match.end(), "scanned_text": text})
    return hits


def advice_hits(text, target):
    hits = []
    for match in _clauses(text):
        clause = match.group()
        rule = None
        future = re.search(r"(?:下次|下一次|今后|以后|未来)", clause)
        prohibited = re.search(r"(?:不得|不能|不要|禁止|不应|避免)", clause)
        if not future and not prohibited and (
            re.search(r"(?:将|把)(?:本次|原定|想取得的)?(?:关键结果|目标).{0,16}(?:改为|调整为|校准为|写明为|写成)", clause)
            or re.search(r"(?:关键结果|原定目标)(?:应|须|要)(?:当)?(?:改为|调整为|写明为|写成)", clause)
        ):
            rule = "retroactive_goal_replacement"
        if target in {"O_KR", "A2"} and not future and not prohibited and not re.search(
            r"(?:原定|原计划|原本|事先|保留原目标|本次计划|此次计划)", clause
        ) and re.search(r"(?:关键结果|目标).{0,10}(?:补充|细化|写明)", clause):
            rule = "goal_clarification_must_preserve_original_intent"
        if target in {"N", "facts.reason"} and not prohibited and re.search(
            r"(?:与|和)(?:本次|此次).{0,12}(?:无衔接|不衔接|毫无关联|没有关联)", clause
        ):
            rule = "overstated_next_action_disconnect"
        if rule:
            hits.append({"target": target, "rule": rule, "quote": clause,
                         "start": match.start(), "end": match.end(), "scanned_text": text})
    return hits
