from __future__ import annotations

from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4

from .models import (
    DimensionScore,
    EvaluationResponse,
    Issue,
    PostEvaluationRequest,
    PrecheckRequest,
    PrecheckResponse,
    Severity,
    WritebackResult,
)
from .rules import canonical_hash, load_rule_catalog, normalized_text
from .scoring import (
    TOTAL_RULE_VERSION,
    precheck_advice_issues,
    q33_completeness,
    score_level,
    score_q33,
    score_q34,
)
from .semantic import HeuristicSemanticReviewer, SemanticReviewer


class TaoranAgent:
    def __init__(self, semantic_reviewer: SemanticReviewer | None = None) -> None:
        self.catalog = load_rule_catalog()
        self.semantic_reviewer = semantic_reviewer or HeuristicSemanticReviewer()
        self.vague_phrases = {
            normalized_text(value) for value in self.catalog["vague_exact_phrases"]
        }

    def precheck(self, request: PrecheckRequest) -> PrecheckResponse:
        """提交前按钮检查：只给修改建议，任何结果都不阻断简道云提交。"""
        started = monotonic()
        trace_id = f"tr_{uuid4().hex}"
        snapshot_hash = canonical_hash(
            {
                "form_revision": request.context.form_revision,
                "source_record_id": request.context.source_record_id,
                "visit": request.visit.model_dump(mode="json"),
            }
        )
        completeness = q33_completeness(request.visit)
        issues = precheck_advice_issues(request.visit, self.vague_phrases)
        semantic_review = self.semantic_reviewer.review(request.visit)
        semantic_issues = self._normalize_semantic_issues(semantic_review.issues)
        semantic_review.issues = semantic_issues
        issues.extend(semantic_issues)
        quality_score = round(completeness["rate"] * 100)
        elapsed = monotonic() - started
        if elapsed > self.catalog["precheck_response_budget_seconds"]:
            issues.append(
                Issue(
                    code="PRECHECK_BUDGET_EXCEEDED",
                    dimension="SYSTEM",
                    severity=Severity.WARNING,
                    field_paths=[],
                    message="AI检查超过前端响应预算。",
                    suggestion="保留当前记录并稍后重试AI检查；该问题不会阻断提交。",
                    source="system",
                )
            )
        if semantic_review.status in {"unavailable", "timeout"} or elapsed > 12:
            status = "review"
        elif any(issue.severity == Severity.ERROR for issue in issues):
            status = "needs_revision"
        else:
            status = "passed"
        suggestions = list(dict.fromkeys(issue.suggestion for issue in issues))
        return PrecheckResponse(
            check_id=(
                "chk_"
                + canonical_hash(
                    {
                        "tenant_id": request.context.tenant_id,
                        "snapshot_hash": snapshot_hash,
                    }
                )[:20]
            ),
            trace_id=trace_id,
            request_id=request.context.request_id,
            tenant_id=request.context.tenant_id,
            status=status,
            can_submit=True,
            submission_policy="advisory_only",
            submission_blocked=False,
            record_quality_score=quality_score,
            level=self._precheck_level(quality_score),
            dimensions=[
                DimensionScore(
                    code="Q33-COMPLETENESS",
                    name="Q33 TAORAN字段完整率预检",
                    score=quality_score,
                    max_score=100,
                )
            ],
            blocking_issues=[],
            issues=issues,
            questions=self._questions(issues),
            suggestions=suggestions,
            feedback_text=self._precheck_feedback_text(
                quality_score,
                status,
                suggestions,
            ),
            semantic_review=semantic_review,
            input_snapshot_hash=snapshot_hash,
            rule_version=TOTAL_RULE_VERSION,
            agent_version=self.catalog["agent_version"],
            checked_at=datetime.now(UTC),
            latency_ms=int((monotonic() - started) * 1000),
        )

    @staticmethod
    def _precheck_feedback_text(
        quality_score: int,
        status: str,
        suggestions: list[str],
    ) -> str:
        status_text = {
            "passed": "记录规范，可继续提交",
            "needs_revision": "建议修改后提交",
            "review": "需要人工复核，但不阻断提交",
        }[status]
        lines = [
            "【提交前TAORAN检查】",
            f"记录完整度：{quality_score}/100",
            f"检查结论：{status_text}",
        ]
        if suggestions:
            lines.append("修改建议：")
            lines.extend(f"{index}. {item}" for index, item in enumerate(suggestions, 1))
        else:
            lines.append("修改建议：当前未发现需要补充的规范性问题。")
        lines.append("本结果仅供填写参考，不阻断表单提交；正式评分以提交后深度评价为准。")
        return "\n".join(lines)

    def evaluate(self, request: PostEvaluationRequest, job_id: str) -> EvaluationResponse:
        """提交后深度评价：Q33与Q34各100分，总分200分。"""
        trace_id = f"tr_{uuid4().hex}"
        snapshot_hash = canonical_hash(request)
        visit = request.visit.model_copy(
            update={"evidence_ids": [item.evidence_id for item in request.evidence]}
        )
        q33, q33_issues = score_q33(visit)
        semantic_facts = self.semantic_reviewer.review_q34(visit)
        q34, q34_issues = score_q34(visit, semantic_facts)
        issues = [*q33_issues, *q34_issues]
        self._append_business_closure_advice(request, issues)

        total_score = round(q33.score + q34.score, 2)
        overall_percentage = round(total_score / 2, 2)
        level, recommendation = score_level(overall_percentage)
        if (
            semantic_facts.status in {"fallback", "unavailable", "timeout"}
            and recommendation == "yes"
        ):
            recommendation = "manager_review"
        if any(
            issue.code in {"INFORMATION_NOT_PERSISTED", "OPPORTUNITY_NOT_UPDATED"}
            for issue in issues
        ):
            recommendation = "manager_review"
        ai_opinion = self._ai_opinion(q33.score, q34.score, total_score, issues)
        return EvaluationResponse(
            evaluation_id=f"eval_{snapshot_hash[:20]}",
            job_id=job_id,
            trace_id=trace_id,
            tenant_id=request.context.tenant_id,
            visit_record_code=request.visit_record_code,
            status="completed",
            q33_score=q33.score,
            q34_score=q34.score,
            total_score=total_score,
            total_max_score=200,
            overall_percentage=overall_percentage,
            question_scores=[q33, q34],
            effectiveness_score=round(overall_percentage),
            effectiveness_level=level,
            count_as_effective_visit_recommendation=recommendation,
            dimensions=[
                DimensionScore(code="Q33", name=q33.name, score=q33.score, max_score=100),
                DimensionScore(code="Q34", name=q34.name, score=q34.score, max_score=100),
            ],
            issues=issues,
            manager_coaching_suggestions=self._manager_suggestions(issues),
            recommended_training_projects=self._training_projects(issues),
            ai_opinion=ai_opinion,
            semantic_facts=semantic_facts,
            writeback=WritebackResult(status="skipped"),
            input_snapshot_hash=snapshot_hash,
            rule_version=TOTAL_RULE_VERSION,
            agent_version=self.catalog["agent_version"],
            completed_at=datetime.now(UTC),
        )

    @staticmethod
    def _append_business_closure_advice(
        request: PostEvaluationRequest, issues: list[Issue]
    ) -> None:
        if request.information_collection_updated is False:
            issues.append(
                Issue(
                    code="INFORMATION_NOT_PERSISTED",
                    dimension="Q34",
                    severity=Severity.WARNING,
                    field_paths=["information_collection_updated"],
                    message="拜访后没有客户信息入库证据。",
                    suggestion="更新客户现场信息并关联来源记录。",
                )
            )
        if (
            request.visit.customer_type_ii
            and request.visit.customer_type_ii.value == "opportunity"
            and request.opportunity_updated is False
        ):
            issues.append(
                Issue(
                    code="OPPORTUNITY_NOT_UPDATED",
                    dimension="Q34",
                    severity=Severity.WARNING,
                    field_paths=["opportunity_updated"],
                    message="商机客户拜访后没有商机更新证据。",
                    suggestion="记录商机阶段、动态或下一步变化。",
                )
            )

    @staticmethod
    def _normalize_semantic_issues(issues: list[Issue]) -> list[Issue]:
        """语义模型只提供候选意见，不能创建提交阻断项。"""
        return [
            issue.model_copy(
                update={
                    "severity": (
                        Severity.WARNING if issue.severity == Severity.BLOCKING else issue.severity
                    ),
                    "source": "semantic",
                }
            )
            for issue in issues
        ]

    def _precheck_level(self, score: int) -> str:
        return next(
            item["level"] for item in self.catalog["precheck_levels"] if score >= item["minimum"]
        )

    @staticmethod
    def _questions(issues: list[Issue]) -> list[str]:
        mapping = {
            "TAORAN_FIELD_MISSING": "缺失字段能否依据本次真实拜访事实补充？",
            "TAORAN_FIELD_VAGUE": "哪位客户确认了什么、提出了什么条件或异议？",
            "KR_SEMANTICALLY_VAGUE": "什么客户事实可以证明本次目标达成？",
            "RESULT_LACKS_CUSTOMER_FACTS": "过程描述中有哪些可以核验的客户事实？",
            "NEXT_ACTION_SEMANTICALLY_VAGUE": "下一步何时针对谁完成什么动作？",
        }
        return list(dict.fromkeys(mapping[issue.code] for issue in issues if issue.code in mapping))

    @staticmethod
    def _training_projects(issues: list[Issue]) -> list[str]:
        mapping = {
            "Q33_REQUIRED_FIELD_MISSING": "TAORAN记录完整性训练",
            "Q33_IMMUTABLE_SUBMITTED_AT_MISSING": "拜访记录及时提交训练",
            "Q34_KEY_RESULT_QUALITY_NOT_MET": "关键结果KR具体化训练",
            "Q34_PROCESS_NOT_FACT_BASED": "TAORAN事实记录训练",
            "Q34_SELF_EVALUATION_INCONSISTENT": "拜访自评与复盘训练",
            "Q34_NEXT_ACTION_NOT_QUALIFIED": "Next Action闭环训练",
            "INFORMATION_NOT_PERSISTED": "客户信息沉淀训练",
            "OPPORTUNITY_NOT_UPDATED": "商机拜访闭环训练",
        }
        return list(dict.fromkeys(mapping[issue.code] for issue in issues if issue.code in mapping))

    @staticmethod
    def _manager_suggestions(issues: list[Issue]) -> list[str]:
        return list(
            dict.fromkeys(
                f"核对 {issue.code} 对应的原始事实，再决定是否辅导或要求补充。"
                for issue in issues
                if issue.severity in {Severity.ERROR, Severity.WARNING}
            )
        )

    @staticmethod
    def _ai_opinion(q33_score: float, q34_score: float, total: float, issues: list[Issue]) -> str:
        if issues:
            priorities = "；".join(dict.fromkeys(issue.suggestion for issue in issues[:4]))
        else:
            priorities = "记录完整、提交及时，自评与下一行动均与现有事实一致。"
        return (
            f"TAORAN综合得分{total:.1f}/200：Q33 {q33_score:.1f}/100，"
            f"Q34 {q34_score:.1f}/100。建议优先处理：{priorities}"
        )
