from __future__ import annotations

from abc import ABC, abstractmethod
from time import monotonic

import httpx

from .models import (
    Issue,
    Q34SemanticFacts,
    SelfAssessment,
    SemanticReview,
    Severity,
    VisitDraftInput,
)
from .rules import normalized_text


class SemanticReviewer(ABC):
    @abstractmethod
    def review(self, visit: VisitDraftInput) -> SemanticReview:
        raise NotImplementedError

    def review_q34(self, visit: VisitDraftInput) -> Q34SemanticFacts:
        return HeuristicSemanticReviewer().review_q34(visit)


class HeuristicSemanticReviewer(SemanticReviewer):
    """离线可运行的保守语义基线，不生成或改写客户事实。"""

    def review(self, visit: VisitDraftInput) -> SemanticReview:
        started = monotonic()
        issues: list[Issue] = []
        checks = (
            (
                "expected_key_result",
                visit.expected_key_result,
                "O",
                "KR_SEMANTICALLY_VAGUE",
                ("了解", "沟通", "有兴趣", "推进", "跟进"),
                "关键结果需要包含可观察的客户确认、事实、条件或承诺。",
            ),
            (
                "process_description",
                visit.process_description,
                "R",
                "RESULT_LACKS_CUSTOMER_FACTS",
                ("沟通了", "认可", "交流了", "拜访了"),
                "补充哪位客户确认了什么、提出什么条件、异议或下一步承诺。",
            ),
            (
                "next_action_purpose",
                visit.next_action_purpose,
                "N",
                "NEXT_ACTION_SEMANTICALLY_VAGUE",
                ("继续跟进", "保持联系", "推进项目", "后续联系"),
                "写明具体行动、对象、时间和期望结果。",
            ),
        )
        supplied = visit.metadata.get("source_supplied_fields")
        defaulted = visit.metadata.get("precheck_defaulted_fields", [])
        defaulted = defaulted if isinstance(defaulted, list) else []
        for field, value, dimension, code, vague_tokens, suggestion in checks:
            if (isinstance(supplied, list) and field not in supplied) or field in defaulted:
                continue
            text = normalized_text(value)
            if text and any(token in text for token in vague_tokens) and len(text) < 18:
                issues.append(
                    Issue(
                        code=code,
                        dimension=dimension,
                        severity=Severity.WARNING,
                        field_paths=[field],
                        message="字段虽然已填写，但表达仍然偏空泛。",
                        suggestion=suggestion,
                        source="semantic",
                    )
                )
        return SemanticReview(
            status="completed",
            issues=issues,
            provider="heuristic-v1",
            latency_ms=int((monotonic() - started) * 1000),
        )

    def review_q34(self, visit: VisitDraftInput) -> Q34SemanticFacts:
        started = monotonic()
        key_result = normalized_text(visit.expected_key_result)
        process = normalized_text(visit.process_description)
        next_action = normalized_text(visit.next_action_purpose)
        key_result_quality_ok = bool(
            key_result
            and len(key_result) >= 8
            and any(
                token in key_result
                for token in (
                    "确认",
                    "同意",
                    "明确",
                    "提供",
                    "确定",
                    "完成",
                    "日期",
                    "时间",
                    "预算",
                    "名单",
                    "方案",
                )
            )
        )
        process_fact_based = bool(
            process
            and len(process) >= 10
            and any(
                token in process
                for token in (
                    "客户",
                    "主任",
                    "经理",
                    "负责人",
                    "确认",
                    "提出",
                    "要求",
                    "同意",
                    "拒绝",
                    "反馈",
                )
            )
        )
        negative = any(token in process for token in ("未同意", "拒绝", "未确认", "未达成"))
        positive = any(
            token in process for token in ("确认", "同意", "认可", "承诺", "约定", "达成一致")
        )
        if process_fact_based and key_result_quality_ok and positive and not negative:
            achievement = SelfAssessment.ACHIEVED
        elif process_fact_based and (positive or negative):
            achievement = SelfAssessment.PARTIALLY_ACHIEVED
        else:
            achievement = SelfAssessment.NOT_ACHIEVED
        consensus = positive and not negative
        next_action_logic_ok = bool(
            next_action
            and len(next_action) >= 6
            and visit.next_contact_date
            and visit.next_contact_date > visit.visit_date
        )
        evidence_fields = [
            field
            for field, value in (
                ("expected_key_result", visit.expected_key_result),
                ("process_description", visit.process_description),
                ("next_action_purpose", visit.next_action_purpose),
                ("next_contact_at", visit.next_contact_at),
            )
            if value
        ]
        return Q34SemanticFacts(
            provider="heuristic-q34-v1",
            key_result_quality_ok=key_result_quality_ok,
            process_fact_based=process_fact_based,
            purpose_achievement=achievement,
            next_action_logic_ok=next_action_logic_ok,
            customer_consensus_met=consensus,
            evidence_fields=evidence_fields,
            reason="依据输入中的关键结果、过程事实、客户共识词和下一行动时间进行保守判断。",
            latency_ms=int((monotonic() - started) * 1000),
        )


class HttpSemanticReviewer(SemanticReviewer):
    """可插拔的企业语义服务；服务必须返回 SemanticReview JSON。"""

    def __init__(self, endpoint: str, api_key: str | None, timeout_seconds: float) -> None:
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def review(self, visit: VisitDraftInput) -> SemanticReview:
        started = monotonic()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            response = httpx.post(
                self.endpoint,
                headers=headers,
                json={"visit": visit.model_dump(mode="json")},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            review = SemanticReview.model_validate(response.json())
            review.latency_ms = int((monotonic() - started) * 1000)
            return review
        except httpx.TimeoutException:
            return SemanticReview(
                status="timeout",
                provider="external-http",
                latency_ms=int((monotonic() - started) * 1000),
            )
        except (httpx.HTTPError, ValueError):
            return SemanticReview(
                status="unavailable",
                provider="external-http",
                latency_ms=int((monotonic() - started) * 1000),
            )

    def review_q34(self, visit: VisitDraftInput) -> Q34SemanticFacts:
        started = monotonic()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            response = httpx.post(
                self.endpoint,
                headers=headers,
                json={
                    "task": "q34_taoran_semantic_facts",
                    "visit": visit.model_dump(mode="json"),
                    "output_contract": {
                        "key_result_quality_ok": "boolean",
                        "process_fact_based": "boolean",
                        "purpose_achievement": "achieved | partially_achieved | not_achieved",
                        "next_action_logic_ok": "boolean",
                        "customer_consensus_met": "boolean",
                        "evidence_fields": "仅限输入字段名数组",
                        "reason": "简短事实理由",
                    },
                },
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            facts = Q34SemanticFacts.model_validate(
                {
                    **response.json(),
                    "status": "completed",
                    "provider": "external-http",
                    "latency_ms": int((monotonic() - started) * 1000),
                }
            )
            allowed_fields = set(visit.model_fields)
            facts.evidence_fields = [
                field for field in facts.evidence_fields if field in allowed_fields
            ]
            return facts
        except (httpx.HTTPError, ValueError):
            fallback = HeuristicSemanticReviewer().review_q34(visit)
            return fallback.model_copy(
                update={
                    "status": "fallback",
                    "provider": "heuristic-after-external-failure",
                    "latency_ms": int((monotonic() - started) * 1000),
                    "reason": "外部语义服务不可用，已使用本地保守规则。",
                }
            )
