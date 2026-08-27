from __future__ import annotations

from collections.abc import Iterable

from .field_labels import display_field_name
from .models import (
    FeedbackMode,
    Issue,
    KnowledgeReference,
    Q34SemanticFacts,
    SemanticReview,
    Severity,
    TaoranSectionCheck,
    VisitDraftInput,
)

_SECTIONS = (
    (
        "T",
        "客户类型",
        {"customer_type_ii", "opportunity_stage", "opportunities", "purpose_policy"},
    ),
    ("A", "预约与拜访方式", {"is_appointment", "visit_method"}),
    (
        "O/KR",
        "拜访目的与关键结果",
        {"purpose_code", "other_purpose", "expected_key_result"},
    ),
    ("R", "过程事实与结果", {"process_description", "customer_feedback"}),
    ("A", "达成评价", {"self_assessment", "deviation_reason"}),
    (
        "N",
        "下一步客户行动",
        {
            "next_action_target_id",
            "next_action_purpose",
            "next_action_other_purpose",
            "next_action_expected_result",
            "next_contact_at",
        },
    ),
)


def build_precheck_feedback(
    visit: VisitDraftInput,
    quality_score: int,
    status: str,
    issues: list[Issue],
    supplied_fields: set[str] | None,
    knowledge_references: list[KnowledgeReference] | None = None,
    semantic_review: SemanticReview | None = None,
    taoran_sections: list[TaoranSectionCheck] | None = None,
) -> str:
    # `quality_score` is retained as an internal diagnostic used to select the
    # advice status. It must never be displayed as a pre-submit business score.
    del quality_score
    status_text = {
        "passed": "已检查字段未发现明显规范问题",
        "needs_revision": "存在需要优先完善的内容",
        "review": "本次检测尚未完成，请核对字段传递或系统提示",
    }[status]
    lines = [
        "【提交前TAORAN检查｜AI反馈意见】",
        f"检查结论：{status_text}",
    ]
    # Knowledge/model provenance remains in structured results and audit, not display text.
    lines.extend(["", "TAORAN六项检查："])
    section_results = {section.name: section for section in taoran_sections or []}
    for index, (code, name, fields) in enumerate(_SECTIONS):
        if index:
            lines.append("")
        section_issues = _precheck_issues_for_section(name, fields, issues)
        failed_issues = [issue for issue in section_issues if issue.severity != Severity.INFO]
        if failed_issues:
            analysis = _failed_section_analysis(name, failed_issues)
        else:
            section = section_results.get(name)
            analysis = _section_standard_and_status(name, section.status if section else None)
        section = section_results.get(name)
        if section and section.unreceived_fields:
            missing_labels = _unique(display_field_name(path) for path in section.unreceived_fields)
            analysis += "\n接口未获取：" + "、".join(f"“{label}”" for label in missing_labels) + "。"
            analysis += "请管理员核对本次字段传递，不能据此认定用户未填写。"
        lines.append(f"{code}｜{name}：" + analysis)
        notices = [issue.message for issue in section_issues if issue.severity == Severity.INFO]
        if notices:
            lines.append("说明：" + _join_sentences(_unique(notices)))
    suggestions = _unique(issue.suggestion for issue in issues)
    if suggestions:
        lines.extend(["", "优先修改建议："])
        lines.extend(f"{index}. {suggestion}" for index, suggestion in enumerate(suggestions, 1))
    elif any(section.status != "met" for section in taoran_sections or []):
        lines.extend(["", "优先修改建议：请先补齐字段传递并重新检测，暂不能给出完整结论。"])
    else:
        lines.extend(["", "优先修改建议：当前未发现需要优先补充的规范性问题。"])
    lines.append("提交成功后，系统将自动进行深度评价并回写正式评分与反馈意见。")
    return "\n".join(lines)


def build_model_precheck_feedback(
    mode: FeedbackMode,
    status: str,
    issues: list[Issue],
    semantic_review: SemanticReview,
) -> str:
    """将纯AI或知识库模型结果单独呈现，不混入规则结论。"""
    if mode not in {FeedbackMode.AI, FeedbackMode.KNOWLEDGE}:
        raise ValueError("模型反馈生成器仅支持ai和knowledge模式")
    title = "纯AI反馈" if mode == FeedbackMode.AI else "知识库反馈"
    status_text = {
        "passed": "六项分析已完成，未发现明显规范问题",
        "needs_revision": "六项分析已完成，存在需要优先完善的内容",
        "review": "本次分析尚未完整完成，请核对系统提示后重试",
    }[status]
    lines = [
        f"【提交前TAORAN检查｜{title}】",
        f"检查结论：{status_text}",
        "",
        "TAORAN六项分析：",
    ]
    analyses = {section.code: section for section in semantic_review.sections}
    for index, (display_code, name, _) in enumerate(_SECTIONS):
        if index:
            lines.append("")
        model_code = {
            "客户类型": "T",
            "预约与拜访方式": "A1",
            "拜访目的与关键结果": "O_KR",
            "过程事实与结果": "R",
            "达成评价": "A2",
            "下一步客户行动": "N",
        }[name]
        analysis = analyses.get(model_code)
        if semantic_review.status != "completed" or analysis is None:
            lines.append(f"{display_code}｜{name}：未完成。")
            continue
        verdict = {
            "met": "达标。",
            "needs_revision": "待改进。",
            "not_evaluated": "未检查。",
        }[analysis.verdict]
        lines.append(f"{display_code}｜{name}：{verdict}")
        lines.append("分析：" + analysis.reason)
        if analysis.evidence:
            evidence = "；".join(
                f"“{display_field_name(item.field)}”：{item.quote}"
                for item in analysis.evidence
            )
            lines.append("输入依据：" + evidence)
        if analysis.suggestion:
            lines.append("修改建议：" + analysis.suggestion)
    suggestions = _unique(issue.suggestion for issue in issues)
    if suggestions:
        lines.extend(["", "优先修改建议："])
        lines.extend(f"{index}. {suggestion}" for index, suggestion in enumerate(suggestions, 1))
    elif semantic_review.status == "completed":
        lines.extend(["", "优先修改建议：当前未发现需要优先补充的内容。"])
    lines.append("本次只提供提交前建议，不阻断提交，不生成正式分数。")
    return "\n".join(lines)


def build_evaluation_feedback(
    visit: VisitDraftInput,
    q33_score: float,
    q34_score: float,
    total_score: float,
    issues: list[Issue],
    semantic_facts: Q34SemanticFacts,
) -> str:
    lines = [
        "【提交后TAORAN深度评价｜AI反馈意见】",
        f"综合得分：{total_score:.2f}/100（Q33 {q33_score:.2f}/50；Q34 {q34_score:.2f}/50）",
        f"综合结论：{_evaluation_conclusion(total_score)}",
        "",
        "TAORAN六项判断：",
    ]
    if semantic_facts.provider.startswith("llm-") and semantic_facts.status != "completed":
        lines.insert(1, "模型分析未完成：以下仅为本地参考结果，暂停正式评分回写，需重试复核。")
    for code, name, fields in _SECTIONS:
        section_issues = _issues_for_fields(issues, fields)
        lines.append(
            f"{code}｜{name}："
            + _evaluation_section_text(visit, name, section_issues, semantic_facts)
        )
        model_code = {
            "客户类型": "T", "预约与拜访方式": "A1", "拜访目的与关键结果": "O_KR",
            "过程事实与结果": "R", "达成评价": "A2", "下一步客户行动": "N",
        }[name]
        analysis = next((s for s in semantic_facts.sections if s.code == model_code), None)
        if analysis:
            lines.append("  模型分析：" + analysis.reason)
    if semantic_facts.provider == "llm-chat":
        lines.extend(["", "模型事实依据：" + semantic_facts.reason])
    suggestions = _unique(issue.suggestion for issue in issues)
    if suggestions:
        lines.extend(["", "优先改进建议："])
        lines.extend(f"{index}. {suggestion}" for index, suggestion in enumerate(suggestions, 1))
    else:
        lines.extend(["", "优先改进建议：当前记录未发现明显的TAORAN规范问题。"])
    return "\n".join(lines)


def _precheck_standard(name: str) -> str:
    """已审核知识与现行前检规则的展示摘要，不参与判断或评分。"""
    field = display_field_name
    standards = {
        "客户类型": (
            f"“{field('customer_type_ii')}”应明确；商机客户应有可核验的"
            f"“{field('opportunity_stage')}”，拜访目的应与客户类型及商机阶段匹配。"
        ),
        "预约与拜访方式": (
            f"如实填写“{field('is_appointment')}”和“{field('visit_method')}”；"
            "目标客户和商机客户应优先预约，未预约应说明原因，拜访方式应支持本次目的。"
        ),
        "拜访目的与关键结果": (
            f"“{field('purpose_code')}”应明确，选择其他时填写“{field('other_purpose')}”；"
            f"“{field('expected_key_result')}”应具体、可验证，写明客户确认、条件、承诺、"
            "时间或交付物，不能只写“了解一下”“沟通一下”等空泛表述。"
        ),
        "过程事实与结果": (
            f"“{field('process_description')}”应记录客户角色及可核验的确认事项、"
            "条件、异议、变化或承诺；客户事实、个人判断和假设应分开表达，不能只写感受。"
        ),
        "达成评价": (
            f"“{field('self_assessment')}”应围绕“{field('expected_key_result')}”，"
            f"并与“{field('process_description')}”中的客户事实一致；"
            "缺少达成证据时不能评价为“达到目的”。"
        ),
        "下一步客户行动": (
            f"“{field('next_contact_at')}”应晚于“{field('visit_date')}”"
            "（按北京时间自然日比较，同日不算晚于）；下一步应明确对象、目的及期望结果，"
            "并承接本次客户事实。"
        ),
    }
    return standards[name]


def _failed_section_analysis(name: str, issues: list[Issue]) -> str:
    reasons = _unique(issue.message for issue in issues)
    suggestions = _unique(issue.suggestion for issue in issues)
    parts = [
        "未达标。",
        "检查标准：" + _precheck_standard(name),
        "数据分析：" + _join_sentences(reasons),
    ]
    if suggestions:
        parts.append("修改建议：" + _join_sentences(suggestions))
    return "\n".join(parts)


def _section_standard_and_status(name: str, status: str | None) -> str:
    # Absence of issues is not proof of passing: use the engine's actual coverage.
    states = {
        "met": ("达标。", None),
        "not_received": (
            "未检查。",
            "本次AI检测未获取本项相关字段，暂不判定是否达标。",
        ),
        "partial_input": (
            "待复核。",
            "本次仅获取部分相关字段，已检查内容未发现问题，尚不能判定整项达标。",
        ),
        "needs_revision": ("未达标。", "本项规则检查未通过，请核对相关内容。"),
    }
    label, explanation = states.get(status, ("待复核。", "未获得完整检查结果，暂不判定是否达标。"))
    lines = [label, "检查标准：" + _precheck_standard(name)]
    if explanation:
        lines.append("检查说明：" + explanation)
    return "\n".join(lines)


def _precheck_issues_for_section(
    name: str,
    fields: set[str],
    issues: list[Issue],
) -> list[Issue]:
    prefixes = {
        "客户类型": ("TAORAN_TYPE_", "TAORAN_OPPORTUNITY_", "LLM_T_"),
        "预约与拜访方式": ("TAORAN_APPOINTMENT_", "TAORAN_VISIT_METHOD_", "LLM_A1_"),
        "拜访目的与关键结果": ("TAORAN_OBJECTIVE_", "TAORAN_KR_", "KR_", "LLM_O_KR_"),
        "过程事实与结果": ("TAORAN_RESULT_", "TAORAN_FACT_", "RESULT_", "LLM_R_"),
        "达成评价": ("TAORAN_ASSESSMENT_", "LLM_A2_"),
        "下一步客户行动": ("TAORAN_NSA_", "NEXT_ACTION_", "LLM_N_"),
    }
    known_prefixes = tuple(prefix for values in prefixes.values() for prefix in values)
    matched: list[Issue] = []
    for issue in issues:
        if issue.code.startswith(prefixes[name]):
            matched.append(issue)
            continue
        if issue.code.startswith(known_prefixes):
            continue
        if any(
            field in fields or field.split("[].", 1)[0] in fields
            for field in issue.field_paths
        ):
            matched.append(issue)
    return matched


def _join_sentences(values: list[str]) -> str:
    cleaned = [value.rstrip("；。 ") for value in values if value.rstrip("；。 ")]
    return "；".join(cleaned) + ("。" if cleaned else "")


def _evaluation_section_text(
    visit: VisitDraftInput,
    name: str,
    issues: list[Issue],
    semantic_facts: Q34SemanticFacts,
) -> str:
    model_code = {
        "客户类型": "T", "预约与拜访方式": "A1", "拜访目的与关键结果": "O_KR",
        "过程事实与结果": "R", "达成评价": "A2", "下一步客户行动": "N",
    }[name]
    analysis = next((s for s in semantic_facts.sections if s.code == model_code), None)
    if semantic_facts.provider.startswith("llm-") and (
        semantic_facts.status != "completed" or not analysis
        or analysis.verdict == "not_evaluated"
    ):
        return "本项检测未完成，暂不判定达标；请完成分析后重新核验。"
    if issues:
        return "待改进。" + "；".join(_unique(issue.suggestion for issue in issues))
    if name == "客户类型":
        return "客户类型和商机阶段信息满足当前规则要求。"
    if name == "预约与拜访方式":
        return "预约状态和拜访方式与当前客户类型及拜访目的相符。"
    if name == "拜访目的与关键结果":
        return (
            "拜访目标与关键结果具体、可验证。"
            if semantic_facts.key_result_quality_ok
            else "关键结果缺少具体、可验证的客户事实。"
        )
    if name == "过程事实与结果":
        return (
            "过程记录包含可核验的客户事实、确认事项或条件。"
            if semantic_facts.process_fact_based
            else "过程记录需要补充客户角色、确认事项、条件、异议或承诺。"
        )
    if name == "达成评价":
        return (
            "达成评价与系统识别的关键结果完成情况一致。"
            if visit.self_assessment == semantic_facts.purpose_achievement
            else "达成评价与关键结果及过程事实不一致，需要校准。"
        )
    return (
        "下一步客户行动与本次拜访结果衔接，具备继续执行条件。"
        if semantic_facts.next_action_logic_ok
        else "下一步客户行动与本次结果衔接不足，需要明确时间、对象、目的和期望结果。"
    )


def _issues_for_fields(issues: list[Issue], fields: set[str]) -> list[Issue]:
    return [
        issue
        for issue in issues
        if any(
            field in fields or field.split("[].", 1)[0] in fields
            for field in issue.field_paths
        )
    ]


def _evaluation_conclusion(total_score: float) -> str:
    if total_score >= 170:
        return "高质量拜访记录，TAORAN关键闭环较完整。"
    if total_score >= 140:
        return "记录基本有效，仍有少量关键内容需要完善。"
    if total_score >= 100:
        return "记录存在明显缺口，建议经理复核后针对性改进。"
    return "有效性证据不足，需要补充事实并重新核验。"


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
