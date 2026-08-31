from __future__ import annotations

from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4

from .evidence_standard import load_quality_evidence_standard
from .feedback import (
    build_evaluation_feedback,
    build_model_precheck_feedback,
    build_precheck_feedback,
)
from .field_labels import display_field_name
from .llm import section_issues
from .models import (
    ClassifiedEvidence,
    DimensionScore,
    EvaluationResponse,
    FeedbackMode,
    Issue,
    PostEvaluationRequest,
    PrecheckRequest,
    PrecheckResponse,
    Severity,
    StandardAudit,
    TaoranSectionCheck,
    WritebackResult,
)
from .precheck_engine import TaoranPrecheckEngine
from .rules import canonical_hash, load_rule_catalog, normalized_text
from .scoring import (
    precheck_context_issues,
    score_level,
    score_q33,
    score_q34,
)
from .scoring_contract import QUESTION_MAX_SCORE, TOTAL_MAX_SCORE, TOTAL_RULE_VERSION
from .semantic import HeuristicSemanticReviewer, SemanticReviewer


def _purpose_mapping_failure_message(reason: object) -> str:
    return {
        "knowledge_api_not_configured": "知识库接口尚未配置。",
        "knowledge_api_unavailable": "知识库接口调用失败。",
        "knowledge_record_missing": "知识库未返回拜访目的与关键结果标准。",
        "knowledge_mapping_invalid": "知识库标准无法形成有效的拜访目的与商机阶段映射。",
    }.get(str(reason), "未取得可用的拜访目的与商机阶段公司标准。")


class TaoranAgent:
    def __init__(
        self,
        semantic_reviewer: SemanticReviewer | None = None,
        precheck_engine: TaoranPrecheckEngine | None = None,
        *,
        direct_knowledge_feedback: bool = False,
    ) -> None:
        self.catalog = load_rule_catalog()
        self.semantic_reviewer = semantic_reviewer or HeuristicSemanticReviewer()
        self.precheck_engine = precheck_engine or TaoranPrecheckEngine()
        self.direct_knowledge_feedback = direct_knowledge_feedback
        self.vague_phrases = {
            normalized_text(value) for value in self.catalog["vague_exact_phrases"]
        }

    def precheck(self, request: PrecheckRequest) -> PrecheckResponse:
        """提交前按钮检查：只给修改建议，任何结果都不阻断简道云提交。"""
        started = monotonic()
        trace_id = f"tr_{uuid4().hex}"
        snapshot_payload = {
            "form_revision": request.context.form_revision,
            "source_record_id": request.context.source_record_id,
            "visit": request.visit.model_dump(mode="json"),
        }
        # 保留默认rule模式的旧幂等哈希；只为新增模式扩展快照。
        if request.feedback_mode != FeedbackMode.RULE:
            snapshot_payload["feedback_mode"] = request.feedback_mode.value
        snapshot_hash = canonical_hash(snapshot_payload)
        supplied_field_values = request.visit.metadata.get("source_supplied_fields")
        supplied_fields = (
            {str(field) for field in supplied_field_values}
            if isinstance(supplied_field_values, list)
            else None
        )
        engine_result = self.precheck_engine.check(
            request.visit,
            supplied_fields,
            self.vague_phrases,
        )
        system_issues = self._precheck_default_issues(request)
        if supplied_fields == set():
            system_issues.append(
                Issue(
                    code="PRECHECK_FIELDS_NOT_RECEIVED",
                    dimension="SYSTEM",
                    severity=Severity.ERROR,
                    field_paths=[],
                    message="本次AI检查未收到可检查的拜访字段。",
                    suggestion="请检查AI检测按钮的字段传递配置。",
                    source="system",
                )
            )
        direct_feedback = request.feedback_mode == FeedbackMode.RULE or (
            request.feedback_mode == FeedbackMode.KNOWLEDGE
            and self.direct_knowledge_feedback
        )
        if direct_feedback:
            # 规则反馈使用原本地规则；知识库反馈直接使用实时知识快照中的受控标准。
            # 两条路径都不调用远程大模型。
            semantic_review = HeuristicSemanticReviewer().review(request.visit)
            sections = engine_result.sections
            issues = [
                *precheck_context_issues(request.visit, supplied_fields),
                *system_issues,
                *engine_result.issues,
            ]
            sections = self._apply_t03_status(sections, issues, request.visit.metadata)
            knowledge_snapshot_hash = engine_result.knowledge_snapshot_hash
            knowledge_references = engine_result.knowledge_references
            engine_version = (
                engine_result.engine_version
                if request.feedback_mode == FeedbackMode.RULE
                else "TAORAN-PRECHECK-KNOWLEDGE-DIRECT-V1"
            )
        else:
            semantic_review = (
                self.semantic_reviewer.review_without_knowledge(request.visit)
                if request.feedback_mode == FeedbackMode.AI
                else self.semantic_reviewer.review_with_knowledge(request.visit)
            )
            sections = self._model_precheck_sections(
                engine_result.sections,
                semantic_review,
                include_knowledge=request.feedback_mode == FeedbackMode.KNOWLEDGE,
            )
            issues = list(system_issues)
            knowledge_snapshot_hash = (
                engine_result.knowledge_snapshot_hash
                if request.feedback_mode == FeedbackMode.KNOWLEDGE
                else ""
            )
            knowledge_references = (
                engine_result.knowledge_references
                if request.feedback_mode == FeedbackMode.KNOWLEDGE
                else []
            )
            engine_version = (
                "TAORAN-PRECHECK-PURE-AI-V1"
                if request.feedback_mode == FeedbackMode.AI
                else "TAORAN-PRECHECK-KNOWLEDGE-AI-V1"
            )
        semantic_issues = self._normalize_semantic_issues(semantic_review.issues)
        semantic_review.issues = semantic_issues
        issues.extend(semantic_issues)
        quality_score = round(
            sum(section.score for section in sections)
            / max(1, sum(section.max_score for section in sections))
            * 100
        )
        elapsed = monotonic() - started
        if elapsed > self.catalog["precheck_response_budget_seconds"]:
            issues.append(
                Issue(
                    code="PRECHECK_BUDGET_EXCEEDED",
                    dimension="SYSTEM",
                    severity=Severity.WARNING,
                    field_paths=[],
                    message="AI检查超过前端响应预算。",
                    suggestion="保留当前记录并稍后重试AI检查。",
                    source="system",
                )
            )
        if (
            semantic_review.status != "completed"
            or elapsed > self.catalog["precheck_response_budget_seconds"]
        ):
            status = "review"
        elif any(
            issue.severity in {Severity.ERROR, Severity.WARNING, Severity.BLOCKING}
            and issue.source != "system" for issue in issues
        ) or any(section.status == "needs_revision" for section in sections):
            status = "needs_revision"
        elif any(section.status != "met" for section in sections) or any(
            issue.source == "system" and issue.severity != Severity.INFO for issue in issues
        ):
            status = "review"
        else:
            status = "passed"
        suggestions = list(dict.fromkeys(issue.suggestion for issue in issues))
        standard_audit = self._standard_audit(
            request.visit.metadata,
            knowledge_snapshot_hash,
            engine_version,
            semantic_review.prompt_version,
        )
        return PrecheckResponse(
            check_id=(
                "chk_"
                + canonical_hash(
                    {
                        "tenant_id": request.context.tenant_id,
                        "request_id": request.context.request_id,
                        "snapshot_hash": snapshot_hash,
                    }
                )[:20]
            ),
            trace_id=trace_id,
            request_id=request.context.request_id,
            tenant_id=request.context.tenant_id,
            feedback_mode=request.feedback_mode,
            status=status,
            can_submit=True,
            submission_policy="advisory_only",
            submission_blocked=False,
            record_quality_score=quality_score,
            level=self._precheck_level(quality_score),
            dimensions=[
                DimensionScore(
                    code=section.code,
                    name=f"{section.display_code} {section.name}",
                    score=section.score,
                    max_score=section.max_score,
                )
                for section in sections
            ],
            taoran_sections=sections,
            blocking_issues=[],
            issues=issues,
            questions=self._questions(issues),
            suggestions=suggestions,
            feedback_text=(
                build_precheck_feedback(
                    request.visit,
                    quality_score,
                    status,
                    issues,
                    supplied_fields,
                    knowledge_references,
                    semantic_review,
                    taoran_sections=sections,
                    title=(
                        "知识库反馈"
                        if request.feedback_mode == FeedbackMode.KNOWLEDGE
                        else "AI反馈意见"
                    ),
                    review_status_text=(
                        "已按知识库标准完成检查，部分内容需要补充或完善"
                        if request.feedback_mode == FeedbackMode.KNOWLEDGE
                        else "AI调用异常，请根据异常原因处理后重新检测"
                    ),
                )
                if direct_feedback
                else build_model_precheck_feedback(
                    request.feedback_mode,
                    status,
                    issues,
                    semantic_review,
                )
            ),
            semantic_review=semantic_review,
            input_snapshot_hash=snapshot_hash,
            rule_version=TOTAL_RULE_VERSION,
            engine_version=engine_version,
            knowledge_snapshot_hash=knowledge_snapshot_hash,
            knowledge_references=knowledge_references,
            standard_audit=standard_audit,
            agent_version=self.catalog["agent_version"],
            checked_at=datetime.now(UTC),
            latency_ms=int((monotonic() - started) * 1000),
        )

    @staticmethod
    def _model_precheck_sections(
        rule_sections: list[TaoranSectionCheck],
        semantic_review,
        *,
        include_knowledge: bool,
    ) -> list[TaoranSectionCheck]:
        """仅复用六项展示结构和权重，结论来自本次模型分析。"""
        analyses = {item.code: item for item in semantic_review.sections}
        sections: list[TaoranSectionCheck] = []
        for section in rule_sections:
            analysis = analyses.get(section.code)
            if semantic_review.status != "completed" or analysis is None:
                status = "not_received" if section.status == "not_received" else "partial_input"
            elif analysis.verdict == "met":
                status = "met"
            elif analysis.verdict == "needs_revision":
                status = "needs_revision"
            else:
                status = "not_received" if section.status == "not_received" else "partial_input"
            sections.append(
                section.model_copy(
                    update={
                        "status": status,
                        "score": section.max_score if status == "met" else 0.0,
                        "knowledge_ids": section.knowledge_ids if include_knowledge else [],
                        "classified_evidence": [
                            *section.classified_evidence,
                            *[
                                ClassifiedEvidence(
                                    field_path=item.field,
                                    quote=item.quote,
                                    category=item.category,
                                    source="model",
                                )
                                for item in (analysis.evidence if analysis else [])
                            ],
                        ],
                    }
                )
            )
        return sections

    @staticmethod
    def _precheck_default_issues(request: PrecheckRequest) -> list[Issue]:
        defaulted = request.visit.metadata.get("precheck_defaulted_fields")
        if not isinstance(defaulted, list):
            defaulted = []
        messages = {
            "visit_date": "AI调用异常：系统未获取拜访日期。",
            "employee_id": "AI调用异常：系统未获取销售代表。",
            "customer_id": "AI调用异常：系统未获取当前客户标识，不能确认下一步行动对象。",
        }
        issues = [
            Issue(
                code=f"PRECHECK_{field.upper()}_MISSING",
                dimension="SYSTEM",
                severity=Severity.ERROR,
                field_paths=[field],
                message=messages[field],
                suggestion=f"请管理员核对“{display_field_name(field)}”的字段绑定与传递配置后重新检测。",
                source="system",
            )
            for field in defaulted
            if field in messages
        ]
        if not request.visit.customer_id and "customer_id" not in defaulted:
            issues.append(
                Issue(
                    code="PRECHECK_CUSTOMER_ID_MISSING",
                    dimension="SYSTEM",
                    severity=Severity.ERROR,
                    field_paths=["customer_id"],
                    message=messages["customer_id"],
                    suggestion="请管理员核对当前客户字段的绑定与传递配置后重新检测。",
                    source="system",
                )
            )
        if request.visit.metadata.get("purpose_mapping_status") == "unavailable":
            issues.append(
                Issue(
                    code="TAORAN_T03_PURPOSE_MAPPING_UNAVAILABLE",
                    dimension="SYSTEM",
                    severity=Severity.INFO,
                    field_paths=["purpose_policy"],
                    message=(
                        "AI调用异常："
                        + _purpose_mapping_failure_message(
                            request.visit.metadata.get("purpose_mapping_failure_reason")
                        )
                    ),
                    suggestion="请稍后重新点击检测；持续失败请联系管理员检查知识库中的拜访目的与关键结果标准。",
                    source="system",
                )
            )
        return issues

    @staticmethod
    def _apply_t03_status(
        sections: list[TaoranSectionCheck],
        issues: list[Issue],
        metadata: dict,
    ) -> list[TaoranSectionCheck]:
        mismatch = any(issue.code == "TAORAN_T03_PURPOSE_POLICY_MISMATCH" for issue in issues)
        unavailable = metadata.get("purpose_mapping_status") == "unavailable"
        if not mismatch and not unavailable:
            return sections
        updated: list[TaoranSectionCheck] = []
        for section in sections:
            if section.code != "T":
                updated.append(section)
                continue
            knowledge_ids = list(section.knowledge_ids)
            if metadata.get("purpose_mapping_knowledge_id"):
                knowledge_ids.append(metadata["purpose_mapping_knowledge_id"])
            updated.append(
                section.model_copy(
                    update={
                        "status": (
                            "needs_revision"
                            if mismatch or section.status == "needs_revision"
                            else "partial_input"
                        ),
                        "score": 0.0 if mismatch else section.score,
                        "knowledge_ids": list(dict.fromkeys(knowledge_ids)),
                    }
                )
            )
        return updated

    def evaluate(self, request: PostEvaluationRequest, job_id: str) -> EvaluationResponse:
        """提交后深度评价：Q33与Q34各50分，总分100分。"""
        trace_id = f"tr_{uuid4().hex}"
        snapshot_hash = canonical_hash(request)
        visit = request.visit.model_copy(
            update={"evidence_ids": [item.evidence_id for item in request.evidence]}
        )
        q33, q33_issues = score_q33(visit)
        semantic_facts = self.semantic_reviewer.review_q34(visit)
        q34, q34_issues = score_q34(visit, semantic_facts)
        issues = [*q33_issues, *q34_issues, *section_issues(semantic_facts.sections)]
        self._append_business_closure_advice(request, issues)

        total_score = round(q33.score + q34.score, 2)
        overall_percentage = round(total_score / TOTAL_MAX_SCORE * 100, 2)
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
        ai_opinion = build_evaluation_feedback(
            visit,
            q33.score,
            q34.score,
            total_score,
            issues,
            semantic_facts,
        )
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
            total_max_score=TOTAL_MAX_SCORE,
            overall_percentage=overall_percentage,
            question_scores=[q33, q34],
            effectiveness_score=round(overall_percentage),
            effectiveness_level=level,
            count_as_effective_visit_recommendation=recommendation,
            dimensions=[
                DimensionScore(
                    code="Q33", name=q33.name, score=q33.score, max_score=QUESTION_MAX_SCORE,
                ),
                DimensionScore(
                    code="Q34", name=q34.name, score=q34.score, max_score=QUESTION_MAX_SCORE,
                ),
            ],
            issues=issues,
            manager_coaching_suggestions=self._manager_suggestions(issues),
            recommended_training_projects=self._training_projects(issues),
            ai_opinion=ai_opinion,
            semantic_facts=semantic_facts,
            writeback=WritebackResult(status="skipped"),
            input_snapshot_hash=snapshot_hash,
            rule_version=TOTAL_RULE_VERSION,
            standard_audit=self._standard_audit(
                visit.metadata,
                self.precheck_engine.snapshot.snapshot_hash,
                "TAORAN-EVALUATION-Q33-Q34-V2",
                semantic_facts.prompt_version,
            ),
            agent_version=self.catalog["agent_version"],
            completed_at=datetime.now(UTC),
        )

    @staticmethod
    def _standard_audit(
        metadata: dict,
        knowledge_snapshot_hash: str,
        engine_version: str,
        model_prompt_version: str | None,
    ) -> StandardAudit:
        standard = load_quality_evidence_standard()
        return StandardAudit(
            standard_id=standard.standard_id,
            standard_version=standard.standard_version,
            standard_source=standard.source_file,
            standard_content_hash=canonical_hash(standard.model_dump(mode="json")),
            knowledge_snapshot_hash=knowledge_snapshot_hash,
            rule_version=TOTAL_RULE_VERSION,
            engine_version=engine_version,
            field_mapping_version=metadata.get("field_mapping_version"),
            model_prompt_version=model_prompt_version,
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
                    "source": issue.source if issue.source == "system" else "semantic",
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
