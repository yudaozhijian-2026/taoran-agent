"""Frozen offline replay of the r14 0473 failure and adjacent role boundaries."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from taoran_agent.config import Settings
from taoran_agent.deep_review_gates import commitment_boundary_hits
from taoran_agent.experimental_semantic_streaming_v22 import (
    detect_unsupported_specific_facts as backend_preview,
)
from taoran_agent.front_v46.experimental_semantic_streaming_v22 import (
    detect_unsupported_specific_facts as frontend_preview,
)
from taoran_agent.knowledge import load_taoran_knowledge_snapshot
from taoran_agent.llm import ChatModelReviewer, ModelCallError
from taoran_agent.models import VisitDraftInput
from taoran_agent.post_repair import RepairContractError, merge_repair

FIXTURE = json.loads(
    (Path(__file__).parent / "data/bfjl2026092200473_failure_replay.json").read_text()
)
SOURCE = FIXTURE["visit"]["process_description"]


@pytest.mark.parametrize(
    ("candidate", "blocked"),
    [
        ("建议把关键结果具体化，期望客户确认后续态度。", False),
        ("建议下次确认客户是否接受报价。", False),
        ("推动客户确认下周采购计划。", False),
        ("尚无法确认客户是否接受报价。", False),
        ("争取客户回复具体时间。", False),
        ("客户已确认下周采购。", True),
        ("客户承诺月底付款。", True),
        ("建议确认客户态度，但客户已承诺月底付款。", True),
        ("希望客户确认下一步安排，但客户已确认下周采购。", True),
    ],
)
def test_customer_commitment_role_is_event_local_across_stages(candidate, blocked):
    post_hits = commitment_boundary_hits(candidate, SOURCE, "O_KR")
    snapshot = {"process_description": SOURCE}
    front = frontend_preview(candidate, snapshot, interactive=True)["failure_category"]
    back = backend_preview(candidate, snapshot, interactive=True)["failure_category"]
    assert bool(post_hits) is blocked
    assert bool(front) is blocked
    assert bool(back) is blocked
    if "但客户已" in candidate:
        assert len(post_hits) == 1
        assert post_hits[0]["candidate_event"]["event_window"].startswith("但客户已")


def test_exact_0473_candidate_passes_post_gate_and_full_revalidation(tmp_path):
    assert FIXTURE["record_code"] == "BFJL2026092200473"
    assert FIXTURE["source_hash"] == (
        "64eb26fd3d707e5cca30faf7f2726f1d391579f075c4bbce1953f0830673a5c8"
    )
    assert FIXTURE["initial_failure"]["reason"] == "post_commitment_boundary_conflict"
    assert FIXTURE["revalidation_failure"]["reason"] == "invalid_contract"
    assert FIXTURE["repair_candidate"]["facts_reason"] == ""
    original = FIXTURE["original_candidate"]
    suggestion = next(section["suggestion"] for section in original["sections"] if section["code"] == "O_KR")
    assert commitment_boundary_hits(suggestion, SOURCE, "O_KR") == []
    snapshot = {"process_description": SOURCE}
    assert frontend_preview(suggestion, snapshot, interactive=True)["failure_category"] is None
    assert backend_preview(suggestion, snapshot, interactive=True)["failure_category"] is None

    reviewer = ChatModelReviewer(
        Settings(_env_file=None, database_path=str(tmp_path / "db.sqlite")),
        load_taoran_knowledge_snapshot(),
    )
    try:
        data = reviewer._input(VisitDraftInput.model_validate(FIXTURE["visit"]), precheck=False)
        parsed, _ = reviewer._validate_observed(original, data)
        assert parsed.facts.reason == original["facts"]["reason"]

        # A valid targeted repair may replace O_KR without replacing the
        # unrelated, already valid required facts.reason.
        section_only = merge_repair(original, FIXTURE["repair_candidate"], ["O_KR"])
        assert section_only["facts"]["reason"] == original["facts"]["reason"]
        repaired, _ = reviewer._validate_observed(section_only, data)
        assert repaired.facts.reason == original["facts"]["reason"]
    finally:
        reviewer.close()


def test_exact_0473_candidate_completes_without_repair_or_fallback(tmp_path, monkeypatch):
    reviewer = ChatModelReviewer(
        Settings(_env_file=None, database_path=str(tmp_path / "db.sqlite")),
        load_taoran_knowledge_snapshot(),
    )
    calls = []

    def request(messages, precheck, timeout, lease, progress=None, repair=False):
        del messages, precheck, timeout, progress
        lease.release()
        calls.append(repair)
        return deepcopy(FIXTURE["original_candidate"]), {}

    monkeypatch.setattr(reviewer, "_request", request)
    attempts = []
    try:
        parsed, _ = reviewer._analyze(VisitDraftInput.model_validate(FIXTURE["visit"]), False, attempts)
        assert calls == [False]
        assert len(attempts) == 1
        assert attempts[0].failure_reason is None
        assert parsed.facts.reason == FIXTURE["original_candidate"]["facts"]["reason"]
    finally:
        reviewer.close()


def test_empty_targeted_reason_repair_is_rejected_without_mutating_original():
    original = deepcopy(FIXTURE["original_candidate"])
    before = deepcopy(original)
    with pytest.raises(RepairContractError) as raised:
        merge_repair(original, FIXTURE["repair_candidate"], FIXTURE["repair_targets"])
    assert raised.value.missing_fields == ["facts.reason"]
    assert original == before
    assert original["facts"]["reason"]


@pytest.mark.parametrize("patch_change", ["missing_reason", "empty_section_reason"])
def test_missing_required_repair_fields_are_contract_errors(patch_change):
    patch = deepcopy(FIXTURE["repair_candidate"])
    if patch_change == "missing_reason":
        del patch["facts_reason"]
    else:
        patch["sections"][0]["reason"] = ""
        patch["facts_reason"] = FIXTURE["original_candidate"]["facts"]["reason"]
    with pytest.raises(RepairContractError):
        merge_repair(FIXTURE["original_candidate"], patch, FIXTURE["repair_targets"])


def test_repair_failure_diagnostic_keeps_initial_boundary_and_both_candidates(tmp_path, monkeypatch):
    reviewer = ChatModelReviewer(
        Settings(_env_file=None, database_path=str(tmp_path / "db.sqlite"), llm_format_retries=1),
        load_taoran_knowledge_snapshot(),
    )
    original = deepcopy(FIXTURE["original_candidate"])
    next(section for section in original["sections"] if section["code"] == "O_KR")[
        "suggestion"
    ] = "客户已承诺下周付款。"
    raw_repair = deepcopy(FIXTURE["repair_candidate"])
    calls = []

    def request(messages, precheck, timeout, lease, progress=None, repair=False):
        del messages, precheck, timeout, progress
        lease.release()
        calls.append(repair)
        return deepcopy(raw_repair if repair else original), {}

    monkeypatch.setattr(reviewer, "_request", request)
    attempts = []
    try:
        with pytest.raises(ModelCallError, match="repair_contract_invalid"):
            reviewer._analyze(VisitDraftInput.model_validate(FIXTURE["visit"]), False, attempts)
        assert calls == [False, True]
        assert [attempt.failure_reason for attempt in attempts] == [
            "post_commitment_boundary_conflict", "repair_contract_invalid",
        ]
        chain = attempts[-1].repair_chain
        assert chain[0]["failure_reason"] == "post_commitment_boundary_conflict"
        assert chain[0]["hits"][0]["target"] == "O_KR"
        assert chain[1]["repair_output_contract_status"] == "invalid"
        assert chain[1]["missing_fields"] == ["facts.reason"]
        assert "facts.reason" in chain[1]["changed_fields"]
        assert chain[2]["status"] == "not_run"
        assert chain[3]["status"] == "failed"
        saved = json.loads(
            (tmp_path / "model-failure-evidence" / (attempts[-1].diagnostic_evidence_id + ".json")).read_text()
        )
        assert saved["details"]["original_candidate"] == original
        assert saved["details"]["repair_candidate"] == raw_repair
        assert saved["details"]["revalidation_result"] == "not_run"
        assert saved["candidate"] == raw_repair
    finally:
        reviewer.close()


def test_valid_targeted_repair_revalidates_true_unsupported_commitment(tmp_path, monkeypatch):
    reviewer = ChatModelReviewer(
        Settings(_env_file=None, database_path=str(tmp_path / "db.sqlite"), llm_format_retries=1),
        load_taoran_knowledge_snapshot(),
    )
    original = deepcopy(FIXTURE["original_candidate"])
    next(section for section in original["sections"] if section["code"] == "O_KR")[
        "suggestion"
    ] = "客户已承诺下周付款。"
    patch = deepcopy(FIXTURE["repair_candidate"])
    patch["facts_reason"] = original["facts"]["reason"]
    calls = []

    def request(messages, precheck, timeout, lease, progress=None, repair=False):
        del messages, precheck, timeout, progress
        lease.release()
        calls.append(repair)
        return deepcopy(patch if repair else original), {}

    monkeypatch.setattr(reviewer, "_request", request)
    attempts = []
    try:
        parsed, _ = reviewer._analyze(VisitDraftInput.model_validate(FIXTURE["visit"]), False, attempts)
        assert calls == [False, True]
        assert [attempt.failure_reason for attempt in attempts] == ["post_commitment_boundary_conflict", None]
        assert attempts[-1].repair_chain[1]["repair_output_contract_status"] == "valid"
        assert attempts[-1].repair_chain[2]["status"] == "passed"
        assert parsed.facts.reason == original["facts"]["reason"]
    finally:
        reviewer.close()
