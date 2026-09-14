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
        "version": "CONTACT-POLICY-20260914-V1",
        "customer_type": kind,
        "period": period,
        "standard": ("客户类型未取得，不选择跨月或跨季度标准。" if kind == "unknown" else {
            "month": "下一次联系日期必须晚于拜访日期，并跨北京时间自然月。",
            "quarter": "下一次联系日期必须晚于拜访日期，并跨北京时间自然季度。",
            "none": "下一次联系日期必须晚于拜访日期，不增加跨月或跨季度门槛。",
        }[period]),
        "customer_consensus_required": kind == "opportunity",
        "feedback_guidance": (
            "商机客户未填写日期时，建议必须表述为：请补充下一次联系客户时间安排，商机客户建议与客户达成下一次拜访时间共识。"
            "已填写时核对客户对下一步是否形成共识，不凭日期认定已约定；不输出跨月、跨季度、固定间隔或日期先后要求。"
            "以上仅是意见表达要求，评分门槛不变。"
            if kind == "opportunity" else "使用本客户类型的时间标准。"
        ),
        "date_state": "present" if following else "missing",
        "after_visit": after,
        "period_met": period_met,
        "guidance": "时间与行动内容分别判断。next_action_logic_ok只判断行动目的、期望结果与本次事实的衔接及具体性；日期缺失或周期不符不得单独导致该值为false。客户共识另用customer_consensus_met。N整体判断仍需满足时间、行动及适用共识要求。日期为空只说未填写，不能说已填写的日期未跨期。不得将所有客户解释成跨自然月。",
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
        basis = section.get("advice_basis") or {}
        if (section.get("code") == "N"
                and basis.get("fields") == ["next_contact_at"]
                and payload.get("facts", {}).get("next_action_logic_ok") is False):
            hits.extend({"target": target, "path": target,
                         "rule": "date_only_gap_is_not_action_content_failure",
                         "quote": section.get("reason", "")}
                        for target in ("N", "facts.reason", "facts.next_action_logic_ok"))
    for target, path, text in texts:
        for clause in re.finditer(r"[^。；，,\n]+", text):
            value = clause.group()
            if re.search(r"无需|不必|不要求|不增加|不强制|不得要求|不能要求|不适用", value):
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
    return hits
