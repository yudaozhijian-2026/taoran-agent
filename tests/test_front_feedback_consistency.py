import httpx
import pytest

from taoran_agent.config import Settings
from taoran_agent.front_v46.confirmation_shape import ConfirmationShapeError, normalize
from taoran_agent.front_v46.feedback_consistency import (
    candidate_errors,
    information_goal_completion_expansion,
    non_actionable_advice,
    preview_errors,
    unsupported_optional_requirement,
)
from taoran_agent.front_v46.observed_feedback import generate
from taoran_agent.front_v46.reviewer import FrontReviewer


def context(**changes):
    value = {
        "expected_key_result": "确认六台设备的安装位置及电源准备情况",
        "process_description": (
            "客户已邮件发来六台设备清单，并逐台确认安装位置。"
            "客户随后补发现场照片，确认六处电源均已准备完成；销售按清单逐项核对。"
        ),
        "next_action_expected_result": "保持联系",
        "next_contact_at": None,
    }
    value.update(changes)
    return value


def payload(items=None, points=None):
    return {
        "analysis_points": points or [{
            "kind": "objective_result",
            "text": "原定确认目标已达成。",
            "proofs": [{
                "field": "process_description",
                "quote": "确认六处电源均已准备完成",
            }],
        }],
        "items": items or [],
        "confirmations": [],
        "suggestion_status": "has_suggestions" if items else "no_change_needed",
        "suggestion_reason": "存在需修改内容" if items else "现有记录支持本次结论",
    }


def item(suggestion, *, code="R", field="process_description", quote="确认六处电源均已准备完成"):
    return {
        "code": code,
        "suggestion": suggestion,
        "proofs": [{"field": field, "quote": quote}],
    }


def test_confirmation_objective_is_not_upgraded_to_execution_completion():
    assert information_goal_completion_expansion(
        "请确认客户承诺的周五电源准备是否已按期落实。", context(), "R",
    )
    assert not information_goal_completion_expansion(
        "请补充下一次安装安排。", context(), "N",
    )
    assert not information_goal_completion_expansion(
        "请确认安装是否已经完成。",
        context(expected_key_result="完成六台设备安装"),
        "R",
    )


@pytest.mark.parametrize("text", [
    "过程描述已包含客户动作和销售核对结果，信息完整，无需补充。",
    "现有记录符合要求。",
])
def test_positive_observations_are_not_improvement_items(text):
    assert non_actionable_advice(text)


def test_positive_observation_can_precede_a_real_action_in_one_item():
    assert not non_actionable_advice("过程事实完整，但请补充下一次联系日期。")


@pytest.mark.parametrize("text", [
    "请补充设备型号，以便判断目标是否达成。",
    "当前未记录客户对接人，请补充。",
])
def test_optional_details_are_not_universal_requirements(text):
    assert unsupported_optional_requirement(text, context())


def test_explicit_optional_detail_objective_remains_checkable():
    value = context(expected_key_result="确认设备型号及采购负责人")
    assert not unsupported_optional_requirement("请补充设备型号和采购负责人。", value)


def test_candidate_errors_are_local_and_repairable():
    advice = item("过程事实完整，无需补充。")
    duplicate = item("过程事实完整，无需补充。")
    raw = payload([advice, duplicate])
    errors = candidate_errors(raw, context())
    assert {e["code"] for e in errors} == {
        "non_actionable_suggestion", "duplicate_suggestion",
    }
    assert {e["location"] for e in errors} == {"items.0", "items.1"}
    with pytest.raises(ConfirmationShapeError) as error:
        normalize(raw, context())
    assert error.value.validation_errors == errors


def test_missing_field_advice_can_bind_to_empty_source():
    raw = payload([item(
        "请补充下一次联系客户时间安排。",
        code="N", field="next_contact_at", quote="",
    )])
    assert not candidate_errors(raw, context())


def test_unresolved_or_absent_suggestion_evidence_requires_local_repair():
    without = item("请细化下一步跟进事项。", code="N")
    without["proofs"] = []
    wrong = item("请明确下一步客户期望结果。", code="N", quote="不存在的原文")
    errors = candidate_errors(payload([without, wrong]), context())
    assert {(e["location"], e["code"]) for e in errors} == {
        ("items.0", "ungrounded_suggestion"),
        ("items.1", "unresolved_suggestion_source"),
    }


def test_goal_summary_contradiction_targets_conflicting_analysis_only():
    raw = payload(points=[
        {"kind": "objective_result", "text": "本次目标已达成。", "proofs": []},
        {"kind": "judgment_gap", "text": "本次目标未达成。", "proofs": []},
    ])
    assert candidate_errors(raw, context()) == [{
        "location": "analysis_points.1", "code": "goal_summary_contradiction",
    }]
    assert preview_errors("本次目标已达成，但本次目标未达成。", context())


def test_confirmed_rule_gap_cannot_be_silently_changed_to_no_change(tmp_path):
    no_change = payload()
    fixed = {
        "items": [item(
            "请补充下一次联系客户时间安排，并写清后续跟进事项。",
            code="N", field="next_contact_at", quote="",
        )],
        "confirmations": [],
        "suggestion_status": "has_suggestions",
        "suggestion_reason": "下一步联系时间尚未填写。",
    }
    calls = []

    def provider(request):
        calls.append(request)
        value = no_change if len(calls) == 1 else fixed
        return httpx.Response(200, json={
            "choices": [{"message": {"content": __import__("json").dumps(value)},
                         "finish_reason": "stop"}],
        })

    settings = Settings(
        _env_file=None, database_path=str(tmp_path / "db"), llm_model="test",
        llm_api_url="https://example.test/chat", llm_api_key="test",
    )
    reviewer = FrontReviewer(settings, None, transport=httpx.MockTransport(provider))
    try:
        result = generate(
            reviewer,
            [{"code": "N"}],
            {
                "visit_analysis_context": context(),
                "required_advice": [{"code": "N", "field": "next_contact_at"}],
            },
            30,
        )
    finally:
        reviewer.close()
    assert len(calls) == 2
    assert result.status == "completed"
    assert result.suggestion_status == "has_suggestions"
    assert result.recovered_after_retry
    assert [value.code for value in result.items] == ["N"]


def test_each_distinct_required_field_must_remain_covered(tmp_path):
    first = {
        **payload(),
        "items": [item(
            "请将保持联系细化为具体跟进事项。",
            code="N", field="next_action_expected_result", quote="保持联系",
        )],
        "suggestion_status": "has_suggestions",
        "suggestion_reason": "下一步内容需要细化。",
    }
    fixed = {
        "items": [
            first["items"][0],
            item("请补充下一次联系客户时间安排。", code="N", field="next_contact_at", quote=""),
        ],
        "confirmations": [],
        "suggestion_status": "has_suggestions",
        "suggestion_reason": "下一步内容和联系时间均需完善。",
    }
    calls = []

    def provider(request):
        calls.append(request)
        value = first if len(calls) == 1 else fixed
        return httpx.Response(200, json={
            "choices": [{"message": {"content": __import__("json").dumps(value)},
                         "finish_reason": "stop"}],
        })

    settings = Settings(_env_file=None, database_path=str(tmp_path / "db"), llm_model="test",
                        llm_api_url="https://example.test/chat", llm_api_key="test")
    reviewer = FrontReviewer(settings, None, transport=httpx.MockTransport(provider))
    try:
        result = generate(reviewer, [{"code": "N"}], {
            "visit_analysis_context": context(),
            "required_advice": [
                {"code": "N", "field": "next_action_expected_result"},
                {"code": "N", "field": "next_contact_at"},
            ],
        }, 30)
    finally:
        reviewer.close()
    assert len(calls) == 2 and result.recovered_after_retry
    assert {i.suggestion for i in result.items} == {i["suggestion"] for i in fixed["items"]}


def test_preview_cannot_claim_whole_record_needs_nothing_when_date_is_empty():
    value = context(_record_contract={"presence": {"next_contact_at": "empty"}})
    assert preview_errors("本次目标已达成，当前记录无需再补充。", value) == [
        {"code": "known_gap_declared_complete"},
    ]
