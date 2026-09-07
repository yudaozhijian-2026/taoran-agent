from copy import deepcopy
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_api import isolated_database  # noqa: F401
from test_writeback import evaluation_request

from taoran_agent import TaoranAgent, api
from taoran_agent.calibration import CalibrationDataset, CalibrationThresholds
from taoran_agent.config import Settings
from taoran_agent.models import EvaluationResponse, SelfAssessment
from taoran_agent.q40_integration import _record_facts, rule_compatibility
from taoran_agent.rules import canonical_hash, load_rule_catalog
from taoran_agent.scoring import band_score, score_q33, score_q34
from taoran_agent.scoring_contract import LEGACY_TOTAL_RULE_VERSION, TOTAL_RULE_VERSION
from taoran_agent.semantic import HeuristicSemanticReviewer
from taoran_agent.writeback import writeback_evaluation


def legacy_result(request, job_id="legacy-job"):
    data = TaoranAgent().evaluate(request, job_id).model_dump(mode="json")
    for field in ("q33_score", "q34_score", "total_score", "total_max_score"):
        data[field] *= 2
    data["rule_version"] = LEGACY_TOTAL_RULE_VERSION
    data["agent_version"] = "0.6.5"
    data["ai_opinion"] = "历史200分制评分，不自动换算。"
    for question in data["question_scores"]:
        question["score"] *= 2
        question["max_score"] *= 2
        question["rule_version"] = f"TAORAN-{question['question_code']}-100-V1"
        for component in question["components"]:
            component["score"] *= 2
            component["max_score"] *= 2
    for dimension in data["dimensions"]:
        dimension["score"] *= 2
        dimension["max_score"] *= 2
    return EvaluationResponse.model_validate(data)


def test_all_score_surfaces_and_catalog_use_100_scale():
    result = TaoranAgent().evaluate(evaluation_request(), "scale-full")
    assert (result.q33_score, result.q34_score, result.total_score) == (50, 50, 100)
    assert result.overall_percentage == result.effectiveness_score == 100
    assert result.total_max_score == 100
    assert [q.max_score for q in result.question_scores] == [50, 50]
    assert [d.max_score for d in result.dimensions] == [50, 50]
    assert [c.max_score for q in result.question_scores for c in q.components] == [25, 25, 35, 15]
    assert "100.00/100（Q33 50.00/50；Q34 50.00/50）" in result.ai_opinion
    assert "/200" not in result.ai_opinion
    catalog = load_rule_catalog()
    assert catalog["rule_version"] == TOTAL_RULE_VERSION
    assert catalog["total_max_score"] == 100
    for question in result.question_scores:
        configured = catalog["questions"][question.question_code]
        assert configured["max_score"] == question.max_score
        for component in question.components:
            assert configured["components"][component.code]["max_score"] == component.max_score


@pytest.mark.parametrize(("rate", "points"), [
    (1, 25), (.9, 25), (.8999, 18.75), (.8, 18.75), (.7999, 12.5),
    (.7, 12.5), (.6999, 6.25), (.5, 6.25), (.4999, 0), (0, 0),
])
def test_q33_thresholds_keep_exact_quarter_points(rate, points):
    assert band_score(rate, (.9, .8, .7, .5)) / 4 * 25 == points


@pytest.mark.parametrize(("count", "points"), [
    (10, 25), (9, 25), (8, 18.75), (7, 12.5), (6, 6.25), (5, 6.25), (4, 0),
])
def test_q33_real_complete_field_counts(count, points):
    draft = evaluation_request().visit
    fields = ["expected_key_result", "process_description", "self_assessment",
              "next_action_expected_result", "purpose_code", "is_appointment"]
    draft = draft.model_copy(update={key: None for key in fields[:10-count]})
    result, _ = score_q33(draft)
    assert result.components[0].details["present_field_count"] == count
    assert result.components[0].score == points
    assert result.score == points + 25


@pytest.mark.parametrize(("hours", "points"), [(0, 25), (24, 25), (24.001, 0), (-.001, 0)])
def test_q33_timeliness_boundaries_are_unchanged(hours, points):
    draft = evaluation_request().visit
    draft = draft.model_copy(update={"submitted_at": draft.actual_end_at + timedelta(hours=hours)})
    result, _ = score_q33(draft)
    assert result.components[1].score == points


@pytest.mark.parametrize(("consistent", "action_ok", "expected"), [
    (True, True, 50), (True, False, 35), (False, True, 15), (False, False, 0),
])
def test_q34_binary_components_keep_weights(consistent, action_ok, expected):
    draft = evaluation_request().visit
    facts = HeuristicSemanticReviewer().review_q34(draft).model_copy(update={
        "purpose_achievement": draft.self_assessment if consistent else SelfAssessment.NOT_ACHIEVED,
        "next_action_logic_ok": action_ok,
    })
    result, _ = score_q34(draft, facts)
    assert result.score == expected


def test_feedback_preserves_6_25_precision():
    request = evaluation_request()
    draft = request.visit.model_copy(update={
        "expected_key_result": None, "process_description": None,
        "self_assessment": None, "next_action_expected_result": None,
    })
    result = TaoranAgent().evaluate(request.model_copy(update={"visit": draft}), "decimal")
    assert result.q33_score == 31.25
    assert "Q33 31.25/50" in result.ai_opinion


def test_legacy_reads_without_conversion_and_cannot_write(monkeypatch):
    request = evaluation_request()
    result = legacy_result(request)
    assert result.total_score == result.total_max_score == 200
    monkeypatch.setattr("taoran_agent.writeback.httpx.post", lambda *a, **k: pytest.fail("network"))
    written = writeback_evaluation(Settings(_env_file=None), request, result)
    assert written.status == "failed" and "历史评分" in written.error_message
    roundtrip = EvaluationResponse.model_validate_json(result.model_dump_json())
    assert roundtrip == result


@pytest.mark.parametrize("mutation", ["total_max", "question_max", "total", "percentage", "component"])
def test_mixed_scale_or_inconsistent_scores_rejected(mutation):
    data = TaoranAgent().evaluate(evaluation_request(), "validate").model_dump(mode="json")
    if mutation == "total_max":
        data["total_max_score"] = 200
    elif mutation == "question_max":
        data["question_scores"][0]["max_score"] = 100
    elif mutation == "total":
        data["total_score"] = 200
    elif mutation == "percentage":
        data["overall_percentage"] = 50
    else:
        data["question_scores"][1]["components"][0]["score"] = 70
    with pytest.raises(ValidationError):
        EvaluationResponse.model_validate(data)


def seed_legacy_job():
    request = evaluation_request()
    snapshot = canonical_hash(request)
    job_id = f"job_{snapshot[:20]}"
    store = api.get_store()
    store.create_evaluation_job(job_id, request, snapshot)
    result = legacy_result(request, job_id)
    store.complete_evaluation(result)
    return request, job_id, result


def test_legacy_job_readable_but_retry_or_reuse_cannot_publish_old_scale(monkeypatch):
    request, job_id, result = seed_legacy_job()
    monkeypatch.setattr(api, "writeback_evaluation", lambda *a: pytest.fail("writeback"))
    client = TestClient(api.app)
    url = f"/api/v1/visit/evaluations/{job_id}"
    assert client.get(url, params={"tenant_id": "tenant_demo"}).json()["response"]["total_max_score"] == 200
    assert client.post(url+"/writeback", params={"tenant_id": "tenant_demo"}).status_code == 409
    assert client.post("/api/v1/visit/evaluations", json=request.model_dump(mode="json")).status_code == 409
    stored = api.get_store().get_evaluation("tenant_demo", job_id)
    assert stored["response"] == result.model_dump(mode="json")


def test_q40_contract_exposes_units_and_rejects_old_version():
    assert rule_compatibility(TOTAL_RULE_VERSION).compatible is True
    assert rule_compatibility(LEGACY_TOTAL_RULE_VERSION).compatible is False
    request = evaluation_request()
    result = TaoranAgent().evaluate(request, "q40-units")
    facts = _record_facts({"request": request.model_dump(), "response": result.model_dump()})
    assert (facts.q33_max_score, facts.q34_max_score, facts.total_max_score) == (50, 50, 100)
    assert (facts.q33_score_projection, facts.q34_score_projection, facts.total_score_projection) == (50, 50, 100)


def test_new_q40_batch_does_not_reuse_legacy_child(monkeypatch):
    from test_api import _enable_q40

    request = evaluation_request()
    _enable_q40(monkeypatch)
    request = request.model_copy(update={"writeback_target": None})
    # Seed a separate legacy no-writeback child with the exact batch input hash.
    request = request.model_copy(update={"context": request.context.model_copy(update={"request_id": "legacy-batch-child"})})
    snapshot = canonical_hash(request)
    job_id = f"job_{snapshot[:20]}"
    api.get_store().create_evaluation_job(job_id, request, snapshot)
    api.get_store().complete_evaluation(legacy_result(request, job_id))
    body = {"tenant_id": "tenant_demo", "request_id": "batch-v2", "requested_by": "test",
            "required_rule_version": TOTAL_RULE_VERSION, "evaluations": [request.model_dump(mode="json")]}
    client = TestClient(api.app)
    headers = {"X-Service-Id": "dsm-q40-agent", "X-Service-Key": "q40-secret"}
    response = client.post("/api/v1/integrations/q40/evaluations:batch", json=body, headers=headers)
    assert response.status_code == 202
    result = client.get(f"/api/v1/integrations/q40/batches/{response.json()['batch_job_id']}",
                        params={"tenant_id": "tenant_demo"}, headers=headers).json()["response"]
    assert result["failed_count"] == 1 and result["reused_count"] == 0
    assert api.get_store().get_evaluation("tenant_demo", job_id)["response"]["total_max_score"] == 200


def test_calibration_requires_explicit_new_scale_and_scaled_tolerance():
    from test_calibration import matching_dataset

    data = matching_dataset().model_dump(mode="json")
    assert data["rule_version"] == TOTAL_RULE_VERSION
    missing = deepcopy(data)
    missing.pop("rule_version")
    with pytest.raises(ValidationError):
        CalibrationDataset.model_validate(missing)
    data["samples"][0]["expert"]["q34_score"] = 100
    with pytest.raises(ValidationError):
        CalibrationDataset.model_validate(data)
    thresholds = CalibrationThresholds()
    assert (thresholds.q33_tolerance, thresholds.q34_tolerance) == (.005, 5)
