"""Candidate-only alignment of provisional findings and validated wording."""
import re

# These findings are re-evaluated by the existing field-specific evidence checks.
# Do not feed a provisional negative back as an already-confirmed model fact.
_RECHECKED = {
    "O_KR": {"TAORAN_KR_NOT_VERIFIABLE", "KR_SEMANTICALLY_VAGUE", "Q34_KEY_RESULT_QUALITY_NOT_MET"},
    "R": {"TAORAN_RESULT_NOT_FACT_BASED", "TAORAN_FACT_JUDGMENT_MIXED", "RESULT_LACKS_CUSTOMER_FACTS", "Q34_PROCESS_NOT_FACT_BASED"},
    "N": {"TAORAN_NSA_RESULT_NOT_ACTIONABLE", "NEXT_ACTION_NOT_QUALIFIED", "Q34_NEXT_ACTION_NOT_QUALIFIED"},
}


def pending_finding_codes(checks: list[dict]) -> set[str]:
    # _assessment_checks only tests evidence/marker presence. Candidate wording
    # independently compares the actual goal and outcome, even with no O/R/N
    # check pending, so this provisional finding must not pre-judge attainment.
    return {"TAORAN_ASSESSMENT_NOT_EVIDENCED"} | set().union(
        *(_RECHECKED.get(item["code"], set()) for item in checks)
    )


def has_analysis_conflict(text: str, specificity: dict[str, bool | None]) -> bool:
    """Reject explicit contradictions; never manufacture replacement analysis.

    This bounded guard does not claim general natural-language entailment.
    A poorly specified objective can still coexist with concrete process facts.
    """
    patterns = {
        "R": (
            r"(?:过程|记录)[^。！？；]{0,22}(?:缺少|缺乏|没有|未记录)[^。！？；]{0,12}客户[^。！？；]{0,12}(?:事实|表达|动作|反馈)",
            r"(?:缺少|缺乏|没有)[^。！？；]{0,8}(?:可核验的)?客户事实",
        ),
        "O_KR": (r"(?:本次目标|关键结果)[^。！？；]{0,8}(?:不具体|不清楚|过于宽泛|仅填[写了]*[“\"']?1)",),
        "N": (r"(?:下次期望结果|下次目标)[^。！？；]{0,8}(?:不具体|不清楚|过于宽泛|仅填[写了]*[“\"']?1)",),
    }
    return any(
        specificity.get(code) is True and any(re.search(pattern, text) for pattern in rules)
        for code, rules in patterns.items()
    )


def has_field_role_conflict(text: str, context: dict) -> bool:
    """Catch explicit assertions that unequal objective/process fields are equal."""
    expected = str(context.get("expected_key_result") or "").strip()
    process = str(context.get("process_description") or "").strip()
    combined = re.search(r"(?:关键结果|目标)(?:和|与)(?:过程|实际过程)(?:均为|都是|都是指)", text)
    if combined and expected != process:
        return True
    for match in re.finditer(r"本次目标(?:是|为)([^，。；！？]+)", text):
        claim = match.group(1).strip("“”\" ")
        if claim and claim == process and claim != expected:
            return True
    return False


def has_grounded_visit_result(entries: list) -> bool:
    return any(
        kind in {"customer_fact", "objective_result", "legacy"}
        and any(proof.field in {"process_description", "customer_feedback"} for proof in proofs)
        for kind, _text, proofs in entries
    )


def process_fully_covered_in_advice(items, context: dict) -> bool:
    """A short process can be quoted completely in the model's R feedback.

    This accepts existing grounded wording; it does not invent a visit result
    when the source records only an action such as payment follow-up.
    """
    source = str(context.get("process_description") or "").strip()
    return bool(source) and any(
        item.code == "R" and source in item.suggestion
        and any(proof.field == "process_description" and proof.quote.strip() == source
                for proof in item.evidence)
        for item in items
    )


def denies_recorded_customer_action(text: str, context: dict) -> bool:
    """Protect explicit source actions without overriding the R quality verdict."""
    source = str(context.get("process_description") or "")
    explicit_action = re.search(r"(?:^|[，,。；;：:\n])\s*客户(?:已经|已)?(?:下单|签收|付款|签署了|确认了|明确表示|明确拒绝)", source)
    completed_arrangement = re.search(r"(?:^|[，,。；;\n])\s*(?:简单)?与客户沟通(?:后|之后)(?:已)?约定下次拜访", source)
    if not (explicit_action or completed_arrangement):
        return False
    # Specific gaps (e.g. payment confirmation or support for a sales forecast)
    # remain permissible; only blanket denial of any customer action is blocked.
    return bool(re.search(
        r"(?:缺少|缺乏|没有|未记录)(?:任何|可核验的)?客户(?:表达或动作|表达或行为|事实)(?:的客观描述|的客观事实|支撑|。|，|$)",
        text,
    ))


def invents_sales_forecast(text: str, context: dict) -> bool:
    """Do not criticize a particular optimistic claim absent from the record.

    Limited wording guard only: does not assign R or certify a forecast as true.
    """
    source = str(context.get("process_description") or "")
    return "项目可按计划推进" in text and "项目可按计划推进" not in source


def asserts_unrecorded_receipt(text: str, context: dict) -> bool:
    """Sales delivery must not become asserted customer acceptance."""
    source = str(context.get("process_description") or "") + "。" + str(context.get("customer_feedback") or "")
    if not re.search(r"客户(?:已经|已)(?:接收|签收|接收了|签收了)", text):
        return False
    return not re.search(r"(?:^|[，,。；;：:\n])\s*客户(?:已经|已)?(?:接收|签收)", source)
