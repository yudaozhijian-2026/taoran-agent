from copy import deepcopy

import pytest
from test_post_policy import visit
from test_post_repair import reviewer, valid_payload

from taoran_agent.llm import ModelCallError
from taoran_agent.post_review_policy import requirement_hits


def test_clear_customer_expression_survives_unrelated_actor_ambiguity(tmp_path):
    r = reviewer(tmp_path)
    v = visit(expected_key_result='确认预算是否已获批',
              process_description='客户表示预算尚未批准。现场给客户收货。')
    payload = valid_payload(r, v)
    payload['facts'].update(process_fact_based=True, reason='客户表示预算尚未批准，已取得预算审批状态。')
    section = payload['sections'][3]
    section.update(verdict='met', reason='客户已明确表达预算审批状态。', suggestion='', advice_basis=None)
    try:
        parsed, _ = r._validate(payload, r._input(v, precheck=False), False)
        assert parsed.facts.process_fact_based
        assert parsed.sections[3].verdict == 'met'
        fabricated = deepcopy(payload)
        fabricated['facts']['reason'] = '客户已经签收。'
        with pytest.raises(ModelCallError) as error:
            r._validate(fabricated, r._input(v, precheck=False), False)
        assert str(error.value) == 'post_fact_grounding_conflict'
    finally:
        r.close()


def test_goal_specific_responsible_person_is_not_optional():
    assert not requirement_hits('本次目标是确认采购负责人，目前尚不能识别负责人，请核实负责人信息。', 'potential')


@pytest.mark.parametrize('source,claim,blocked', [
    ('现场给客户收货', '现场给客户收货是销售动作', True),
    ('现场给客户收货', '销售已给客户收货', True),
    ('现场给客户收货', '销售是否代为收货需核实', False),
    ('销售现场给客户收货', '销售给客户收货', False),
    ('我方代为收货', '收货是我方动作', False),
])
def test_receipt_cannot_invent_sales_actor_either(source,claim,blocked):
    from taoran_agent.post_claim_guards import claim_hits
    assert bool(claim_hits(claim,'R',{'source_text':source})) == blocked


@pytest.mark.parametrize('date,claim,blocked', [
    ('2026-10-05T09:00:00+08:00','下次联系日期未跨自然季度',True),
    ('2026-10-05T09:00:00+08:00','下次联系日期跨自然季度',False),
    ('2026-09-25T09:00:00+08:00','下次联系日期跨自然季度',True),
    ('2026-09-25T09:00:00+08:00','下次联系日期未跨自然季度',False),
    ('2027-01-05T09:00:00+08:00','下次联系日期未跨自然季度',True),
])
def test_calendar_conflicts_use_normalized_dates(tmp_path,date,claim,blocked):
    from taoran_agent.post_claim_guards import claim_hits
    r=reviewer(tmp_path)
    try:
        context=r._input(visit(next_contact_at=date),precheck=False)['_authoritative_checks']
        assert bool(claim_hits(claim,'N',context)) == blocked
    finally:r.close()


@pytest.mark.parametrize('claim', ['商机客户联系日期无需跨自然季度', '建议下次联系日期跨自然季度', '项目预算跨自然季度'])
def test_calendar_guard_does_not_reinterpret_advice_or_other_dates(claim):
    from taoran_agent.post_claim_guards import claim_hits
    assert not claim_hits(claim,'N',{'calendar':{'different_quarter':False}})


def test_wrong_calendar_claim_uses_full_fact_reassessment(tmp_path):
    r=reviewer(tmp_path)
    v=visit(next_contact_at='2026-10-05T09:00:00+08:00')
    payload=valid_payload(r,v)
    payload['facts']['reason']='下次联系日期未跨自然季度。'
    try:
        with pytest.raises(ModelCallError,match='post_fact_grounding_conflict'):
            r._validate(payload,r._input(v,precheck=False),False)
    finally:r.close()


@pytest.mark.parametrize('text,blocked', [
    ('补充说明是谁完成收货动作、客户是否实际确认签收，以明确客户签收状态。',False),
    ('补充实际签收方及客户是否确认签收的具体事实，明确是销售送货还是客户收货。',False),
    ('请补充签收信息，客户已签收。',True),
    ('已明确客户签收状态，客户已经签收。',True),
])
def test_clarification_after_comma_is_not_completed_assertion(text,blocked):
    from taoran_agent.post_claim_guards import claim_hits
    assert bool(claim_hits(text,'R',{'source_text':'现场给客户收货。'})) == blocked
