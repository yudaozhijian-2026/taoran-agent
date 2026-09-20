from __future__ import annotations

from time import time

import pytest
from test_launch_readiness import env  # noqa: F401 - shared isolated fixture

from taoran_agent import api
from taoran_agent import writeback as delivery
from taoran_agent.front_analysis_artifact import build_artifact

FRONT_FEEDBACK = (
    "本次拜访分析：客户确认已有预算，但审批负责人尚未明确。\n\n"
    "AI改善建议：\n1. 请补充客户审批负责人。"
)


def accepted_artifact(store, request):
    value = build_artifact(
        visit=request.visit,
        tenant_id=request.context.tenant_id,
        user_id=request.context.user_id,
        check_id="qc-fallback",
        quick_check_input_hash="f" * 64,
        source_record_id=None,
        feedback_text=FRONT_FEEDBACK,
        decision_ledger={"version": "test-ledger"},
        front_review=None,
        policy_version="test-policy",
    )
    store.save_front_analysis_artifact(
        value.model_dump(mode="json"),
        retention_until=time() + 3600,
    )
    assert store.acknowledge_front_analysis_artifact(
        request.context.tenant_id,
        value.check_id,
        value.quick_check_input_hash,
    )
    return value


def queue_job(store, job_id):
    with store._connection:
        store._connection.execute(
            "UPDATE evaluation_jobs SET status='queued', response_json=NULL "
            "WHERE job_id=?",
            (job_id,),
        )


def test_blank_opinion_receives_front_fallback_only(env):  # noqa: F811
    settings, _store, raw, _mapping, create, calls = env
    request, _response = create()
    raw["feedback"] = ""
    result = delivery.writeback_front_feedback_fallback(
        settings,
        request,
        FRONT_FEEDBACK,
    )
    assert result.status == "succeeded"
    assert raw["feedback"] == FRONT_FEEDBACK
    assert raw["score"] == 5
    assert len(calls) == 1
    assert set(calls[0]["json"]["data"]) == {"feedback"}
    assert calls[0]["json"]["is_start_trigger"] is False


def test_existing_opinion_skips_front_fallback_without_write(env):  # noqa: F811
    settings, _store, raw, _mapping, create, calls = env
    request, _response = create()
    raw["feedback"] = "提交前意见已经随表单保存。"
    result = delivery.writeback_front_feedback_fallback(
        settings,
        request,
        FRONT_FEEDBACK,
    )
    assert result.status == "skipped"
    assert result.error_message == "FRONT_FEEDBACK_ALREADY_PRESENT"
    assert raw["feedback"] == "提交前意见已经随表单保存。"
    assert calls == []


def test_incomplete_formal_review_uses_front_fallback(env, monkeypatch):  # noqa: F811
    _settings, store, raw, _mapping, create, calls = env
    request, response = create()
    raw["feedback"] = ""
    accepted_artifact(store, request)
    queue_job(store, response.job_id)
    incomplete = response.model_copy(
        update={
            "semantic_facts": response.semantic_facts.model_copy(
                update={
                    "provider": "llm-test",
                    "status": "fallback",
                    "failure_reason": "timeout",
                }
            )
        }
    )

    class IncompleteAgent:
        def evaluate(self, *_args, **_kwargs):
            return incomplete

    monkeypatch.setattr(api, "get_agent", lambda: IncompleteAgent())
    monkeypatch.setattr(
        api,
        "_execute_post_submit_rule_enrichment",
        lambda *_args, **_kwargs: pytest.fail("must use front fallback directly"),
    )
    api.execute_evaluation(response.job_id, request)
    saved = store.get_evaluation("a", response.job_id)
    assert saved["status"] == "completed"
    assert saved["response"]["ai_opinion"] == FRONT_FEEDBACK
    assert saved["response"]["writeback"]["status"] == "succeeded"
    assert saved["response"]["deep_review_diagnostics"]["fallback_reason"] == (
        "formal_semantic_review_incomplete"
    )
    assert raw["feedback"] == FRONT_FEEDBACK
    assert raw["score"] == 5
    assert len(calls) == 1
    assert set(calls[0]["json"]["data"]) == {"feedback"}


def test_formal_exception_still_fills_blank_opinion(env, monkeypatch):  # noqa: F811
    _settings, store, raw, _mapping, create, calls = env
    request, response = create()
    raw["feedback"] = ""
    accepted_artifact(store, request)
    queue_job(store, response.job_id)

    class FailingAgent:
        def evaluate(self, *_args, **_kwargs):
            raise RuntimeError("formal generation failed")

    monkeypatch.setattr(api, "get_agent", lambda: FailingAgent())
    api.execute_evaluation(response.job_id, request)
    saved = store.get_evaluation("a", response.job_id)
    assert saved["status"] == "failed"
    assert "front_feedback_fallback=succeeded" in saved["error_message"]
    assert raw["feedback"] == FRONT_FEEDBACK
    assert raw["score"] == 5
    assert len(calls) == 1
    assert set(calls[0]["json"]["data"]) == {"feedback"}
