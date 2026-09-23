"""Business-labelled, fixed and generated local proposition-boundary checks."""

import json
from pathlib import Path

import pytest

from taoran_agent.config import Settings
from taoran_agent.deep_review_gates import (
    advice_truthfulness_hits,
    classify_semantic_roles,
    commitment_boundary_hits,
)
from taoran_agent.experimental_semantic_streaming_v22 import (
    detect_unsupported_specific_facts as backend_preview,
)
from taoran_agent.front_v46.experimental_semantic_streaming_v22 import (
    detect_unsupported_specific_facts as frontend_preview,
)
from taoran_agent.knowledge import load_taoran_knowledge_snapshot
from taoran_agent.llm import ChatModelReviewer
from taoran_agent.models import VisitDraftInput

CORPUS = json.loads(
    (Path(__file__).parent / "data/commitment_semantic_role_corpus.json").read_text()
)["cases"]
ORIGINAL_0525 = json.loads(
    (Path(__file__).parent / "data/bfjl2026092200525_failure_replay.json").read_text()
)
ACTIONS = ("接受报价", "采购", "付款", "测试", "提供资料", "确认下一步安排")


def _role_for(text: str, source: str, fragment: str) -> str:
    matches = [item["role"] for item in classify_semantic_roles(text, source) if fragment in item["clause"]]
    assert matches, (text, fragment)
    return matches[-1]


def _post_blocked(text: str, source: str) -> bool:
    return bool(commitment_boundary_hits(text, source, "facts.reason"))


@pytest.mark.parametrize("case", CORPUS, ids=lambda case: case["case_id"])
def test_permanent_historical_source_candidate_role_and_gate(case):
    source = case["source_fact"]
    candidate = case["candidate_clause"]
    assert _role_for(candidate, source, case["target_fragment"]) == case["expected_semantic_role"]
    validator = advice_truthfulness_hits if case["validator"] == "advice" else commitment_boundary_hits
    blocked = bool(validator(candidate, source, "O_KR"))
    assert blocked is (case["expected_validator_result"] == "BLOCK")
    if case["validator"] == "commitment":
        snapshot = {"process_description": source}
        # Preview can report a different error category (e.g. an unsupported
        # date), but it must agree on whether the proposition is admissible.
        assert bool(frontend_preview(candidate, snapshot, interactive=True)["failure_category"]) is blocked
        assert bool(backend_preview(candidate, snapshot, interactive=True)["failure_category"]) is blocked


def test_r15_0525_original_candidate_is_replayed_without_editing_its_words():
    case = next(item for item in CORPUS if item["case_id"] == "BFJL2026092200525")
    assert case["candidate_clause"] == (
        "建议将想取得的关键结果补充为具体可检查的内容，例如本次拜访希望客户确认的具体事项、"
        "客户对某项合作的具体反馈或客户同意的下一步行动，使关键结果能够依据客户事实判断是否达成。"
    )
    roles = classify_semantic_roles(case["candidate_clause"], case["source_fact"])
    assert sum(item["role"] == "EXAMPLE_OR_HYPOTHETICAL" for item in roles) == 2
    assert not _post_blocked(case["candidate_clause"], case["source_fact"])


def test_r15_0525_full_original_candidate_passes_post_revalidation_offline(tmp_path):
    assert ORIGINAL_0525["input_snapshot_hash"] == (
        "38c4714b1a7286426d5ba2dbf73a0f459c5df196e496c098369e239b631ae5f7"
    )
    assert ORIGINAL_0525["initial_failure"]["reason"] == "post_commitment_boundary_conflict"
    reviewer = ChatModelReviewer(
        Settings(_env_file=None, database_path=str(tmp_path / "db.sqlite")),
        load_taoran_knowledge_snapshot(),
    )
    try:
        visit = VisitDraftInput.model_validate(ORIGINAL_0525["visit"])
        data = reviewer._input(visit, precheck=False)
        parsed, _ = reviewer._validate_observed(ORIGINAL_0525["original_candidate"], data)
        assert parsed.facts.reason == ORIGINAL_0525["original_candidate"]["facts"]["reason"]
    finally:
        reviewer.close()


@pytest.mark.parametrize("action", ACTIONS)
def test_customer_modalities_are_decided_by_business_meaning(action):
    source = "本次客户没有作出未来承诺。"
    variants = (
        (f"客户承诺下周{action}。", "UNSUPPORTED_CUSTOMER_COMMITMENT", True),
        (f"客户尚未承诺下周{action}。", "NEGATED_FACT", False),
        (f"希望客户承诺下周{action}。", "DESIRED_OUTCOME", False),
        (f"例如客户承诺下周{action}。", "EXAMPLE_OR_HYPOTHETICAL", False),
        (f"如果审批通过则客户承诺下周{action}。", "CONDITIONAL_FUTURE", False),
        (f"需要确认客户是否承诺下周{action}。", "QUESTION_OR_PENDING_CONFIRMATION", False),
        (f"客户表示后续会考虑{action}。", "CUSTOMER_INTENT", False),
    )
    for text, role, blocked in variants:
        assert _role_for(text, source, "客户") == role
        assert _post_blocked(text, source) is blocked

    supported = f"客户承诺下周{action}。"
    assert _role_for(supported, supported, "客户") == "SUPPORTED_CUSTOMER_COMMITMENT"
    assert not _post_blocked(supported, supported)
    # Only the tense changes: a promised future action cannot become done.
    assert _post_blocked(f"客户已{action}。", supported)


@pytest.mark.parametrize("action", ACTIONS)
def test_undated_customer_promise_still_requires_source_evidence(action):
    text = f"客户承诺{action}。"
    assert _role_for(text, "客户还在考虑。", "客户") == "UNSUPPORTED_CUSTOMER_COMMITMENT"
    assert _post_blocked(text, "客户还在考虑。")
    assert _role_for(text, text, "客户") == "SUPPORTED_CUSTOMER_COMMITMENT"
    assert not _post_blocked(text, text)


@pytest.mark.parametrize("source", (
    "客户希望下周付款。",
    "客户表示后续会考虑付款。",
    "客户拟于下周付款。",
))
def test_customer_intent_is_not_source_evidence_for_a_firm_promise(source):
    assert _post_blocked("客户承诺下周付款。", source)


@pytest.mark.parametrize("actor", ("销售", "我方"))
@pytest.mark.parametrize("action", ACTIONS)
def test_actor_and_scope_combinations_do_not_transfer_sales_plans_to_customer(actor, action):
    source = "客户未确认下一步安排。"
    variants = (
        (f"{actor}计划下周{action}。", "SALES_RECOMMENDATION", False),
        (f"{actor}希望客户下周{action}。", "DESIRED_OUTCOME", False),
        (f"{actor}建议客户下周{action}。", "SALES_RECOMMENDATION", False),
        (f"{actor}确认客户尚未承诺下周{action}。", "NEGATED_FACT", False),
        (f"{actor}举例：客户承诺下周{action}。", "EXAMPLE_OR_HYPOTHETICAL", False),
        (f"{actor}记录客户承诺下周{action}。", "UNSUPPORTED_CUSTOMER_COMMITMENT", True),
    )
    for text, role, blocked in variants:
        assert _role_for(text, source, "客户" if "客户" in text else actor) == role
        assert _post_blocked(text, source) is blocked


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("separator", ("，但", ",但", "。但", "；但"))
def test_example_scope_never_protects_a_separate_asserted_payment(action, separator):
    source = "客户尚未承诺付款。"
    text = f"建议记录例如客户同意的后续安排{separator}客户承诺周五{action}。"
    roles = classify_semantic_roles(text, source)
    assert roles[0]["role"] == "EXAMPLE_OR_HYPOTHETICAL"
    assert roles[-1]["role"] == "UNSUPPORTED_CUSTOMER_COMMITMENT"
    assert len(commitment_boundary_hits(text, source, "N.suggestion")) == 1


@pytest.mark.parametrize(
    ("text", "source", "blocked"),
    [
        ("客户已回复技术问题。", "客户已回复技术问题。", False),
        ("尚无法确认客户是否接受报价。", "客户希望降价。", False),
        ("建议下一步确认客户态度。", "客户希望降价。", False),
        ("希望客户确认下一步安排。", "客户希望降价。", False),
        ("建议记录客户的下一步行动，例如客户同意的后续安排。", "客户希望降价。", False),
        ("如测试通过，再推动客户确认采购。", "客户希望降价。", False),
        ("客户表示后续会考虑。", "客户希望降价。", False),
        ("客户承诺周五付款。", "客户承诺周五付款。", False),
        ("客户承诺周五付款。", "客户没有承诺付款。", True),
        ("建议确认客户态度，但客户承诺周五付款。", "客户没有承诺付款。", True),
    ],
)
def test_required_ten_language_structures(text, source, blocked):
    assert _post_blocked(text, source) is blocked


def test_local_negation_and_conditional_scope_do_not_hide_a_second_event():
    source = "客户没有接受报价，也没有承诺付款。"
    for text in (
        "尚无证据表明客户已同意采购，但客户承诺周五付款。",
        "如果审批通过客户再考虑采购，但客户承诺周五付款。",
        "建议确认客户是否采购，但客户承诺周五付款。",
    ):
        hits = commitment_boundary_hits(text, source, "facts.reason")
        assert len(hits) == 1
        assert hits[0]["candidate_event"]["action"] == "付款"


def test_advice_truthfulness_matches_completed_evidence_by_actor_and_action():
    source = "客户已提交资料，但付款尚待审批。我方已提供方案。"
    assert advice_truthfulness_hits("请补充客户已提交资料。", source, "N.suggestion") == []
    assert advice_truthfulness_hits("请补充我方已提供方案。", source, "N.suggestion") == []
    assert advice_truthfulness_hits("请补充客户已付款。", source, "N.suggestion")
    assert advice_truthfulness_hits("请补充客户已提供方案。", source, "N.suggestion")


def test_two_completed_actions_in_one_source_clause_remain_separate_evidence():
    source = "客户已提交资料并已付款。"
    assert not commitment_boundary_hits("客户已提交资料并已付款。", source, "facts.reason")
    assert advice_truthfulness_hits("请补充客户已提交资料。", source, "N.suggestion") == []
    assert advice_truthfulness_hits("请补充客户已付款。", source, "N.suggestion") == []
    assert advice_truthfulness_hits("请补充客户已采购。", source, "N.suggestion")
