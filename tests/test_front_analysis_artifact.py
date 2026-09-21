from __future__ import annotations

from datetime import UTC, date, datetime
from time import time

import pytest

from taoran_agent.agent import TaoranAgent
from taoran_agent.deep_review import (
    DeepReviewFinding,
    DeepReviewResult,
    evaluate_with_front_fallback,
    load_front_context,
    reconcile,
)
from taoran_agent.feedback import build_evaluation_feedback, merge_evaluation_with_knowledge
from taoran_agent.front_analysis_artifact import (
    FrontAnalysisArtifact,
    FrontFinding,
    analysis_input_hash,
    build_artifact,
    is_trusted_artifact_user_id,
)
from taoran_agent.models import (
    ModelEvidence,
    ModelSectionAnalysis,
    PostEvaluationRequest,
    PrecheckResponse,
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


def request(
    item: VisitDraftInput,
    *,
    user_id: str = "sales-a",
    source_record_id: str | None = "record-a",
) -> PostEvaluationRequest:
    return PostEvaluationRequest(
        context=RequestContext(
            tenant_id="tenant-a",
            request_id="request-a",
            user_id=user_id,
            source="test",
            source_record_id=source_record_id,
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


def test_hash_ignores_platform_fields_but_includes_business_evidence():
    original = visit(metadata={"source_supplied_fields": ["process_description"]})
    saved = original.model_copy(update={
        "submitted_at": datetime(2026, 9, 20, 12, tzinfo=UTC),
        "metadata": {"field_mapping_version": "later"},
        "evidence_ids": ["server-generated"],
    })
    assert analysis_input_hash(original) != analysis_input_hash(saved)
    no_evidence_change = saved.model_copy(update={"evidence_ids": []})
    assert analysis_input_hash(original) == analysis_input_hash(no_evidence_change)
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
        "unresolved": 0,
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


def test_builder_derives_minimal_findings_when_front_review_is_absent():
    built = build_artifact(
        visit=visit(),
        tenant_id="tenant-a",
        user_id="sales-a",
        check_id="qc-advice-findings",
        quick_check_input_hash="c" * 64,
        source_record_id="record-a",
        feedback_text=(
            "本次拜访分析：客户确认已预留预算。\n\n"
            "AI改善建议：\n"
            "1. 关键结果尚未说明客户审批负责人，请补充。\n"
            "2. 请补充下一次联系时间。"
        ),
        decision_ledger={"version": "ledger-v2"},
        front_review=None,
        policy_version="front-policy",
    )
    by_dimension = {item.dimension: item for item in built.findings}
    assert set(by_dimension) == {"O_KR", "N"}
    assert all(item.conclusion == "needs_revision" for item in by_dimension.values())
    assert all(item.finding_type == "gap" for item in by_dimension.values())
    assert {item.dimension: item.finding_id for item in built.suggestions} == {
        "O_KR": by_dimension["O_KR"].finding_id,
        "N": by_dimension["N"].finding_id,
    }


def test_no_improvement_needed_sentence_is_not_persisted_as_a_gap():
    built = build_artifact(
        visit=visit(), tenant_id="tenant-a", user_id="sales-a",
        check_id="qc-no-gap", quick_check_input_hash="f" * 64,
        source_record_id="record-a",
        feedback_text=(
            "本次拜访分析：关键信息已完整确认。\n\n"
            "AI改善建议：\n"
            "本次无需额外补充填写。本次记录已完整覆盖目标与下一步。"
        ),
        decision_ledger={"version": "ledger-v2"}, front_review=None,
        policy_version="front-policy",
    )
    assert built.suggestions == []
    assert built.findings == []


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


def test_formal_scoring_path_never_receives_front_context():
    item = visit(submitted_at=datetime(2026, 9, 20, 13, tzinfo=UTC))

    class FrontSensitiveAgent:
        def __init__(self):
            self.calls = []

        def evaluate(self, evaluation_request, job_id, *, front_analysis=None):
            self.calls.append(front_analysis)
            return TaoranAgent().evaluate(evaluation_request, job_id)

    front = artifact(item)
    agent = FrontSensitiveAgent()
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
    assert agent.calls == [None]
    assert final_diagnostic.front_artifact_used is True


def _save_and_ack(store, value, *, seconds=3600):
    store.save_front_analysis_artifact(
        value.model_dump(mode="json"), retention_until=time() + seconds
    )
    assert store.acknowledge_front_analysis_artifact(
        value.tenant_id, value.check_id, value.quick_check_input_hash
    )


def test_unique_cross_user_artifact_is_never_used(tmp_path):
    store = AgentStore(tmp_path / "agent.db")
    item = visit()
    _save_and_ack(store, artifact(item))
    found, diagnostic = load_front_context(store, request(item, user_id="sales-b"))
    assert found is None
    assert diagnostic.front_artifact_status == "user_mismatch"
    assert diagnostic.other_user_candidate_count == 1


def test_multiple_cross_user_artifacts_are_never_used(tmp_path):
    store = AgentStore(tmp_path / "agent.db")
    item = visit()
    first = artifact(item)
    second = first.model_copy(update={
        "artifact_id": "fa_other_2", "user_id": "sales-c", "check_id": "qc-other-2",
        "quick_check_input_hash": "c" * 64,
    })
    _save_and_ack(store, first)
    _save_and_ack(store, second)
    found, diagnostic = load_front_context(store, request(item, user_id="sales-b"))
    assert found is None
    assert diagnostic.front_artifact_status == "user_mismatch"
    assert diagnostic.other_user_candidate_count == 2


@pytest.mark.parametrize("user_id", ["", "jiandaoyun-user", "jiandaoyun-submit-event", "unknown"])
def test_placeholder_identity_is_untrusted(user_id):
    assert is_trusted_artifact_user_id(user_id) is False


def test_untrusted_formal_identity_uses_standalone_review(tmp_path):
    store = AgentStore(tmp_path / "agent.db")
    item = visit()
    value = artifact(item).model_copy(update={"user_id": "jiandaoyun-user"})
    _save_and_ack(store, value)
    found, diagnostic = load_front_context(
        store, request(item, user_id="jiandaoyun-user")
    )
    assert found is None
    assert diagnostic.front_artifact_status == "identity_untrusted"


def test_artifact_bound_to_other_record_is_rejected(tmp_path):
    store = AgentStore(tmp_path / "agent.db")
    item = visit()
    value = artifact(item).model_copy(update={"source_record_id": "record-a"})
    _save_and_ack(store, value)
    found, diagnostic = load_front_context(
        store, request(item, source_record_id="record-b")
    )
    assert found is None
    assert diagnostic.front_artifact_status == "record_mismatch"
    assert diagnostic.record_match is False


def test_unbound_artifact_is_claimed_once_by_current_record(tmp_path):
    store = AgentStore(tmp_path / "agent.db")
    item = visit()
    value = artifact(item)
    _save_and_ack(store, value)
    found, diagnostic = load_front_context(
        store, request(item, source_record_id="record-b")
    )
    assert found is not None and diagnostic.front_artifact_status == "used"
    with store._lock:
        row = store._connection.execute(
            "SELECT source_record_id FROM front_analysis_artifacts WHERE artifact_id=?",
            (value.artifact_id,),
        ).fetchone()
    assert row["source_record_id"] == "record-b"


def test_unsupported_schema_is_not_used(tmp_path):
    store = AgentStore(tmp_path / "agent.db")
    item = visit()
    value = artifact(item)
    _save_and_ack(store, value)
    with store._lock, store._connection:
        import json
        payload = value.model_dump(mode="json")
        payload["schema_version"] = "front-analysis-artifact-v2"
        store._connection.execute(
            "UPDATE front_analysis_artifacts SET payload_json=? WHERE artifact_id=?",
            (json.dumps(payload, ensure_ascii=False), value.artifact_id),
        )
    found, diagnostic = load_front_context(store, request(item))
    assert found is None
    assert diagnostic.front_artifact_status == "schema_incompatible"


def test_formal_not_evaluated_is_unresolved_not_corrected():
    item = visit()
    front = artifact(item)
    formal = facts([section("T", "not_evaluated", "本项无法评价。")])
    base = DeepReviewResult(
        front_artifact_status="used", front_artifact_used=True,
        submitted_input_hash=front.input_hash,
    )
    result = reconcile(front, formal, base)
    assert result.findings[0].status == "unresolved"
    assert result.counts["corrected"] == 0
    assert result.counts["unresolved"] >= 1


def test_expired_artifact_cleanup_is_explicit_and_safe(tmp_path):
    store = AgentStore(tmp_path / "agent.db")
    value = artifact(visit())
    store.save_front_analysis_artifact(
        value.model_dump(mode="json"), retention_until=time() - 1
    )
    assert store.purge_expired_front_analysis_artifacts() == 1
    with store._lock:
        count = store._connection.execute(
            "SELECT count(*) FROM front_analysis_artifacts"
        ).fetchone()[0]
    assert count == 0


def test_decision_ledger_is_bounded_and_drops_repair_traces():
    built = build_artifact(
        visit=visit(), tenant_id="tenant-a", user_id="sales-a", check_id="qc-ledger",
        quick_check_input_hash="d" * 64, source_record_id=None,
        feedback_text="本次拜访分析：客户确认预算。",
        decision_ledger={
            "version": "v1", "validated_analysis": "客户确认预算。",
            "joint_repair_errors": ["internal"], "raw_model_output": "secret",
        },
        front_review=None, policy_version="p1",
    )
    assert built.decision_ledger == {
        "version": "v1", "validated_analysis": "客户确认预算。"
    }
    with pytest.raises(ValueError, match="size limit"):
        build_artifact(
            visit=visit(), tenant_id="tenant-a", user_id="sales-a", check_id="qc-large",
            quick_check_input_hash="e" * 64, source_record_id=None,
            feedback_text="本次拜访分析：客户确认预算。",
            decision_ledger={"validated_analysis": "大" * 20000},
            front_review=None, policy_version="p1",
        )


def _complete_formal_facts():
    return facts([
        section("T", "met", "客户类型与目的匹配。"),
        section("A1", "met", "拜访方式记录清楚。"),
        section("O_KR", "needs_revision", "正式确认预算已取得，但审批时间尚未明确。"),
        section("R", "met", "客户确认预算30万元。"),
        section("A2", "met", "自评与客户事实一致。"),
        section("N", "needs_revision", "客户尚未确认下一次评审时间。"),
    ])


def test_deep_review_findings_enter_final_feedback_without_internal_labels():
    item = visit()
    formal = _complete_formal_facts()
    deep = DeepReviewResult(
        front_artifact_status="used", front_artifact_used=True,
        submitted_input_hash=analysis_input_hash(item),
        findings=[
            DeepReviewFinding(
                finding_id="d1", status="corrected", dimension="O_KR",
                front_statement="客户采购时间完全不明确。",
                final_statement="正式确认预算已取得，但审批时间尚未明确。",
            ),
            DeepReviewFinding(
                finding_id="d2", status="new_finding", dimension="N",
                final_statement="客户尚未确认下一次评审时间。",
            ),
        ],
    )
    text = build_evaluation_feedback(item, 25, 25, 50, [], formal, deep_review=deep)
    assert "正式确认预算已取得" in text
    assert "客户尚未确认下一次评审时间" in text
    assert "客户采购时间完全不明确" not in text
    assert "corrected" not in text and "new_finding" not in text


def test_deep_review_builder_error_falls_back_to_formal_feedback(monkeypatch):
    from taoran_agent import feedback
    item = visit()
    formal = _complete_formal_facts()
    deep = DeepReviewResult(
        front_artifact_status="used", front_artifact_used=True,
        submitted_input_hash=analysis_input_hash(item),
    )
    expected = build_evaluation_feedback(item, 25, 25, 50, [], formal)
    monkeypatch.setattr(
        feedback, "_apply_deep_review_continuity",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("broken")),
    )
    knowledge = PrecheckResponse.model_construct(suggestions=[], issues=[])
    actual = merge_evaluation_with_knowledge(
        item, 25, 25, 50, [], formal, knowledge, deep_review=deep
    )
    assert actual == expected
