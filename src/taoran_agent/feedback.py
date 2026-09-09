from __future__ import annotations

import re
from collections.abc import Iterable

from .business_wording import business_wording
from .field_labels import display_field_name, display_form_field_name
from .models import (
    FrontVisitAnalysisSection,
    Issue,
    KnowledgeReference,
    KnowledgeWordingResult,
    ModelSectionAnalysis,
    PrecheckResponse,
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
            "customer_id",
            "customer_type_ii",
            "next_action_purpose",
            "next_action_other_purpose",
            "next_action_expected_result",
            "next_contact_at",
            "visit_date",
            "process_description",
            "customer_feedback",
        },
    ),
)

# 提交后用六项结构归类、去重建议；展示时不输出维度、字段编号或固定占位项。
_POST_ADVICE_SECTIONS = (
    (
        "客户类型",
        ("客户分类II", "最新商机阶段"),
    ),
    (
        "预约与拜访方式",
        ("是否预约", "拜访方式"),
    ),
    (
        "拜访目的与关键结果",
        ("拜访目的", "具体其他目的", "想取得的关键结果"),
    ),
    (
        "过程事实与结果",
        ("过程详细描述", "客户反馈"),
    ),
    (
        "达成评价",
        ("评价", "偏差原因"),
    ),
    (
        "下一步客户行动",
        ("下一次行动目的", "下一次具体其他目的", "下次拜访期望的关键结果", "下一次联系客户时间安排"),
    ),
)

_POST_ADVICE_KEYWORDS = {
    "客户类型": ("客户分类", "商机", "阶段"),
    "预约与拜访方式": ("预约", "拜访方式", "视频"),
    "拜访目的与关键结果": ("拜访目的", "关键结果", "交付物", "目的映射"),
    "过程事实与结果": ("过程", "客户事实", "客户角色", "异议", "条件", "承诺"),
    "达成评价": ("评价", "达成", "偏差"),
    "下一步客户行动": ("下一次", "下次", "联系", "行动", "跟进"),
}

_FAILURE_REASON_LABELS = {
    "post_semantic_audit_unavailable": "独立事实复核未完成，已保留失败详情，请重新分析；本次未发布正式结果",
    "timeout": "大模型调用超时",
    "authentication_failed": "大模型鉴权失败",
    "access_denied": "大模型访问被拒绝",
    "rate_limited": "大模型服务限流",
    "provider_http_error": "大模型服务返回异常",
    "invalid_contract": "大模型返回格式不符合约定",
    "invalid_json": "大模型返回内容无法解析",
    "invalid_response_or_network_error": "大模型响应或网络异常",
    "required_analysis_not_completed": "大模型未完成六项分析",
    "section_fact_conflict": "大模型分析结果存在矛盾",
    "assessment_fact_conflict": "达成评价与模型识别事实存在矛盾",
    "model_not_configured": "TAORAN专用大模型尚未配置",
    "feedback_mode_not_supported": "当前反馈模式不支持大模型分析",
    "busy": "大模型服务繁忙",
    "queue_full": "当前同时检测人数已达到上限",
    "queue_timeout": "等待大模型处理超时",
    "unavailable": "大模型服务不可用",
    "input_too_large": "本次输入内容超过大模型处理上限",
    "output_too_large": "大模型返回内容超过处理上限",
    "incomplete_or_refused": "大模型未返回完整分析结果",
    "invalid_content": "大模型返回内容格式异常",
}

_FRONT_HIDDEN_FIELDS = {
    "metadata",
    "purpose_policy",
    "source_record_id",
    "submitted_at",
    "next_action_target_id",
}

_FRONT_MISSING_FIELDS = {
    "TAORAN_TYPE_MISSING": ("customer_type_ii",),
    "TAORAN_OPPORTUNITY_STAGE_MISSING": ("opportunities[].current_stage",),
    "TAORAN_APPOINTMENT_MISSING": ("is_appointment",),
    "TAORAN_VISIT_METHOD_MISSING": ("visit_method",),
    "TAORAN_OBJECTIVE_MISSING": ("purpose_code",),
    "TAORAN_KR_MISSING": ("expected_key_result",),
    "TAORAN_RESULT_MISSING": ("process_description",),
    "TAORAN_ASSESSMENT_MISSING": ("self_assessment",),
    "TAORAN_NSA_CUSTOMER_MISSING": ("customer_id",),
    "TAORAN_NSA_TIME_MISSING": ("next_contact_at",),
    "TAORAN_NSA_PURPOSE_MISSING": ("next_action_purpose",),
    "TAORAN_NSA_OTHER_PURPOSE_MISSING": ("next_action_other_purpose",),
    "TAORAN_NSA_RESULT_MISSING": ("next_action_expected_result",),
}

_FRONT_SPECIFICITY_CHECKS = (
    (
        "O_KR",
        "想取得的关键结果不具体",
        "拜访目的与关键结果",
        "expected_key_result",
        {
            "TAORAN_KR_MISSING",
            "TAORAN_KR_NOT_VERIFIABLE",
            "KR_SEMANTICALLY_VAGUE",
        },
        {"TAORAN_KR_MISSING"},
    ),
    (
        "R",
        "过程详细描述不具体",
        "过程事实与结果",
        "process_description",
        {
            "TAORAN_RESULT_MISSING",
            "TAORAN_RESULT_NOT_FACT_BASED",
            "TAORAN_FACT_JUDGMENT_MIXED",
            "RESULT_LACKS_CUSTOMER_FACTS",
        },
        {"TAORAN_RESULT_MISSING"},
    ),
    (
        "N",
        "下次拜访期望的关键结果不具体",
        "下一步客户行动",
        "next_action_expected_result",
        {
            "TAORAN_NSA_RESULT_MISSING",
            "TAORAN_NSA_RESULT_NOT_ACTIONABLE",
            "NEXT_ACTION_NOT_QUALIFIED",
        },
        {"TAORAN_NSA_RESULT_MISSING"},
    ),
)


def _failure_reason_text(reason: str | None, fallback: str) -> str:
    if not reason:
        return fallback
    return _FAILURE_REASON_LABELS.get(reason, "模型服务返回未分类异常")


def _ai_exception(reason: str, suggestion: str) -> str:
    return f"AI调用异常。异常原因：{reason}。处理建议：{suggestion}"


def build_precheck_feedback(
    visit: VisitDraftInput,
    quality_score: int,
    status: str,
    issues: list[Issue],
    supplied_fields: set[str] | None,
    knowledge_references: list[KnowledgeReference] | None = None,
    semantic_review: SemanticReview | None = None,
    taoran_sections: list[TaoranSectionCheck] | None = None,
    title: str = "AI反馈意见",
    review_status_text: str = "AI调用异常，请根据异常原因处理后重新检测",
) -> str:
    # `quality_score` is retained as an internal diagnostic used to select the
    # advice status. It must never be displayed as a pre-submit business score.
    del quality_score
    has_system_error = taoran_sections is None or any(
        issue.source == "system" and issue.severity != Severity.INFO for issue in issues
    ) or any(section.unreceived_fields for section in taoran_sections or [])
    status_text = {
        "passed": "已检查字段未发现明显规范问题",
        "needs_revision": "存在需要优先完善的内容",
        "review": review_status_text,
    }[status]
    if has_system_error and status == "needs_revision":
        status_text += "；另有AI调用异常需要处理"
    lines = [
        f"【提交前TAORAN检查｜{title}】",
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
        business_issues = [issue for issue in failed_issues if issue.source != "system"]
        system_issues = [issue for issue in section_issues if issue.source == "system"]
        if business_issues:
            analysis = _failed_section_analysis(name, business_issues)
        else:
            section = section_results.get(name)
            analysis = _section_standard_and_status(name, section.status if section else None)
        section = section_results.get(name)
        if section and section.unreceived_fields:
            missing_labels = _unique(display_field_name(path) for path in section.unreceived_fields)
            reason = "系统未获取" + "、".join(f"“{label}”" for label in missing_labels)
            exception = _ai_exception(reason, "请管理员核对字段绑定与传递配置后重新检测")
            analysis = analysis + "\n" + exception if business_issues else (
                exception + "\n检查标准：" + _precheck_standard(name)
            )
        elif system_issues:
            reason = _join_sentences(_unique(issue.message for issue in system_issues)).rstrip("。")
            reason = reason.removeprefix("AI调用异常：").removeprefix("AI调用异常。").strip()
            advice = _join_sentences(_unique(issue.suggestion for issue in system_issues)).rstrip("。")
            exception = _ai_exception(reason, advice or "请管理员核对系统配置后重新检测")
            analysis = analysis + "\n" + exception if business_issues else (
                exception + "\n检查标准：" + _precheck_standard(name)
            )
        lines.append(f"{code}｜{name}：" + analysis)
        notices = [issue.message for issue in section_issues if issue.severity == Severity.INFO]
        if notices:
            lines.append("说明：" + _join_sentences(_unique(notices)))
    global_system_issues = [
        issue for issue in issues
        if issue.source == "system" and issue.severity != Severity.INFO
    ]
    if global_system_issues:
        lines.extend(["", "系统异常："])
        for issue in global_system_issues:
            reason = issue.message.removeprefix("AI调用异常：").rstrip("。")
            lines.append(_ai_exception(reason, issue.suggestion.rstrip("。")))
    suggestions = _unique(issue.suggestion for issue in issues if issue.source != "system")
    if suggestions:
        lines.extend(["", "优先修改建议："])
        lines.extend(f"{index}. {suggestion}" for index, suggestion in enumerate(suggestions, 1))
    elif any(section.status != "met" for section in taoran_sections or []):
        lines.extend(["", "优先修改建议：请管理员根据上述异常原因修复字段传递或调用配置后重新检测。"])
    else:
        lines.extend(["", "优先修改建议：当前未发现需要优先补充的规范性问题。"])
    lines.append("提交成功后，系统将自动进行深度评价并回写正式评分与反馈意见。")
    return business_wording("\n".join(lines))


def build_front_ai_suggestions_with_model(
    structured: PrecheckResponse,
    wording: KnowledgeWordingResult,
    *,
    experimental: bool = False,
) -> str:
    """Build salesperson-facing suggestions from structured checks and model wording."""
    if wording.status != "completed":
        return _front_ai_suggestions(
            structured,
            # Model wording is optional. The deterministic checks already hold
            # field-specific findings from this exact record, so return those
            # instead of a fixed "analysis not completed" sentence.
            ai_only=False,
            analysis_completed=False,
        )
    natural = {item.code: item for item in wording.items}
    model_code_by_name = {
        "客户类型": "T",
        "预约与拜访方式": "A1",
        "拜访目的与关键结果": "O_KR",
        "过程事实与结果": "R",
        "达成评价": "A2",
        "下一步客户行动": "N",
    }
    natural_by_section = {
        name: natural[code].suggestion
        for name, code in model_code_by_name.items()
        if code in natural and natural[code].specific is False
    }
    specificity_by_section = {
        name: natural[code].specific
        for name, code in model_code_by_name.items()
        if code in natural and natural[code].specific is not None
    }
    advice_labels = {code: name for name, code in model_code_by_name.items()}
    if experimental and any(repair.get("kind") == "goal_reminder_relocated" and repair.get("code") == "R"
                            for attempt in wording.model_attempts for repair in attempt.get("recommendation_repairs", [])):
        advice_labels["R"] = "目标核对建议"
    result = _front_ai_suggestions(
        structured,
        natural_by_section=natural_by_section,
        explicit_advice=[(advice_labels.get(item.code, "填写核对"), item.suggestion)
                        for item in wording.items if item.code != "C" and item.suggestion.strip()] if experimental else None,
        specificity_by_section=specificity_by_section,
        natural_completion=("\n".join(item.suggestion for item in wording.items if item.code == "C")
                            if experimental else natural.get("C").suggestion if natural.get("C") else ""),
        visit_analysis=wording.visit_analysis,
        visit_analysis_sections=[] if experimental else wording.visit_analysis_sections,
        recommendation_labels={"过程事实与结果": "目标核对建议"} if experimental and any(
            repair.get("kind")=="goal_reminder_relocated" and repair.get("code")=="R"
            for attempt in wording.model_attempts for repair in attempt.get("recommendation_repairs", [])
        ) else {},
        ai_only=True,
        experimental=experimental,
    )

    if experimental and wording.suggestion_status:
        empty_message = "本次未生成逐项填写建议；目标达成判断请见上方分析。"
        if wording.suggestion_status == "no_change_needed":
            result = result.replace(empty_message, "本次无需额外补充填写。" + _clean_front_text(wording.suggestion_reason))
        elif wording.suggestion_status == "needs_confirmation":
            result = result.replace(empty_message, "请核对下方需确认事项。")
        elif wording.suggestion_status == "incomplete":
            notice = "填写建议完整性核对未完成，不能据此认定无需补充。"
            if empty_message in result:
                result = result.replace(empty_message, notice)
            else:
                result = result.replace("智能填写建议：", "智能填写建议：\n" + notice)
    if wording.confirmation_items:
        footer = "提交后，系统将自动生成正式评分和反馈意见。"
        body = "需确认事项：\n" + "\n".join(f"{i}. {text}" for i, text in enumerate(wording.confirmation_items, 1))
        result = result.replace(footer, body + "\n\n" + footer) if footer in result else result + "\n\n" + body
    return business_wording(result)


def build_front_ai_suggestions(
    structured: PrecheckResponse,
    *,
    system_notice: str | None = None,
) -> str:
    """Format a unified button fallback using business labels only."""
    return _front_ai_suggestions(structured, system_notice=system_notice)


def _front_ai_suggestions(
    structured: PrecheckResponse,
    *,
    natural_by_section: dict[str, str] | None = None,
    explicit_advice: list[tuple[str, str]] | None = None,
    recommendation_labels: dict[str, str] | None = None,
    specificity_by_section: dict[str, bool] | None = None,
    system_notice: str | None = None,
    natural_completion: str = "",
    visit_analysis: str = "",
    visit_analysis_sections: list[FrontVisitAnalysisSection] | None = None,
    ai_only: bool = False,
    analysis_completed: bool = True,
    experimental: bool = False,
) -> str:
    # Local selection: concurrent official requests retain their original cleaner.
    _clean_front_text = _clean_experimental_front_text if experimental else globals()["_clean_front_text"]
    natural_by_section = natural_by_section or {}
    specificity_by_section = specificity_by_section or {}
    system_advice = []
    for issue in structured.issues:
        if issue.source != "system" or issue.severity == Severity.INFO:
            continue
        labels = _unique(
            display_form_field_name(path)
            for path in issue.field_paths
            if path not in _FRONT_HIDDEN_FIELDS
            and display_form_field_name(path)
        )
        label_text = "、".join(f"“{label}”" for label in labels)
        suggestion = _clean_front_text(issue.suggestion)
        text = (
            f"系统未正确获取{label_text}，{suggestion}"
            if label_text
            else suggestion
        )
        if text and text not in system_advice:
            system_advice.append(text)
    for section in structured.taoran_sections:
        labels = _unique(
            display_form_field_name(path)
            for path in section.unreceived_fields
            if display_form_field_name(path)
        )
        if labels:
            system_advice.append(
                "系统未正确获取"
                + "、".join(f"“{label}”" for label in labels)
                + "，请管理员检查字段绑定后重新检测。"
            )

    unreceived = {
        path
        for section in structured.taoran_sections
        for path in section.unreceived_fields
    }
    if structured.field_completion:
        missing_paths = [
            path
            for path, completed in structured.field_completion.items()
            if not completed
            and path not in unreceived
            and path.split("[].", 1)[0] not in unreceived
        ]
    else:
        missing_paths = [
            path
            for issue in structured.issues
            if issue.source != "system" and issue.severity != Severity.INFO
            for path in _FRONT_MISSING_FIELDS.get(issue.code, ())
        ]
    missing_labels = _unique(
        display_form_field_name(path)
        for path in missing_paths
        if display_form_field_name(path)
    )
    advice: list[str] = []
    natural_completion = _clean_front_text(natural_completion)
    if ai_only and natural_completion:
        advice.append(natural_completion)
    elif missing_labels and not ai_only:
        advice.append(
            "相关字段未填写：“"
            + "”、“".join(missing_labels)
            + "”。请根据实际拜访情况补充。"
        )

    if explicit_advice is not None:
        advice.extend(f"{label}：{_clean_front_text(text)}" for label, text in explicit_advice if _clean_front_text(text))
    checks = _FRONT_SPECIFICITY_CHECKS if explicit_advice is None else ()
    for _, label, section_name, field, issue_codes, missing_codes in checks:
        field_unreceived = (
            field in unreceived or field.split("[].", 1)[0] in unreceived
        )
        relevant = [
            issue for issue in structured.issues
            if issue.source != "system"
            and issue.severity != Severity.INFO
            and issue.code in issue_codes
            and field in issue.field_paths
        ]
        physically_completed = structured.field_completion.get(field)
        # 接口漏传由系统提示处理；空值已在“字段未填写”合并提示。
        # 只对“已填写但不具体”的内容输出单独改善意见。
        semantic_specific = specificity_by_section.get(section_name)
        if ai_only:
            natural = _clean_front_text(natural_by_section.get(section_name, ""))
            if semantic_specific is False and natural:
                advice.append(f"{(recommendation_labels or {}).get(section_name, label)}：{natural}")
            continue
        if (
            field_unreceived
            or physically_completed is False
            or semantic_specific is True
            or (semantic_specific is None and not relevant)
        ):
            continue
        natural = _clean_front_text(natural_by_section.get(section_name, ""))
        if not natural:
            visible_problems = [
                issue.message
                for issue in relevant
                if not (
                    physically_completed is True
                    and issue.code in missing_codes
                )
            ]
            problem = _clean_front_text(_join_sentences(_unique(visible_problems)))
            if not problem:
                problem = "已填写，但内容较空泛，尚未形成可核验的具体信息。"
            suggestion = _clean_front_text(
                next((i.suggestion for i in relevant if i.suggestion), "")
            )
            natural = problem + suggestion
        advice.append(f"{label}：{natural}")
    lines = []
    rendered_sections = []
    section_labels = {
        "visit_context": "拜访概况",
        "objective_result": "本次结果",
        "assessment": "达成判断",
        "next_step": "下一步安排",
    }
    for section in visit_analysis_sections or []:
        section_text = _clean_front_text(section.text)
        if section_text:
            rendered_sections.append(f"{section_labels[section.kind]}：{section_text}")
    visit_analysis = _clean_front_text(visit_analysis)
    if rendered_sections or visit_analysis:
        lines.extend(["", "本次拜访分析："])
        lines.extend(rendered_sections or [visit_analysis])
        lines.extend(["", "智能填写建议："])
    if advice:
        lines.extend(f"{index}、{item}" for index, item in enumerate(advice, 1))
    elif not analysis_completed:
        lines.append("本次智能分析未完成，请重新点击检测按钮。")
    elif ai_only and experimental:
        lines.append("本次未生成逐项填写建议；目标达成判断请见上方分析。")
    elif ai_only:
        lines.append("AI结合本次填写内容分析后，未发现需要改善的内容。")
    else:
        lines.append("当前填写内容在字段完整性及三个具体性维度中未发现需要改善的内容。")
    if system_notice:
        system_advice.insert(0, _clean_front_text(system_notice))
    if system_advice:
        lines.append("")
        notice = "；".join(_unique(system_advice)).rstrip("，。；： ")
        lines.append("系统提示：" + notice + "。")
    lines.append("提交后，系统将自动生成正式评分和反馈意见。")
    return business_wording("\n".join(lines))


def _clean_experimental_front_text(value: str) -> str:
    """Experimental: preserve business tokens; translate only known field paths."""
    text = business_wording(value)
    def replace_field(match: re.Match[str]) -> str:
        field = match.group(0)
        return display_form_field_name(field) or field
    text = re.sub(r"[a-z][a-z0-9_]*(?:\[\]\.[a-z][a-z0-9_]*)?", replace_field, text)
    text = re.sub(r"\bQ(?:33|34)_[A-Z0-9_]+\b", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _clean_front_text(value: str) -> str:
    """Remove internal identifiers while preserving Chinese business wording."""
    text = business_wording(_normalize_opportunity_stage_wording(value or ""))
    text = re.sub(r"'([^'\n]{1,100})'", r"“\1”", text)
    text = re.sub(
        r"(?:原文|填写内容(?:为|是)?)\s*(“[^”\n]{1,120}”)",
        r"\1",
        text,
    )
    # Model-created examples may look helpful but are not visit facts. Remove
    # them so salesperson-facing advice remains grounded only in submitted data.
    text = re.sub(r"[，,；;]\s*(?:例如|如)“[^”\n]{1,100}”", "", text)
    canonical_fields = set(
        re.findall(r"[a-z][a-z0-9_]*(?:\[\]\.[a-z][a-z0-9_]*)?", text)
    )
    for field in sorted(canonical_fields, key=len, reverse=True):
        label = display_form_field_name(field)
        text = text.replace(
            field,
            label
            if field not in _FRONT_HIDDEN_FIELDS and label
            else "",
        )
    replacements = {
        "partially_achieved": "部分达到目的",
        "not_achieved": "未达到目的",
        "achieved": "达到目的",
        "TAORAN": "拜访有效性",
        "SMART": "具体、可衡量、相关且有时限",
        "AI": "智能",
        "O/KR": "拜访目的与关键结果",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    # P1-P6 are official business codes and must survive the generic English
    # identifier scrub below. Protect them with Chinese-only placeholders first.
    stage_placeholders = {
        "1": "商机阶段代号壹",
        "2": "商机阶段代号贰",
        "3": "商机阶段代号叁",
        "4": "商机阶段代号肆",
        "5": "商机阶段代号伍",
        "6": "商机阶段代号陆",
    }
    text = re.sub(
        r"P([1-6])(?:阶段)?",
        lambda match: stage_placeholders[match.group(1)],
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?:对应商机阶段商机阶段|商机阶段对应商机阶段|对应商机阶段阶段)",
        "商机阶段待确认",
        text,
    )
    text = re.sub(r"[A-Za-z_][A-Za-z0-9_/-]*", "", text)
    for digit, placeholder in stage_placeholders.items():
        text = text.replace(placeholder, f"P{digit}阶段")
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"([，。；：])\1+", r"\1", text)
    return text.strip("，。；： ") + ("。" if text.strip("，。；： ") else "")


def _normalize_opportunity_stage_wording(value: str) -> str:
    """Keep official P1-P6 stage codes instead of Chinese ordinal wording."""
    chinese_digits = {
        "一": "1",
        "二": "2",
        "三": "3",
        "四": "4",
        "五": "5",
        "六": "6",
    }
    return re.sub(
        r"商机第([一二三四五六])阶段",
        lambda match: f"商机P{chinese_digits[match.group(1)]}阶段",
        value or "",
    )


def merge_evaluation_with_knowledge(
    visit: VisitDraftInput,
    q33_score: float,
    q34_score: float,
    total_score: float,
    issues: list[Issue],
    semantic_facts: Q34SemanticFacts,
    knowledge_check: PrecheckResponse,
) -> str:
    """将知识库补充建议合并进提交后唯一的AI改善建议。"""
    knowledge_suggestions = [
        suggestion.strip()
        for suggestion in knowledge_check.suggestions
        if suggestion.strip()
        and suggestion.strip() not in {
            issue.suggestion.strip() for issue in knowledge_check.issues if issue.suggestion.strip()
        }
    ]
    return build_evaluation_feedback(
        visit,
        q33_score,
        q34_score,
        total_score,
        issues,
        semantic_facts,
        knowledge_suggestions=knowledge_suggestions,
        knowledge_issues=knowledge_check.issues,
    )


def build_evaluation_feedback(
    visit: VisitDraftInput,
    q33_score: float,
    q34_score: float,
    total_score: float,
    issues: list[Issue],
    semantic_facts: Q34SemanticFacts,
    *,
    knowledge_suggestions: list[str] | None = None,
    knowledge_issues: list[Issue] | None = None,
) -> str:
    # 分数、六项规则明细继续作为结构化字段保存并回写评分；这里仅保留供销售
    # 代表阅读的本次分析和可执行改善建议。
    del visit, q33_score, q34_score, total_score
    lines = []
    required_model_sections = {"T", "A1", "O_KR", "R", "A2", "N"}
    completed_sections = {
        section.code
        for section in semantic_facts.sections
        if section.verdict != "not_evaluated"
    }
    model_completed = (
        semantic_facts.provider.startswith("llm-")
        and semantic_facts.status == "completed"
        and required_model_sections <= completed_sections
    )
    if semantic_facts.provider.startswith("llm-") and not model_completed:
        reason = (
            "模型对动作主体、完成状态或原定目标的解释与原文证据冲突，已暂停正式评分回写"
            if semantic_facts.failure_reason == "post_fact_grounding_conflict"
            else
            "反馈与确定性规则或记录证据不一致，未通过校验"
            if semantic_facts.failure_reason == "post_feedback_conflict"
            else
            "模型意见额外增加了公司未要求的填写条件，未通过校验"
            if semantic_facts.failure_reason == "unsupported_company_requirement"
            else _failure_reason_text(semantic_facts.failure_reason, "大模型未完成完整分析")
        )
        analysis_text = _ai_exception(
            reason,
            "请稍后重试；持续失败请联系管理员核对模型服务配置",
        )
    else:
        analysis_text = _normalize_opportunity_stage_wording(
            business_wording(semantic_facts.reason.strip()) or "本次拜访未形成可展示的分析结论。"
        )
    lines.extend(["", "本次拜访分析：" + analysis_text])
    if semantic_facts.provider.startswith("llm-") and not model_completed:
        # Do not present heuristic fallback advice as completed AI analysis.
        return business_wording("\n".join(lines))
    advice_items = _build_post_advice(
        issues,
        semantic_facts,
        model_completed=model_completed,
        knowledge_issues=knowledge_issues or [],
        knowledge_suggestions=knowledge_suggestions or [],
    )
    if advice_items:
        lines.extend(["", "AI改善建议："])
        lines.extend(f"{index}. {suggestion}" for index, suggestion in enumerate(advice_items, 1))
    result = "\n".join(lines)
    context = semantic_facts.quality_audit.get("authoritative_checks")
    if context:
        from .semantic_observation import final_feedback_observations
        semantic_facts.quality_audit["final_review"] = final_feedback_observations(
            analysis_text, advice_items, context,
        )
    return business_wording(result)


def _build_post_advice(
    issues: list[Issue],
    semantic_facts: Q34SemanticFacts,
    *,
    model_completed: bool,
    knowledge_issues: list[Issue],
    knowledge_suggestions: list[str],
) -> list[str]:
    """按TAORAN六项归类去重，只返回实际需要改善的建议。"""
    if model_completed and semantic_facts.quality_audit.get("authoritative_checks"):
        # V4: no generic rule/knowledge fallback for a successful model section.
        # Rules still determine scoring and remain in structured audit records.
        validated = semantic_facts.quality_audit.get("advice_basis", {})
        return _unique([_clean_post_text(s.suggestion) for s in semantic_facts.sections
                        if s.verdict == "needs_revision" and s.code in validated and s.suggestion.strip()])
    model_by_section = {section.code: section for section in semantic_facts.sections}
    model_codes = {
        "客户类型": "T",
        "预约与拜访方式": "A1",
        "拜访目的与关键结果": "O_KR",
        "过程事实与结果": "R",
        "达成评价": "A2",
        "下一步客户行动": "N",
    }
    unassigned_knowledge = list(knowledge_suggestions)
    results: list[str] = []
    for name, _ in _POST_ADVICE_SECTIONS:
        evaluation_section_issues = _post_issues_for_section(name, issues)
        knowledge_section_issues = _post_issues_for_section(name, knowledge_issues)
        model_section = model_by_section.get(model_codes[name])
        model_suggestion = (
            model_section.suggestion
            if model_completed and model_section and model_section.verdict == "needs_revision"
            else ""
        )
        # 模型已针对当前记录形成具体建议时，直接采用这条具体建议；规则和知识库
        # 继续参与判断与审计，但不再把同一行动拆成多句重复展示。模型未给出本项
        # 建议时，才用规则和知识库补足。
        source_issues = (
            []
            if model_suggestion
            else [*evaluation_section_issues, *knowledge_section_issues]
        )
        candidates = [
            issue.suggestion
            for issue in source_issues
            if issue.source != "system" and not issue.code.startswith("LLM_") and issue.suggestion
        ]
        if model_suggestion:
            candidates.insert(0, model_suggestion)
        matched_knowledge = [
            suggestion
            for suggestion in unassigned_knowledge
            if _knowledge_suggestion_matches_section(suggestion, name)
        ]
        if matched_knowledge and not model_suggestion:
            candidates.extend(matched_knowledge)
        if matched_knowledge:
            unassigned_knowledge = [
                suggestion for suggestion in unassigned_knowledge if suggestion not in matched_knowledge
            ]
        advice = _merge_similar_advice(candidates)
        if advice:
            results.append(advice)

    # 无字段归属的知识文本不随机拼入某一维度；它仍保留在结构化审计中，避免误导销售。
    return results


def _post_issues_for_section(section_name: str, issues: list[Issue]) -> list[Issue]:
    """按规则维度归类，避免共用字段使建议落入多个维度。"""
    dimensions = {
        "客户类型": {"T"},
        "预约与拜访方式": {"A1"},
        "拜访目的与关键结果": {"O", "O_KR"},
        "过程事实与结果": {"R"},
        "达成评价": {"A2"},
        "下一步客户行动": {"N"},
    }[section_name]
    q34_codes = {
        "拜访目的与关键结果": {"Q34_KEY_RESULT_QUALITY_NOT_MET"},
        "达成评价": {"Q34_SELF_EVALUATION_INCONSISTENT"},
        "下一步客户行动": {
            "Q33_REQUIRED_FIELD_MISSING",
            "Q34_NEXT_ACTION_NOT_QUALIFIED",
        },
    }.get(section_name, set())
    fields = next(fields for _, name, fields in _SECTIONS if name == section_name)
    return [
        issue
        for issue in issues
        if issue.dimension in dimensions
        or issue.code in q34_codes
        or (
            issue.code.startswith(("TAORAN_", "KR_", "RESULT_", "NEXT_ACTION_"))
            and issue in _precheck_issues_for_section(section_name, set(fields), [issue])
        )
    ]


def _knowledge_suggestion_matches_section(suggestion: str, section_name: str) -> bool:
    return any(keyword in suggestion for keyword in _POST_ADVICE_KEYWORDS[section_name])


def _merge_similar_advice(candidates: list[str]) -> str:
    cleaned = [_clean_post_text(item) for item in candidates if _clean_post_text(item)]
    merged: list[str] = []
    for suggestion in cleaned:
        if any(_advice_is_similar(suggestion, kept) for kept in merged):
            continue
        merged.append(suggestion)
    return "；".join(merged).rstrip("；。") + ("。" if merged else "")


def _clean_post_text(value: str) -> str:
    """Post-only rendering: preserve product names/units, translate known keys."""
    text = business_wording(_normalize_opportunity_stage_wording(value or ""))
    def translate(match: re.Match[str]) -> str:
        token = match.group(0)
        if token.lower() in {"true", "false"}:
            return {"true": "是", "false": "否"}[token.lower()]
        return display_form_field_name(token) or token
    text = re.sub(r"[A-Za-z_][A-Za-z0-9_]*(?:\[\]\.[A-Za-z_][A-Za-z0-9_]*)?", translate, text)
    text = re.sub(r"\bQ(?:33|34)_[A-Z0-9_]+\b", "", text)
    text = re.sub(r"\s+", " ", text).strip().rstrip("；。")
    return text + ("。" if text else "")


def _advice_is_similar(left: str, right: str) -> bool:
    left_normalized = re.sub(r"[，。；：、\s]", "", left)
    right_normalized = re.sub(r"[，。；：、\s]", "", right)
    if left_normalized in right_normalized or right_normalized in left_normalized:
        return True
    focus_terms = (
        "客户分类", "商机阶段", "预约", "拜访方式", "拜访目的", "关键结果",
        "过程详细描述", "客户反馈", "客户事实", "评价", "偏差原因",
        "下一次行动目的", "下次拜访期望", "下一次联系客户时间安排",
    )
    left_focus = {term for term in focus_terms if term in left_normalized}
    right_focus = {term for term in focus_terms if term in right_normalized}
    if left_focus & right_focus:
        return True
    left_pairs = {left_normalized[index:index + 2] for index in range(len(left_normalized) - 1)}
    right_pairs = {right_normalized[index:index + 2] for index in range(len(right_normalized) - 1)}
    if not left_pairs or not right_pairs:
        return left_normalized == right_normalized
    return len(left_pairs & right_pairs) / min(len(left_pairs), len(right_pairs)) >= 0.45


def _evaluation_section_lines(
    visit: VisitDraftInput,
    display_code: str,
    name: str,
    issues: list[Issue],
    semantic_facts: Q34SemanticFacts,
    analysis: ModelSectionAnalysis | None,
) -> list[str]:
    """Merge the locked rule result with grounded, record-specific model wording."""
    if semantic_facts.provider.startswith("llm-") and (
        semantic_facts.status != "completed"
        or analysis is None
        or analysis.verdict == "not_evaluated"
    ):
        reason = _failure_reason_text(semantic_facts.failure_reason, "大模型未完成本项分析")
        return [
            f"{display_code}｜{name}："
            + _ai_exception(reason, "请稍后重试；持续失败请联系管理员核对模型服务配置")
        ]

    # LLM_* issues are projections of the same model section. Excluding them here
    # prevents the final opinion from repeating the model's reason as a rule result.
    rule_issues = [issue for issue in issues if not issue.code.startswith("LLM_")]
    failed = bool(rule_issues) or (analysis is not None and analysis.verdict == "needs_revision")
    status_label = "未达标" if failed else "达标"
    lines = [f"{display_code}｜{name}：{status_label}。"]

    if analysis is not None:
        lines.append("实际数据分析：" + analysis.reason)
        if analysis.evidence:
            evidence = "；".join(
                f"“{display_field_name(item.field)}”：{item.quote}"
                for item in analysis.evidence
            )
            lines.append("本条记录依据：" + evidence)
    else:
        # Offline/local fallback keeps the old deterministic explanation. It is
        # never presented as an AI analysis.
        lines.append(
            "规则分析："
            + _evaluation_section_text(visit, name, rule_issues, semantic_facts)
        )

    if failed:
        suggestions = _unique([
            analysis.suggestion if analysis is not None else "",
            *(issue.suggestion for issue in rule_issues),
        ])
        if suggestions:
            lines.append("针对本条记录的改进建议：" + _join_sentences(suggestions))
    return lines


def _precheck_standard(name: str) -> str:
    """已审核知识与现行前检规则的展示摘要，不参与判断或评分。"""
    field = display_field_name
    standards = {
        "客户类型": (
            f"“{field('customer_type_ii')}”应明确；商机客户应有可核验的"
            f"“{field('opportunity_stage')}”（仅限P1-P6）及可追溯字段来源，"
            "拜访目的应与客户类型及商机阶段匹配。"
        ),
        "预约与拜访方式": (
            f"如实填写“{field('is_appointment')}”和“{field('visit_method')}”；"
            "视频拜访必须预约，商机客户应优先预约；目标客户单次未预约不判错，"
            "只在周期统计中检查预约率。拜访方式应支持本次目的。"
        ),
        "拜访目的与关键结果": (
            f"“{field('purpose_code')}”应明确，选择其他时填写“{field('other_purpose')}”；"
            f"“{field('expected_key_result')}”应具体、可验证，并做到可衡量、相关且有时限，写明客户确认、"
            "条件、承诺、时间或交付物，并与目的对应；不能只写“了解一下”“沟通一下”等空泛表述。"
        ),
        "过程事实与结果": (
            f"“{field('process_description')}”应记录客户角色及可核验的确认事项、"
            "条件、异议、变化或承诺；客户事实、个人判断和假设应分开表达，不能只写感受。"
        ),
        "达成评价": (
            f"“{field('self_assessment')}”应围绕“{field('expected_key_result')}”，"
            f"并与“{field('process_description')}”中的客户事实一致；"
            "应能说明哪些达成、哪些未达成；缺少达成证据时不能评价为“达到目的”。"
        ),
        "下一步客户行动": (
            f"“{field('next_contact_at')}”应晚于“{field('visit_date')}”"
            "（按北京时间自然日比较，同日不算晚于）；行动对象统一为当前客户，不要求细化联系人。"
            "行动目的应承接本次客户事实、结果、异议、条件、承诺或未完成事项，不能只写继续跟进、"
            "再沟通、发资料或保持联系；期望结果应写明客户将确认、认可、提供、决定、承诺或完成什么。"
            "目标客户须跨自然月，潜力客户须跨自然季度；商机客户须有客户明确同意、确认、认可、"
            "约定或承诺的具体下一步及时间、条件或期望结果。"
        ),
    }
    return standards[name]


def _failed_section_analysis(name: str, issues: list[Issue]) -> str:
    reasons = _unique(issue.message for issue in issues)
    suggestions = _unique(issue.suggestion for issue in issues)
    parts = [
        "待改进。",
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
            "AI调用异常。",
            "系统未获取本项相关字段，请管理员核对字段绑定与传递配置后重新检测。",
        ),
        "partial_input": (
            "AI调用异常。",
            "系统仅获取部分相关字段，请管理员核对字段绑定与传递配置后重新检测。",
        ),
        "needs_revision": ("待改进。", "本项规则检查未通过，请核对相关内容。"),
    }
    label, explanation = states.get(
        status,
        ("AI调用异常。", "系统未返回本项检查结果，请管理员核对服务状态后重新检测。"),
    )
    lines = [label, "检查标准：" + _precheck_standard(name)]
    if explanation:
        lines.append("检查说明：" + explanation)
    return business_wording("\n".join(lines))


def _precheck_issues_for_section(
    name: str,
    fields: set[str],
    issues: list[Issue],
) -> list[Issue]:
    prefixes = {
        "客户类型": (
            "TAORAN_TYPE_",
            "TAORAN_OPPORTUNITY_",
            "TAORAN_T03_",
            "LLM_T_",
        ),
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
        reason = _failure_reason_text(semantic_facts.failure_reason, "大模型未完成本项分析")
        return _ai_exception(reason, "请稍后重试；持续失败请联系管理员核对模型服务配置")
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
        else "下一步客户行动与本次结果衔接不足，需要明确联系日期、具体目的和可观察的客户期望结果；行动对象默认为当前客户。"
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
    if total_score >= 85:
        return "高质量拜访记录，TAORAN关键闭环较完整。"
    if total_score >= 70:
        return "记录基本有效，仍有少量关键内容需要完善。"
    if total_score >= 50:
        return "记录存在明显缺口，建议经理复核后针对性改进。"
    return "有效性证据不足，需要补充事实并重新核验。"


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
