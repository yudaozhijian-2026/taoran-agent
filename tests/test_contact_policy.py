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


def test_missing_date_guidance_preserves_consensus_and_empty_input_boundaries():
    policy = contact_policy(visit('opportunity'))
    guidance = policy['feedback_guidance']
    assert '日期未填写不等于客户没有共识' in guidance
    assert '将真实约定时间补入' in guidance
    assert '只建议协商尚未确定的联系时间' in guidance
    assert '条件、异议或拒绝' in guidance
    assert '不编造个性化事实' in guidance
    assert policy['after_visit'] is None
    assert policy['customer_consensus_required'] is True


@pytest.mark.parametrize('text', [
    '建议每7天联系一次客户。',
    '建议安排在下个月联系客户。',
    '商机客户无需预约但日期必须跨自然月。',
    '请补充下一次联系客户时间安排，商机客户建议与客户达成下一次拜访时间共识，以承接本次事实。',
])
def test_reported_guard_gaps(text):
    payload = {'facts': {'reason': text}, 'sections': [{'code': 'N', 'suggestion': text}]}
    assert contact_policy_hits(payload, contact_policy(visit('opportunity')))


@pytest.mark.parametrize('category,field,blocked', [
    ('customer_commitment', 'process_description', False),
    ('planned_action', 'process_description', True),
    ('customer_commitment', 'next_action_expected_result', True),
])
def test_interval_requires_customer_timing_evidence(category, field, blocked):
    payload = {'sections': [{'code': 'N', 'suggestion': '建议按客户约定每7天联系一次。',
                           'evidence': [{'field': field, 'category': category,
                                         'quote': '客户同意每7天联系一次'}]}]}
    assert bool(contact_policy_hits(payload, contact_policy(visit('opportunity')))) == blocked


def test_feedback_standard_does_not_export_scoring_date_order():
    p = contact_policy(visit('opportunity', '2026-09-09'))
    assert p['after_visit'] is False  # Existing scoring date information is retained.
    assert '必须晚于' not in p['standard']
    assert '评分内部' in p['scoring_standard']


@pytest.mark.parametrize('text', [
    '客户已约定周五会面，建议补入约定时间。',
    '客户同意收到COA后评估，建议协商反馈时间并填写。',
    '客户要求寄样，不代表已确认具体联系日期，建议协商并填写时间。',
    '记录是我方计划，未体现客户回应，建议确认安排及时间。',
    '客户暂不面谈，建议尊重其条件确认后续沟通安排。',
    '仅内部批准预留货物，建议核实客户回应及联系时间。',
    '会议时间正在确认，建议确认后补入字段。',
    '客户十月后采购，不代表之前不能联系，建议按实际沟通安排填写。',
    '客户承诺明天问财务，并非审批完成，建议按其反馈节点填写。',
    '交货时间不代表约定联系日期，建议据实补充。',
    '已确认寄样但尚未同意试用，建议分别确认尚未明确的安排。',
    '原文仅下周，具体日期未确定，建议据实约定，不直接填写某一天。',
])
def test_fact_boundaries_are_not_rejected(text):
    assert not contact_policy_hits({'sections': [{'code': 'N', 'suggestion': text}]},
                                   contact_policy(visit('opportunity')))


def test_date_and_action_separation_with_additional_fields():
    payload = {'facts': {'next_action_logic_ok': True}, 'sections': [
        {'code': 'N', 'advice_basis': {'fields': ['next_contact_at', 'process_description']},
         'reason': '已确认行动，联系时间未填写。'}]}
    assert not contact_policy_hits(payload, contact_policy(visit('opportunity')))


def test_legacy_missing_date_boilerplate_requests_scoped_repair():
    payload = {'facts': {}, 'sections': [{'code': 'N', 'suggestion':
        '请补充下一次联系客户时间安排，商机客户建议与客户达成下一次拜访时间共识。'}]}
    hits = contact_policy_hits(payload, contact_policy(visit('opportunity')))
    assert [h['rule'] for h in hits] == ['missing_date_requires_fact_based_advice']
    assert targets_for_error('post_feedback_conflict', {'hits': hits}) == ['N']
    for v in (visit('target'), visit('potential'), visit('opportunity', '2026-09-11')):
        assert not contact_policy_hits(payload, contact_policy(v))


@pytest.mark.parametrize('suggestion', [
    '请确认客户是否同意周五收货，再将双方实际约定的联系时间补入下一次联系客户时间安排。',
    '客户已同意收到COA后评估，尚未明确反馈时间，建议围绕评估安排协商联系时间并填写。',
    '计划周五发货是我方安排，记录未体现客户回应，建议确认收货安排后补充联系时间。',
    '客户表示暂不安排面谈，建议先确认方便继续沟通的条件，再据实填写联系时间。',
    '过程及下一步内容均未填写，请据实补充后续事项、客户回应及下一次联系时间。',
])
def test_fact_specific_missing_date_advice_not_replaced(tmp_path, suggestion):
    from test_post_policy import visit as full_visit
    from test_post_repair import reviewer, valid_payload
    r = reviewer(tmp_path)
    try:
        v = full_visit(customer_type_ii='opportunity', next_contact_at=None)
        payload = valid_payload(r, v)
        section = next(x for x in payload['sections'] if x['code'] == 'N')
        section['suggestion'] = suggestion
        section['advice_basis']['fields'] = ['next_contact_at']
        payload['facts']['next_action_logic_ok'] = True
        parsed, _ = r._validate_observed(payload, r._input(v, precheck=False))
        assert next(x for x in parsed.sections if x.code == 'N').suggestion == suggestion
    finally:
        r.close()
