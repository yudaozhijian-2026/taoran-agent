"""Cross-surface and metamorphic tests, independent of the 15 live records."""

import json

import pytest
from test_post_policy import visit
from test_post_repair import reviewer, valid_payload

from taoran_agent.experimental_business_semantic_state import build_business_state
from taoran_agent.experimental_semantic_streaming_v22 import _interactive_snapshot
from taoran_agent.goal_contract import GoalReview, review_hits
from taoran_agent.llm import ModelCallError, _evidence_catalog
from taoran_agent.record_contract import contract_from_values, field_claim_hits, visit_contract


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, "empty"),
        ("", "empty"),
        ("  ", "empty"),
        ("客户表示暂不采购", "present"),
        (False, "present"),
        (0, "present"),
    ],
)
def test_presence_is_not_content_quality(value, expected):
    assert (
        contract_from_values({"customer_feedback": value})["presence"]["customer_feedback"]
        == expected
    )
    assert (
        contract_from_values({"customer_feedback": value}, [])["presence"]["customer_feedback"]
        == "not_received"
    )


@pytest.mark.parametrize("supplied", [True, False])
def test_identical_presence_and_revision_in_preview_final_and_backend(tmp_path, supplied):
    from taoran_agent.api import _knowledge_model_context
    from taoran_agent.models import PrecheckRequest, RequestContext

    v = visit(customer_feedback="")
    v.metadata["source_supplied_fields"] = list(
        v.model_fields_set - ({"customer_feedback"} if not supplied else set())
    )
    r = reviewer(tmp_path)
    try:
        request = PrecheckRequest(
            context=RequestContext(tenant_id="test", request_id="same-input", user_id="tester"),
            visit=v,
        )
        final = _knowledge_model_context(request, r.snapshot, experimental=True)["visit_snapshot"][
            "_record_contract"
        ]
        preview = _interactive_snapshot(v)["_record_contract"]
        post = r._input(v, precheck=False)["_record_contract"]
        assert preview == final == post == visit_contract(v)
        assert post["presence"]["customer_feedback"] == ("empty" if supplied else "not_received")
    finally:
        r.close()


@pytest.mark.parametrize("text", ["客户反馈尚未填写。", "未填写客户反馈。", "客户反馈字段为空。"])
def test_unmapped_field_cannot_be_reported_unfilled(text):
    assert field_claim_hits(text, {})
    assert not field_claim_hits(text, {"customer_feedback": ""})
    assert field_claim_hits(text, {"customer_feedback": "客户明确拒绝采购"})


def test_fact_clarification_is_not_a_blanket_field_requirement():
    assert not field_claim_hits("请说明采购方案由谁确认，当前记录尚不能证明客户认可。", {})
    assert not field_claim_hits("过程未体现客户对方案的实际反馈。", {})


@pytest.mark.parametrize(
    "source,actor",
    [
        ("现场给客户收货，送卡", "unknown"),
        ("现场给客户收货", "unknown"),
        ("销售现场给客户收货", "sales"),
        ("客户已完成收货", "customer"),
    ],
)
def test_actor_does_not_depend_on_exact_golden_case(source, actor):
    state = build_business_state({"expected_key_result": "现场收货", "process_description": source})
    assert state["facts"][0]["actor"] == actor
    from taoran_agent.experimental_receipt_role import receipt_role_hint

    assert (
        receipt_role_hint({"expected_key_result": "现场收货", "process_description": source}) == ""
    )


@pytest.mark.parametrize(
    "source", ["双方约定下次拜访", "简单沟通后约定下次拜访", "与客户商定下次沟通"]
)
def test_joint_agreement_is_preserved(source):
    state = build_business_state({"process_description": source})
    assert state["relations"]["JOINT_AGREEMENT"]["status"] == "recorded"


def test_negative_need_answer_supports_information_not_purchase_commitment():
    state = build_business_state(
        {
            "expected_key_result": "客户确认现阶段需求，并同意反馈具体条件",
            "process_description": "客户表示暂无采购计划。",
        }
    )
    assert len(state["goal_items"]) == 2
    assert state["goal_items"][0]["status"] == "supported"
    assert state["goal_items"][1]["status"] == "unresolved"


def test_plan_is_not_completed_and_source_revision_changes():
    actual = visit(process_description="客户已完成签字")
    planned = visit(process_description="客户计划下周签字")
    assert visit_contract(actual)["source_hash"] != visit_contract(planned)["source_hash"]
    assert (
        build_business_state(_interactive_snapshot(actual))["facts"][0]["temporality"] == "actual"
    )
    assert (
        build_business_state(_interactive_snapshot(planned))["facts"][0]["temporality"] == "planned"
    )


def test_calendar_not_inferred_from_defaulted_visit_date():
    v = visit()
    v.metadata["precheck_defaulted_fields"] = ["visit_date"]
    assert "calendar" not in visit_contract(v)
    assert (
        visit_contract(visit(next_contact_at="2026-09-03T09:00:00+08:00"))["calendar"][
            "after_visit"
        ]
        is False
    )


def test_unknown_material_actor_cannot_support_goal(tmp_path):
    r = reviewer(tmp_path)
    v = visit(expected_key_result="客户确认方案")
    data = r._input(v, precheck=False)
    catalog = _evidence_catalog(data)
    event = next(e for e in catalog if e["field"] == "process_description")
    try:
        row = GoalReview(
            goal_id="G1",
            status="supported",
            evidence_ids=[event["evidence_id"]],
            actor_ambiguity_material=True,
            reason="确认主体不明。",
        )
        assert any(
            h["rule"] == "material_actor_unknown_as_supported"
            for h in review_hits([row], data, catalog, "achieved")
        )
        plan = next(e for e in catalog if e["field"] == "next_action_expected_result")
        row.actor_ambiguity_material = False
        row.evidence_ids = [plan["evidence_id"]]
        assert any(
            h["rule"] == "goal_evidence_not_actual_source"
            for h in review_hits([row], data, catalog, "achieved")
        )
    finally:
        r.close()


def test_omitted_original_goal_fails_before_any_wording_repair(tmp_path):
    r = reviewer(tmp_path)
    v = visit(expected_key_result="确认预算，并确认采购负责人")
    payload = valid_payload(r, v)
    payload["goal_reviews"].pop()
    try:
        with pytest.raises(ModelCallError, match="post_fact_grounding_conflict"):
            r._validate(payload, r._input(v, precheck=False), False)
    finally:
        r.close()


def test_independent_semantic_gate_does_not_control_scoring(tmp_path, monkeypatch):
    from copy import deepcopy

    r = reviewer(tmp_path)
    r.settings = r.settings.model_copy(update={"llm_enabled": True})
    v = visit()
    first = valid_payload(r, v)
    second = deepcopy(first)
    second["facts"]["process_fact_based"] = True
    generated = []
    audited = []

    def model(messages, precheck, timeout, lease, **kwargs):
        lease.release()
        generated.append(kwargs.get("repair", False))
        return deepcopy(first if len(generated) == 1 else second), {}

    def gate(context, analysis, suggestions, timeout, audit, **kwargs):
        assert kwargs["stage"] == "backend"
        audited.append(analysis)
        if len(audited) == 1:
            audit.update(status="rejected", failed_checks=["goal"])
            kwargs["repair_details"]["semantic_issues"] = [
                {"check": "goal", "reason": "原目标范围被扩大"}
            ]
            raise ModelCallError("wording_experimental_audit_goal")
        audit.update(status="passed")

    monkeypatch.setattr(r, "_request", model)
    monkeypatch.setattr(r, "_experimental_audit_wording", gate)
    try:
        result = r.review_q34(v)
        assert result.status == "completed"
        assert result.process_fact_based == first["facts"]["process_fact_based"]
        assert generated == [False] and not audited
        assert result.quality_audit["semantic_gate"]["status"] == "observed"
        assert result.quality_audit["fact_grounding_reassessed"] is False
    finally:
        r.close()


def test_unavailable_independent_gate_is_never_a_pass(tmp_path, monkeypatch):
    r = reviewer(tmp_path)
    r.settings = r.settings.model_copy(update={"llm_enabled": True})
    v = visit()
    payload = valid_payload(r, v)

    def model(messages, precheck, timeout, lease, **kwargs):
        lease.release()
        return payload, {}

    def gate(*args, **kwargs):
        raise ModelCallError("wording_experimental_audit_upstream")

    monkeypatch.setattr(r, "_request", model)
    monkeypatch.setattr(r, "_experimental_audit_wording", gate)
    try:
        result = r.review_q34(v)
        assert result.status == "completed"
        assert result.quality_audit["semantic_gate"]["status"] == "observed"
    finally:
        r.close()


@pytest.mark.parametrize("goal", ["现场收货", "完成设备安装", "完成货物配送"])
def test_action_goal_does_not_acquire_customer_acceptance_requirement(goal):
    from taoran_agent.record_contract import goal_scope_hits

    ctx = {"expected_key_result": goal}
    assert goal_scope_hits("未体现客户确认完成，目标缺少事实支撑。", ctx)
    assert not goal_scope_hits("请核实实际动作及结果。", ctx)
    assert not goal_scope_hits("无需客户签收证明该动作发生。", ctx)
    assert not goal_scope_hits("客户已签收。", ctx)
    assert not goal_scope_hits("下一步请取得客户签收。", ctx, "N")
    assert not goal_scope_hits("未体现客户签收。", {"expected_key_result": "取得客户签收确认"})


def test_current_goal_contract_is_not_a_golden_case_allowlist():
    from taoran_agent.experimental_assessment import approved_goal_instruction

    assert (
        approved_goal_instruction(
            {"expected_key_result": "沟通采购情况", "process_description": "客户表示暂无需求"}
        )
        == ""
    )


def test_explicit_process_plan_can_support_next_analysis_but_not_actual_completion():
    from taoran_agent.experimental_rendering_guidance import next_action_fact_ids, rendering_input

    context = {
        "expected_key_result": "客户已完成签署合同",
        "process_description": "客户表示预算已批准。下一步计划：销售下周发送合同。",
        "next_action_expected_result": "确认条款",
    }
    state = build_business_state(context)
    planned = [
        f
        for f in state["facts"]
        if f["source_field"] == "process_description" and f["temporality"] == "planned"
    ]
    assert planned and all(f["fact_id"] in next_action_fact_ids(state) for f in planned)
    contract = next(
        c for c in rendering_input(state)["RENDERING_CONTRACTS"] if c["contract_id"] == "C_NEXT"
    )
    assert "process_description" in contract["source_fields"]
    assert not {f["fact_id"] for f in planned} & set(
        state["goal_fact_alignments"][0]["supporting_fact_ids"]
    )


def test_scope_gate_uses_section_and_keeps_future_advice_separate():
    from taoran_agent.experimental_record_state import boundary_issues

    ctx = {"expected_key_result": "现场收货", "process_description": "现场给客户收货"}
    assert any(
        h["error_type"] == "unrequested_acceptance_condition"
        for h in boundary_issues("未体现客户签收，目标无法确定。", ctx)
    )
    assert not any(
        h["error_type"] == "unrequested_acceptance_condition"
        for h in boundary_issues("需补充客户签收。", ctx, target="N")
    )


@pytest.mark.parametrize("field", ["process_description", "customer_feedback"])
@pytest.mark.parametrize(
    "quote,allowed",
    [
        ("等明年续标前再确定。", True),
        ("下个月计划核对报价。", True),
        ("已完成合同签署。", False),
        ("客户表示预算尚未批准。", False),
        ("客户已经完成下月安排的发货。", False),
    ],
)
def test_next_step_proof_scope_uses_explicit_plan(field, quote, allowed):
    from taoran_agent.experimental_rendering_guidance import next_step_proof_allowed

    assert next_step_proof_allowed(field, quote) is allowed


def test_unassessable_goal_can_quote_goal_but_cannot_use_it_as_achievement():
    data = {"expected_key_result": "收集信息", "process_description": "客户表示预算尚未批准。"}
    catalog = [{"evidence_id": "goal", "field": "expected_key_result", "quote": "收集信息"}]
    row = GoalReview(
        goal_id="G1",
        status="not_assessable",
        evidence_ids=["goal"],
        actor_ambiguity_material=False,
        reason="目标没有说明待收集的信息事项。",
    )
    assert not review_hits([row], data, catalog, "not_achieved")
    row.status = "supported"
    assert any(
        h["rule"] == "goal_evidence_not_actual_source"
        for h in review_hits([row], data, catalog, "achieved")
    )


def test_independent_reviewer_receives_sources_not_keyword_attainment():
    from taoran_agent.experimental_semantic_audit import messages

    payload = json.loads(
        messages(
            {"expected_key_result": "现场收货", "process_description": "现场给客户收货"},
            "动作结果待核实",
            [],
        )[1]["content"]
    )
    assert set(payload["record_state"]) == {"version", "field_states", "sources", "goal"}
    assert payload["record_state"]["sources"]


def test_goal_comparison_can_cite_both_goal_and_actual_process():
    data = {"expected_key_result": "取得参与权", "process_description": "客户表示需要进一步评估。"}
    catalog = [
        {
            "evidence_id": "goal",
            "field": "expected_key_result",
            "quote": data["expected_key_result"],
        },
        {
            "evidence_id": "event",
            "field": "process_description",
            "quote": data["process_description"],
        },
    ]
    row = GoalReview(
        goal_id="G1",
        status="insufficient_evidence",
        evidence_ids=["goal", "event"],
        actor_ambiguity_material=False,
        reason="客户仍需评估，记录不足以证明已经取得参与权。",
    )
    assert not review_hits([row], data, catalog, "not_achieved")
