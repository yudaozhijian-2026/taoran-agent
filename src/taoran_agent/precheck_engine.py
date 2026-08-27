from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from .field_labels import display_field_name
from .knowledge import TaoranKnowledgeSnapshot, load_taoran_knowledge_snapshot
from .models import (
    CustomerTypeII,
    Issue,
    KnowledgeReference,
    Severity,
    TaoranSectionCheck,
    VisitDraftInput,
)
from .rules import is_meaningful, normalized_text

ENGINE_VERSION = "TAORAN-PRECHECK-KB-V1"
CORE_KNOWLEDGE_ID = "DSM-BS-01-07"
GENERAL_KNOWLEDGE_ID = "DSM-BS-000"

_FACT_MARKERS = (
    "主任",
    "经理",
    "负责人",
    "确认",
    "提出",
    "要求",
    "同意",
    "拒绝",
    "反馈",
    "异议",
    "条件",
    "承诺",
    "约定",
    "日期",
    "时间",
    "预算",
    "数量",
    "名单",
)
_VERIFIABLE_MARKERS = (
    "确认",
    "同意",
    "明确",
    "提供",
    "确定",
    "完成",
    "日期",
    "时间",
    "预算",
    "数量",
    "名单",
    "方案",
    "承诺",
    "约定",
)
_JUDGMENT_MARKERS = ("我认为", "我感觉", "应该", "估计", "可能", "大概", "沟通顺利", "非常满意")


def _has_check_content(value: str | None, vague_phrases: set[str]) -> bool:
    """Reject obvious filler locally; do not redefine the shared Q33/Q34 scoring helper."""
    text = normalized_text(value)
    return is_meaningful(value, vague_phrases) and not bool(
        re.fullmatch(r"(.{1,4})\1{2,}", text)
    )


@dataclass(frozen=True)
class _RuleCheck:
    field_paths: tuple[str, ...]
    passed: bool
    code: str
    message: str
    suggestion: str
    severity: Severity = Severity.ERROR
    requires_all_supplied: bool = False


class TaoranPrecheckEngineResult(BaseModel):
    score: int
    sections: list[TaoranSectionCheck]
    issues: list[Issue]
    knowledge_snapshot_hash: str
    knowledge_references: list[KnowledgeReference]
    engine_version: str = ENGINE_VERSION


class TaoranPrecheckEngine:
    """知识库驱动的提交前检查；仅使用本地已审核快照，避免前端等待远程API。"""

    def __init__(self, snapshot: TaoranKnowledgeSnapshot | None = None) -> None:
        self.snapshot = snapshot or load_taoran_knowledge_snapshot()
        ids = {record.id for record in self.snapshot.records}
        if CORE_KNOWLEDGE_ID not in ids:
            raise ValueError(f"TAORAN知识快照缺少核心记录 {CORE_KNOWLEDGE_ID}")

    def check(
        self,
        visit: VisitDraftInput,
        supplied_fields: set[str] | None,
        vague_phrases: set[str],
    ) -> TaoranPrecheckEngineResult:
        defaulted = visit.metadata.get("precheck_defaulted_fields")
        if isinstance(defaulted, list) and defaulted:
            supplied_fields = (
                set(VisitDraftInput.model_fields) if supplied_fields is None else supplied_fields
            ) - {field for field in defaulted if isinstance(field, str)}
        section_definitions = (
            ("T", "T", "客户类型", 15.0, self._type_checks(visit)),
            ("A1", "A", "预约与拜访方式", 10.0, self._appointment_checks(visit)),
            ("O_KR", "O/KR", "拜访目的与关键结果", 20.0, self._objective_checks(visit, vague_phrases)),
            ("R", "R", "过程事实与结果", 25.0, self._result_checks(visit, vague_phrases)),
            ("A2", "A", "达成评价", 15.0, self._assessment_checks(visit, vague_phrases)),
            ("N", "N", "下一步客户行动", 15.0, self._next_step_checks(visit, vague_phrases)),
        )
        sections: list[TaoranSectionCheck] = []
        issues: list[Issue] = []
        evaluated_max = 0.0
        evaluated_score = 0.0
        for code, display_code, name, weight, checks in section_definitions:
            active = [
                check for check in checks if self._was_supplied(check, supplied_fields)
            ]
            inactive = [check for check in checks if check not in active]
            if not active:
                status: Literal["met", "needs_revision", "partial_input", "not_received"] = (
                    "not_received"
                )
                score = 0.0
            else:
                passed_count = sum(check.passed for check in active)
                score = round(weight * passed_count / len(active), 2)
                evaluated_max += weight
                evaluated_score += score
                status = (
                    "needs_revision"
                    if passed_count < len(active)
                    else "partial_input" if inactive else "met"
                )
            for check in active:
                if not check.passed:
                    issues.append(
                        Issue(
                            code=check.code,
                            dimension=display_code,
                            severity=check.severity,
                            field_paths=list(check.field_paths),
                            message=check.message,
                            suggestion=check.suggestion,
                            source="rule",
                        )
                    )
            sections.append(
                TaoranSectionCheck(
                    code=code,
                    display_code=display_code,
                    name=name,
                    status=status,
                    score=score,
                    max_score=weight,
                    evaluated_fields=self._unique_paths(active),
                    unreceived_fields=[
                        path for path in self._unique_paths(inactive)
                        if supplied_fields is not None
                        and not self._path_supplied(path, supplied_fields)
                    ],
                    knowledge_ids=[CORE_KNOWLEDGE_ID, GENERAL_KNOWLEDGE_ID],
                )
            )

        if visit.visit_method is not None and visit.visit_method.value != "face_to_face":
            issues.append(
                Issue(
                    code="TAORAN_SCOPE_REFERENCE_ONLY",
                    dimension="SYSTEM",
                    severity=Severity.INFO,
                    field_paths=["visit_method"],
                    message="权威TAORAN标准的正式适用范围为TOB面对面销售。",
                    suggestion="当前记录仍可按TAORAN结构检查，但不作为面对面拜访标准的直接结论。",
                    source="rule",
                )
            )
        score = round(evaluated_score / evaluated_max * 100) if evaluated_max else 0
        references = [
            KnowledgeReference(
                id=record.id,
                title=record.title,
                status=record.status,
                version=record.version,
                content_hash=record.content_hash,
            )
            for record in self.snapshot.records
        ]
        return TaoranPrecheckEngineResult(
            score=score,
            sections=sections,
            issues=issues,
            knowledge_snapshot_hash=self.snapshot.snapshot_hash,
            knowledge_references=references,
        )

    @staticmethod
    def _type_checks(visit: VisitDraftInput) -> list[_RuleCheck]:
        checks = [
            _RuleCheck(
                ("customer_type_ii",),
                visit.customer_type_ii is not None,
                "TAORAN_TYPE_MISSING",
                "缺少TAORAN的客户类型上下文。",
                f"建议补充“{display_field_name('customer_type_ii')}”。",
            )
        ]
        if visit.customer_type_ii == CustomerTypeII.OPPORTUNITY:
            stage_ok = bool(
                all(item.current_stage for item in visit.opportunities)
                if visit.opportunities
                else visit.opportunity_stage
            )
            checks.append(
                _RuleCheck(
                    ("opportunities[].current_stage", "opportunity_stage"),
                    stage_ok,
                    "TAORAN_OPPORTUNITY_STAGE_MISSING",
                    "商机客户缺少可核验的当前商机阶段。",
                    "建议补充“最新商机阶段”，并确认其与本次拜访目的匹配。",
                )
            )
        return checks

    @staticmethod
    def _appointment_checks(visit: VisitDraftInput) -> list[_RuleCheck]:
        checks = [
            _RuleCheck(
                ("is_appointment",),
                visit.is_appointment is not None,
                "TAORAN_APPOINTMENT_MISSING",
                "缺少本次拜访是否预约的信息。",
                f"建议补充“{display_field_name('is_appointment')}”。",
            ),
            _RuleCheck(
                ("visit_method",),
                visit.visit_method is not None,
                "TAORAN_VISIT_METHOD_MISSING",
                "缺少本次拜访方式。",
                f"建议补充“{display_field_name('visit_method')}”。",
            ),
        ]
        if visit.customer_type_ii in {CustomerTypeII.OPPORTUNITY, CustomerTypeII.TARGET}:
            checks.append(
                _RuleCheck(
                    ("is_appointment",),
                    visit.is_appointment is True,
                    "TAORAN_APPOINTMENT_NOT_ALIGNED",
                    "当前客户类型的本次拜访未体现预约。",
                    "关键客户拜访应优先预约；如确属未预约，请保留真实事实并说明原因。",
                    Severity.WARNING,
                )
            )
        return checks

    @staticmethod
    def _objective_checks(
        visit: VisitDraftInput, vague_phrases: set[str]
    ) -> list[_RuleCheck]:
        purpose_ok = bool(
            normalized_text(visit.purpose_code)
            and (
                "other" not in normalized_text(visit.purpose_code)
                and "其他" not in normalized_text(visit.purpose_code)
                or normalized_text(visit.other_purpose)
            )
        )
        kr = normalized_text(visit.expected_key_result)
        kr_present = is_meaningful(visit.expected_key_result, vague_phrases)
        kr_verifiable = bool(
            _has_check_content(visit.expected_key_result, vague_phrases)
            and len(kr) >= 8 and any(token in kr for token in _VERIFIABLE_MARKERS)
        )
        return [
            _RuleCheck(
                ("purpose_code", "other_purpose"),
                purpose_ok,
                "TAORAN_OBJECTIVE_MISSING",
                "拜访目的未完整记录。",
                "建议明确“拜访目的”；选择其他时补充“具体其他目的”。",
            ),
            _RuleCheck(
                ("expected_key_result",),
                kr_verifiable,
                "TAORAN_KR_MISSING" if not kr_present else "TAORAN_KR_NOT_VERIFIABLE",
                "关键结果缺失。" if not kr_present else "关键结果尚不具体或不可验证。",
                (
                    f"建议补充“{display_field_name('expected_key_result')}”，并写明可观察的客户确认、条件、承诺、时间或交付物。"
                    if not kr_present
                    else "关键结果应写明可观察的客户确认、条件、承诺、时间或交付物。"
                ),
            ),
        ]

    @staticmethod
    def _result_checks(
        visit: VisitDraftInput, vague_phrases: set[str]
    ) -> list[_RuleCheck]:
        raw = visit.process_description or ""
        process = normalized_text(raw)
        present = is_meaningful(raw, vague_phrases)
        fact_based = bool(
            _has_check_content(raw, vague_phrases)
            and len(process) >= 10 and any(marker in process for marker in _FACT_MARKERS)
        )
        has_judgment = any(marker in raw for marker in _JUDGMENT_MARKERS)
        separated = not has_judgment or any(
            marker in raw for marker in ("事实：", "判断：", "假设：", "\n")
        )
        return [
            _RuleCheck(
                ("process_description",),
                fact_based,
                "TAORAN_RESULT_MISSING" if not present else "TAORAN_RESULT_NOT_FACT_BASED",
                "过程结果缺失。" if not present else "过程结果缺少可核验的客户事实。",
                (
                    f"建议补充“{display_field_name('process_description')}”，记录客户角色、确认事项、条件、异议、变化或承诺。"
                    if not present
                    else "请记录客户角色、确认事项、条件、异议、变化或承诺，避免只写感受。"
                ),
            ),
            _RuleCheck(
                ("process_description",),
                separated,
                "TAORAN_FACT_JUDGMENT_MIXED",
                "过程记录中的事实、判断和假设没有清晰区分。",
                "请将客户事实与个人判断或假设分开表达。",
                Severity.WARNING,
            ),
        ]

    @staticmethod
    def _assessment_checks(
        visit: VisitDraftInput, vague_phrases: set[str]
    ) -> list[_RuleCheck]:
        present = visit.self_assessment is not None
        kr = normalized_text(visit.expected_key_result)
        process = normalized_text(visit.process_description)
        # All three self-assessments need evidence. This is a local evidence-presence
        # check, not a claim that the result semantically achieved the intended KR.
        evidenced = bool(
            present
            and _has_check_content(visit.expected_key_result, vague_phrases)
            and any(marker in kr for marker in _VERIFIABLE_MARKERS)
            and _has_check_content(visit.process_description, vague_phrases)
            and any(marker in process for marker in _FACT_MARKERS)
        )
        return [
            _RuleCheck(
                ("self_assessment",),
                present,
                "TAORAN_ASSESSMENT_MISSING",
                "缺少对本次关键结果达成情况的评价。",
                f"建议补充“{display_field_name('self_assessment')}”，并依据关键结果选择达成程度。",
            ),
            _RuleCheck(
                ("self_assessment", "expected_key_result", "process_description"),
                evidenced,
                "TAORAN_ASSESSMENT_NOT_EVIDENCED",
                "达成评价缺少关键结果和过程事实支持。",
                "请让达成评价回到关键结果，并以过程中的客户事实作为依据。",
                requires_all_supplied=True,
            ),
        ]

    @staticmethod
    def _next_contact_time_check(visit: VisitDraftInput) -> _RuleCheck:
        label = display_field_name("next_contact_at")
        contact_date = visit.next_contact_date
        if contact_date is None:
            return _RuleCheck(
                ("next_contact_at",),
                False,
                "TAORAN_NSA_TIME_MISSING",
                f"{label}：未填写。下一步客户行动缺少联系时间。",
                f"请根据实际后续联系计划补充“{label}”。",
            )
        relation = "早于" if contact_date < visit.visit_date else "与"
        comparison = (
            f"{relation}本次拜访日期（{visit.visit_date.isoformat()}）"
            + ("相同" if contact_date == visit.visit_date else "")
        )
        return _RuleCheck(
            ("next_contact_at", "visit_date"),
            contact_date > visit.visit_date,
            "TAORAN_NSA_TIME_NOT_AFTER_VISIT",
            f"{label}：异常。异常说明：当前安排日期（{contact_date.isoformat()}）"
            f"{comparison}，不满足下一次联系日期须晚于本次拜访日期的要求。",
            f"请核对“{label}”与“{display_field_name('visit_date')}”，"
            "按实际拜访及后续联系计划修正日期；下一次联系日期应晚于本次拜访日期，"
            "修改后可再次点击AI检测。",
            requires_all_supplied=True,
        )

    @staticmethod
    def _next_step_checks(
        visit: VisitDraftInput, vague_phrases: set[str]
    ) -> list[_RuleCheck]:
        purpose = normalized_text(visit.next_action_purpose)
        purpose_ok = is_meaningful(visit.next_action_purpose, vague_phrases)
        result_ok = is_meaningful(visit.next_action_expected_result, vague_phrases)
        target_ok = bool(visit.next_action_target_id or visit.participants)
        checks = [
            _RuleCheck(
                ("next_action_purpose",),
                purpose_ok,
                "TAORAN_NSA_PURPOSE_MISSING",
                "下一步客户行动缺少明确目的。",
                "请写明下一步针对客户完成什么行动。",
            ),
            TaoranPrecheckEngine._next_contact_time_check(visit),
            _RuleCheck(
                ("next_action_expected_result",),
                result_ok,
                "TAORAN_NSA_RESULT_MISSING",
                "下一步客户行动缺少期望结果。",
                f"请补充“{display_field_name('next_action_expected_result')}”。",
            ),
            _RuleCheck(
                ("next_action_target_id", "participants"),
                target_ok,
                "TAORAN_NSA_TARGET_MISSING",
                "下一步客户行动缺少明确对象。",
                "请明确下一步行动对应的客户联系人或参与对象。",
            ),
        ]
        if "other" in purpose or "其他" in purpose:
            checks.append(
                _RuleCheck(
                    ("next_action_other_purpose",),
                    _has_check_content(visit.next_action_other_purpose, vague_phrases),
                    "TAORAN_NSA_OTHER_PURPOSE_MISSING",
                    "下一次行动目的选择了其他，但未填写有效的具体目的。",
                    f"请补充“{display_field_name('next_action_other_purpose')}”，写明具体客户任务。",
                )
            )
        return checks

    @staticmethod
    def _path_supplied(path: str, supplied_fields: set[str]) -> bool:
        return path in supplied_fields or path.split("[].", 1)[0] in supplied_fields

    @staticmethod
    def _was_supplied(check: _RuleCheck, supplied_fields: set[str] | None) -> bool:
        if supplied_fields is None:
            return True
        matches = [
            TaoranPrecheckEngine._path_supplied(path, supplied_fields)
            for path in check.field_paths
        ]
        return all(matches) if check.requires_all_supplied else any(matches)

    @staticmethod
    def _unique_paths(checks: list[_RuleCheck]) -> list[str]:
        return list(dict.fromkeys(path for check in checks for path in check.field_paths))
