from copy import deepcopy

import pytest
from test_post_policy import visit
from test_post_repair import reviewer, valid_payload

from taoran_agent import feedback
from taoran_agent.feedback import build_evaluation_feedback_with_diagnostics
from taoran_agent.llm import _evidence_catalog
from taoran_agent.models import Q34SemanticFacts
from taoran_agent.scoring import score_q33, score_q34

PRICE_SOURCE = (
    "5kg AGMLT, 客户反馈，希望采购的价格是USD650/kg，告知客户我们成本一直在升高，"
    "仍然努力在维持原有价格给客户，没有再度降价的空间。仍然按USD690/kg价格报价。"
)
ORIGINAL_0497_SUGGESTION = (
    "本次记录中客户提出期望价格USD650/kg，销售方已按USD690/kg报价并说明无降价空间，"
    "双方尚未就价格达成一致。下一步行动目的为击败竞争对手、期望结果为价格确认，"
    "但过程描述中未体现客户对后续价格确认事项的明确回应。建议在下一次沟通中与客户确认"
    "其对当前报价的具体反馈及后续采购意向，如客户已有明确回应请据实补充，"
    "若尚未确认请保持真实状态并继续跟进。"
)
ORIGINAL_0497_REASON = (
    "下一步目的为击败竞争对手、期望结果为价格确认，与本次价格沟通事实衔接。"
    "但本次过程中客户仅表达期望价格且销售方已明确拒绝降价，双方未就后续价格确认形成共识，"
    "下一步缺少客户对具体后续事项的明确回应。商机客户要求客户共识，"
    "当前记录不足以证明客户已同意该下一步安排。"
)
SAVED_R12_N_SUGGESTION = (
    "过程中客户提出USD650/kg而我方维持USD690/kg报价，双方尚未就价格达成一致。"
    "建议在下一次沟通前先与客户确认其是否愿意继续就价格进行协商，并将客户对后续价格沟通的"
    "真实回应补充到记录中。如客户已明确同意继续讨论价格，请据实补充该共识；"
    "若客户尚未确认，请保持真实状态并继续跟进。"
)
SAVED_R12_N_REASON = (
    "下一步目的为击败竞争对手、期望结果为价格确认，与本次价格分歧衔接。"
    "但过程中客户仅提出期望价格且我方已拒绝降价，记录未体现客户对后续价格沟通的"
    "明确回应或共识，商机客户下一步缺少客户确认。"
)


def _price_record():
    return visit(
        visit_date="2026-09-15",
        customer_type_ii="opportunity",
        opportunity_stage="P3",
        visit_method="asynchronous_message",
        purpose_code="击败竞争对手",
        expected_key_result="确认价格",
        process_description=PRICE_SOURCE,
        self_assessment="partially_achieved",
        next_action_purpose="击败竞争对手",
        next_action_expected_result="价格确认",
        next_contact_at="2026-09-17T16:00:00Z",
    )


def _price_payload(subject, record, *, suggestion=ORIGINAL_0497_SUGGESTION, reason=ORIGINAL_0497_REASON):
    payload = valid_payload(subject, record)
    section = next(item for item in payload["sections"] if item["code"] == "N")
    section["suggestion"] = suggestion
    section["reason"] = reason
    return payload


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


def test_deterministic_repair_handles_chained_known_violations_without_second_model_call(
    tmp_path, monkeypatch,
):
    subject = reviewer(tmp_path)
    record = visit()
    payload = valid_payload(subject, record)
    next(item for item in payload["sections"] if item["code"] == "N")["suggestion"] = (
        "请补充已取得的具体信息方向，使下一步行动更加明确。"
    )
    next(item for item in payload["sections"] if item["code"] == "O_KR")["suggestion"] = (
        "建议在关键结果中补充本次需要收集的具体信息类别，例如客户主营产品方向、采购需求或供应商资质要求等，以便后续验证目标达成情况。"
    )
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
        assert calls == [False]
        assert [audit["violation_code"] for audit in parsed._semantic_gate["targeted_repairs"]] == [
            "post_advice_truthfulness_conflict",
            "post_requirement_provenance_conflict",
        ]
        repaired = {item.code: item.suggestion for item in parsed.sections}
        assert repaired["N"].startswith("下一步可以")
        assert repaired["O_KR"].startswith("下一步可以")
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


def test_saved_0497_candidate_completes_without_repair_or_fallback(tmp_path, monkeypatch):
    subject = reviewer(tmp_path)
    record = _price_record()
    payload = _price_payload(subject, record)
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
        before = record.model_dump(mode="json")
        q33_before = score_q33(record)[0].model_dump()
        result = subject.review_q34(record)
        baseline = Q34SemanticFacts(
            provider="llm-chat",
            **payload["facts"],
            sections=[
                {key: value for key, value in section.items() if key != "advice_basis"}
                for section in payload["sections"]
            ],
        )
        assert result.status == "completed"
        assert result.failure_reason is None
        assert calls == [False]
        assert result.model_attempts[0].failure_reason is None
        assert record.model_dump(mode="json") == before
        assert score_q33(record)[0].model_dump() == q33_before
        assert score_q34(record, result)[0].model_dump() == score_q34(record, baseline)[0].model_dump()
    finally:
        subject.close()


def test_saved_r12_0497_output_still_replays_through_the_post_gate(tmp_path):
    subject = reviewer(tmp_path)
    record = _price_record()
    payload = _price_payload(
        subject,
        record,
        suggestion=SAVED_R12_N_SUGGESTION,
        reason=SAVED_R12_N_REASON,
    )
    try:
        parsed, _ = subject._validate_observed(payload, subject._input(record, precheck=False))
        section = next(item for item in parsed.sections if item.code == "N")
        assert section.suggestion == SAVED_R12_N_SUGGESTION
        assert section.reason == SAVED_R12_N_REASON
    finally:
        subject.close()


def test_repair_chain_retains_initial_error_and_final_completed_state(tmp_path, monkeypatch):
    subject = reviewer(tmp_path)
    record = _price_record()
    payload = _price_payload(
        subject,
        record,
        suggestion="本次记录中我方已报价，请补充客户已同意采购。",
    )

    def request(*args, **kwargs):
        lease = args[3]
        try:
            return deepcopy(payload), {}
        finally:
            lease.release()

    monkeypatch.setattr(subject, "_request", request)
    attempts = []
    try:
        parsed, _ = subject._analyze(record, False, attempts)
        assert parsed
        assert attempts[0].failure_reason == "post_advice_truthfulness_conflict"
        assert [entry["stage"] for entry in attempts[0].repair_chain] == [
            "initial_validation",
            "targeted_repair",
            "final",
        ]
        assert attempts[0].repair_chain[0]["hits"] == [{
            "target": "N",
            "quote": "请补充客户已同意采购",
            "rule": "advice_requests_unproven_completed_fact",
        }]
        assert attempts[0].repair_chain[-1] == {
            "stage": "final",
            "status": "completed",
            "failure_reason": None,
        }
    finally:
        subject.close()


def test_repair_chain_reports_a_preexisting_second_error_without_calling_it_repair_output(
    tmp_path, monkeypatch,
):
    subject = reviewer(tmp_path)
    record = _price_record()
    unsafe_reason = "当前记录不足以证明客户已同意下一步安排，但客户承诺下周付款。"
    payload = _price_payload(
        subject,
        record,
        suggestion="本次记录中我方已报价，请补充客户已同意采购。",
        reason=unsafe_reason,
    )

    def request(*args, **kwargs):
        lease = args[3]
        try:
            return deepcopy(payload), {}
        finally:
            lease.release()

    monkeypatch.setattr(subject, "_request", request)
    attempts = []
    try:
        with pytest.raises(Exception, match="post_commitment_boundary_conflict"):
            subject._analyze(record, False, attempts)
        chain = attempts[0].repair_chain
        assert [entry["stage"] for entry in chain] == [
            "initial_validation",
            "targeted_repair",
            "revalidation",
            "final",
        ]
        assert chain[2]["failure_reason"] == "post_commitment_boundary_conflict"
        assert chain[2]["hits"][0]["quote"] == unsafe_reason.rstrip("。")
        assert chain[-1]["status"] == "failed"
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
