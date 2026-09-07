from types import SimpleNamespace

from taoran_agent import api
from taoran_agent.experimental_final_diagnostics import (
    audit,
    category,
    repair_instruction,
    safe_code,
)
from taoran_agent.models import KnowledgeWordingResult


def test_diagnostics_never_return_raw_error_or_input():
    assert safe_code("Bearer very-secret /customer/张三") == "unknown_failure"
    assert safe_code({"secret": "token"}) == "unknown_failure"
    value = audit(SimpleNamespace(failure_reason="wording_experimental_visit_result_missing", attempt_count=2,
        model_attempts=[{"failure_reason": "invalid_json", "text": "secret"}],
        validation_errors=[{"code": "analysis_point_length", "quote": "secret"}]))
    assert "secret" not in str(value)
    assert value["model_attempt_count"] == 2
    assert value["validation_codes"] == ["analysis_point_length"]
    assert category(value["failure_reason"]) == "final_analysis_missing"
    assert category("output_truncated") == "final_output_truncated"


def test_targeted_repair_uses_fixed_instructions_not_raw_errors():
    text = repair_instruction("wording_experimental_visit_result_missing")
    assert "55字" in text and "连续原文" in text
    assert "secret" not in repair_instruction("secret")


def test_format_failure_without_semantic_target_does_not_retry(monkeypatch):
    calls = []
    class Reviewer:
        def verbalize_knowledge_issues(self, *args, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return KnowledgeWordingResult(status="unavailable", failure_reason="wording_experimental_visit_result_missing",
                    model_attempts=[{"failure_reason": "wording_experimental_visit_result_missing"}],
                    validation_errors=[{"location": "analysis_points", "code": "analysis_point_length"}])
            return KnowledgeWordingResult(status="completed", visit_analysis="客户暂不增加二供，先反馈QA部门。", model_attempts=[{"failure_reason": None}])

    store = SimpleNamespace(get_feedback_artifact=lambda *args: None, save_feedback_artifact=lambda *args: args[-1])
    monkeypatch.setattr(api, "get_store", lambda settings: store)
    monkeypatch.setattr(api, "_front_specificity_items", lambda *args: [{"code": "R"}])
    monkeypatch.setattr(api, "_cached_knowledge_wording", lambda key: None)
    monkeypatch.setattr(api, "_apply_knowledge_wording", lambda response, wording, settings, **kwargs: wording)
    response = SimpleNamespace(knowledge_snapshot_hash="k", knowledge_references=["r"], issues=[], tenant_id="t", input_snapshot_hash="i")
    settings = SimpleNamespace(llm_model="test", frontend_model_format_retries=1, knowledge_semantic_cache_seconds=0)
    result = api._enhance_front_suggestions(response, Reviewer(), settings, 30, {"visit_snapshot": {}}, experimental=True)
    assert len(calls) == 1
    assert "repair_reason" not in calls[0]
    assert result.status == "unavailable" and not result.recovered_after_retry
    assert audit(result)["attempt_failure_codes"] == ["wording_experimental_visit_result_missing"]
