import pytest

from taoran_agent.experimental_business_semantic_state import (
    build_business_state,
    classify_field_state,
    decompose_goal,
)
from taoran_agent.experimental_semantic_invariants import validate_invariants


@pytest.mark.parametrize('value,expected', [
    (None, 'missing'), ('', 'missing'), ('  ', 'missing'),
    ('1', 'placeholder'), ('11', 'placeholder'), ('测试', 'placeholder'),
    ('无', 'placeholder'), ('待定', 'placeholder'), ('-', 'placeholder'), ('/', 'placeholder'),
    ('办公物资', 'vague'), ('保持关系', 'vague'), ('项目顺利实施', 'vague'),
    ('推进项目', 'vague'), ('争取合作', 'vague'),
    ('客户确认预算范围', 'assessable'), ('完成需求单签字并下单', 'assessable'),
    ('客户确认下一次技术评审时间', 'assessable'),
])
def test_field_state(value, expected):
    assert classify_field_state('expected_key_result', value) == expected


@pytest.mark.parametrize('field', ['expected_key_result', 'next_action_purpose',
    'next_action_other_purpose', 'next_action_expected_result', 'deviation_reason',
    'next_contact_at', 'customer_feedback', 'process_description'])
def test_uniform_presence_and_placeholder(field):
    assert classify_field_state(field, None) == 'missing'
    assert classify_field_state(field, '1') == 'placeholder'
    assert classify_field_state(field, False) != 'missing'


def test_decomposition_never_computes_attainment_or_splits_noun_lists():
    items = decompose_goal('expected_key_result', '确认现阶段合作需求，并同意下一次沟通中反馈具体条件')
    assert len(items) == 2
    assert all(g.status == 'unassessed' for g in items)
    assert decompose_goal('expected_key_result', '1') == []
    assert len(decompose_goal('expected_key_result', '确认预算、审批流程')) == 1
    for separator in ['并', '以及', '同时', '且', '、', '；']:
        assert len(decompose_goal('expected_key_result', f'完成需求单签字{separator}下单')) == 2


def test_goal_fact_and_self_alignment_keep_negative_information():
    state = build_business_state({'expected_key_result': '客户确认现阶段合作需求，并同意下一次沟通中反馈具体条件',
        'process_description': '客户反馈目前没有原料药新的采购计划。客户表示会将资料转告QA部门。',
        'self_assessment': 'partially_achieved'})
    assert [a['alignment_status'] for a in state['goal_fact_alignments']] == ['supported', 'unsupported']
    assert [g['status'] for g in state['goal_items']] == ['supported', 'unresolved']
    assert state['self_assessment_alignment']['alignment'] == 'aligned'
    assert any(f['polarity'] == 'negative' and f['record_status'] == 'negative_fact' for f in state['facts'])
    assert state['relations']['CUSTOMER_COMMITMENT']['status'] == 'recorded'
    # A promise to transfer is not a promise to feed back conditions.
    assert state['goal_fact_alignments'][1]['supporting_fact_ids'] == []


def test_unrelated_feedback_does_not_negate_completed_receipt():
    state = build_business_state({'expected_key_result': '现场收货', 'process_description': '现场给客户收货，送卡', 'self_assessment': 'achieved'})
    assert state['goal_fact_alignments'][0]['alignment_status'] == 'supported'
    assert state['self_assessment_alignment']['alignment'] == 'aligned'
    assert state['relations']['SALES_ACTION']['status'] == 'recorded'
    assert state['relations']['CUSTOMER_INDIVIDUAL_ACTION']['status'] == 'not_recorded'


def test_joint_agreement_is_actual_but_future_visit_not_completed():
    state = build_business_state({'process_description': '客户因开会未深入交流，简单与客户沟通后约定下次拜访'})
    facts = [f for f in state['facts'] if f['fact_type'] == 'JOINT_AGREEMENT']
    assert facts and all(f['actor'] == 'both' and f['temporality'] == 'actual' for f in facts)
    assert state['relations']['JOINT_AGREEMENT']['status'] == 'recorded'
    assert not any(f['fact_type'] == 'COMPLETED_EVENT' and '拜访' in f['text'] for f in state['facts'])


def test_actual_planned_actor_and_offsets():
    context = {'process_description': '销售介绍产品。客户表示下周会转交资料。客户已经下单。', 'next_action_expected_result': '客户确认预算'}
    state = build_business_state(context)
    assert any(f['actor'] == 'sales' for f in state['facts'])
    assert any(f['actor'] == 'customer' and f['temporality'] == 'planned' for f in state['facts'])
    assert any(f['actor'] == 'customer' and f['temporality'] == 'actual' and '下单' in f['text'] for f in state['facts'])
    for fact in state['facts']:
        lo, hi = fact['source_span']
        assert context[fact['source_field']][lo:hi] == fact['text']


def test_vague_goal_cannot_overrule_self_assessment():
    state = build_business_state({'expected_key_result': '办公物资', 'process_description': '需求单只差经理签字', 'self_assessment': 'achieved'})
    assert state['goal_fact_alignments'][0]['alignment_status'] == 'not_assessable'
    assert state['self_assessment_alignment']['alignment'] == 'not_assessable'


@pytest.mark.parametrize('context,bad,good,code', [
    ({'next_action_expected_result': '1'}, '下次期望结果未填写。', '下次期望结果已填写1，但属于占位内容。', 'FIELD_STATE_CONTRADICTION'),
    ({'expected_key_result': '办公物资'}, '记录不足以证明目标实现。', '已填写办公物资，但无法明确验收事项，不能判断达成程度。', 'FIELD_STATE_CONTRADICTION'),
    ({'process_description': '客户表示会转交资料'}, '客户已经转交资料。', '客户表示会转交资料，实际转交尚未体现。', 'TEMPORALITY_MISMATCH'),
    ({'process_description': '双方约定下次拜访'}, '没有明确客户动作。', '已约定再访，但尚未记录客户另外需要完成的独立动作。', 'JOINT_AGREEMENT_ERASED'),
    ({'process_description': '销售介绍产品'}, '客户介绍产品。', '销售介绍产品，未记客户回应。', 'ACTOR_MISMATCH'),
    ({'expected_key_result': '客户同意下次反馈具体条件', 'process_description': '客户暂无采购计划'}, '客户没有同意反馈具体条件。', '当前记录未体现客户同意反馈具体条件。', 'NOT_RECORDED_AS_NEGATIVE_FACT'),
    ({'expected_key_result': '现场收货', 'process_description': '现场给客户收货，送卡', 'self_assessment': 'achieved'}, '虽然自评达到目的，但缺少客户接收或反馈。', '代收动作已完成，客户接收后的反馈未记录，不影响代收成立。', 'CROSS_GOAL_CONTAMINATION'),
])
def test_invariant_positive_negative(context, bad, good, code):
    state = build_business_state(context)
    assert code in {e['code'] for e in validate_invariants(bad, state)}
    assert validate_invariants(good, state) == []


def test_supported_goal_not_downgraded_by_other_goal():
    state = build_business_state({'expected_key_result': '确认现阶段合作需求，并同意下次反馈具体条件',
        'process_description': '客户反馈暂无采购计划', 'self_assessment': 'partially_achieved'})
    bad = '客户尚未确认现阶段合作需求，两个目标均只部分达成。'
    assert 'SUPPORTED_GOAL_DOWNGRADED' in {x['code'] for x in validate_invariants(bad, state)}
    assert validate_invariants('当前需求状态已确认；下次反馈条件的承诺尚未记录，目标部分达成。', state) == []


@pytest.mark.parametrize('goal,process,expected', [
    ('客户确认合同最终版本', '客户尚未确认合同最终版本', 'unsupported'),
    ('客户确认预算范围', '客户预算范围尚不清楚', 'unsupported'),
    ('客户确认预算范围', '客户拒绝提供预算范围', 'contradicted'),
    ('客户确认预算范围', '销售确认预算范围', 'unsupported'),
    ('客户同意引荐负责人', '双方约定明天引荐负责人', 'unsupported'),
])
def test_alignment_does_not_infer_from_unknown_negative_or_other_actor(goal, process, expected):
    state = build_business_state({'expected_key_result': goal, 'process_description': process})
    assert state['goal_fact_alignments'][0]['alignment_status'] == expected


def test_source_text_survives_noun_list_separator():
    text = '确认预算以及审批流程，同时同意下次反馈具体条件'
    for item in decompose_goal('expected_key_result', text):
        assert item.source_text in text


def test_policy_v2_good_probes_survive_invariants():
    import json
    from pathlib import Path

    from taoran_agent.experimental_record_state import boundary_issues
    rows = json.loads((Path(__file__).resolve().parent / 'fixtures/semantic_v362_cases.json').read_text())
    good = [r for r in rows if r['expected_accept']]
    assert len(good) == 12
    for row in good:
        assert not boundary_issues(row['analysis'] + '。' + '。'.join(row['suggestions']), row['record']), row['case']


def test_preview_input_reuses_state_without_changing_stream_protocol():
    import json

    from taoran_agent.experimental_semantic_streaming_v22 import _interactive_messages, _messages
    context = {'expected_key_result': '办公物资', 'process_description': '等待签字'}
    data = json.loads(_interactive_messages(context)[1]['content'])
    assert data['record_state']['BUSINESS_SEMANTIC_STATE']['field_states']['expected_key_result'] == 'vague'
    assert data['untrusted_visit_data'] == context
    assert 'record_state' not in json.loads(_messages(context)[1]['content'])


@pytest.mark.parametrize('text', [
    '下次期望结果为1，未填写联系时间。',
    '下次期望结果为占位内容且未填写联系时间。',
    '下一步期望结果仍是1，联系时间尚未填写。',
])
def test_placeholder_and_missing_neighbor_field_are_not_mixed(text):
    state = build_business_state({'next_action_expected_result': '1', 'next_contact_at': None})
    assert not validate_invariants(text, state)


def test_explicit_negative_needs_confirmation_satisfies_coverage():
    state = build_business_state({'expected_key_result': '确认现阶段合作需求，并同意下次反馈具体条件',
        'process_description': '客户反馈暂无采购计划', 'self_assessment': 'partially_achieved'})
    text = '已确认现阶段无采购计划，但客户同意下次反馈具体条件这一目标在记录中尚未体现，自评部分达成与实际一致。'
    assert not validate_invariants(text, state, require_goal_coverage=True)


def test_joint_action_cannot_be_erased_inside_result_advice():
    state = build_business_state({'process_description': '简单与客户沟通后约定下次拜访'})
    text = '过程只写简单沟通后约定下次拜访，属于宽泛互动，没有客户表达的具体内容或动作来支撑关系拉近。'
    assert 'JOINT_AGREEMENT_ERASED' in {e['code'] for e in validate_invariants(text, state)}


@pytest.mark.parametrize('text', [
    '李部长表示会转告，后续看机会再增加，但未同意反馈具体条件。',
    '当前记录未体现该承诺，因此客户没有同意反馈具体条件。',
    '客户尚未明确承诺反馈具体条件。',
])
def test_implicit_actor_and_separate_clause_do_not_turn_unknown_into_denial(text):
    state = build_business_state({'expected_key_result': '客户同意下次反馈具体条件', 'process_description': '客户反馈暂无采购计划'})
    assert 'NOT_RECORDED_AS_NEGATIVE_FACT' in {e['code'] for e in validate_invariants(text, state)}


@pytest.mark.parametrize('goal,process,status', [
    ('客户同意引荐负责人', '客户不同意引荐负责人', 'contradicted'),
    ('客户确认A产品预算范围', '客户确认B产品预算范围', 'unsupported'),
    ('客户确认预算范围', '客户介绍预算工具', 'unsupported'),
])
def test_alignment_requires_matching_polarity_entity_and_operation(goal, process, status):
    state = build_business_state({'expected_key_result': goal, 'process_description': process})
    assert state['goal_fact_alignments'][0]['alignment_status'] == status


@pytest.mark.parametrize('join', ['，', '、', '：', '但'])
def test_negation_of_deeper_talk_does_not_erase_following_agreement(join):
    state = build_business_state({'process_description': '客户因开会未深入交流，简单与客户沟通后约定下次拜访'})
    text = '本次拜访已记录客户因开会未深入交流' + join + '简单沟通后双方约定下次拜访，这是已发生的互动事实。'
    assert not validate_invariants(text, state)


def test_compound_presentation_plan_does_not_assign_unrelated_facts_to_goal():
    state = build_business_state({'expected_key_result': '确认现阶段合作需求，并同意下次反馈具体条件',
        'process_description': '客户反馈暂无采购计划。客户表示会把资料转交QA。', 'self_assessment': 'partially_achieved'})
    plan = state['analysis_plan']
    goals = [p for p in plan if p['role'] == 'goal_alignment']
    assert [p['goal_id'] for p in goals] == ['G1', 'G2']
    assert goals[0]['fact_ids'] and not goals[1]['fact_ids']
    assert goals[1]['statement_form'] == 'observation_gap'
    assert len(plan) <= 4
    assert not any(p['kind'] == 'visit_context' for p in plan)
