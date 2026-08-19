from copy import deepcopy

from taoran_agent import TaoranAgent
from taoran_agent.models import (
    Issue,
    PostEvaluationRequest,
    PrecheckRequest,
    Q34SemanticFacts,
    SemanticReview,
    Severity,
    VisitDraftInput,
)
from taoran_agent.scoring import score_q34
from taoran_agent.semantic import SemanticReviewer


class BlockingSemanticReviewer(SemanticReviewer):
    def review(self, visit) -> SemanticReview:
        return SemanticReview(
            status="completed",
            provider="untrusted-test-reviewer",
            issues=[
                Issue(
                    code="MODEL_WANTS_TO_BLOCK",
                    dimension="O",
                    severity=Severity.BLOCKING,
                    field_paths=["expected_key_result"],
                    message="模型建议阻断。",
                    suggestion="人工核对。",
                    source="semantic",
                )
            ],
        )


def complete_precheck_payload(request_id: str = "req-001") -> dict:
    return {
        "context": {
            "tenant_id": "tenant_demo",
            "request_id": request_id,
            "user_id": "EMP001",
            "source": "test",
            "form_revision": "draft-1",
        },
        "visit": {
            "visit_date": "2026-08-18",
            "employee_id": "EMP001",
            "customer_id": "KH001",
            "customer_type_ii": "opportunity",
            "opportunity_id": "OPP001",
            "opportunity_stage": "P3",
            "visit_method": "face_to_face",
            "is_appointment": True,
            "participants": [{"contact_id": "CONTACT001"}],
            "purpose_code": "advance_opportunity",
            "expected_key_result": "客户确认技术验证日期和预算审批责任人",
            "process_description": "信息中心主任确认8月25日验证，并确认财务负责人负责预算审批。",
            "self_assessment": "achieved",
            "next_action_target_id": "CONTACT001",
            "next_action_purpose": "发送验证清单并确认参会人员",
            "next_action_expected_result": "客户书面确认验证范围和参会角色",
            "next_contact_at": "2026-08-20T10:00:00+08:00",
            "evidence_ids": ["EV001"],
            "purpose_policy": {
                "policy_version": "demo-v1",
                "status": "published",
                "allowed_purposes": ["advance_opportunity"],
                "effective_from": "2026-01-01",
            },
        },
    }


def test_complete_precheck_passes_with_100() -> None:
    result = TaoranAgent().precheck(PrecheckRequest.model_validate(complete_precheck_payload()))

    assert result.status == "passed"
    assert result.can_submit is True
    assert result.record_quality_score == 100
    assert result.level == "A"
    assert result.blocking_issues == []


def test_missing_core_fields_returns_advice_without_blocking() -> None:
    payload = complete_precheck_payload()
    payload["visit"]["customer_id"] = None
    payload["visit"]["expected_key_result"] = "客户有兴趣"
    payload["visit"]["process_description"] = "沟通一下"
    payload["visit"]["next_action_target_id"] = None
    payload["visit"]["next_action_purpose"] = None
    payload["visit"]["next_action_expected_result"] = None
    payload["visit"]["next_contact_at"] = None

    result = TaoranAgent().precheck(PrecheckRequest.model_validate(payload))
    codes = {issue.code for issue in result.issues}

    assert result.can_submit is True
    assert result.submission_blocked is False
    assert result.status == "needs_revision"
    assert result.blocking_issues == []
    assert "CUSTOMER_ID_MISSING" in codes
    assert "TAORAN_FIELD_MISSING" in codes


def test_purpose_policy_mismatch_is_explainable() -> None:
    payload = complete_precheck_payload()
    payload["visit"]["purpose_code"] = "collect_information"

    result = TaoranAgent().precheck(PrecheckRequest.model_validate(payload))

    issue = next(issue for issue in result.issues if issue.code == "PURPOSE_POLICY_MISMATCH")
    assert issue.field_paths == ["purpose_code", "purpose_policy"]
    assert result.record_quality_score == 100
    assert result.can_submit is True


def test_same_input_produces_same_check_id_and_hash() -> None:
    agent = TaoranAgent()
    request = PrecheckRequest.model_validate(complete_precheck_payload())

    first = agent.precheck(request)
    second = agent.precheck(request)

    assert first.check_id == second.check_id
    assert first.input_snapshot_hash == second.input_snapshot_hash


def test_same_input_is_namespaced_by_tenant() -> None:
    first_payload = complete_precheck_payload("req-tenant-a")
    second_payload = deepcopy(first_payload)
    second_payload["context"]["tenant_id"] = "tenant_other"

    first = TaoranAgent().precheck(PrecheckRequest.model_validate(first_payload))
    second = TaoranAgent().precheck(PrecheckRequest.model_validate(second_payload))

    assert first.input_snapshot_hash == second.input_snapshot_hash
    assert first.check_id != second.check_id


def test_semantic_reviewer_cannot_create_blocking_issue() -> None:
    request = PrecheckRequest.model_validate(complete_precheck_payload())

    result = TaoranAgent(BlockingSemanticReviewer()).precheck(request)

    issue = next(item for item in result.issues if item.code == "MODEL_WANTS_TO_BLOCK")
    assert issue.severity == Severity.WARNING
    assert result.status == "passed"
    assert result.can_submit is True
    assert result.blocking_issues == []


def test_post_evaluation_uses_evidence_and_context() -> None:
    payload = complete_precheck_payload("eval-001")
    payload["visit"].update(
        {
            "actual_start_at": "2026-08-18T09:00:00+08:00",
            "actual_end_at": "2026-08-18T10:00:00+08:00",
            "submitted_at": "2026-08-18T11:00:00+08:00",
        }
    )
    request_payload = {
        "context": payload["context"],
        "visit_record_code": "BFJL001",
        "visit": payload["visit"],
        "previous_visit_summary": "上次确认需要澄清数据同步范围。",
        "evidence": [{"evidence_id": "EV001", "source_object": "VisitEvent"}],
        "opportunity_updated": True,
    }
    result = TaoranAgent().evaluate(PostEvaluationRequest.model_validate(request_payload), "job1")

    assert result.status == "completed"
    assert result.q33_score == 100
    assert result.q34_score == 100
    assert result.total_score == 200
    assert result.effectiveness_score == 100
    assert result.effectiveness_level == "high_quality"
    assert result.count_as_effective_visit_recommendation == "yes"


def test_post_evaluation_flags_missing_opportunity_update() -> None:
    payload = complete_precheck_payload("eval-002")
    payload["visit"].update(
        {
            "actual_start_at": "2026-08-18T09:00:00+08:00",
            "actual_end_at": "2026-08-18T10:00:00+08:00",
            "submitted_at": "2026-08-18T11:00:00+08:00",
        }
    )
    request_payload = {
        "context": payload["context"],
        "visit_record_code": "BFJL002",
        "visit": deepcopy(payload["visit"]),
        "evidence": [{"evidence_id": "EV001", "source_object": "VisitEvent"}],
        "opportunity_updated": False,
    }
    result = TaoranAgent().evaluate(PostEvaluationRequest.model_validate(request_payload), "job2")

    assert "OPPORTUNITY_NOT_UPDATED" in {issue.code for issue in result.issues}
    assert result.count_as_effective_visit_recommendation == "manager_review"


def test_target_customer_single_record_requires_appointment_proxy() -> None:
    payload = complete_precheck_payload("eval-target-001")
    payload["visit"].update(
        {
            "customer_type_ii": "target",
            "is_appointment": False,
            "next_contact_at": "2026-09-02T10:00:00+08:00",
            "actual_start_at": "2026-08-18T09:00:00+08:00",
            "actual_end_at": "2026-08-18T10:00:00+08:00",
            "submitted_at": "2026-08-18T11:00:00+08:00",
        }
    )
    request = PostEvaluationRequest.model_validate(
        {
            "context": payload["context"],
            "visit_record_code": "BFJL-TARGET-001",
            "visit": payload["visit"],
        }
    )

    result = TaoranAgent().evaluate(request, "job-target")

    assert result.q34_score == 30
    assert "Q34_APPOINTMENT_STANDARD_NOT_MET" in {issue.code for issue in result.issues}


def test_q34_ai_cannot_bypass_key_result_quality_gate() -> None:
    visit = VisitDraftInput.model_validate(complete_precheck_payload()["visit"])
    facts = Q34SemanticFacts(
        provider="test",
        key_result_quality_ok=False,
        process_fact_based=True,
        purpose_achievement="achieved",
        next_action_logic_ok=True,
        customer_consensus_met=True,
        reason="模型错误地给出完全达成。",
    )

    score, _ = score_q34(visit, facts)

    assert score.score == 30
    assert score.components[0].passed is False
