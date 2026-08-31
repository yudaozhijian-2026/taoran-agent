from __future__ import annotations

from datetime import timedelta
from typing import Any

from .field_labels import display_field_name
from .models import (
    CustomerTypeII,
    Issue,
    Q34SemanticFacts,
    QuestionScore,
    ScoreComponent,
    SelfAssessment,
    Severity,
    VisitDraftInput,
)
from .rules import is_meaningful, normalized_text
from .scoring_contract import (
    COMPLETENESS_MAX_SCORE,
    CONSISTENCY_MAX_SCORE,
    NEXT_ACTION_MAX_SCORE,
    Q33_RULE_VERSION,
    Q34_RULE_VERSION,
    TIMELINESS_MAX_SCORE,
)


def band_score(rate: float, thresholds: tuple[float, float, float, float]) -> int:
    for score, threshold in zip((4, 3, 2, 1), thresholds, strict=True):
        if rate >= threshold:
            return score
    return 0


def _has_text(value: str | None) -> bool:
    return bool(normalized_text(value))


def _purpose_present(code: str | None, other: str | None) -> bool:
    if not _has_text(code):
        return False
    normalized_code = normalized_text(code)
    is_other = "other" in normalized_code or "其他" in normalized_code
    return not is_other or _has_text(other)


def _opportunity_stage_present(visit: VisitDraftInput) -> bool:
    if visit.opportunities:
        return all(item.current_stage for item in visit.opportunities)
    return bool(visit.opportunity_stage)


def q33_required_fields(
    visit: VisitDraftInput,
    available_fields: set[str] | None = None,
) -> list[tuple[str, bool]]:
    fields: list[tuple[str, bool]] = [
        ("customer_type_ii", visit.customer_type_ii is not None),
    ]
    if visit.customer_type_ii == CustomerTypeII.OPPORTUNITY:
        fields.append(("opportunities[].current_stage", _opportunity_stage_present(visit)))
    fields.extend(
        [
            ("is_appointment", visit.is_appointment is not None),
            ("purpose_code", _purpose_present(visit.purpose_code, visit.other_purpose)),
            ("expected_key_result", _has_text(visit.expected_key_result)),
            ("process_description", _has_text(visit.process_description)),
            ("self_assessment", visit.self_assessment is not None),
            ("next_contact_at", visit.next_contact_at is not None),
            (
                "next_action_purpose",
                _purpose_present(visit.next_action_purpose, visit.next_action_other_purpose),
            ),
            ("next_action_expected_result", _has_text(visit.next_action_expected_result)),
        ]
    )
    if available_fields is None:
        return fields
    return [
        (field, present)
        for field, present in fields
        if field in available_fields or field.split("[].", 1)[0] in available_fields
    ]


def q33_completeness(
    visit: VisitDraftInput,
    available_fields: set[str] | None = None,
) -> dict[str, Any]:
    fields = q33_required_fields(visit, available_fields)
    present = [name for name, is_present in fields if is_present]
    missing = [name for name, is_present in fields if not is_present]
    rate = len(present) / len(fields) if fields else 0.0
    band = band_score(rate, (0.90, 0.80, 0.70, 0.50))
    return {
        "present_field_count": len(present),
        "required_field_count": len(fields),
        "present_fields": present,
        "missing_fields": missing,
        "rate": rate,
        "band_score": band,
    }


def score_q33(visit: VisitDraftInput) -> tuple[QuestionScore, list[Issue]]:
    completeness = q33_completeness(visit)
    completeness_points = completeness["band_score"] / 4 * COMPLETENESS_MAX_SCORE
    issues: list[Issue] = []
    for field in completeness["missing_fields"]:
        issues.append(
            Issue(
                code="Q33_REQUIRED_FIELD_MISSING",
                dimension="Q33",
                severity=Severity.ERROR,
                field_paths=[field],
                message="Q33 TAORAN必填信息缺失。",
                suggestion=f"补充“{display_field_name(field)}”后再进行深度评价。",
            )
        )

    baseline = visit.actual_end_at
    baseline_source = "actual_end_at" if baseline else None
    if baseline is None and visit.actual_start_at is not None:
        baseline = visit.actual_start_at + timedelta(minutes=visit.duration_minutes or 0)
        baseline_source = (
            "actual_start_at+duration_minutes" if visit.duration_minutes else "actual_start_at"
        )
    timely = bool(
        baseline
        and visit.submitted_at
        and timedelta(0) <= visit.submitted_at - baseline <= timedelta(hours=24)
    )
    if visit.submitted_at is None:
        issues.append(
            Issue(
                code="Q33_IMMUTABLE_SUBMITTED_AT_MISSING",
                dimension="Q33",
                severity=Severity.ERROR,
                field_paths=["submitted_at"],
                message="缺少不可变首次提交时间，无法证明24小时内提交。",
                suggestion="由简道云系统元数据提供首次提交时间，禁止使用最后更新时间替代。",
            )
        )
    if baseline is None:
        issues.append(
            Issue(
                code="Q33_VISIT_END_BASELINE_MISSING",
                dimension="Q33",
                severity=Severity.ERROR,
                field_paths=["actual_end_at", "actual_start_at", "duration_minutes"],
                message="缺少拜访结束时间基准。",
                suggestion="依次提供实际结束时间、开始时间加时长或实际开始时间。",
            )
        )
    timeliness_rate = 1.0 if timely else 0.0
    timeliness_band = band_score(timeliness_rate, (0.90, 0.80, 0.70, 0.50))
    timeliness_points = timeliness_band / 4 * TIMELINESS_MAX_SCORE
    score = completeness_points + timeliness_points
    return (
        QuestionScore(
            question_code="Q33",
            name="TAORAN记录完整性与及时性",
            score=score,
            rule_version=Q33_RULE_VERSION,
            components=[
                ScoreComponent(
                    code="information_completeness",
                    name="信息完整性",
                    score=completeness_points,
                    max_score=COMPLETENESS_MAX_SCORE,
                    rate=completeness["rate"],
                    band_score=completeness["band_score"],
                    details={
                        "present_field_count": completeness["present_field_count"],
                        "required_field_count": completeness["required_field_count"],
                        "missing_fields": completeness["missing_fields"],
                    },
                ),
                ScoreComponent(
                    code="submission_timeliness",
                    name="24小时提交及时性",
                    score=timeliness_points,
                    max_score=TIMELINESS_MAX_SCORE,
                    rate=timeliness_rate,
                    band_score=timeliness_band,
                    passed=timely,
                    details={
                        "submitted_at": visit.submitted_at,
                        "visit_end_baseline": baseline,
                        "baseline_source": baseline_source,
                    },
                ),
            ],
            calculation_trace={
                "formula": "完整性档位分÷4×25 + 及时性档位分÷4×25",
                "thresholds": [0.90, 0.80, 0.70, 0.50],
                "single_record_projection": True,
                "q40_period_rule_note": "周期评价先汇总记录比例，再按档位计算。",
            },
        ),
        issues,
    )


def score_q34(visit: VisitDraftInput, facts: Q34SemanticFacts) -> tuple[QuestionScore, list[Issue]]:
    issues: list[Issue] = []
    if visit.customer_type_ii in {CustomerTypeII.OPPORTUNITY, CustomerTypeII.TARGET}:
        # 单条记录投影：两类客户均要求本次为预约拜访。40题周期汇总时，
        # 目标客户改为按周期预约率是否达到50%判断。
        appointment_standard_met = visit.is_appointment is True
    else:
        appointment_standard_met = True
    achievement = facts.purpose_achievement
    if (
        not facts.key_result_quality_ok or not facts.process_fact_based
    ) and achievement == SelfAssessment.ACHIEVED:
        # 语义服务不能只给“达成”结论而绕开KR和过程事实质量门槛。
        achievement = SelfAssessment.PARTIALLY_ACHIEVED
    if not appointment_standard_met and achievement == SelfAssessment.ACHIEVED:
        achievement = SelfAssessment.PARTIALLY_ACHIEVED
    self_consistent = visit.self_assessment is not None and visit.self_assessment == achievement

    next_contact_date = visit.next_contact_date
    concrete_next_action = bool(
        _purpose_present(visit.next_action_purpose, visit.next_action_other_purpose)
        and _has_text(visit.next_action_expected_result)
        and next_contact_date
        and next_contact_date > visit.visit_date
    )
    segment_gate = True
    segment_gate_name = "none"
    if visit.customer_type_ii == CustomerTypeII.OPPORTUNITY:
        segment_gate = facts.customer_consensus_met
        segment_gate_name = "customer_consensus"
    elif visit.customer_type_ii == CustomerTypeII.TARGET and next_contact_date:
        segment_gate = (
            next_contact_date.year,
            next_contact_date.month,
        ) != (visit.visit_date.year, visit.visit_date.month)
        segment_gate_name = "different_month"
    elif visit.customer_type_ii == CustomerTypeII.POTENTIAL and next_contact_date:
        current_quarter = (visit.visit_date.month - 1) // 3
        next_quarter = (next_contact_date.month - 1) // 3
        segment_gate = (next_contact_date.year, next_quarter) != (
            visit.visit_date.year,
            current_quarter,
        )
        segment_gate_name = "different_quarter"
    next_action_qualified = concrete_next_action and facts.next_action_logic_ok and segment_gate

    checks = (
        ("Q34_APPOINTMENT_STANDARD_NOT_MET", appointment_standard_met, ["is_appointment"]),
        ("Q34_KEY_RESULT_QUALITY_NOT_MET", facts.key_result_quality_ok, ["expected_key_result"]),
        ("Q34_PROCESS_NOT_FACT_BASED", facts.process_fact_based, ["process_description"]),
        ("Q34_SELF_EVALUATION_INCONSISTENT", self_consistent, ["self_assessment"]),
        (
            "Q34_NEXT_ACTION_NOT_QUALIFIED",
            next_action_qualified,
            ["next_action_purpose", "next_action_expected_result", "next_contact_at"],
        ),
    )
    for code, passed, fields in checks:
        if not passed:
            issues.append(
                Issue(
                    code=code,
                    dimension="Q34",
                    severity=Severity.WARNING,
                    field_paths=fields,
                    message="Q34拜访质量检查未满足对应标准。",
                    suggestion="根据AI事实理由补充客观事实、校准自评或明确下一行动。",
                    source="semantic" if code != "Q34_APPOINTMENT_STANDARD_NOT_MET" else "rule",
                )
            )

    self_points = float(CONSISTENCY_MAX_SCORE) if self_consistent else 0.0
    action_points = float(NEXT_ACTION_MAX_SCORE) if next_action_qualified else 0.0
    return (
        QuestionScore(
            question_code="Q34",
            name="拜访质量自评与下一行动",
            score=self_points + action_points,
            rule_version=Q34_RULE_VERSION,
            components=[
                ScoreComponent(
                    code="self_evaluation_consistency",
                    name="系统事实判断与自评一致",
                    score=self_points,
                    max_score=CONSISTENCY_MAX_SCORE,
                    rate=1.0 if self_consistent else 0.0,
                    band_score=4 if self_consistent else 0,
                    passed=self_consistent,
                    details={
                        "appointment_standard_met": appointment_standard_met,
                        "appointment_projection_note": (
                            "目标客户在40题周期汇总中按预约率≥50%判断；"
                            "本次单记录评分采用是否预约作为代理。"
                            if visit.customer_type_ii == CustomerTypeII.TARGET
                            else None
                        ),
                        "key_result_quality_ok": facts.key_result_quality_ok,
                        "process_fact_based": facts.process_fact_based,
                        "ai_purpose_achievement": achievement.value,
                        "system_self_assessment": (
                            visit.self_assessment.value if visit.self_assessment else None
                        ),
                    },
                ),
                ScoreComponent(
                    code="next_action_quality",
                    name="下一行动合理性",
                    score=action_points,
                    max_score=NEXT_ACTION_MAX_SCORE,
                    rate=1.0 if next_action_qualified else 0.0,
                    band_score=4 if next_action_qualified else 0,
                    passed=next_action_qualified,
                    details={
                        "concrete_next_action": concrete_next_action,
                        "semantic_logic_ok": facts.next_action_logic_ok,
                        "segment_gate": segment_gate,
                        "segment_gate_name": segment_gate_name,
                        "customer_consensus_met": facts.customer_consensus_met,
                    },
                ),
            ],
            calculation_trace={
                "formula": "自评一致性35分 + 下一行动15分",
                "single_record_projection": True,
                "included_in_q40_formal_aggregate": (
                    visit.visit_method is not None and visit.visit_method.value == "face_to_face"
                ),
                "q40_period_thresholds": [0.95, 0.85, 0.75, 0.60],
            },
        ),
        issues,
    )


def score_level(percentage: float) -> tuple[str, str]:
    if percentage >= 85:
        return "high_quality", "yes"
    if percentage >= 70:
        return "acceptable", "yes"
    if percentage >= 50:
        return "low_quality", "manager_review"
    return "invalid_or_unclear", "no"


def precheck_context_issues(
    visit: VisitDraftInput,
    available_fields: set[str] | None = None,
) -> list[Issue]:
    issues: list[Issue] = []
    purpose = visit.purpose_code or visit.other_purpose
    purpose_forbidden = bool(
        purpose
        and visit.purpose_policy
        and any(
            normalized_text(marker) in normalized_text(purpose)
            for marker in visit.purpose_policy.excluded_purposes
        )
    )
    if (
        purpose
        and visit.purpose_policy
        and visit.purpose_policy.is_effective_on(visit.visit_date)
        and (
            purpose_forbidden
            or normalized_text(purpose)
            not in {normalized_text(value) for value in visit.purpose_policy.allowed_purposes}
        )
    ):
        allowed = "、".join(visit.purpose_policy.allowed_purposes)
        issues.append(
            Issue(
                code="TAORAN_T03_PURPOSE_POLICY_MISMATCH",
                dimension="T",
                severity=Severity.ERROR,
                field_paths=["purpose_code", "purpose_policy"],
                message="当前填写的拜访目的与客户现阶段允许的拜访目的不一致。",
                suggestion=f"请将拜访目的修改为以下合适内容之一：{allowed}。",
            )
        )
    return issues


def precheck_advice_issues(
    visit: VisitDraftInput,
    vague_phrases: set[str],
    available_fields: set[str] | None = None,
) -> list[Issue]:
    """旧版兼容入口；新提交前流程由知识库驱动引擎执行。"""
    issues = precheck_context_issues(visit, available_fields)
    completeness = q33_completeness(visit, available_fields)
    for field in completeness["missing_fields"]:
        issues.append(
            Issue(
                code="TAORAN_FIELD_MISSING",
                dimension="Q33",
                severity=Severity.ERROR,
                field_paths=[field],
                message="TAORAN规范字段尚未填写完整。",
                suggestion=f"建议补充“{display_field_name(field)}”。",
            )
        )
    semantic_fields = (
        (
            "expected_key_result",
            visit.expected_key_result,
            "关键结果应具体、可验证，并与拜访目的保持一致。",
        ),
        (
            "process_description",
            visit.process_description,
            "过程描述应包含客户确认、条件、异议或变化等客观事实。",
        ),
        (
            "next_action_purpose",
            visit.next_action_purpose,
            "下一行动应写明具体目的，并配置明确时间。",
        ),
    )
    for field, value, suggestion in semantic_fields:
        if value and not is_meaningful(value, vague_phrases):
            issues.append(
                Issue(
                    code="TAORAN_FIELD_VAGUE",
                    dimension="Q34",
                    severity=Severity.WARNING,
                    field_paths=[field],
                    message="字段虽已填写，但内容过于空泛。",
                    suggestion=suggestion,
                    source="semantic",
                )
            )
    return issues
