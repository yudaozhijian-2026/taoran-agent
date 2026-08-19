import pytest
from fastapi.testclient import TestClient
from test_agent import complete_precheck_payload

from taoran_agent import api
from taoran_agent.config import get_settings


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    monkeypatch.setenv("DSM_TAORAN_DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DSM_TAORAN_ENVIRONMENT", "development")
    monkeypatch.setenv("DSM_TAORAN_TENANT_KEYS_JSON", "{}")
    monkeypatch.setenv("DSM_TAORAN_ENABLE_Q40_INTEGRATION", "false")
    monkeypatch.setenv("DSM_TAORAN_Q40_SERVICE_KEYS_JSON", "{}")
    get_settings.cache_clear()
    api._stores.clear()
    api._agents.clear()
    yield
    get_settings.cache_clear()
    api._stores.clear()
    api._agents.clear()


def test_health() -> None:
    response = TestClient(api.app).get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_precheck_is_persisted_and_idempotent() -> None:
    client = TestClient(api.app)
    payload = complete_precheck_payload("idem-001")

    first = client.post("/api/v1/visit/checks", json=payload)
    second = client.post("/api/v1/visit/checks", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["check_id"] == second.json()["check_id"]


def test_idempotency_key_rejects_changed_input() -> None:
    client = TestClient(api.app)
    payload = complete_precheck_payload("idem-002")
    assert client.post("/api/v1/visit/checks", json=payload).status_code == 200
    payload["visit"]["process_description"] = "修改后的不同输入"

    response = client.post("/api/v1/visit/checks", json=payload)

    assert response.status_code == 409


def test_async_evaluation_job_completes() -> None:
    client = TestClient(api.app)
    payload = complete_precheck_payload("async-001")
    payload["visit"].update(
        {
            "actual_start_at": "2026-08-18T09:00:00+08:00",
            "actual_end_at": "2026-08-18T10:00:00+08:00",
            "submitted_at": "2026-08-18T11:00:00+08:00",
        }
    )
    evaluation = {
        "context": payload["context"],
        "visit_record_code": "BFJL001",
        "visit": payload["visit"],
        "evidence": [{"evidence_id": "EV001", "source_object": "VisitEvent"}],
        "opportunity_updated": True,
    }

    submitted = client.post("/api/v1/visit/evaluations", json=evaluation)
    job_id = submitted.json()["job_id"]
    record = client.get(f"/api/v1/visit/evaluations/{job_id}", params={"tenant_id": "tenant_demo"})

    assert submitted.status_code == 202
    assert record.status_code == 200
    assert record.json()["status"] == "completed"
    assert record.json()["response"]["total_score"] == 200
    assert record.json()["response"]["effectiveness_score"] == 100


def test_q40_reserved_endpoints_are_disabled_by_default() -> None:
    response = TestClient(api.app).get(
        "/api/v1/integrations/q40/compatibility",
        params={
            "tenant_id": "tenant_demo",
            "required_rule_version": "TAORAN-Q33-Q34-200-V1",
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "q40 integration is disabled"


def test_q40_can_query_period_facts_when_explicitly_enabled(monkeypatch) -> None:
    _enable_q40(monkeypatch)
    client = TestClient(api.app)
    evaluation = _evaluation_payload("q40-period-001", "BFJL-Q40-001")
    assert client.post("/api/v1/visit/evaluations", json=evaluation).status_code == 202

    response = client.get(
        "/api/v1/integrations/q40/period-facts",
        params={
            "tenant_id": "tenant_demo",
            "employee_id": "EMP001",
            "period_start": "2026-08-01",
            "period_end": "2026-08-31",
            "required_rule_version": "TAORAN-Q33-Q34-200-V1",
            "expected_visit_record_count": 1,
        },
        headers={"X-Service-Id": "dsm-q40-agent", "X-Service-Key": "q40-secret"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["evaluated_record_count"] == 1
    assert body["coverage_rate"] == 1.0
    assert body["records"][0]["q33_timely_submission"] is True
    assert body["records"][0]["q34_self_evaluation_consistent"] is True


def test_q40_batch_backfill_is_idempotent_when_enabled(monkeypatch) -> None:
    _enable_q40(monkeypatch)
    client = TestClient(api.app)
    batch = {
        "tenant_id": "tenant_demo",
        "request_id": "q40-batch-001",
        "requested_by": "Q40-SERVICE",
        "required_rule_version": "TAORAN-Q33-Q34-200-V1",
        "evaluations": [_evaluation_payload("q40-child-001", "BFJL-Q40-BATCH-001")],
    }
    headers = {"X-Service-Id": "dsm-q40-agent", "X-Service-Key": "q40-secret"}

    first = client.post(
        "/api/v1/integrations/q40/evaluations:batch", json=batch, headers=headers
    )
    second = client.post(
        "/api/v1/integrations/q40/evaluations:batch", json=batch, headers=headers
    )
    batch_job_id = first.json()["batch_job_id"]
    result = client.get(
        f"/api/v1/integrations/q40/batches/{batch_job_id}",
        params={"tenant_id": "tenant_demo"},
        headers=headers,
    )

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["batch_job_id"] == second.json()["batch_job_id"]
    assert result.status_code == 200
    assert result.json()["status"] == "completed"
    assert result.json()["response"]["completed_count"] == 1


def test_q40_rejects_incompatible_rule_without_starting_batch(monkeypatch) -> None:
    _enable_q40(monkeypatch)
    client = TestClient(api.app)
    batch = {
        "tenant_id": "tenant_demo",
        "request_id": "q40-batch-incompatible",
        "requested_by": "Q40-SERVICE",
        "required_rule_version": "TAORAN-FUTURE-RULE",
        "evaluations": [_evaluation_payload("q40-child-future", "BFJL-Q40-FUTURE")],
    }

    response = client.post(
        "/api/v1/integrations/q40/evaluations:batch",
        json=batch,
        headers={"X-Service-Id": "dsm-q40-agent", "X-Service-Key": "q40-secret"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["compatible"] is False


def test_q40_batch_forbids_jiandaoyun_writeback(monkeypatch) -> None:
    _enable_q40(monkeypatch)
    evaluation = _evaluation_payload("q40-child-writeback", "BFJL-Q40-WRITEBACK")
    evaluation["writeback_target"] = {
        "app_id": "app",
        "entry_id": "entry",
        "data_id": "data",
    }
    batch = {
        "tenant_id": "tenant_demo",
        "request_id": "q40-batch-writeback",
        "requested_by": "Q40-SERVICE",
        "required_rule_version": "TAORAN-Q33-Q34-200-V1",
        "evaluations": [evaluation],
    }

    response = TestClient(api.app).post(
        "/api/v1/integrations/q40/evaluations:batch",
        json=batch,
        headers={"X-Service-Id": "dsm-q40-agent", "X-Service-Key": "q40-secret"},
    )

    assert response.status_code == 422
    assert "不得触发简道云回写" in response.text


def _enable_q40(monkeypatch) -> None:
    monkeypatch.setenv("DSM_TAORAN_ENABLE_Q40_INTEGRATION", "true")
    monkeypatch.setenv(
        "DSM_TAORAN_Q40_SERVICE_KEYS_JSON", '{"tenant_demo":"q40-secret"}'
    )
    get_settings.cache_clear()
    api._stores.clear()
    api._agents.clear()


def _evaluation_payload(request_id: str, visit_record_code: str) -> dict:
    payload = complete_precheck_payload(request_id)
    payload["visit"].update(
        {
            "actual_start_at": "2026-08-18T09:00:00+08:00",
            "actual_end_at": "2026-08-18T10:00:00+08:00",
            "submitted_at": "2026-08-18T11:00:00+08:00",
        }
    )
    return {
        "context": payload["context"],
        "visit_record_code": visit_record_code,
        "visit": payload["visit"],
        "opportunity_updated": True,
    }
