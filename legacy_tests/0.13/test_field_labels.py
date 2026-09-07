from test_agent import complete_precheck_payload

from taoran_agent import TaoranAgent
from taoran_agent.field_labels import display_field_name
from taoran_agent.models import PostEvaluationRequest, PrecheckRequest


def test_field_paths_use_actual_jiandaoyun_labels() -> None:
    assert display_field_name("expected_key_result") == "想取得的关键结果"
    assert display_field_name("process_description") == "过程详细描述"
    assert display_field_name("next_contact_at") == "下一次联系客户时间安排"
    assert (
        display_field_name("opportunities[].current_stage")
        == "关联商机阶段信息.最新商机阶段"
    )


def test_precheck_feedback_does_not_expose_internal_field_names() -> None:
    payload = complete_precheck_payload("field-label-precheck")
    payload["visit"]["expected_key_result"] = None
    payload["visit"]["process_description"] = None
    payload["visit"]["next_contact_at"] = None

    result = TaoranAgent().precheck(PrecheckRequest.model_validate(payload))

    assert "想取得的关键结果" in result.feedback_text
    assert "过程详细描述" in result.feedback_text
    assert "下一次联系客户时间安排" in result.feedback_text
    assert "expected_key_result" not in result.feedback_text
    assert "process_description" not in result.feedback_text
    assert "next_contact_at" not in result.feedback_text
    assert "不阻断" not in result.feedback_text


def test_deep_evaluation_opinion_uses_actual_subform_field_name() -> None:
    payload = complete_precheck_payload("field-label-evaluation")
    payload["visit"].update(
        {
            "opportunities": [{"opportunity_id": "OPP001"}],
            "actual_start_at": "2026-08-18T09:00:00+08:00",
            "actual_end_at": "2026-08-18T10:00:00+08:00",
            "submitted_at": "2026-08-18T11:00:00+08:00",
        }
    )
    request = {
        "context": payload["context"],
        "visit_record_code": "FIELD-LABEL-001",
        "visit": payload["visit"],
    }

    result = TaoranAgent().evaluate(PostEvaluationRequest.model_validate(request), "job-label")

    assert "关联商机阶段信息.最新商机阶段" in result.ai_opinion
    assert "opportunities[].current_stage" not in result.ai_opinion
