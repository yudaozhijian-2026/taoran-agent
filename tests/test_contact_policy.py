import json
from pathlib import Path

import pytest

from taoran_agent.contact_policy import contact_policy, contact_policy_hits
from taoran_agent.models import VisitDraftInput
from taoran_agent.post_repair import merge_repair, targets_for_error


def visit(kind, following=None, current="2026-09-10"):
    return VisitDraftInput(visit_date=current, employee_id="regression",
                           customer_type_ii=kind, next_contact_at=following)


@pytest.mark.parametrize("kind,period", [("target", "month"), ("potential", "quarter"),
                                         ("opportunity", "none")])
def test_empty_still_has_standard(kind, period):
    p = contact_policy(visit(kind))
    assert p["period"] == period
    assert p["after_visit"] is None
    assert p["period_met"] is None
    assert "next_action_logic_ok" in p["guidance"]


@pytest.mark.parametrize("kind,current,following,after,period", [
    ("target", "2026-09-10", "2026-09-10T23:00:00+08:00", False, False),
    ("target", "2026-09-10", "2026-09-09T10:00:00+08:00", False, False),
    ("target", "2026-09-10", "2026-09-28T10:00:00+08:00", True, False),
    ("target", "2026-09-30", "2026-09-30T16:00:00Z", True, True),
    ("target", "2026-12-31", "2027-01-01T00:00:00+08:00", True, True),
    ("potential", "2026-07-10", "2026-08-10T00:00:00+08:00", True, False),
    ("potential", "2026-09-10", "2026-10-01T00:00:00+08:00", True, True),
    ("potential", "2026-12-31", "2027-01-01T00:00:00+08:00", True, True),
    ("opportunity", "2026-09-10", "2026-09-11T00:00:00+08:00", True, True),
    ("opportunity", "2026-09-10", "2026-09-10T12:00:00+08:00", False, True),
])
def test_boundaries(kind, current, following, after, period):
    p = contact_policy(visit(kind, following, current))
    assert (p["after_visit"], p["period_met"]) == (after, period)


@pytest.mark.parametrize("kind,text", [
    ("potential", "建议补充下一次联系客户时间安排，且日期应晚于本次拜访并跨自然月，以满足潜力客户下一步时间要求。"),
    ("opportunity", "下一步主题衔接但缺联系时间，商机客户需跨自然月，下一步逻辑不满足。"),
    ("opportunity", "确保日期晚于本次拜访日期2026-09-10且跨自然月。"),
])
def test_actual_failed_wording(kind, text):
    payload = {"facts": {"reason": text}, "sections": [
        {"code": "N", "reason": text, "suggestion": text,
         "advice_basis": {"missing_detail": text, "decision_impact": text}}]}
    hits = contact_policy_hits(payload, contact_policy(visit(kind)))
    assert len(hits) >= 5
    assert targets_for_error("post_feedback_conflict", {"hits": hits}) == ["N", "facts.reason"]
    updated = merge_repair(payload, {"sections": [{"code": "N", "reason": "请补充联系时间"}],
                                    "facts_reason": "已取得现场信息，联系时间未填写。"},
                           ["N", "facts.reason"])
    assert not contact_policy_hits(updated, contact_policy(visit(kind)))


@pytest.mark.parametrize("kind,text", [("potential", "日期须跨自然季度"),
                                       ("target", "日期须跨自然月"),
                                       ("opportunity", "无需跨自然月，也不要求跨自然季度")])
def test_allowed_policy(kind, text):
    assert not contact_policy_hits({"facts": {"reason": text}}, contact_policy(visit(kind)))


def test_two_real_snapshots():
    cases = json.loads((Path(__file__).parent / "fixtures/contact_policy_real_cases.json").read_text())
    for case in cases:
        v = VisitDraftInput.model_validate(case["visit"])
        policy = contact_policy(v)
        assert contact_policy_hits({"facts": {"reason": case["old_feedback"]}}, policy)
        assert policy["date_state"] == "missing"
        assert policy["period"] == {"potential": "quarter", "opportunity": "none"}[v.customer_type_ii.value]


def test_observer_cannot_swallow_policy_error(tmp_path):
    from test_post_policy import visit as full_visit
    from test_post_repair import reviewer, valid_payload

    from taoran_agent.llm import ModelCallError
    r = reviewer(tmp_path)
    try:
        v = full_visit(customer_type_ii="opportunity")
        payload = valid_payload(r, v)
        payload["facts"]["reason"] = "商机客户需跨自然月。"
        with pytest.raises(ModelCallError) as caught:
            r._validate_observed(payload, r._input(v, precheck=False))
        assert caught.value.details["contact_policy_only"] is True
        assert not r._input(v, precheck=True)["_authoritative_checks"].get("next_contact_policy")
    finally:
        r.close()


def test_local_repair_only_changes_action_logic():
    original = {"facts": {"next_action_logic_ok": False, "process_fact_based": True,
                           "purpose_achievement": "partially_achieved", "reason": "旧"},
                "sections": [{"code": "N"}, {"code": "R", "reason": "客户提供尺寸"}]}
    patch = {"sections": [{"code": "N", "verdict": "needs_revision"}],
             "facts_reason": "行动衔接，日期未填写", "next_action_logic_ok": True}
    new = merge_repair(original, patch, ["N", "facts.reason", "facts.next_action_logic_ok"])
    assert new["facts"]["next_action_logic_ok"] is True
    assert new["facts"]["process_fact_based"] is True
    assert new["facts"]["purpose_achievement"] == original["facts"]["purpose_achievement"]
    assert new["sections"][1] == original["sections"][1]
    assert original["facts"]["next_action_logic_ok"] is False
