from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import test_launch_readiness
from fastapi import BackgroundTasks

from taoran_agent import api
from taoran_agent.models import PostEvaluationRequest
from taoran_agent.source_revision import analysis_input_revision

env = test_launch_readiness.env


@pytest.mark.parametrize("status", ["queued", "running", "completed"])
def test_same_saved_inputs_reuse_without_model(env, monkeypatch, status):
    _, store, _, _, create, _ = env
    request, _ = create()
    latest = store.latest_source_job(request)
    latest["status"] = status
    latest["response"]["semantic_facts"]["status"] = "completed"
    latest["response"]["writeback"]["status"] = "succeeded"
    monkeypatch.setattr(store, "latest_source_job", lambda _: latest)
    submit = Mock()
    monkeypatch.setattr(api, "submit_evaluation", submit)
    changed = request.model_copy(deep=True)
    changed.context.request_id = "manual_saved_new"
    changed.visit.metadata["irrelevant"] = "changed"
    changed.visit_record_code = "different routing label"
    result = api._submit_saved_analysis(changed, BackgroundTasks(), "a", "key-a")
    assert result.job_id == latest["job_id"]
    submit.assert_not_called()


@pytest.mark.parametrize("scenario", ["changed", "failed", "writeback_failed", "model_failed"])
def test_changed_or_failed_inputs_can_retry(env, monkeypatch, scenario):
    _, store, _, _, create, _ = env
    request, _ = create()
    latest = store.latest_source_job(request)
    latest["response"]["semantic_facts"]["status"] = "completed"
    latest["response"]["writeback"]["status"] = "succeeded"
    if scenario == "changed":
        request.visit.process_description = "客户确认需要四张办公桌"
    elif scenario == "failed":
        latest["status"] = "failed"
    elif scenario == "writeback_failed":
        latest["response"]["writeback"]["status"] = "failed"
    else:
        latest["response"]["semantic_facts"]["status"] = "failed"
    monkeypatch.setattr(store, "latest_source_job", lambda _: latest)
    submit = Mock(return_value="created")
    monkeypatch.setattr(api, "submit_evaluation", submit)
    assert api._submit_saved_analysis(request, BackgroundTasks(), "a", "key-a") == "created"
    submit.assert_called_once()


def test_relevant_fingerprint_ignores_only_metadata(env):
    request, _ = env[4]()
    value = request.model_dump(mode="json")
    original = analysis_input_revision(request)
    value["visit"]["metadata"] = {"updated_at": "later"}
    assert analysis_input_revision(PostEvaluationRequest.model_validate(value)) == original
    changed = deepcopy(value)
    changed["visit"]["process_description"] = "客户已确认交货时间"
    assert analysis_input_revision(PostEvaluationRequest.model_validate(changed)) != original


def test_saved_modal_uses_saved_record_not_page(env, monkeypatch):
    from taoran_agent import saved_record_check as saved
    _, store, _, mapping, create, _ = env
    request, _ = create()
    latest = store.latest_source_job(request)
    latest["response"]["semantic_facts"]["status"] = "completed"
    latest["response"]["writeback"]["status"] = "succeeded"
    monkeypatch.setattr(api, "_require_interactive_quick_check", lambda _: None)
    monkeypatch.setattr(store, "get_evaluation", lambda *args: latest)
    mapping["record_fields"] = {"visit_record_code": {"widget_id": "code", "widget_type": "text"}}
    monkeypatch.setattr(api, "tenant_mapping", lambda *args: mapping)
    server = {"_id": "actual-id", "process": "saved facts"}
    monkeypatch.setattr(saved, "find_jiandaoyun_record_by_field", lambda *args: server)
    enqueue = Mock(return_value=SimpleNamespace(job_id="job1", input_snapshot_hash="a" * 64))
    monkeypatch.setattr(api, "_enqueue_jiandaoyun_record", enqueue)
    result = api.create_interactive_quick_check_task({
        "saved_record_check": True, "record_code": "code-1", "data_id": "wrong-id",
        "form_snapshot": {"process": "unsaved facts"},
    }, "a", "key-a")
    assert enqueue.call_args.args[0].data_id == "actual-id"
    assert enqueue.call_args.args[1] == server
    task = api._quick_check_tasks[result["check_id"]]
    task["future"].result(timeout=2)
    complete = api._quick_check_task_response(task)
    assert complete["final_feedback_text"] == latest["response"]["ai_opinion"]
    api._quick_check_cleanup(task["expires_at"] + 1)


def test_update_webhook_does_not_enqueue(env, monkeypatch):
    import hashlib
    import json

    from fastapi.testclient import TestClient

    from taoran_agent.config import Settings

    monkeypatch.setattr(Settings, "jiandaoyun_webhook_secret_for", lambda *args: "secret")
    enqueue = Mock()
    monkeypatch.setattr(api, "_enqueue_jiandaoyun_record", enqueue)
    body = json.dumps({"op": "data_update", "data": {"_id": "record"}}).encode()
    signature = hashlib.sha1(b"nonce:" + body + b":secret:1").hexdigest()
    result = TestClient(api.app).post(
        "/api/v1/connectors/jiandaoyun/visit/webhook?tenant_id=a&nonce=nonce&timestamp=1",
        content=body, headers={"X-JDY-Signature": signature},
    )
    assert result.status_code == 202
    assert result.json()["status"] == "ignored"
    enqueue.assert_not_called()
