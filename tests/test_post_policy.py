import json

import pytest


def test_post_rendering_preserves_business_units_and_translates_boolean():
    from taoran_agent.feedback import _clean_post_text, _merge_similar_advice
    assert "500g M13和DDQ" in _merge_similar_advice(["核实500g M13和DDQ交付时间"])
    assert "更正为是" in _clean_post_text("如已预约请将是否预约更正为true")
    assert "更正为否" in _clean_post_text("如未预约请更正为false")
    assert "P2阶段" in _clean_post_text("商机第二阶段")


def test_post_failed_model_does_not_show_completed_business_advice():
    from taoran_agent.feedback import build_evaluation_feedback
    from taoran_agent.semantic import HeuristicSemanticReviewer
    facts = HeuristicSemanticReviewer().review_q34(visit()).model_copy(update={
        "provider": "llm-test", "status": "fallback",
        "failure_reason": "unsupported_company_requirement"})
    text = build_evaluation_feedback(None, 0, 0, 0, [], facts)
    assert "额外增加了公司未要求" in text
    assert "AI改善建议" not in text

from taoran_agent.agent import TaoranAgent
from taoran_agent.config import Settings
from taoran_agent.knowledge import load_taoran_knowledge_snapshot
from taoran_agent.llm import ChatModelReviewer
from taoran_agent.models import PostEvaluationRequest, VisitDraftInput
from taoran_agent.post_review_policy import (
    POLICY, PostInputNotReceived, compare_snapshots, knowledge_manifest,
    input_boundary, require_evaluation_input,
)


def visit(**changes):
    data = dict(visit_date="2026-09-03", employee_id="E1", customer_id="C1",
                customer_type_ii="potential", visit_method="face_to_face",
                is_appointment=False, purpose_code="收集信息",
                expected_key_result="确认客户设备数量",
                process_description="客户表示设备数量暂不能披露，但确认由采购部统一负责。",
                self_assessment="not_achieved", next_action_purpose="收集信息",
                next_action_expected_result="客户确认可提供的设备信息范围",
                next_contact_at="2026-10-05T09:00:00+08:00")
    data.update(changes)
    return VisitDraftInput.model_validate(data)


def test_false_is_present_and_empty_is_distinct_from_not_received():
    v = visit(process_description="")
    assert input_boundary(v)["is_appointment"] == "present"
    assert require_evaluation_input(v)["process_description"] == "empty"
    v.metadata["source_supplied_fields"] = sorted(v.model_fields_set - {"process_description"})
    with pytest.raises(PostInputNotReceived, match="process_description"):
        require_evaluation_input(v)


def test_boundary_fails_before_any_scoring_or_model_call():
    v = visit()
    v.metadata["source_supplied_fields"] = ["visit_date"]
    request = PostEvaluationRequest.model_validate({
        "context": {"tenant_id": "test", "request_id": "r1", "source": "test", "user_id": "u1"},
        "visit_record_code": "test-1", "visit": v.model_dump(mode="json"),
    })
    class NeverCalled:
        def review_q34(self, visit):
            pytest.fail("model must not run for transport failures")
    with pytest.raises(PostInputNotReceived):
        TaoranAgent(semantic_reviewer=NeverCalled()).evaluate(request, "job")


def test_audit_detects_unversioned_edit_even_when_provider_hash_is_stale():
    old = load_taoran_knowledge_snapshot()
    new = old.model_copy(deep=True)
    new.records[0].content += "\n正文变化"
    changes = compare_snapshots(old, new)
    assert changes[0]["kind"] == "same_version_content_changed"
    assert knowledge_manifest(old)["actual_records_hash"] != knowledge_manifest(new)["actual_records_hash"]
    assert changes[0]["activation"] == "review_required"
    assert json.loads(json.dumps(knowledge_manifest(old), ensure_ascii=False))["enabled_records"]


def test_new_record_and_search_omission_never_auto_activate():
    old = load_taoran_knowledge_snapshot()
    new = old.model_copy(deep=True)
    new.records[0].id = "DSM-MP-TEST"
    assert {x["kind"] for x in compare_snapshots(old, new)} == {"new_record", "not_returned"}


def test_policy_is_post_only_and_preserves_front_prompt():
    reviewer = ChatModelReviewer(Settings(_env_file=None), load_taoran_knowledge_snapshot())
    try:
        data = reviewer._input(visit(), precheck=False)
        post = reviewer._messages(data, False)[0]["content"]
        front = reviewer._messages(data, True)[0]["content"]
        assert POLICY in post
        assert POLICY not in front
        assert "不得用获得新信息冒充原目标已经达成" in post
        assert "不能自动判达标" in post
        assert "依据真实拜访补充客户角色、确认事项和结果" not in post
        assert "收集信息不等于必须取得订单" in post
        assert "提交后冲突处理" not in front
    finally:
        reviewer.close()


def test_complete_evaluation_retains_audit_through_json_storage():
    from taoran_agent.models import EvaluationResponse
    from taoran_agent.scoring import score_q33, score_q34
    from taoran_agent.semantic import HeuristicSemanticReviewer
    v = visit()
    request = PostEvaluationRequest.model_validate({
        "context": {"tenant_id": "test", "request_id": "r2", "source": "test", "user_id": "u1"},
        "visit_record_code": "test-2", "visit": v.model_dump(mode="json"),
    })
    result = TaoranAgent().evaluate(request, "job2")
    facts = HeuristicSemanticReviewer().review_q34(v)
    assert result.q33_score == score_q33(v)[0].score
    assert result.q34_score == score_q34(v, facts)[0].score
    assert result.total_max_score == 100
    saved = EvaluationResponse.model_validate_json(result.model_dump_json())
    assert saved.knowledge_version_audit["enabled_records"]
    assert saved.input_boundary_audit["process_description"] == "present"


@pytest.mark.parametrize("text,kind,invalid", [
    ("缺口在于缺少联系人角色信息。", "potential", False),
    ("在联系人信息中补充客户角色", "target", False),
    ("下一步行动对象必须填写具体联系人", "target", True),
    ("下次对象缺少联系人姓名", "opportunity", True),
    ("未记录客户角色，但客户已确认采购数量", "target", False),
    ("不清楚是谁确认了预算，建议核实这项确认的来源", "target", False),
    ("本次目标是确认采购负责人，尚未取得负责人信息，需补充核实", "target", False),
    ("下次拜访目标是确认采购负责人，应核实负责人信息", "target", False),
    ("潜力客户缺少下一步承诺", "potential", True),
    ("商机客户缺少客户共识", "opportunity", False),
    ("下一步无需补充联系人", "potential", False),
    ("联系人王经理确认采购时间", "target", False),
])
def test_unapproved_requirements(text, kind, invalid):
    from taoran_agent.post_review_policy import unsupported_requirement
    assert unsupported_requirement(text, kind) is invalid


def test_confirmed_contact_policy_is_in_post_prompt_and_retry_source():
    assert "不强制补具体联系人" in POLICY
    assert "允许指出具体事实来源缺口" in POLICY
    assert "不能用联系人可选的保护规则免除该目标检查" in POLICY
    reviewer = ChatModelReviewer(Settings(_env_file=None), load_taoran_knowledge_snapshot())
    try:
        post = reviewer._messages(reviewer._input(visit(), precheck=False), False)[0]["content"]
        assert "不得把联系人或客户角色缺少列为不达标原因或补填建议" not in post
        assert "正常检查该目标，不能免检" in post
    finally:
        reviewer.close()


def test_unapproved_requirement_has_bounded_regeneration():
    from taoran_agent.llm import _format_retry_allowed, ModelCallError
    assert _format_retry_allowed(ModelCallError("unsupported_company_requirement"))


def test_optional_role_does_not_become_empty_required_input():
    reviewer = ChatModelReviewer(Settings(_env_file=None), load_taoran_knowledge_snapshot())
    try:
        v = visit(participants=[{"contact_id":"c1"}])
        assert "participants" not in reviewer._input(v, precheck=False)
        assert "participants" in reviewer._input(v, precheck=True)
        v = visit(participants=[{"contact_id":"c1","role":"采购经理"}])
        assert reviewer._input(v, precheck=False)["participants"][0]["角色"] == "采购经理"
    finally:
        reviewer.close()
