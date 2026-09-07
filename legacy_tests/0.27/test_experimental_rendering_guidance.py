from copy import deepcopy

from taoran_agent.experimental_business_semantic_state import build_business_state
from taoran_agent.experimental_rendering_guidance import GUIDANCE, rendering_input


def test_guidance_lookup_does_not_change_state_or_reinterpret_raw_text():
    state = build_business_state({'expected_key_result': '办公物资', 'next_action_expected_result': '1'})
    state['field_values']['next_action_expected_result'] = '客户确认预算'
    before = deepcopy(state)
    result = rendering_input(state)
    assert state == before
    assert len(GUIDANCE) == 10
    assert any(g['guidance_code'] == 'FIELD_STATE_PLACEHOLDER' and g['target'] == 'next_action_expected_result' for g in result['KNOWLEDGE_GUIDANCE'])
    assert next(c for c in result['RENDERING_CONTRACTS'] if c['contract_id'] == 'C_G1')['allowed_claim_types'] == ['not_assessable']


def test_joint_and_independent_action_guidance_are_separate():
    state = build_business_state({'expected_key_result': '拉近客户关系', 'process_description': '简单与客户沟通后约定下次拜访'})
    contracts = rendering_input(state)['RENDERING_CONTRACTS']
    process = next(c for c in contracts if c['contract_id'] == 'C_PROCESS')
    assert process['required']
    assert process['allowed_claim_types'] == ['joint_agreement_recorded']
    assert 'JOINT_AGREEMENT_WITHOUT_COMMITMENT' in process['knowledge_guidance_codes']
    assert 'NOT_RECORDED_CUSTOMER_ACTION' in process['knowledge_guidance_codes']


def test_composite_contracts_keep_fact_ownership():
    state = build_business_state({'expected_key_result': '客户确认现阶段合作需求，并同意下一次沟通中反馈具体条件', 'process_description': '客户反馈目前没有原料药新的采购计划。客户表示会将资料转告QA部门。'})
    g1, g2 = rendering_input(state)['RENDERING_CONTRACTS'][:2]
    assert g1['allowed_claim_types'] == ['supported'] and g1['supporting_fact_ids']
    assert g2['allowed_claim_types'] == ['unresolved'] and not g2['supporting_fact_ids']

import pytest

from taoran_agent.experimental_rendering_binding import (
    check_targeted_retry,
    repair_targets,
    validate_bindings,
)
from taoran_agent.experimental_semantic_invariants import validate_invariants


def composite():
    return build_business_state({'expected_key_result': '客户确认现阶段合作需求，并同意下一次沟通中反馈具体条件', 'process_description': '客户反馈目前没有原料药新的采购计划。', 'self_assessment': 'partially_achieved'})


def bound(state, cid, text):
    c = next(c for c in rendering_input(state)['RENDERING_CONTRACTS'] if c['contract_id'] == cid)
    goal = c['semantic_state'].get('goal')
    return {'contract_id': cid, 'goal_id': c['goal_id'], 'claim_type': c['allowed_claim_types'][0], 'text': text,
            'fact_ids': c['supporting_fact_ids'], 'kind': 'objective_result',
            'proofs': ([{'field': goal['source_field'], 'quote': goal['source_text']}] if goal else []) +
            [{'field': f['source_field'], 'quote': f['text']} for f in state['facts'] if f['fact_id'] in c['supporting_fact_ids']]}


@pytest.mark.parametrize('text', [
    '当前记录尚未体现客户已对具体条件作出明确承诺。',
    '记录尚不足以证明客户已作出反馈具体条件的承诺。',
    '记录尚无客户对反馈具体条件的明确承诺。',
    '客户是否答应反馈具体条件，现有材料无法判断。',
    '反馈具体条件的承诺，仍待记录核实。',
])
def test_equivalent_unresolved_bindings(text):
    s = composite()
    points = [bound(s, 'C_G1', '客户反馈暂无采购计划，已确认现阶段需求。'), bound(s, 'C_G2', text)]
    assert not validate_invariants('。'.join(p['text'] for p in points), s, require_goal_coverage=True, bindings=points)
    assert not validate_bindings(points, s, retained_texts=[p['text'] for p in points])


def test_binding_cannot_cross_goals_or_disappear_after_normalization():
    s = composite()
    points = [bound(s, 'C_G1', '已确认现阶段需求。'), bound(s, 'C_G2', '当前记录未体现反馈具体条件的承诺。')]
    points[1]['goal_id'] = 'G1'
    assert any(e['code'] == 'BINDING_STATE_MISMATCH' for e in validate_bindings(points, s))
    points[1]['goal_id'] = 'G2'
    points[0]['text'], points[1]['text'] = points[1]['text'], points[0]['text']
    assert any(e['code'] == 'GOAL_TEXT_BINDING_MISMATCH' for e in validate_bindings(points, s))
    assert any(e['code'] == 'BOUND_TEXT_DROPPED' for e in validate_bindings(points, s, retained_texts=[]))


@pytest.mark.parametrize('record,bad,good', [
    ({'next_action_expected_result': '1'}, '下次期望结果未具体填写。', '下次期望结果已填写，但“1”属于占位内容，无法表达有效期望结果。联系时间未填写。'),
    ({'expected_key_result': '拉近客户关系'}, '记录不足以证明客户关系已经拉近。', '目标已填写，但缺少明确验收标准，当前无法准确判断达成程度。'),
    ({'expected_key_result': '办公物资'}, '建议将自评修改为部分达成。', '目标已填写，但缺少明确验收标准，当前无法准确判断达成程度。'),
    ({'expected_key_result': '现场收货', 'process_description': '现场给客户收货'}, '客户未签收，不能认定代收目标完成。', '销售代收动作已完成。客户反馈尚未填写。'),
    ({'process_description': '简单与客户沟通后约定下次拜访'}, '没有任何客户动作。', '已形成双方共同约定，尚未记录客户另外需要完成的独立行动。'),
])
def test_text_boundaries(record, bad, good):
    s = build_business_state(record)
    assert validate_invariants(bad, s)
    assert not validate_invariants(good, s)


def test_not_recorded_is_not_negative_even_with_good_binding():
    s = composite()
    points = [bound(s, 'C_G1', '已确认需求。'), bound(s, 'C_G2', '客户没有承诺反馈具体条件。')]
    assert any(e['code'] == 'NOT_RECORDED_AS_NEGATIVE_FACT' for e in validate_bindings(points, s))


def test_retry_locks_other_contracts_and_suggestions():
    s = composite()
    previous = {'analysis_points': [bound(s, 'C_G1', '已确认需求。'), bound(s, 'C_G2', '客户没有承诺反馈具体条件。')], 'items': [{'code': 'N', 'suggestion': '明确下次联系时间'}]}
    errors = validate_bindings(previous['analysis_points'], s)
    context = {'previous_candidate': previous, 'rejection': {'rendering_repairs': repair_targets(errors, s)}}
    assert {r['failed_contract_id'] for r in context['rejection']['rendering_repairs']} == {'C_G2'}
    revised = deepcopy(previous)
    revised['analysis_points'][1]['text'] = '当前记录尚无反馈具体条件的承诺。'
    assert not check_targeted_retry(revised, context)
    revised['analysis_points'][0]['text'] = '需求未确认。'
    assert check_targeted_retry(revised, context)
