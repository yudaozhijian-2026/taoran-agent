import pytest
from test_post_policy import visit
from test_post_repair import reviewer, valid_payload

from taoran_agent.feedback import _build_post_advice, build_evaluation_feedback
from taoran_agent.knowledge import load_taoran_knowledge_snapshot
from taoran_agent.llm import ModelCallError
from taoran_agent.models import Q34SemanticFacts
from taoran_agent.post_quality import PostFeedbackConflict, quality_context, quality_hits


def test_summary_cannot_call_mismatched_self_assessment_objective():
    from taoran_agent.post_quality import assessment_summary_hits
    text = "销售自评部分达到客观反映了沟通完成但未促成合作。"
    assert assessment_summary_hits(text, True)[0]["target"] == "facts.reason"
    assert not assessment_summary_hits(text, False)
    assert not assessment_summary_hits("销售自评与实际达成不一致，不能认为客观。", True)


def test_p3_collect_information_is_authoritative_and_not_a_kr_pass():
    v = visit(customer_type_ii="opportunity", opportunity_stage="P3", purpose_code="收集信息")
    context = quality_context(v, load_taoran_knowledge_snapshot())
    assert context["purpose"]["matches"] is True
    assert set(context["purpose"]["allowed"]) == {"收集信息","强化关系","其他目的","击败竞争对手"}
    assert quality_hits("拜访目的与P3阶段不匹配", "T", context)
    assert not quality_hits("关键结果未说明具体收集什么信息", "O_KR", context)
    assert not quality_hits("拜访目的与关键结果不一致", "O_KR", context)
    assert quality_hits("客户反馈字段未传入", "R", context)


def test_optional_evidence_and_purpose_context_only_in_post_prompt(tmp_path):
    r = reviewer(tmp_path)
    try:
        v = visit()
        post = r._messages(r._input(v,precheck=False),False)[0]["content"]
        assert '"required_evidence"' not in post
        assert '"optional_evidence_types"' in post
        assert "authoritative_checks=" in post
        front = r._messages(r._input(v,precheck=True),True)[0]["content"]
        assert "authoritative_checks=" not in front
    finally:
        r.close()


@pytest.mark.parametrize("case", ["purpose", "basis_missing", "false_empty"])
def test_quality_conflicts_have_targeted_locations(tmp_path,case):
    r=reviewer(tmp_path)
    v=visit(customer_type_ii="opportunity",opportunity_stage="P3",purpose_code="收集信息")
    data=r._input(v,precheck=False)
    payload=valid_payload(r,v)
    if case == "purpose":
        payload["sections"][0]["reason"]="拜访目的与P3阶段不匹配"
    elif case == "basis_missing":
        payload["sections"][0]["advice_basis"]=None
    else:
        payload["sections"][0]["advice_basis"]["gap_kind"]="missing_value"
    try:
        with pytest.raises(ModelCallError,match="post_feedback_conflict") as e:
            r._validate(payload,data,False)
        assert e.value.details["hits"][0]["target"]=="T"
        assert e.value.details["hits"][0]["path"].startswith("sections.T")
    finally:
        r.close()


def test_met_sections_do_not_get_knowledge_filler_and_final_conflict_blocks(tmp_path):
    r=reviewer(tmp_path)
    v=visit(customer_type_ii="opportunity",opportunity_stage="P3",purpose_code="收集信息")
    payload=valid_payload(r,v)
    for s in payload["sections"]:
        s.update(verdict="met",suggestion="",advice_basis=None)
        s.pop("advice_basis")
    facts=Q34SemanticFacts(**payload["facts"],provider="llm-chat",sections=payload["sections"],
        quality_audit={"authoritative_checks":quality_context(v,load_taoran_knowledge_snapshot()),"advice_basis":{}})
    assert _build_post_advice([],facts,model_completed=True,knowledge_issues=[],
        knowledge_suggestions=["补充联系人角色使过程更完整"])==[]
    facts.reason="拜访目的与P3阶段不匹配"
    with pytest.raises(PostFeedbackConflict):
        build_evaluation_feedback(v,50,50,100,[],facts)
    r.close()
