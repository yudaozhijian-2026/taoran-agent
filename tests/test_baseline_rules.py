from datetime import UTC, datetime, timedelta

import pytest
from test_post_policy import visit

from taoran_agent.models import CustomerTypeII, SelfAssessment
from taoran_agent.scoring import score_q33, score_q34
from taoran_agent.semantic import HeuristicSemanticReviewer


def test_target_appointment_does_not_change_score():
    v = visit(customer_type_ii="target", self_assessment="achieved")
    facts = HeuristicSemanticReviewer().review_q34(v).model_copy(update={
        "purpose_achievement": SelfAssessment.ACHIEVED,
        "key_result_quality_ok": True, "process_fact_based": True,
        "next_action_logic_ok": True,
    })
    assert score_q34(v, facts)[0].score == score_q34(v.model_copy(update={"is_appointment": True}), facts)[0].score
    assert not any(i.code == "Q34_APPOINTMENT_STANDARD_NOT_MET" for i in score_q34(v, facts)[1])
    opportunity = v.model_copy(update={"customer_type_ii": CustomerTypeII.OPPORTUNITY})
    assert any(i.code == "Q34_APPOINTMENT_STANDARD_NOT_MET" for i in score_q34(opportunity, facts)[1])


def test_p6_retires_purpose_matching():
    from taoran_agent.knowledge import load_taoran_knowledge_snapshot
    from taoran_agent.purpose_mapping import purpose_policy_for_visit, structure_purpose_mapping
    record = next(r for r in load_taoran_knowledge_snapshot().records if r.id == "DSM-BS-01-06")
    v = visit(customer_type_ii="opportunity", opportunity_stage="P6")
    policy = purpose_policy_for_visit(structure_purpose_mapping(record), v)
    assert policy.status == "retired"
    assert policy.allowed_purposes == []


@pytest.mark.parametrize("hours,passed", [(0,True),(24,True),(24.001,False),(-0.001,False)])
def test_fixed_timeliness(hours, passed):
    end = datetime(2026,9,3,10,tzinfo=UTC)
    v = visit(actual_end_at=end, submitted_at=end+timedelta(hours=hours))
    score, _ = score_q33(v)
    assert score.components[1].passed == passed
    assert score.max_score == 50


def test_refetch_is_authorized_and_uses_original_target(monkeypatch):
    from fastapi import BackgroundTasks

    from taoran_agent import api
    calls = []
    monkeypatch.setattr(api, "authorize", lambda *args: calls.append("authorized"))
    raw = {"context":{"tenant_id":"t","user_id":"u","request_id":"r","source":"test"},
           "visit_record_code":"v", "visit":visit().model_dump(mode="json"),
           "writeback_target":{"app_id":"a","entry_id":"e","data_id":"d"}}
    class Store:
        def get_evaluation(self, tenant, job):
            assert calls == ["authorized"]
            return {"status":"failed","request":raw}
    monkeypatch.setattr(api,"get_store",lambda:Store())
    def submitted(event, *args):
        assert (event.tenant_id,event.app_id,event.entry_id,event.data_id)==("t","a","e","d")
        return "queued"
    monkeypatch.setattr(api,"submit_jiandaoyun_record_event",submitted)
    assert api.refetch_retry_evaluation("job_1",BackgroundTasks(),"t","t","key")=="queued"
