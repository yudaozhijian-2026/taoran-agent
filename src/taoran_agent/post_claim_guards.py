"""Conservative, post-only alarms for explicit claim/source contradictions.

These checks reject candidates; they never manufacture facts or scores. They
cover recognizable wording, not unrestricted natural-language entailment.
"""
import re


def _clauses(text):
    return re.finditer(r"[^。；，,\n]+", text)


def _assertion(text):
    # Suggestions, conditions and explicit uncertainty are not occurrence claims.
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
            # Omitted actors and actions performed for a customer do not identify
            # the customer as actor. Keep such source wording without inventing one.
            for action in (
                r"(?:收货|接收(?:了)?(?:货物|商品|产品)|收到(?:了)?(?:货物|商品|产品))",
                "签收", "验收", "核对", "签署", "评估", "收集",
            ):
                claimed = re.search(r"(?<!给)(?<!替)(?<!代)(?<!帮)(?<!助)客户(?:本人|已|已经|现场|实际|完成|明确|亲自|正在)*" + action, clause)
                explicit = any(
                    _assertion(m.group()) and re.search(
                        r"(?<!给)(?<!替)(?<!代)(?<!帮)(?<!助)客户(?:本人|已|已经|现场|实际|完成|明确|亲自|正在)*" + action,
                        m.group(),
                    ) for m in _clauses(source)
                )
                if claimed and not explicit:
                    rule = "customer_actor_not_established"
            pending_contract = re.search(
                r"(?:正在|还在|仍在|在走|待).{0,6}合同(?:流程|签署)|合同(?:流程|签署).{0,5}(?:进行中|待完成|尚未完成)", source
            )
            completed_contract = any(
                _assertion(m.group()) and re.search(
                    r"合同(?:流程|签署)?(?:已经|已)?完成|(?:已经|已)完成合同(?:流程|签署)", m.group()
                ) for m in _clauses(source)
            )
            if pending_contract and not completed_contract and (
                re.search(r"完成.{0,14}合同(?:流程|签署)", clause)
                or re.search(r"合同(?:流程|签署)(?:已经|已)?完成", clause)
            ):
                rule = "pending_process_presented_as_completed"
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
            r"(?:原定|原计划|原本|事先|保留原目标)", clause
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
