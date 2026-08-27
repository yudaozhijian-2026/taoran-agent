import hashlib
import json

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
    monkeypatch.setenv("DSM_TAORAN_JIANDAOYUN_API_KEYS_JSON", "{}")
    monkeypatch.setenv("DSM_TAORAN_LLM_ENABLED", "false")
    monkeypatch.setenv("DSM_TAORAN_SEMANTIC_ENDPOINT", "")
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


def test_model_button_still_returns_advice_only(monkeypatch):
    from test_llm import reviewer_for

    from taoran_agent.agent import TaoranAgent

    reviewer, calls = reviewer_for()
    agent = TaoranAgent(reviewer)
    monkeypatch.setattr(api, "get_agent", lambda: agent)
    payload = complete_precheck_payload()
    response = TestClient(api.app).post(
        "/api/v1/connectors/jiandaoyun/visit/button-check",
        json={"context": payload["context"], "form_data": payload["visit"]},
    )
    reviewer.close()
    assert response.status_code == 200
    assert response.json()["official_score_generated"] is False
    assert "record_quality_score" not in response.json()
    assert "分析方式" not in response.json()["feedback_text"]
    assert "知识依据" not in response.json()["feedback_text"]
    assert "规则＋大模型" not in response.json()["feedback_text"]
    assert calls == []


def test_model_failure_retry_requires_enabled_model_and_reanalysis(monkeypatch):
    from test_llm import deep_payload, model_settings, reviewer_for
    from test_writeback import evaluation_request

    from taoran_agent.agent import TaoranAgent
    from taoran_agent.models import WritebackResult

    failing_reviewer, _ = reviewer_for(lambda data: {})
    monkeypatch.setattr(api, "get_agent", lambda: TaoranAgent(failing_reviewer))
    client = TestClient(api.app)
    request = evaluation_request()
    posted = client.post("/api/v1/visit/evaluations", json=request.model_dump(mode="json"))
    job_id = posted.json()["job_id"]
    failing_reviewer.close()
    result = client.get(f"/api/v1/visit/evaluations/{job_id}", params={"tenant_id": "tenant_demo"})
    assert result.json()["response"]["writeback"]["status"] == "failed"
    url = f"/api/v1/visit/evaluations/{job_id}/writeback"
    disabled = client.post(url, params={"tenant_id": "tenant_demo"})
    assert disabled.status_code == 409

    settings = model_settings(database_path=api.get_settings().database_path)
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    successful_reviewer, calls = reviewer_for(deep_payload)
    monkeypatch.setattr(api, "get_agent", lambda: TaoranAgent(successful_reviewer))
    written = []

    def fake_writeback(settings, request, evaluation):
        assert evaluation.semantic_facts.status == "completed"
        written.append(evaluation.total_score)
        return WritebackResult(status="succeeded")

    monkeypatch.setattr(api, "writeback_evaluation", fake_writeback)
    retried = client.post(url, params={"tenant_id": "tenant_demo"})
    successful_reviewer.close()
    assert retried.status_code == 200
    assert len(calls) == 1
    assert written == [100]
    assert retried.json()["writeback"]["status"] == "succeeded"


def test_model_activation_changes_auto_submission_identity_without_breaking_idempotency(monkeypatch):
    from taoran_agent.agent import TaoranAgent

    record = {"_id": "MODEL-ACTIVATION-TEST", "createTime": "2026-08-18T03:00:00Z",
              **complete_precheck_payload()["visit"]}
    monkeypatch.setattr(api, "get_jiandaoyun_record", lambda *args: record)
    monkeypatch.setattr(api, "get_agent", lambda: TaoranAgent())
    client = TestClient(api.app)
    url = "/api/v1/connectors/jiandaoyun/visit/submitted"
    payload = {"tenant_id": "tenant_demo", "data_id": record["_id"]}
    first = client.post(url, json=payload)
    monkeypatch.setenv("DSM_TAORAN_LLM_ENABLED", "true")
    monkeypatch.setenv("DSM_TAORAN_LLM_API_URL", "https://model.example.test/chat/completions")
    monkeypatch.setenv("DSM_TAORAN_LLM_API_KEY", "dedicated-test-key")
    monkeypatch.setenv("DSM_TAORAN_LLM_MODEL", "glm-4.5-air")
    get_settings.cache_clear()
    second = client.post(url, json=payload)
    repeated = client.post(url, json=payload)
    assert first.status_code == second.status_code == repeated.status_code == 202
    assert first.json()["job_id"] != second.json()["job_id"]
    assert second.json()["job_id"] == repeated.json()["job_id"]


def test_jiandaoyun_mapping_requires_tenant_auth(monkeypatch) -> None:
    monkeypatch.setenv(
        "DSM_TAORAN_TENANT_KEYS_JSON", '{"tenant_demo":"tenant-secret"}'
    )
    get_settings.cache_clear()
    client = TestClient(api.app)

    unauthenticated = client.get(
        "/api/v1/connectors/jiandaoyun/mapping",
        params={"tenant_id": "tenant_demo"},
    )
    authenticated = client.get(
        "/api/v1/connectors/jiandaoyun/mapping",
        params={"tenant_id": "tenant_demo"},
        headers={"X-Tenant-Id": "tenant_demo", "X-API-Key": "tenant-secret"},
    )

    assert unauthenticated.status_code == 401
    assert authenticated.status_code == 200
    assert authenticated.json()["status"] == "copy_widget_ids_synced"


def test_precheck_is_persisted_and_idempotent() -> None:
    client = TestClient(api.app)
    payload = complete_precheck_payload("idem-001")

    first = client.post("/api/v1/visit/checks", json=payload)
    second = client.post("/api/v1/visit/checks", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["check_id"] == second.json()["check_id"]


def test_jiandaoyun_button_accepts_flat_front_event_payload() -> None:
    payload = {
        "tenant_id": "tenant_demo",
        "request_id": "fixed-front-event-request-id",
        "user_id": "EMP001",
        "visit_date": "2026-08-19",
        "employee_id": "EMP001",
        "customer_id": "KH001",
        "customer_type_ii": "商机客户",
        "visit_method": "面对面拜访",
        "is_appointment": "是",
        "purpose_code": "推进商机",
        "expected_key_result": "客户确认验证日期和参会角色",
        "process_description": "客户确认验证日期，并提出明确补充要求。",
        "self_assessment": "达到目的",
        "next_action_purpose": "发送清单并确认参会人员",
        "next_action_expected_result": "客户书面确认验证范围",
        "next_contact_at": "2026-08-20T10:00:00+08:00",
    }
    response = TestClient(api.app).post(
        "/api/v1/connectors/jiandaoyun/visit/button-check",
        json=payload,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["can_submit"] is True
    assert body["stage"] == "pre_submit_advice"
    assert body["official_score_generated"] is False
    assert "record_quality_score" not in body
    assert "dimensions" not in body
    assert "taoran_sections" not in body
    assert body["request_id"].startswith("jdy_button_")
    assert "提交前TAORAN检查" in body["feedback_text"]
    assert "TAORAN六项检查：" in body["feedback_text"]
    assert body["feedback_text"].count("检查标准：") == 6
    assert "记录完整度" not in body["feedback_text"]
    assert "/100" not in body["feedback_text"]
    assert "不阻断" not in body["feedback_text"]
    assert "提交成功后，系统将自动进行深度评价并回写正式评分与反馈意见" in body["feedback_text"]
    assert body["engine_version"] == "TAORAN-PRECHECK-KB-V1"
    assert body["knowledge_references"][1]["id"] == "DSM-BS-01-07"

    repeated = TestClient(api.app).post(
        "/api/v1/connectors/jiandaoyun/visit/button-check",
        json=payload,
    )

    assert repeated.status_code == 200
    assert repeated.json()["check_id"] != body["check_id"]
    assert repeated.json()["request_id"] != body["request_id"]

    payload["process_description"] = "修改表单内容后再次进行AI检测。"
    after_edit = TestClient(api.app).post(
        "/api/v1/connectors/jiandaoyun/visit/button-check",
        json=payload,
    )

    assert after_edit.status_code == 200
    assert after_edit.json()["request_id"] != repeated.json()["request_id"]
    assert after_edit.json()["input_snapshot_hash"] != repeated.json()["input_snapshot_hash"]


def test_jiandaoyun_button_normalizes_front_event_null_strings() -> None:
    response = TestClient(api.app).post(
        "/api/v1/connectors/jiandaoyun/visit/button-check",
        json={
            "tenant_id": "tenant_demo",
            "visit_date": "2026-08-19",
            "employee_id": "EMP001",
            "next_contact_at": "null",
            "actual_start_at": "undefined",
            "actual_end_at": "",
            "evidence_ids": "null",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["can_submit"] is True
    assert "不阻断" not in body["feedback_text"]


@pytest.mark.parametrize("shape", ["flat", "structured", "widgets", "standard"])
@pytest.mark.parametrize("day", ["2026-08-17", "2026-08-18"])
def test_date_anomaly_returns_feedback_instead_of_server_error(shape, day, monkeypatch):
    from test_llm import reviewer_for

    from taoran_agent.agent import TaoranAgent
    from taoran_agent.connector import load_jiandaoyun_mapping

    reviewer, model_calls = reviewer_for()
    monkeypatch.setattr(api, "get_agent", lambda: TaoranAgent(reviewer))
    payload = complete_precheck_payload()
    payload["visit"]["next_contact_at"] = f"{day}T10:00:00+08:00"
    url = "/api/v1/connectors/jiandaoyun/visit/button-check"
    if shape == "structured":
        body = {"context": payload["context"], "form_data": payload["visit"]}
    elif shape == "standard":
        body = payload
        url = "/api/v1/visit/checks"
    elif shape == "widgets":
        fields = load_jiandaoyun_mapping()["fields"]
        body = {"tenant_id": "tenant_demo"}
        for field in ("visit_date", "employee_id", "next_contact_at"):
            body[fields[field]["widget_id"]] = {"value": payload["visit"][field]}
    else:
        body = {"tenant_id": "tenant_demo", **payload["visit"]}
    try:
        response = TestClient(api.app, raise_server_exceptions=False).post(url, json=body)
    finally:
        reviewer.close()

    assert response.status_code == 200
    result = response.json()
    assert result["can_submit"] is True
    assert result["submission_blocked"] is False
    assert result["status"] == "needs_revision"
    assert "TAORAN_NSA_TIME_NOT_AFTER_VISIT" in {i["code"] for i in result["issues"]}
    assert "TAORAN_NSA_TIME_MISSING" not in {i["code"] for i in result["issues"]}
    assert "下一次联系客户时间安排：异常。异常说明：" in result["feedback_text"]
    assert "N｜下一步客户行动：未达标" in result["feedback_text"]
    assert day in result["feedback_text"]
    assert "2026-08-18" in result["feedback_text"]
    for hidden in ("next_contact_at", "visit_date", "知识依据", "分析方式", "不阻断"):
        assert hidden not in result["feedback_text"]
    if shape != "standard":
        assert result["official_score_generated"] is False
        assert "record_quality_score" not in result
    assert model_calls == []


def test_repeated_date_anomaly_then_correction_refreshes_button_feedback():
    client = TestClient(api.app, raise_server_exceptions=False)
    payload = {"tenant_id": "tenant_demo", **complete_precheck_payload()["visit"]}
    payload["next_contact_at"] = "2026-08-17T10:00:00+08:00"
    url = "/api/v1/connectors/jiandaoyun/visit/button-check"
    first = client.post(url, json=payload)
    second = client.post(url, json=payload)
    payload["next_contact_at"] = "2026-08-19T10:00:00+08:00"
    corrected = client.post(url, json=payload)

    assert [r.status_code for r in (first, second, corrected)] == [200, 200, 200]
    assert first.json()["check_id"] != second.json()["check_id"]
    assert "下一次联系客户时间安排：异常" in second.json()["feedback_text"]
    assert "下一次联系客户时间安排：异常" not in corrected.json()["feedback_text"]
    assert "N｜下一步客户行动：达标。\n检查标准：" in corrected.json()["feedback_text"]
    # The fixture's scalar opportunity_stage has no Jiandaoyun source mapping.
    # Date correction cannot silently imply the missing T context was checked.
    assert corrected.json()["status"] == "review"
    assert corrected.json()["input_snapshot_hash"] != second.json()["input_snapshot_hash"]


@pytest.mark.parametrize("missing_date", [None, "omitted"])
def test_default_visit_date_does_not_create_false_next_contact_anomaly(missing_date):
    payload = {"tenant_id": "tenant_demo", "next_contact_at": "2020-01-01T10:00:00+08:00"}
    if missing_date is None:
        payload["visit_date"] = None
    response = TestClient(api.app, raise_server_exceptions=False).post(
        "/api/v1/connectors/jiandaoyun/visit/button-check", json=payload,
    )
    assert response.status_code == 200
    result = response.json()
    assert "请补充“拜访日期”" in result["feedback_text"]
    assert "下一次联系客户时间安排：异常" not in result["feedback_text"]
    assert "TAORAN_NSA_TIME_NOT_AFTER_VISIT" not in {i["code"] for i in result["issues"]}


@pytest.mark.parametrize("day", ["2026-08-17", "2026-08-18"])
def test_submitted_visit_with_date_anomaly_can_be_evaluated_without_awarding_next_action(day):
    client = TestClient(api.app)
    payload = complete_precheck_payload()
    payload["visit_record_code"] = "SYNTHETIC-DATE-ANOMALY"
    payload["visit"].update(
        next_contact_at=f"{day}T10:00:00+08:00",
        actual_start_at="2026-08-18T09:00:00+08:00",
        actual_end_at="2026-08-18T10:00:00+08:00",
        submitted_at="2026-08-18T11:00:00+08:00",
    )
    accepted = client.post("/api/v1/visit/evaluations", json=payload)
    assert accepted.status_code == 202
    job = client.get(
        f"/api/v1/visit/evaluations/{accepted.json()['job_id']}",
        params={"tenant_id": "tenant_demo"},
    ).json()
    assert job["status"] == "completed"
    result = job["response"]
    assert result["q33_score"] == 50
    assert result["q34_score"] == 35
    assert result["total_score"] == 85
    assert result["writeback"]["status"] == "skipped"
    assert "Q34_NEXT_ACTION_NOT_QUALIFIED" in {i["code"] for i in result["issues"]}


def test_jiandaoyun_button_returns_advice_when_core_context_is_null() -> None:
    response = TestClient(api.app).post(
        "/api/v1/connectors/jiandaoyun/visit/button-check",
        json={
            "tenant_id": "tenant_demo",
            "visit_date": None,
            "employee_id": None,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["can_submit"] is True
    assert body["submission_blocked"] is False
    assert "请补充“拜访日期”" in body["feedback_text"]
    assert "请补充“销售代表（通讯录）”" in body["feedback_text"]


def test_jiandaoyun_button_shows_unreceived_section_without_claiming_pass_or_failure() -> None:
    response = TestClient(api.app).post(
        "/api/v1/connectors/jiandaoyun/visit/button-check",
        json={
            "tenant_id": "tenant_demo",
            "visit_date": "2026-08-19",
            "employee_id": "EMP001",
            "customer_id": "KH001",
            "customer_type_ii": "目标客户",
            "visit_method": "面对面拜访",
            "is_appointment": "是",
            "purpose_code": "推进项目",
            "expected_key_result": "客户确认下一步技术交流安排",
            "self_assessment": "达到目的",
            "next_action_purpose": "安排技术交流",
            "next_action_expected_result": "客户确认参会人员",
            "next_contact_at": "2026-09-20T10:00:00+08:00",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert "本次AI检测未获取“过程详细描述”" not in body["feedback_text"]
    assert "R｜过程事实与结果：未检查。\n检查标准：" in body["feedback_text"]
    assert "R｜过程事实与结果：达标" not in body["feedback_text"]
    assert "A｜达成评价：待复核。\n检查标准：" in body["feedback_text"]
    assert "建议补充“过程详细描述”" not in body["feedback_text"]
    assert "process_description" not in body["feedback_text"]


def test_plugin_transmits_current_process_and_rechecks_after_edit(monkeypatch) -> None:
    from test_button_plugin import run_button_plugin

    def forbidden_record_lookup(*args, **kwargs):
        raise AssertionError("Precheck must not substitute a saved record for the current draft")

    monkeypatch.setattr(api, "get_jiandaoyun_record", forbidden_record_lookup)
    client = TestClient(api.app)
    draft = complete_precheck_payload()["visit"]
    texts = [
        "信息中心主任确认8月25日验证。\n财务负责人确认负责预算审批。",
        "我觉得非常不错",
        "",
        "客户确认9月2日安排技术交流。\n客户提出需要补充验证清单。",
    ]
    previous_hash = None
    for text in texts:
        draft["process_description"] = text
        plugin = run_button_plugin(draft)
        forwarded = plugin["calls"][0]["data"]
        assert forwarded["process_description"] == text
        response = client.post(
            "/api/v1/connectors/jiandaoyun/visit/button-check", json=forwarded,
        )
        assert response.status_code == 200
        result = response.json()
        record = api.get_store().get_precheck("tenant_demo", result["check_id"])
        visit = record["request"]["visit"]
        assert "process_description" in visit["metadata"]["source_supplied_fields"]
        assert (visit["process_description"] or "") == text
        assert "R｜过程事实与结果：未检查" not in result["feedback_text"]
        assert "A｜达成评价：待复核" not in result["feedback_text"]
        assert result["feedback_text"].count("检查标准：") == 6
        assert result["official_score_generated"] is False
        assert "total_score" not in result
        assert previous_hash != result["input_snapshot_hash"]
        previous_hash = result["input_snapshot_hash"]
        if text == "":
            assert "建议补充“过程详细描述”" in result["feedback_text"]
        elif text == "我觉得非常不错":
            assert "R｜过程事实与结果：未达标" in result["feedback_text"]
        else:
            assert "R｜过程事实与结果：达标" in result["feedback_text"]


def test_plugin_subforms_reach_agent_and_updates_replace_old_values(monkeypatch):
    import json

    from test_button_plugin import run_button_plugin

    def no_lookup(*args, **kwargs):
        raise AssertionError("Do not fetch old saved records")
    monkeypatch.setattr(api, "get_jiandaoyun_record", no_lookup)
    client = TestClient(api.app)
    draft = complete_precheck_payload()["visit"]
    draft["participants"] = '[{"关联数据-主键":"SYNTHETIC-C1"}]'
    hashes = set()
    for stage in ['P3', 'P4', '']:
        draft["opportunities"] = json.dumps([
            {"商机编号": "SYNTHETIC-O1", "历史商机阶段": "P2", "最新商机阶段": stage}
        ])
        forwarded = run_button_plugin(draft)["calls"][0]["data"]
        response = client.post('/api/v1/connectors/jiandaoyun/visit/button-check', json=forwarded)
        assert response.status_code == 200
        body = response.json()
        stored = api.get_store().get_precheck('tenant_demo', body['check_id'])["request"]["visit"]
        assert {'process_description', 'participants', 'opportunities'} <= set(
            stored['metadata']['source_supplied_fields'])
        assert stored['participants'][0]['contact_id'] == 'SYNTHETIC-C1'
        assert (stored['opportunities'][0]['current_stage'] or '') == stage
        assert not stored['next_action_target_id']  # Never fabricate a next-contact binding.
        assert body['official_score_generated'] is False
        assert body['submission_blocked'] is False
        assert 'total_score' not in body
        hashes.add(body['input_snapshot_hash'])
        assert ('T｜客户类型：未达标' in body['feedback_text']) == (stage == '')
    assert len(hashes) == 3


@pytest.mark.parametrize('rows', ['broken-json', {'rows': []}, [12], [{'wrong_field': 'secret'}]])
def test_button_malformed_subform_is_safe_422_not_silent_empty_or_500(rows):
    client = TestClient(api.app, raise_server_exceptions=False)
    response = client.post('/api/v1/connectors/jiandaoyun/visit/button-check', json={
        'tenant_id': 'tenant_demo', 'participants': rows,
    })
    assert response.status_code == 422
    assert '检测未完成' in response.json()['detail']
    assert 'secret' not in response.text


def test_jiandaoyun_submitted_event_reads_record_and_enqueues_evaluation(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        api,
        "get_jiandaoyun_record",
        lambda settings, tenant_id, app_id, entry_id, data_id: {
            "_id": data_id,
            "createTime": "2026-08-19T02:58:33.000Z",
            "updateTime": "2026-08-19T03:15:00.000Z",
            "creator": {"username": "EMP001"},
            "visit_record_code": "BFJL2026081900001",
            "visit_date": "2026-08-19",
            "employee_id": "EMP001",
            "customer_id": "KH001",
            "customer_type_ii": "商机客户",
            "visit_method": "面对面拜访",
            "is_appointment": "是",
            "purpose_code": "获得参与",
            "expected_key_result": "客户确认下一阶段参与人",
            "process_description": "客户明确确认参与，并给出下一阶段验证要求。",
            "self_assessment": "达到目的",
            "next_action_purpose": "确认下一阶段安排",
            "next_action_expected_result": "客户书面确认参与范围",
            "next_contact_at": "2026-08-20T10:00:00+08:00",
            "actual_start_at": "2026-08-19T09:00:00+08:00",
            "actual_end_at": "2026-08-19T10:00:00+08:00",
        },
    )
    client = TestClient(api.app)

    submitted = client.post(
        "/api/v1/connectors/jiandaoyun/visit/submitted",
        json={"tenant_id": "tenant_demo", "data_id": "6a851bd917936e24c254f9f5"},
    )
    job_id = submitted.json()["job_id"]
    record = client.get(
        f"/api/v1/visit/evaluations/{job_id}",
        params={"tenant_id": "tenant_demo"},
    )

    assert submitted.status_code == 202
    assert record.status_code == 200
    assert record.json()["status"] == "completed"
    assert record.json()["request"]["context"]["source_record_id"] == (
        "6a851bd917936e24c254f9f5"
    )


def test_jiandaoyun_submitted_event_rejects_non_test_form() -> None:
    response = TestClient(api.app).post(
        "/api/v1/connectors/jiandaoyun/visit/submitted",
        json={
            "tenant_id": "tenant_demo",
            "data_id": "6a851bd917936e24c254f9f5",
            "app_id": "production-app",
            "entry_id": "production-entry",
        },
    )

    assert response.status_code == 422


def test_jiandaoyun_submitted_event_ignores_ai_writeback_update_time(
    monkeypatch,
) -> None:
    calls = 0

    def fake_record(settings, tenant_id, app_id, entry_id, data_id):
        nonlocal calls
        calls += 1
        return {
            "_id": data_id,
            "createTime": "2026-08-19T02:58:33.000Z",
            "updateTime": f"2026-08-19T03:15:0{calls}.000Z",
            "updater": {"username": f"AI{calls}"},
            "visit_date": "2026-08-19",
            "employee_id": "EMP001",
            "_widget_1787037882562": str(90 + calls),
            "_widget_1787037882560": f"AI feedback {calls}",
        }

    monkeypatch.setattr(api, "get_jiandaoyun_record", fake_record)
    client = TestClient(api.app)
    payload = {"tenant_id": "tenant_demo", "data_id": "DATA-IDEMPOTENT"}

    first = client.post(
        "/api/v1/connectors/jiandaoyun/visit/submitted",
        json=payload,
    )
    second = client.post(
        "/api/v1/connectors/jiandaoyun/visit/submitted",
        json=payload,
    )

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]
    assert second.json()["status"] == "completed"


def test_jiandaoyun_signed_webhook_enqueues_evaluation(monkeypatch) -> None:
    monkeypatch.setenv(
        "DSM_TAORAN_TENANT_KEYS_JSON", '{"tenant_demo":"tenant-secret"}'
    )
    monkeypatch.setenv("DSM_TAORAN_JIANDAOYUN_WEBHOOK_SECRET", "webhook-secret")
    get_settings.cache_clear()
    payload = json.dumps(
        {
            "op": "data_create",
            "data": {
                "_id": "WEBHOOK-DATA-001",
                "appId": "60fe7ad79ca2d000075dfab1",
                "entryId": "6a8408b7c5a0d9454090a5bc",
                "createTime": "2026-08-19T02:58:33.000Z",
                "visit_date": "2026-08-19",
                "employee_id": "EMP001",
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    nonce = "nonce-001"
    timestamp = "1787119200"
    signature = hashlib.sha1(
        f"{nonce}:{payload}:webhook-secret:{timestamp}".encode()
    ).hexdigest()

    response = TestClient(api.app).post(
        "/api/v1/connectors/jiandaoyun/visit/webhook",
        params={"tenant_id": "tenant_demo", "nonce": nonce, "timestamp": timestamp},
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-JDY-Signature": signature,
            "X-JDY-DeliverId": "delivery-001",
        },
    )

    assert response.status_code == 202
    assert response.json()["job_id"].startswith("job_")


def test_jiandaoyun_webhook_rejects_invalid_signature(monkeypatch) -> None:
    monkeypatch.setenv("DSM_TAORAN_JIANDAOYUN_WEBHOOK_SECRET", "webhook-secret")
    get_settings.cache_clear()

    response = TestClient(api.app).post(
        "/api/v1/connectors/jiandaoyun/visit/webhook",
        params={"tenant_id": "tenant_demo", "nonce": "n", "timestamp": "1"},
        content='{"op":"connection_test"}',
        headers={"Content-Type": "application/json", "X-JDY-Signature": "invalid"},
    )

    assert response.status_code == 401


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
    assert record.json()["response"]["total_score"] == 100
    assert record.json()["response"]["effectiveness_score"] == 100


def test_q40_reserved_endpoints_are_disabled_by_default() -> None:
    response = TestClient(api.app).get(
        "/api/v1/integrations/q40/compatibility",
        params={
            "tenant_id": "tenant_demo",
            "required_rule_version": "TAORAN-Q33-Q34-100-V2",
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
            "required_rule_version": "TAORAN-Q33-Q34-100-V2",
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
        "required_rule_version": "TAORAN-Q33-Q34-100-V2",
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
        "required_rule_version": "TAORAN-Q33-Q34-100-V2",
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
