"""Program-owned next-contact policy, independent of model interpretation."""
import re


def contact_policy(visit):
    kind = visit.customer_type_ii.value if visit.customer_type_ii else "unknown"
    period = {"target": "month", "potential": "quarter"}.get(kind, "none")
    following = visit.next_contact_date
    current = visit.visit_date
    after = following > current if following else None
    period_met = None
    if following:
        period_met = {
            "month": (following.year, following.month) != (current.year, current.month),
            "quarter": (following.year, (following.month - 1) // 3)
            != (current.year, (current.month - 1) // 3),
            "none": True,
        }[period]
    return {
        "version": "CONTACT-POLICY-20260914-V3",
        "customer_type": kind,
        "period": period,
        "standard": ("客户类型未取得，不选择跨月或跨季度标准。" if kind == "unknown" else {
            "month": "下一次联系日期必须晚于拜访日期，并跨北京时间自然月。",
            "quarter": "下一次联系日期必须晚于拜访日期，并跨北京时间自然季度。",
            "none": "不增加固定周期或日期先后建议；根据客户事实检查下一步共识和时间填写。",
        }[period]),
        "scoring_standard": "评分内部仍保留原日期条件；不得将评分日期条件直接转为商机客户改善建议。",
        "customer_consensus_required": kind == "opportunity",
        "feedback_guidance": (
            "商机客户未填写日期时，先结合过程详细描述、客户反馈与下一步行动，分析客户对具体后续事项的回应，再给补充时间的建议。"
            "日期未填写不等于客户没有共识。已有明确约定时，说明已约定的具体事项，建议将真实约定时间补入下一次联系客户时间安排；"
            "已有行动共识但时间尚未确定时，肯定已确认事项，只建议协商尚未确定的联系时间；"
            "若只有我方计划或客户未确认，指出哪项具体安排缺少客户回应，再建议确认安排及联系时间；"
            "客户提出条件、异议或拒绝时保留这些事实，不写成已同意。已确认与未确认事项分别处理，不重复要求确认已有共识。"
            "客户要求报价、寄样或提供方案，可作为相应行动的客户回应，但不代表已约定联系日期。"
            "正在确认不等于已经确认；内部批准不等于客户同意；交货、采购、内部会议日期不自动等于联系日期。"
            "客户说明十月后采购，不得推断十月前不能联系；客户承诺某日回复可作为联系节点，但不得改写审批已完成。"
            "相对日期只在拜访日期和原文能唯一确定时换算，否则据实确认；多项后续安排分别识别，不能用一项共识覆盖全部安排。"
            "最终意见应自然包含实际事项及时间填写建议，不得只输出固定的‘请补充下一次联系客户时间安排，商机客户建议与客户达成下一次拜访时间共识’。"
            "若过程及下一步内容均为空，明确说明缺少后续沟通事实，建议据实补充事项、客户回应及时间，不编造个性化事实。"
            "已填写时核对客户对下一步是否形成共识，不凭日期认定已约定；不输出跨月、跨季度、固定间隔或日期先后要求。"
            "以上仅是意见表达要求，评分条件不变。"
            if kind == "opportunity" else "使用本客户类型的时间标准。"
        ),
        "date_state": "present" if following else "missing",
        "after_visit": after,
        "period_met": period_met,
        "guidance": "时间与行动内容分别判断。next_action_logic_ok只判断行动目的、期望结果与本次事实的衔接及具体性；日期缺失或周期不符不得单独导致该值为false。客户共识另用customer_consensus_met。下一步最终判定仍需同时检查时间、行动及适用的客户确认要求。日期为空只说未填写，不能说已填写的日期未跨期。不得将所有客户解释成跨自然月。",
    }


def contact_policy_hits(payload, policy):
    """Check generated interpretations, never inspect/replace original quotations."""
    texts = [("facts.reason", "facts.reason", payload.get("facts", {}).get("reason", ""))]
    for section in payload.get("sections", []):
        code = section.get("code", "N")
        for field in ("reason", "suggestion"):
            texts.append((code, f"sections.{code}.{field}", section.get(field, "")))
        for field in ("missing_detail", "decision_impact"):
            texts.append((code, f"sections.{code}.advice_basis.{field}",
                          (section.get("advice_basis") or {}).get(field, "")))
    hits = []
    for section in payload.get("sections", []):
        # Reject only the exact legacy boilerplate, not valid fact-grounded wording.
        if (policy['customer_type'] == 'opportunity' and policy['date_state'] == 'missing'
                and section.get('code') == 'N'
                and re.fullmatch(
                    r'请补充下一次联系客户时间安排商机客户建议与客户达成下一次拜访时间共识'
                    r'(?:(?:以|并|以便|以使)?(?:承接|衔接|满足|体现|确保|使)?'
                    r'(?:本次事实|本次拜访事实|下一步行动|时间衔接|有时间节点|要求|更具体|在时间上))*',
                    re.sub(r'[\s，,。；;：:]', '', section.get('suggestion', '')))):
            hits.append({'target': 'N', 'path': 'sections.N.suggestion',
                         'rule': 'missing_date_requires_fact_based_advice',
                         'quote': section['suggestion']})
        basis = section.get("advice_basis") or {}
        if (section.get("code") == "N"
                and basis.get("fields") == ["next_contact_at"]
                and payload.get("facts", {}).get("next_action_logic_ok") is False):
            hits.extend({"target": target, "path": target,
                         "rule": "date_only_gap_is_not_action_content_failure",
                         "quote": section.get("reason", "")}
                        for target in ("N", "facts.reason", "facts.next_action_logic_ok"))
    for target, path, text in texts:
        # Contrast separates negation of another requirement from a positive demand.
        for clause in re.finditer(r"[^。；，,\n]+", re.sub(r'但是|但|然而', '；', text)):
            value = clause.group()
            if re.search(r"(?:无需|不必|不要求|不增加|不强制|不得要求|不能要求|不适用).{0,8}(?:跨|周期|间隔|晚于)", value):
                continue
            periods = re.findall(r"跨(?:越)?(?:北京时间)?(?:自然)?(季度|月)", value)
            wrong = any(p != {"month": "月", "quarter": "季度"}.get(policy["period"])
                        for p in periods)
            if wrong:
                hits.append({"target": target, "path": path,
                             "rule": "customer_contact_period_mismatch",
                             "quote": value, "start": clause.start(), "end": clause.end()})
            elif (policy["customer_type"] == "opportunity"
                  and re.search(r"(?:日期|时间).{0,12}(?:应|须|必须|需要|确保).{0,12}晚于|(?:应|须|必须|确保).{0,8}(?:日期|时间).{0,6}晚于", value)):
                hits.append({"target": target, "path": path,
                             "rule": "opportunity_feedback_date_requirement",
                             "quote": value, "start": clause.start(), "end": clause.end()})
            elif policy['customer_type'] == 'opportunity':
                interval = re.search(r'每\s*[一二三四五六七八九十\d]+\s*(?:天|周|月)|下个(?:月|季度)|\d+\s*天(?:内|后)', value)
                demand = re.search(r'建议|必须|应当|应在|需要|确保|请', value)
                if interval and demand and re.search(r'联系|拜访|跟进', value):
                    # Only exact, customer-sourced timing evidence can justify an interval.
                    grounded = any(
                        e.get('field') in ('process_description', 'customer_feedback')
                        and e.get('category') == 'customer_commitment'
                        and interval.group() in e.get('quote', '')
                        for section in payload.get('sections', []) if section.get('code') == 'N'
                        for e in section.get('evidence', [])
                    )
                    if not grounded:
                        hits.append({'target': target, 'path': path,
                                     'rule': 'opportunity_invented_contact_interval', 'quote': value})
    return hits
