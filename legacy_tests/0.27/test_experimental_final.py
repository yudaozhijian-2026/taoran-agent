from types import SimpleNamespace

import pytest

from taoran_agent import api
from taoran_agent.feedback import (
    _clean_experimental_front_text,
    _clean_front_text,
    build_front_ai_suggestions_with_model,
)
from taoran_agent.models import FrontVisitAnalysisSection, KnowledgeWordingResult


def test_experimental_cleaner_preserves_product_units_and_departments():
    text = "客户下单500g M13和DDQ，转交QA和生产部门，产品LKXA。"
    assert _clean_experimental_front_text(text) == text
    assert _clean_front_text(text) != text  # Legacy behavior is not changed.


def test_experimental_cleaner_preserves_dates():
    assert _clean_experimental_front_text("下次联系2026-09-04。") == "下次联系2026-09-04。"


def test_candidate_uses_single_analysis_without_section_substitution():
    response = SimpleNamespace(issues=[], taoran_sections=[], field_completion={})
    wording = KnowledgeWordingResult(
        status="completed", visit_analysis="客户暂不增加供应商，目标尚未达成。",
        visit_analysis_sections=[FrontVisitAnalysisSection(kind="visit_context", text="历史四段。")],
    )
    candidate = build_front_ai_suggestions_with_model(response, wording, experimental=True)
    legacy = build_front_ai_suggestions_with_model(response, wording)
    assert "客户暂不增加供应商，目标尚未达成。" in candidate
    assert "拜访概况：" not in candidate
    assert "拜访概况：历史四段。" in legacy


def test_empty_candidate_analysis_is_not_replaced_by_sections():
    response = SimpleNamespace(issues=[], taoran_sections=[], field_completion={})
    wording = KnowledgeWordingResult(
        status="completed",
        visit_analysis_sections=[FrontVisitAnalysisSection(kind="visit_context", text="不能冒充分析。")],
    )
    text = build_front_ai_suggestions_with_model(response, wording, experimental=True)
    assert "本次拜访分析：" not in text


def test_no_field_suggestion_is_not_a_blanket_quality_pass():
    response = SimpleNamespace(issues=[], taoran_sections=[], field_completion={})
    wording = KnowledgeWordingResult(status="completed", visit_analysis="客户申请已提交，但不足以证明合同已确认。")
    candidate = build_front_ai_suggestions_with_model(response, wording, experimental=True)
    legacy = build_front_ai_suggestions_with_model(response, wording)
    assert "目标达成判断请见上方分析" in candidate
    assert "未发现需要改善" not in candidate
    assert "未发现需要改善" in legacy


def test_candidate_keeps_late_negative_customer_condition():
    process = "客户交流。" * 100 + "客户暂不启动新增供应商。"
    request = SimpleNamespace(visit=SimpleNamespace(model_dump=lambda **kwargs: {
        "process_description": process, "opportunities": [],
    }))
    snapshot = SimpleNamespace(records=[])
    candidate = api._knowledge_model_context(request, snapshot, experimental=True)
    legacy = api._knowledge_model_context(request, snapshot)
    assert candidate["visit_snapshot"]["process_description"] == process
    assert len(legacy["visit_snapshot"]["process_description"]) == 300


@pytest.mark.parametrize("experimental,expected", [(True, "2026-09-04"), (False, "2026-09-03T16:00:00Z")])
def test_candidate_date_context_and_cache_are_isolated(monkeypatch, experimental, expected):
    captured = {}
    class Store:
        def get_feedback_artifact(self, tenant, artifact, key):
            captured["cache_key"] = key

        def save_feedback_artifact(self, tenant, artifact, key, value):
            return value

    class Reviewer:
        def verbalize_knowledge_issues(self, items, timeout, **kwargs):
            captured.update(kwargs)
            return KnowledgeWordingResult(status="completed", visit_analysis="客户尚未确认。")

    monkeypatch.setattr(api, "get_store", lambda settings: Store())
    monkeypatch.setattr(api, "_front_specificity_items", lambda *args: [{"code": "R"}])
    monkeypatch.setattr(api, "_cached_knowledge_wording", lambda key: None)
    monkeypatch.setattr(api, "_apply_knowledge_wording", lambda response, wording, settings, **kwargs: wording)
    issues = [
        SimpleNamespace(source="rule", severity="warning", code="TAORAN_RESULT_NOT_FACT_BASED", message="过程结果缺少可核验的客户事实。"),
        SimpleNamespace(source="rule", severity="warning", code="DATE_CHECK", message="联系日期必须晚于拜访日期。"),
    ]
    response = SimpleNamespace(knowledge_snapshot_hash="k", knowledge_references=["ref"], issues=issues, tenant_id="t", input_snapshot_hash="i")
    settings = SimpleNamespace(llm_model="test", frontend_model_format_retries=0, knowledge_semantic_cache_seconds=0)
    context = {"visit_snapshot": {"next_contact_at": "2026-09-03T16:00:00Z"}}
    result = api._enhance_front_suggestions(response, Reviewer(), settings, 10, context, experimental=experimental)
    assert captured["taoran_snapshot"]["visit_analysis_context"]["next_contact_at"] == expected
    assert context["visit_snapshot"]["next_contact_at"] == "2026-09-03T16:00:00Z"
    assert captured.get("experimental", False) is experimental
    findings = captured["taoran_snapshot"]["visit_analysis_context"]["confirmed_findings"]
    assert "联系日期必须晚于拜访日期。" in findings
    assert ("过程结果缺少可核验的客户事实。" not in findings) is experimental
    assert len(response.issues) == 2  # Source findings/scoring remain intact.
    if experimental:
        assert result.visit_analysis_sections == []
