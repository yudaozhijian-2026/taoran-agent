import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event
from uuid import uuid4

import httpx
import pytest
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient
from test_post_policy import visit

from taoran_agent import api
from taoran_agent import evaluation_operations as ops
from taoran_agent import writeback as delivery
from taoran_agent.agent import TaoranAgent
from taoran_agent.config import Settings
from taoran_agent.knowledge import load_taoran_knowledge_snapshot
from taoran_agent.models import PostEvaluationRequest, WritebackResult
from taoran_agent.post_quality import quality_context, quality_hits
from taoran_agent.rules import canonical_hash
from taoran_agent.source_revision import business_revision
from taoran_agent.storage import AgentStore


@pytest.fixture
def env(tmp_path, monkeypatch):
    mapping = {"source_application_id": "app", "source_entry_id": "form",
               "output_fields": {"total_score": "score", "ai_opinion": {"widget_id": "feedback"}}}
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(mapping))
    settings = Settings(_env_file=None, database_path=str(tmp_path / "db.sqlite"),
                        admin_enabled=True, admin_api_key="test-admin",
                        tenant_registry_path=str(tmp_path / "tenants.json"),
                        tenant_keys_json='{"a":"key-a","b":"key-b"}',
                        jiandaoyun_api_keys_json='{"a":"jdy-a","b":"jdy-b"}',
                        jiandaoyun_mapping_path=str(path), startup_prewarm_enabled=False)
    store = AgentStore(settings.database_path)
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    monkeypatch.setattr(api, "get_store", lambda *args: store)
    raw = {"_id": "record", "business": "v1", "score": 5, "feedback": "old"}
    def create(job_id="job1", tenant="a", **updates):
        request = PostEvaluationRequest.model_validate({
            "context": {"tenant_id": tenant, "request_id": job_id, "user_id": "u",
                        "source": "jiandaoyun", "source_record_id": "record",
                        "form_revision": business_revision(raw, mapping)},
            "visit_record_code": "visit-1", "visit": visit().model_dump(mode="json"),
            "writeback_target": {"app_id": "app", "entry_id": "form", "data_id": "record"},
        })
        request.visit.metadata["source_mapping_hash"] = canonical_hash(mapping)
        if updates:
            request = request.model_copy(update=updates)
        store.create_evaluation_job(job_id, request, job_id)
        response = TaoranAgent().evaluate(request, job_id)
        store.complete_evaluation(response)
        return request, response
    calls = []
    monkeypatch.setattr(delivery, "get_jiandaoyun_record", lambda *args: deepcopy(raw))
    def post(*args, **kwargs):
        calls.append(kwargs)
        return httpx.Response(200, json={"data": raw}, request=httpx.Request("POST", args[0]))
    monkeypatch.setattr(delivery.httpx, "post", post)
    return settings, store, raw, mapping, create, calls


def test_revision_ignores_both_output_mapping_shapes_but_not_business():
    mapping = {"output_fields": {"a": "score", "b": {"widget_id": "feedback"}}}
    a = {"business": "v1", "score": 0, "feedback": "old", "updateTime": 1}
    b = {**a, "score": 100, "feedback": "new", "updateTime": 3, "updater": "x"}
    assert business_revision(a, mapping) == business_revision(b, mapping)
    assert business_revision(a, mapping) != business_revision({**b, "business": "v2"}, mapping)


@pytest.mark.parametrize("reason", ["new_job", "source_edit", "old_policy", "missing_revision", "new_form", "read_failure"])
def test_guard_fails_closed_without_any_write(env, monkeypatch, reason):
    settings, store, raw, _mapping, create, calls = env
    request, result = create()
    if reason == "new_job":
        create("job2")
    elif reason == "source_edit":
        raw["business"] = "v2"
    elif reason == "old_policy":
        result.knowledge_version_audit["post_policy"]["version"] = "old"
    elif reason == "missing_revision":
        request.context.form_revision = None
    elif reason == "new_form":
        request.writeback_target.entry_id = "different"
    else:
        def fail(*args):
            raise delivery.JiandaoyunReadError("temporary failure")
        monkeypatch.setattr(delivery, "get_jiandaoyun_record", fail)
    value = delivery.writeback_evaluation(settings, request, result, store=store)
    assert value.status == "failed"
    assert value.error_message.startswith("WRITEBACK_")
    assert calls == []


def test_latest_writes_and_other_tenant_does_not_supersede(env):
    settings, store, _raw, _mapping, create, calls = env
    request, result = create()
    create("job-other", "b")
    assert delivery.writeback_evaluation(settings, request, result, store=store).status == "succeeded"
    assert len(calls) == 1
    assert calls[0]["json"]["data_id"] == "record"
    assert calls[0]["json"]["is_start_trigger"] is False


def test_later_task_wins_when_old_model_finishes_last(env):
    settings, store, _raw, _mapping, create, calls = env
    old_request, old_result = create()
    new_request, new_result = create("job2")
    assert delivery.writeback_evaluation(settings, new_request, new_result, store=store).status == "succeeded"
    store.complete_evaluation(old_result)
    assert delivery.writeback_evaluation(settings, old_request, old_result, store=store).error_message == "WRITEBACK_SUPERSEDED"
    assert len(calls) == 1


def test_source_guard_survives_store_restart(env):
    settings, _store, _raw, _mapping, create, _calls = env
    request, result = create()
    create("job2")
    reopened = AgentStore(settings.database_path)
    assert not reopened.is_latest_source_job(request, result.job_id)


def test_source_lock_serializes_acceptance_and_write(env, monkeypatch):
    settings, store, raw, _mapping, create, _calls = env
    request, result = create()
    reading, release = Event(), Event()
    def read(*args):
        reading.set()
        assert release.wait(3)
        return deepcopy(raw)
    monkeypatch.setattr(delivery, "get_jiandaoyun_record", read)
    with ThreadPoolExecutor(2) as pool:
        writing = pool.submit(delivery.writeback_evaluation, settings, request, result, store=store)
        assert reading.wait(2)
        accepting = pool.submit(create, "job2")
        assert not accepting.done()
        release.set()
        assert writing.result().status == "succeeded"
        accepting.result()
    assert not store.is_latest_source_job(request, "job1")


def test_retry_delivery_never_calls_model_or_regenerates_feedback(env, monkeypatch):
    _settings, store, _raw, _mapping, create, calls = env
    _request, result = create()
    result.writeback = WritebackResult(status="failed")
    store.complete_evaluation(result)
    def forbidden(*args, **kwargs):
        pytest.fail("delivery retry must not regenerate")
    monkeypatch.setattr(api, "get_agent", forbidden)
    monkeypatch.setattr(api, "_execute_unified_button_feedback", forbidden)
    actual = api.retry_evaluation_writeback("job1", "a", "a", "key-a")
    assert actual["ai_opinion"] == result.ai_opinion
    assert actual["writeback"]["status"] == "succeeded"
    api.retry_evaluation_writeback("job1", "a", "a", "key-a")
    assert len(calls) == 1


def test_delivery_failure_is_persisted(env, monkeypatch):
    _settings, store, _raw, _mapping, create, _calls = env
    _request, _result = create()
    def fail(*args, **kwargs):
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(delivery.httpx, "post", fail)
    value = api.retry_evaluation_writeback("job1", "a", "a", "key-a")
    assert value["writeback"]["status"] == "failed"
    assert store.get_evaluation("a", "job1")["response"]["writeback"]["error_message"] == "WRITEBACK_DELIVERY_FAILED"


def test_admin_requires_credential_and_scopes_data(env):
    _settings, store, _raw, _mapping, create, _calls = env
    _request, _result = create()
    store.fail_evaluation("a", "job1", "POST_INPUT_NOT_RECEIVED:secret-business-text")
    client = TestClient(api.app)
    path = "/api/v1/admin/tenants/a/evaluation-jobs"
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"X-Admin-Key": "key-a"}).status_code == 401
    response = client.get(path, headers={"X-Admin-Key": "test-admin"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["items"][0]["actions"] == ["reanalyze"]
    assert "secret-business-text" not in response.text
    assert "request_json" not in response.text and "key-a" not in response.text
    other = client.get(path.replace('/a/', '/b/'), headers={"X-Admin-Key": "test-admin"})
    assert other.json()["total"] == 0


def test_admin_operation_is_durable_idempotent_and_delivers_once(env):
    _settings, store, _raw, _mapping, create, calls = env
    _request, result = create()
    result.writeback = WritebackResult(status="failed")
    store.complete_evaluation(result)
    body = ops.RecoveryAction(action="retry_writeback", idempotency_key=uuid4())
    first = ops.start_action("a", "job1", body, BackgroundTasks(), "test-admin")
    second = ops.start_action("a", "job1", body, BackgroundTasks(), "test-admin")
    assert first["operation_id"] == second["operation_id"]
    ops.run_operation(first["operation_id"])
    ops.run_operation(first["operation_id"])
    assert len(calls) == 1
    status = ops.operation_status("a", first["operation_id"], "test-admin")
    assert status["status"] == "succeeded"
    assert status["result"]["writeback_status"] == "succeeded"
    with pytest.raises(Exception) as exc:
        ops.operation_status("b", first["operation_id"], "test-admin")
    assert exc.value.status_code == 404


def test_running_operation_recovered_from_database(env, monkeypatch):
    _settings, store, _raw, _mapping, create, calls = env
    _request, result = create()
    result.writeback = WritebackResult(status="failed")
    store.complete_evaluation(result)
    op = ops.start_action("a", "job1", ops.RecoveryAction(action="retry_writeback", idempotency_key=uuid4()), BackgroundTasks(), "test-admin")
    with store._connection:
        store._connection.execute("UPDATE evaluation_operations SET status='running'")
    submitted = []
    monkeypatch.setattr(ops._executor, "submit", lambda f, ident: submitted.append(ident))
    ops.recover_operations()
    assert submitted == [op["operation_id"]]
    ops.run_operation(op["operation_id"])
    assert len(calls) == 1


@pytest.mark.parametrize("source,text,blocked", [
    ("客户正在走合同流程", "项目实施尚未发生", True),
    ("客户正在走合同流程", "项目实施尚未完成", True),
    ("客户确认项目实施未完成", "项目实施尚未完成", False),
    ("客户正在走合同流程", "记录尚不能证明项目实施已发生", False),
    ("客户确认项目实施尚未开始", "项目实施尚未开始", False),
    ("客户提供设备清单", "收集信息只有三个字", True),
])
def test_unknown_fact_and_word_count_boundaries(source, text, blocked):
    context = quality_context(visit(process_description=source), load_taoran_knowledge_snapshot())
    assert bool(quality_hits(text, "facts.reason", context)) == blocked


def test_localized_semantic_conflicts_are_observed_without_extra_model_call(tmp_path, monkeypatch):
    from test_post_repair import reviewer, valid_payload
    r = reviewer(tmp_path)
    v = visit(process_description="客户已下单，正在走合同流程。")
    original = valid_payload(r, v)
    original["sections"][0]["field_paths"].append("other_purpose")
    original["sections"][4]["reason"] = "项目实施尚未完成，因此需要核验实际达成。"
    corrected = deepcopy(original)
    corrected["sections"][0]["field_paths"].remove("other_purpose")
    corrected["sections"][4]["reason"] = "记录尚不能证明项目实施已完成，需核验实际达成。"
    calls = []
    def model(messages, precheck, timeout, lease, progress=None, repair=False):
        try:
            calls.append(repair)
            if not repair:
                return deepcopy(original), {}
            body = json.loads(messages[1]["content"])
            assert body["repair_targets"] == ["A2", "T"]
            assert body["validation_details"]["additional_conflicts"]
            return {"sections": [corrected["sections"][0], corrected["sections"][4]], "facts_reason": ""}, {}
        finally:
            lease.release()
    monkeypatch.setattr(r, "_request", model)
    try:
        parsed, _ = r._analyze(v, False)
        assert calls == [False]
        assert parsed._semantic_gate['observation_count'] > 0
        assert parsed.facts.model_dump() == original["facts"]
    finally:
        r.close()


@pytest.mark.parametrize("change", ["knowledge", "mapping", "policy_hash"])
def test_same_version_content_changes_block_delivery(env, change):
    settings, store, _raw, _mapping, create, calls = env
    request, result = create()
    if change == "knowledge":
        result.knowledge_version_audit["actual_records_hash"] = "different"
    elif change == "mapping":
        request.visit.metadata["source_mapping_hash"] = "different"
    else:
        result.knowledge_version_audit["post_policy"]["hash"] = "different"
    assert delivery.writeback_evaluation(settings, request, result, store=store).status == "failed"
    assert calls == []


def test_model_failure_cannot_use_delivery_retry(env):
    _settings, store, _raw, _mapping, create, calls = env
    _request, result = create()
    result.semantic_facts.status = "fallback"
    result.writeback.status = "failed"
    store.complete_evaluation(result)
    with pytest.raises(Exception) as exc:
        api.retry_evaluation_writeback("job1", "a", "a", "key-a")
    assert exc.value.status_code == 409
    assert ops.recovery_summary(store.get_evaluation("a", "job1"), store)["actions"] == ["reanalyze"]
    assert calls == []


def test_reanalyze_uses_fresh_input_and_preserves_parent(env, monkeypatch):
    settings, store, _raw, mapping, create, calls = env
    _request, _result = create()
    store.fail_evaluation("a", "job1", "temporary model failure")
    fresh = visit(process_description="客户最新确认五台设备。")
    fresh_record = {"_id": "record", **fresh.model_dump(mode="json")}
    mapping["fields"] = {k: k for k in type(fresh).model_fields if k != "metadata"}
    from pathlib import Path
    Path(settings.jiandaoyun_mapping_path).write_text(json.dumps(mapping))
    reads, enqueued = [], []
    def read(*args):
        reads.append(args)
        return fresh_record
    monkeypatch.setattr(api, "get_jiandaoyun_record", read)
    monkeypatch.setattr(api._background_executor, "submit", lambda *args: enqueued.append(args))
    op = ops.start_action("a", "job1", ops.RecoveryAction(action="reanalyze", idempotency_key=uuid4()), BackgroundTasks(), "test-admin")
    ops.run_operation(op["operation_id"])
    status = ops.operation_status("a", op["operation_id"], "test-admin")
    assert status["status"] == "succeeded"
    child = store.get_evaluation("a", status["result"]["job_id"])
    assert child["request"]["visit"]["process_description"] == "客户最新确认五台设备。"
    assert store.get_evaluation("a", "job1")["status"] == "failed"
    assert len(reads) == len(enqueued) == 1
    assert calls == []


def test_pipeline_does_not_count_delivery_failure_as_success(env, monkeypatch):
    _settings, store, _raw, _mapping, create, _calls = env
    request, _result = create()
    monkeypatch.setattr(api, "get_agent", lambda: TaoranAgent())
    monkeypatch.setattr(api, "_execute_post_submit_rule_enrichment", lambda req, settings: TaoranAgent().precheck(req))
    monkeypatch.setattr(api, "writeback_evaluation", lambda *args, **kw: WritebackResult(status="failed"))
    observations = []
    monkeypatch.setattr(api, "_observe_pipeline", lambda name, phases, success: observations.append((name, success)))
    api.execute_evaluation("job1", request)
    assert ("post_submit", False) in observations
    assert ("post_generation", True) in observations
    assert ("post_writeback", False) in observations
    assert store.get_evaluation("a", "job1")["response"]["writeback"]["status"] == "failed"


@pytest.mark.parametrize("payload", [{"ok": True}, {"data": {"_id": "wrong-record"}}, {"code": 500}])
def test_http_200_without_target_receipt_is_not_success(env, monkeypatch, payload):
    _settings, store, _raw, _mapping, create, _calls = env
    _request, _result = create()
    monkeypatch.setattr(delivery.httpx, "post", lambda url, **kw: httpx.Response(200, json=payload, request=httpx.Request("POST", url)))
    actual = api.retry_evaluation_writeback("job1", "a", "a", "key-a")
    assert actual["writeback"]["status"] == "failed"
    assert store.get_evaluation("a", "job1")["response"]["writeback"]["status"] == "failed"


def test_new_task_for_another_form_does_not_supersede(env):
    _settings, store, _raw, _mapping, create, _calls = env
    request, _result = create()
    create("job-other-form", writeback_target=type(request.writeback_target)(app_id="app", entry_id="another-form", data_id="record"))
    assert store.is_latest_source_job(request, "job1")



def test_validator_source_copy_is_not_repeated_in_model_checks(tmp_path):
    from test_post_repair import reviewer
    r = reviewer(tmp_path)
    try:
        v = visit(process_description="客户确认设备共有八台。")
        data = r._input(v, precheck=False)
        messages = r._messages(data, False)
        assert data["_authoritative_checks"]["source_text"]
        assert "source_text" not in messages[0]["content"]
        user = json.loads(messages[1]["content"])
        assert "source_text" not in user["authoritative_checks"]
        assert user["untrusted_visit_data"]["process_description"] == v.process_description
    finally:
        r.close()
