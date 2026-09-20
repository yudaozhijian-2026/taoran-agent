from __future__ import annotations

from datetime import UTC, date, datetime
from time import time

from taoran_agent.agent import TaoranAgent
from taoran_agent.deep_review import (
    DeepReviewResult,
    evaluate_with_front_fallback,
    load_front_context,
    reconcile,
)
from taoran_agent.front_analysis_artifact import (
    FrontAnalysisArtifact,
    FrontFinding,
    analysis_input_hash,
    build_artifact,
)
from taoran_agent.models import (
    ModelEvidence,
    ModelSectionAnalysis,
    PostEvaluationRequest,
    Q34SemanticFacts,
    RequestContext,
    SelfAssessment,
    VisitDraftInput,
    VisitMethod,
)
from taoran_agent.storage import AgentStore


def visit(**updates) -> VisitDraftInput:
    data = {
        "visit_date": date(2026, 9, 20),
        "employee_id": "sales-a",
        "customer_id": "customer-a",
        "customer_type_ii": "opportunity",
        "visit_method": VisitMethod.FACE_TO_FACE,
        "is_appointment": True,
        "purpose_code": "收集信息",
        "expected_key_result": "确认客户的预算和审批负责人",
        "process_description": "客户确认已预留预算，审批负责人尚未明确。",
        "self_assessment": SelfAssessment.PARTIALLY_ACHIEVED,
        "next_action_purpose": "收集信息",
        "next_action_expected_result": "确认审批负责人",
        "next_contact_at": datetime(2026, 9, 25, 9, tzinfo=UTC),
    }
    data.update(updates)
    return VisitDraftInput.model_validate(data)


def request(item: VisitDraftInput, *, user_id: str = "sales-a") -> PostEvaluationRequest:
    return PostEvaluationRequest(
        context=RequestContext(
            tenant_id="tenant-a",
            request_id="request-a",
            user_id=user_id,
            source="test",
            source_record_id="record-a",
        ),
        visit_record_code="BFJL-TEST",
        visit=item,
    )


def section(code: str, verdict: str, reason: str) -> ModelSectionAnalysis:
    return ModelSectionAnalysis(
        code=code,
        verdict=verdict,
        field_paths=["process_description"],
        reason=reason,
        suggestion="请根据真实事实补充。" if verdict == "needs_revision" else "",
        evidence=[
            ModelEvidence(
                field="process_description",
                quote="客户确认已预留预算",
                category="customer_fact",
            )
        ],
    )


def facts(sections: list[ModelSectionAnalysis]) -> Q34SemanticFacts:
    return Q34SemanticFacts(
        provider="llm-test",
        key_result_quality_ok=True,
        process_fact_based=True,
        purpose_achievement=SelfAssessment.PARTIALLY_ACHIEVED,
        next_action_logic_ok=True,
        customer_consensus_met=True,
        reason="正式深度分析。",
        sections=sections,
    )


def artifact(item: VisitDraftInput) -> FrontAnalysisArtifact:
    return FrontAnalysisArtifact(
        artifact_id="fa_test",
        input_hash=analysis_input_hash(item),
        quick_check_input_hash="a" * 64,
        generated_at=datetime.now(UTC),
        tenant_id="tenant-a",
        user_id="sales-a",
        check_id="qc-test",
        analysis_summary="提交前分析。",
        findings=[
            FrontFinding(
                finding_id="front-T",
                dimension="T",
                finding_type="observation",
                conclusion="met",
                statement="客户类型与拜访目的匹配。",
            ),
            FrontFinding(
                finding_id="front-O",
                dimension="O_KR",
                finding_type="observation",
                conclusion="met",
                statement="关键结果具体。",
            ),
            FrontFinding(
                finding_id="front-R",
                dimension="R",
                finding_type="gap",
                conclusion="needs_revision",
                statement="过程事实尚需补充。",
            ),
        ],
    )


def test_hash_ignores_platform_fields_but_changes_with_business_content():
    original = visit(metadata={"source_supplied_fields": ["process_description"]})
    saved = original.model_copy(update={
        "submitted_at": datetime(2026, 9, 20, 12, tzinfo=UTC),
        "metadata": {"field_mapping_version": "later"},
        "evidence_ids": ["server-generated"],
    })
    assert analysis_input_hash(original) == analysis_input_hash(saved)
    changed = saved.model_copy(update={"process_description": "客户确认预算并指定了负责人。"})
    assert analysis_input_hash(original) != analysis_input_hash(changed)


def test_artifact_is_not_eligible_until_acknowledged(tmp_path):
    store = AgentStore(tmp_path / "agent.db")
    item = visit()
    value = artifact(item)
    store.save_front_analysis_artifact(
        value.model_dump(mode="json"), retention_until=time() + 3600
    )
    found, diagnostic = load_front_context(store, request(item))
    assert found is None
    assert diagnostic.front_artifact_status == "not_found"

    assert store.acknowledge_front_analysis_artifact("tenant-a", "qc-test", "a" * 64)
    found, diagnostic = load_front_context(store, request(item))
    assert found is not None
    assert diagnostic.front_artifact_status == "used"
    assert diagnostic.front_artifact_used is True


def test_latest_acknowledged_artifact_wins_for_same_user_and_content(tmp_path):
    store = AgentStore(tmp_path / "agent.db")
    item = visit()
    first = artifact(item)
    second = first.model_copy(
        update={
            "artifact_id": "fa_second",
            "check_id": "qc-second",
            "quick_check_input_hash": "b" * 64,
            "analysis_summary": "第二次确认的分析。",
        }
    )
    store.save_front_analysis_artifact(
        first.model_dump(mode="json"), retention_until=time() + 3600
    )
    assert store.acknowledge_front_analysis_artifact(
        "tenant-a", "qc-test", "a" * 64
    )
    store.save_front_analysis_artifact(
        second.model_dump(mode="json"), retention_until=time() + 3600
    )
    assert store.acknowledge_front_analysis_artifact(
        "tenant-a", "qc-second", "b" * 64
    )
    found, diagnostic = load_front_context(store, request(item))
    assert found is not None
    assert found.artifact_id == "fa_second"
    assert found.analysis_summary == "第二次确认的分析。"
    assert diagnostic.front_artifact_status == "used"


def test_mismatch_falls_back_without_blocking(tmp_path):
    store = AgentStore(tmp_path / "agent.db")
    item = visit()
    value = artifact(item)
    store.save_front_analysis_artifact(
        value.model_dump(mode="json"), retention_until=time() + 3600
    )
    store.acknowledge_front_analysis_artifact("tenant-a", "qc-test", "a" * 64)
    changed = item.model_copy(update={"expected_key_result": "确认客户采购时间"})
    found, diagnostic = load_front_context(store, request(changed))
    assert found is None
    assert diagnostic.front_artifact_status == "input_mismatch"
    assert diagnostic.submitted_input_hash == analysis_input_hash(changed)


def test_missing_artifact_uses_standalone_review(tmp_path):
    store = AgentStore(tmp_path / "agent.db")
    item = visit()
    found, diagnostic = load_front_context(store, request(item))
    assert found is None
    assert diagnostic.front_artifact_status == "not_found"
    assert diagnostic.front_artifact_used is False


def test_corrupt_artifact_is_ignored(tmp_path):
    store = AgentStore(tmp_path / "agent.db")
    item = visit()
    value = artifact(item)
    store.save_front_analysis_artifact(
        value.model_dump(mode="json"), retention_until=time() + 3600
    )
    store.acknowledge_front_analysis_artifact("tenant-a", "qc-test", "a" * 64)
    with store._lock, store._connection:
        store._connection.execute(
            "UPDATE front_analysis_artifacts SET payload_json = ? WHERE artifact_id = ?",
            ("{not-json", value.artifact_id),
        )
    found, diagnostic = load_front_context(store, request(item))
    assert found is None
    assert diagnostic.front_artifact_status == "invalid"


def test_expired_artifact_is_ignored(tmp_path):
    store = AgentStore(tmp_path / "agent.db")
    item = visit()
    value = artifact(item)
    store.save_front_analysis_artifact(
        value.model_dump(mode="json"), retention_until=time() - 1
    )
    with store._lock, store._connection:
        store._connection.execute(
            "UPDATE front_analysis_artifacts SET acknowledged_at = ? WHERE artifact_id = ?",
            (datetime.now(UTC).isoformat(), value.artifact_id),
        )
    found, diagnostic = load_front_context(store, request(item))
    assert found is None
    assert diagnostic.front_artifact_status == "expired"


def test_reconciliation_has_all_four_statuses():
    item = visit()
    front = artifact(item)
    formal = facts([
        section("T", "met", "客户类型与拜访目的匹配。"),
        section("O_KR", "needs_revision", "关键结果仍缺可核对的审批信息。"),
        section("R", "needs_revision", "过程有预算事实，但负责人信息尚未明确。"),
        section("N", "needs_revision", "下一步尚需客户确认具体负责人。"),
    ])
    base = DeepReviewResult(
        front_artifact_status="used",
        front_artifact_used=True,
        front_artifact_id=front.artifact_id,
        front_input_hash=front.input_hash,
        submitted_input_hash=front.input_hash,
    )
    result = reconcile(front, formal, base)
    statuses = {row.dimension: row.status for row in result.findings}
    assert statuses == {
        "T": "confirmed",
        "O_KR": "corrected",
        "R": "deepened",
        "N": "new_finding",
    }
    assert result.counts == {
        "confirmed": 1,
        "deepened": 1,
        "corrected": 1,
        "new_finding": 1,
    }


def test_builder_uses_validated_result_and_excludes_workflow_footer():
    item = visit()
    built = build_artifact(
        visit=item,
        tenant_id="tenant-a",
        user_id="sales-a",
        check_id="qc-build",
        quick_check_input_hash="b" * 64,
        source_record_id=None,
        feedback_text=(
            "本次拜访分析：客户已预留预算，负责人尚未明确。\n\n"
            "AI改善建议：\n1. 请补充客户审批负责人。\n"
            "提交后，系统将自动生成正式评分和反馈意见。"
        ),
        decision_ledger={"version": "ledger-v1"},
        front_review={
            "sections": [section("O_KR", "needs_revision", "负责人尚未明确。").model_dump(mode="json")],
            "confirmation_items": [],
            "prompt_version": "front-prompt",
        },
        policy_version="front-policy",
    )
    assert built.analysis_summary == "客户已预留预算，负责人尚未明确。"
    assert [item.text for item in built.suggestions] == ["请补充客户审批负责人。"]
    assert built.findings[0].dimension == "O_KR"


def test_front_context_never_changes_scores():
    item = visit(submitted_at=datetime(2026, 9, 20, 13, tzinfo=UTC))
    agent = TaoranAgent()
    baseline = agent.evaluate(request(item), "job-baseline")
    inherited = agent.evaluate(
        request(item),
        "job-front",
        front_analysis={"analysis_summary": "候选分析", "findings": []},
    )
    assert inherited.q33_score == baseline.q33_score
    assert inherited.q34_score == baseline.q34_score
    assert inherited.total_score == baseline.total_score


def test_front_context_failure_retries_standalone_formal_review():
    item = visit(submitted_at=datetime(2026, 9, 20, 13, tzinfo=UTC))

    class FrontFailingAgent:
        def __init__(self):
            self.calls = []

        def evaluate(self, evaluation_request, job_id, *, front_analysis=None):
            self.calls.append(front_analysis)
            if front_analysis is not None:
                raise RuntimeError("front provider failed")
            return TaoranAgent().evaluate(evaluation_request, job_id)

    front = artifact(item)
    agent = FrontFailingAgent()
    diagnostic = DeepReviewResult(
        front_artifact_status="used",
        front_artifact_used=True,
        front_artifact_id=front.artifact_id,
        front_input_hash=front.input_hash,
        submitted_input_hash=front.input_hash,
    )
    response, final_diagnostic = evaluate_with_front_fallback(
        agent,
        request(item),
        "job-fallback",
        front,
        diagnostic,
    )
    assert response.total_score >= 0
    assert len(agent.calls) == 2
    assert agent.calls[0] is not None and agent.calls[1] is None
    assert final_diagnostic.front_artifact_used is False
    assert final_diagnostic.fallback_reason == (
        "formal_review_with_front_failed:RuntimeError"
    )
