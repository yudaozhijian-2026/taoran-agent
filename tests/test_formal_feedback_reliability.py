from copy import deepcopy

import pytest
from test_post_policy import visit
from test_post_repair import reviewer, valid_payload

from taoran_agent import feedback
from taoran_agent.feedback import build_evaluation_feedback_with_diagnostics
from taoran_agent.llm import _evidence_catalog
from taoran_agent.models import Q34SemanticFacts
from taoran_agent.scoring import score_q34


@pytest.mark.parametrize(
    ("suggestion", "failure_code"),
    [
        (
            "建议将关键结果改为本次必须确认客户采购进度。",
            "post_requirement_provenance_conflict",
        ),
        (
            "请补充客户已确认最终采购计划。",
            "post_advice_truthfulness_conflict",
        ),
    ],
)
def test_known_feedback_violation_uses_one_deterministic_scoped_repair(
    tmp_path, monkeypatch, suggestion, failure_code,
):
    subject = reviewer(tmp_path)
    record = visit()
    payload = valid_payload(subject, record)
    target = next(item for item in payload["sections"] if item["code"] == "O_KR")
    target["suggestion"] = suggestion
    calls = []

    def request(*args, **kwargs):
        lease = args[3]
        calls.append(kwargs.get("repair", False))
        try:
            return deepcopy(payload), {}
        finally:
            lease.release()

    monkeypatch.setattr(subject, "_request", request)
    try:
        parsed, _ = subject._analyze(record, False)
        section = next(item for item in parsed.sections if item.code == "O_KR")
        assert calls == [False]
        assert parsed._semantic_gate["targeted_repair"] == {
            "stage": "targeted_repair",
            "repair_mode": "deterministic",
            "violation_code": failure_code,
            "targets": ["O_KR"],
        }
        assert "必须" not in section.suggestion
        assert "已确认" not in section.suggestion
        assert section.suggestion.startswith("下一步可以")
    finally:
        subject.close()


def test_deterministic_repair_preserves_facts_evidence_and_unrelated_sections(tmp_path, monkeypatch):
    subject = reviewer(tmp_path)
    record = visit()
    payload = valid_payload(subject, record)
    original = deepcopy(payload)
    target = next(item for item in payload["sections"] if item["code"] == "O_KR")
    target["suggestion"] = "建议将关键结果改为本次必须确认客户采购进度。"

    def request(*args, **kwargs):
        lease = args[3]
        try:
            return deepcopy(payload), {}
        finally:
            lease.release()

    monkeypatch.setattr(subject, "_request", request)
    try:
        parsed, _ = subject._analyze(record, False)
        repaired = {item.code: item for item in parsed.sections}
        assert parsed.facts.model_dump() == original["facts"]
        baseline_facts = Q34SemanticFacts(
            provider="llm-chat",
            **original["facts"],
            sections=[
                {key: value for key, value in section.items() if key != "advice_basis"}
                for section in original["sections"]
            ],
        )
        repaired_facts = Q34SemanticFacts(
            provider="llm-chat",
            **parsed.facts.model_dump(),
            sections=[
                {
                    key: value
                    for key, value in section.model_dump().items()
                    if key != "advice_basis"
                }
                for section in parsed.sections
            ],
        )
        assert score_q34(record, repaired_facts)[0].model_dump() == score_q34(
            record, baseline_facts,
        )[0].model_dump()
        for code in ("T", "A1", "R", "A2", "N"):
            expected = next(item for item in original["sections"] if item["code"] == code)
            assert repaired[code].model_dump(exclude={"advice_basis"}) == {
                key: value for key, value in expected.items() if key != "advice_basis"
            }
    finally:
        subject.close()


def test_unresolved_feedback_keeps_the_boundary_without_changing_score_facts(tmp_path, monkeypatch):
    subject = reviewer(tmp_path)
    record = visit()
    payload = valid_payload(subject, record)
    payload["facts"]["reason"] = "原定目标未达到。"

    def request(*args, **kwargs):
        lease = args[3]
        try:
            return deepcopy(payload), {}
        finally:
            lease.release()

    monkeypatch.setattr(subject, "_request", request)
    try:
        parsed, _ = subject._analyze(record, False)
        assert parsed.facts.purpose_achievement == "not_achieved"
        assert "不足以可靠判断是否已经完整达成" in parsed.facts.reason
        assert parsed._semantic_gate["targeted_repair"]["violation_code"] == (
            "post_achievement_boundary_conflict"
        )
    finally:
        subject.close()


def test_case29_style_advice_becomes_future_action_without_completed_fact(tmp_path, monkeypatch):
    subject = reviewer(tmp_path)
    record = visit(process_description="客户询价并提出资料要求，我方已说明可提供相关支持。")
    payload = valid_payload(subject, record)
    target = next(item for item in payload["sections"] if item["code"] == "N")
    target["suggestion"] = "请补充已取得终端客户反馈结果。"

    def request(*args, **kwargs):
        lease = args[3]
        try:
            return deepcopy(payload), {}
        finally:
            lease.release()

    monkeypatch.setattr(subject, "_request", request)
    try:
        parsed, _ = subject._analyze(record, False)
        suggestion = next(item.suggestion for item in parsed.sections if item.code == "N")
        assert "已取得" not in suggestion
        assert "下一步可以" in suggestion
        assert "继续确认" in suggestion
    finally:
        subject.close()


def test_internal_label_uses_deterministic_safe_feedback_without_mutating_facts(
    tmp_path, monkeypatch,
):
    record = visit()
    subject = reviewer(tmp_path)
    try:
        payload = valid_payload(subject, record)
        catalog = _evidence_catalog(subject._input(record, precheck=False))
        for section in payload["sections"]:
            evidence = [item for item in catalog if item["section"] == section["code"]]
            section["evidence"] = [
                {key: value for key, value in evidence[0].items() if key != "section"}
                | {"category": "system_fact"}
            ]
        from taoran_agent.models import Q34SemanticFacts

        facts = Q34SemanticFacts(
            provider="llm-chat",
            **payload["facts"],
            sections=[
                {key: value for key, value in section.items() if key != "advice_basis"}
                for section in payload["sections"]
            ],
            quality_audit={
                "achievement_status": "partially_achieved",
                "actual_outcomes": [{"field": "process_description", "quote": record.process_description}],
                "authoritative_checks": subject._input(record, precheck=False)["_authoritative_checks"],
                "advice_basis": {item["code"]: item["advice_basis"] for item in payload["sections"]},
            },
        )
        before = deepcopy(facts.model_dump())
        monkeypatch.setattr(
            feedback,
            "render_backend_business_feedback",
            lambda *_args: (_ for _ in ()).throw(
                ValueError("backend_salesperson_wording_v2_internal_leak:N-02")
            ),
        )
        result = build_evaluation_feedback_with_diagnostics(record, 50, 50, 100, [], facts)
        assert result.diagnostics["mode"] == "deterministic_safe_feedback"
        assert result.diagnostics["violation_code"] == "internal_label_leak"
        assert "N-02" not in result.text
        assert "本次拜访分析：" in result.text
        assert "AI改善建议：" in result.text
        assert facts.model_dump() == before
    finally:
        subject.close()
