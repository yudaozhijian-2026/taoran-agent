from copy import deepcopy

import pytest

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
    assert "TAORAN六项检查：" in result.feedback_text
    assert result.feedback_text.count("：达标。\n检查标准：") == 6
    assert "T｜客户类型：达标。" in result.feedback_text
    assert "A｜预约与拜访方式：达标。" in result.feedback_text
    assert "O/KR｜拜访目的与关键结果：达标。" in result.feedback_text
    assert "R｜过程事实与结果：达标。" in result.feedback_text
    assert "A｜达成评价：达标。" in result.feedback_text
    assert "N｜下一步客户行动：达标。" in result.feedback_text
    assert "记录完整度" not in result.feedback_text
    assert "/100" not in result.feedback_text
    assert "提交成功后，系统将自动进行深度评价并回写正式评分与反馈意见" in result.feedback_text
    assert result.engine_version == "TAORAN-PRECHECK-KB-V2"
    assert {item.id for item in result.knowledge_references} == {
        "DSM-BS-000",
        "DSM-BS-01-07",
    }


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
    assert "PRECHECK_CUSTOMER_ID_MISSING" in codes
    assert "AI调用异常。异常原因：系统未获取当前客户标识" in result.feedback_text
    assert "TAORAN_KR_MISSING" in codes
    assert "O/KR｜拜访目的与关键结果：待改进。\n检查标准：" in result.feedback_text
    assert "N｜下一步客户行动：待改进。\n检查标准：" in result.feedback_text
    assert "A｜预约与拜访方式：达标。\n检查标准：" in result.feedback_text
    assert "R｜过程事实与结果：待改进。\n检查标准：" in result.feedback_text
    assert result.feedback_text.count("达成评价缺少关键结果和过程事实支持") == 1


def test_purpose_policy_mismatch_is_explainable() -> None:
    payload = complete_precheck_payload()
    payload["visit"]["purpose_code"] = "collect_information"

    result = TaoranAgent().precheck(PrecheckRequest.model_validate(payload))

    issue = next(
        issue
        for issue in result.issues
        if issue.code == "TAORAN_T03_PURPOSE_POLICY_MISMATCH"
    )
    assert issue.field_paths == ["purpose_code", "purpose_policy"]
    assert result.record_quality_score == 85
    assert next(section for section in result.taoran_sections if section.code == "T").status == (
        "needs_revision"
    )
    assert result.can_submit is True


def test_same_input_produces_same_check_id_and_hash() -> None:
    agent = TaoranAgent()
    request = PrecheckRequest.model_validate(complete_precheck_payload())

    first = agent.precheck(request)
    second = agent.precheck(request)

    assert first.check_id == second.check_id
    assert first.input_snapshot_hash == second.input_snapshot_hash


def test_same_snapshot_with_new_click_request_gets_new_check_id() -> None:
    first_payload = complete_precheck_payload("click-001")
    second_payload = deepcopy(first_payload)
    second_payload["context"]["request_id"] = "click-002"

    first = TaoranAgent().precheck(PrecheckRequest.model_validate(first_payload))
    second = TaoranAgent().precheck(PrecheckRequest.model_validate(second_payload))

    assert first.input_snapshot_hash == second.input_snapshot_hash
    assert first.check_id != second.check_id


def test_same_input_is_namespaced_by_tenant() -> None:
    first_payload = complete_precheck_payload("req-tenant-a")
    second_payload = deepcopy(first_payload)
    second_payload["context"]["tenant_id"] = "tenant_other"

    first = TaoranAgent().precheck(PrecheckRequest.model_validate(first_payload))
    second = TaoranAgent().precheck(PrecheckRequest.model_validate(second_payload))

    assert first.input_snapshot_hash == second.input_snapshot_hash
    assert first.check_id != second.check_id


def test_precheck_never_uses_remote_reviewer_or_its_blocking_issue() -> None:
    request = PrecheckRequest.model_validate(complete_precheck_payload())

    result = TaoranAgent(BlockingSemanticReviewer()).precheck(request)

    assert "MODEL_WANTS_TO_BLOCK" not in {item.code for item in result.issues}
    assert result.semantic_review.provider == "heuristic-v1"
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
    assert result.q33_score == 50
    assert result.q34_score == 50
    assert result.total_score == 100
    assert result.effectiveness_score == 100
    assert result.effectiveness_level == "high_quality"
    assert result.count_as_effective_visit_recommendation == "yes"
    assert "【提交后TAORAN深度评价｜AI反馈意见】" in result.ai_opinion
    assert "TAORAN六项判断" in result.ai_opinion
    assert "T｜客户类型" in result.ai_opinion
    assert "N｜下一步客户行动" in result.ai_opinion


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


def test_target_customer_single_unappointed_is_only_checked_in_period_aggregate() -> None:
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

    assert result.q34_score == 50
    assert "Q34_APPOINTMENT_STANDARD_NOT_MET" not in {issue.code for issue in result.issues}
    consistency = next(
        item for item in result.question_scores[1].components
        if item.code == "self_evaluation_consistency"
    )
    assert "目标客户单次未预约不扣分" in consistency.details["appointment_projection_note"]


def test_standard_audit_is_saved_without_changing_score_contract() -> None:
    payload = complete_precheck_payload("eval-audit-001")
    payload["visit"].update({
        "actual_start_at": "2026-08-18T09:00:00+08:00",
        "actual_end_at": "2026-08-18T10:00:00+08:00",
        "submitted_at": "2026-08-18T11:00:00+08:00",
        "metadata": {"field_mapping_version": "mapping-test-v1"},
    })
    request = PostEvaluationRequest.model_validate({
        "context": payload["context"],
        "visit_record_code": "BFJL-AUDIT-001",
        "visit": payload["visit"],
    })

    result = TaoranAgent().evaluate(request, "job-audit")

    assert result.total_max_score == 100
    assert result.rule_version == "TAORAN-Q33-Q34-100-V2"
    assert result.standard_audit is not None
    assert result.standard_audit.standard_id == "DSM-BS-01-07"
    assert result.standard_audit.standard_version == "TAORAN-EVIDENCE-V1.1"
    assert result.standard_audit.field_mapping_version == "mapping-test-v1"
    assert len(result.standard_audit.standard_content_hash) == 64


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

    assert score.score == 15
    assert score.components[0].passed is False


@pytest.mark.parametrize("contact_at", [
    "2026-08-17T10:00:00+08:00", "2026-08-18T20:00:00+08:00",
])
def test_advisory_date_validation_does_not_relax_post_scoring(contact_at):
    payload = complete_precheck_payload()["visit"]
    payload["next_contact_at"] = contact_at
    visit = VisitDraftInput.model_validate(payload)
    facts = Q34SemanticFacts(
        provider="test", key_result_quality_ok=True, process_fact_based=True,
        purpose_achievement="achieved", next_action_logic_ok=True,
        customer_consensus_met=True, reason="模拟模型将下一行动判为合格，日期规则仍须生效。",
    )
    score, issues = score_q34(visit, facts)
    action = next(c for c in score.components if c.code == "next_action_quality")

    assert action.score == 0
    assert action.passed is False
    assert score.score == 35
    assert "Q34_NEXT_ACTION_NOT_QUALIFIED" in {i.code for i in issues}


@pytest.mark.parametrize(("customer_type", "visit_date", "contact_at"), [
    ("opportunity", "2026-08-18", "2026-08-18T16:00:00Z"),
    ("target", "2026-08-31", "2026-08-31T16:00:00Z"),
    ("potential", "2026-09-30", "2026-09-30T16:00:00Z"),
])
def test_next_contact_business_timezone_is_shared_by_precheck_and_post_scoring(
    customer_type, visit_date, contact_at,
):
    from taoran_agent.semantic import HeuristicSemanticReviewer

    payload = complete_precheck_payload()["visit"]
    payload.update(customer_type_ii=customer_type, visit_date=visit_date, next_contact_at=contact_at)
    visit = VisitDraftInput.model_validate(payload)
    facts = HeuristicSemanticReviewer().review_q34(visit)
    score, _ = score_q34(visit, facts)
    action = next(c for c in score.components if c.code == "next_action_quality")

    assert facts.next_action_logic_ok is True
    assert action.score == 15
    assert action.passed is True
