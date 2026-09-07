import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from taoran_agent import api
from taoran_agent.config import Settings
from taoran_agent.experimental_business_semantic_state import build_business_state
from taoran_agent.experimental_rendering_binding import (
    resolve_binding_fact_ids,
    resolve_bindings,
    validate_bindings,
)
from taoran_agent.experimental_rendering_guidance import rendering_input
from taoran_agent.experimental_semantic_invariants import validate_invariants
from taoran_agent.llm import ChatModelReviewer


def contract(state, cid):
    return next(c for c in rendering_input(state)['RENDERING_CONTRACTS'] if c['contract_id'] == cid)


def test_supported_facts_resolved_from_state_even_if_model_or_contract_ids_wrong():
    s = build_business_state({'expected_key_result': '现场收货', 'process_description': '现场给客户收货，送卡'})
    c = contract(s, 'C_G1')
    c['supporting_fact_ids'] = ['invented']
    assert resolve_binding_fact_ids(c, s) == s['goal_fact_alignments'][0]['supporting_fact_ids']
    point = {'contract_id': 'C_G1', 'goal_id': 'G1', 'claim_type': 'supported', 'text': '销售已完成代收货。', 'fact_ids': ['invented']}
    assert not validate_bindings([point], s)
    assert resolve_bindings([point], s)[0]['fact_ids'] == s['goal_fact_alignments'][0]['supporting_fact_ids']


@pytest.mark.parametrize('claim', ['unresolved', 'not_recorded', 'not_assessable', 'placeholder', 'vague'])
def test_observation_claim_can_have_no_supporting_facts(claim):
    s = build_business_state({'expected_key_result': '客户确认合同版本', 'process_description': '销售介绍产品'})
    c = {**contract(s, 'C_G1'), 'claim_type': claim}
    assert resolve_binding_fact_ids(c, s) == []


def test_next_inherits_only_section_facts_without_mutating_state():
    s = build_business_state({'process_description': '客户表示下周会转交资料', 'next_action_expected_result': '客户确认预算', 'next_contact_at': '2026-09-08'})
    before = deepcopy(s)
    c = contract(s, 'C_NEXT')
    ids = resolve_binding_fact_ids(c, s)
    assert ids and c['allowed_fact_ids'] == ids
    assert all(f['source_field'].startswith('next_') for f in s['facts'] if f['fact_id'] in ids)
    unrelated = [f['fact_id'] for f in s['facts'] if f['source_field'] == 'process_description']
    assert unrelated and not set(ids) & set(unrelated)
    assert s == before


@pytest.mark.parametrize('kind,actor,time', [('PLANNED_ACTION','sales','planned'), ('JOINT_AGREEMENT','both','actual'), ('CUSTOMER_COMMITMENT','customer','actual'), ('CUSTOMER_ACTION','customer','actual'), ('SYSTEM_FACT','unknown','unknown')])
def test_next_inherits_allowed_fact_types_from_state(kind, actor, time):
    s = build_business_state({'next_action_expected_result': '客户确认预算'})
    s['facts'][0].update(fact_type=kind, actor=actor, temporality=time)
    assert resolve_binding_fact_ids(contract(s, 'C_NEXT'), s) == [s['facts'][0]['fact_id']]


def test_next_rejects_self_assessed_future_metadata():
    s = build_business_state({'next_action_expected_result': '客户确认预算'})
    s['facts'][0]['temporality'] = 'self_assessed'
    assert not resolve_binding_fact_ids(contract(s,'C_NEXT'), s)


@pytest.mark.parametrize('target,text,allowed', [
    ('办公物资', '自评偏乐观，应改为部分达成。', False),
    ('办公物资', '当前目标缺少明确完成标准，因此暂时无法判断达成程度。', True),
    ('客户确认合同版本', '当前记录对自评的支撑仍不足。', True),
    ('客户确认合同版本', '自评达到目的缺少对应事实支撑。', True),
    ('客户确认合同版本', '当前记录尚不足以判断现有自评是否与事实完全匹配。', True),
    ('客户确认合同版本', '建议补充相关事实后再校准自评。', True),
    ('客户确认合同版本', '自评错误，应改成未达成。', False),
    ('客户确认合同版本', '应将自评改为部分达成。', False),
])
def test_two_not_assessable_scopes(target, text, allowed):
    s = build_business_state({'expected_key_result':target, 'process_description':'给客户网上找商品', 'self_assessment':'achieved'})
    assert bool(validate_invariants(text,s)) is not allowed


def test_placeholder_protection_remains():
    s = build_business_state({'next_action_expected_result':'1'})
    assert validate_invariants('下次期望结果未具体填写。',s)
    assert not validate_invariants('下次期望结果已填“1”，但属于占位内容。联系时间尚未填写。',s)


def test_policy_changes_only_authorized_labels_and_preserves_text():
    fixture=Path(__file__).parent/'fixtures'
    old=json.loads((fixture/'semantic_v35_cases.json').read_text())
    new=json.loads((fixture/'semantic_v362_cases.json').read_text())
    policy=json.loads((fixture/'legacy_probe_policy_v2.json').read_text())
    changes={c['case']:c for c in policy['changes']}
    assert set(changes)=={'relationship_introduction_only_good','case8_original_good','case10_good','expansion_08_actual_v32'}
    for a,b in zip(old,new):
        assert {k:v for k,v in a.items() if k!='expected_accept'}=={k:v for k,v in b.items() if k!='expected_accept'}
        if a['case'] in changes:
            assert a['expected_accept'] is True and b['expected_accept'] is False
            assert changes[a['case']]['reason'] and changes[a['case']]['allowed_alternative']
        else:
            assert a==b


def test_procurement_narrow_rule_does_not_reclassify_alignment():
    fixture=json.loads((Path(__file__).parent/'fixtures/semantic_v362_cases.json').read_text())
    row=next(r for r in fixture if r['case']=='procurement_inflated_bad')
    s=build_business_state(row['record']);before=deepcopy(s)
    assert s['goal_items'][0]['status']=='unresolved'
    assert any(e['code']=='PROCUREMENT_ATTITUDE_AS_COMPLETION_CONDITION' for e in validate_invariants(row['analysis'],s))
    assert s==before
    for text in ['已了解预算与审批困难，合作意愿仍中立。', '不能因合作意愿中立就认为采购沟通目标未完成。', '已沟通采购情况，但客户合作意愿中立，尚不能认为已取得采购承诺。']:
        assert not validate_invariants(text,s)
    s2=build_business_state({**row['record'],'expected_key_result':'客户承诺采购物资'})
    assert not any(e['code']=='PROCUREMENT_ATTITUDE_AS_COMPLETION_CONDITION' for e in validate_invariants(row['analysis'],s2))


@pytest.mark.parametrize('wrong_claim', [False, True])
def test_metadata_omission_no_retry_but_wrong_claim_retries_once(monkeypatch,wrong_claim):
    from test_experimental_semantic_audit import PIPELINE_CONTEXT, pipeline_provider
    calls=[];requests=[];ordinary=pipeline_provider(['pass'],calls)
    def provider(request):
        body=json.loads(request.content)
        if 'candidate' in json.loads(body['messages'][1]['content']):
            return ordinary(request)
        response=ordinary(request);wire=response.json();payload=json.loads(wire['choices'][0]['message']['content'])
        requests.append(body)
        for point in payload['analysis_points']:
            point.pop('fact_ids',None)
            if wrong_claim and len(requests)==1:
                point['claim_type']='unresolved'
        wire['choices'][0]['message']['content']=json.dumps(payload)
        return httpx.Response(200,json=wire)
    settings=Settings(_env_file=None,llm_model='glm-test',llm_api_url='https://example.test/chat',llm_api_key='test',frontend_model_format_retries=1,knowledge_semantic_cache_seconds=0)
    reviewer=ChatModelReviewer(settings,None,transport=httpx.MockTransport(provider))
    monkeypatch.setattr(api,'get_store',lambda _:SimpleNamespace(get_feedback_artifact=lambda *a:None,save_feedback_artifact=lambda *a:a[-1]))
    monkeypatch.setattr(api,'_front_specificity_items',lambda *a:[{'code':'R','source_fields':PIPELINE_CONTEXT}])
    monkeypatch.setattr(api,'_cached_knowledge_wording',lambda _:None)
    monkeypatch.setattr(api,'_apply_knowledge_wording',lambda _,wording,*a,**k:wording)
    response=SimpleNamespace(knowledge_snapshot_hash='k',knowledge_references=['ref'],issues=[],tenant_id='t',input_snapshot_hash='i')
    try:
        result=api._enhance_front_suggestions(response,reviewer,settings,2,{'visit_snapshot':PIPELINE_CONTEXT},experimental=True)
        assert result.status=='completed',result.model_dump()
        assert len(requests)==(2 if wrong_claim else 1)
        assert calls==(['generate','generate','audit'] if wrong_claim else ['generate','audit'])
        assert result.model_attempts[-1]['rendering_bindings'][0]['fact_ids']==['F1']
        if wrong_claim:
            repairs=json.loads(requests[1]['messages'][-1]['content'])['retry_evidence_data']['rejection']['rendering_repairs']
            assert repairs[0]['expected_goal_id']=='G1'
            assert repairs[0]['expected_claim_type']==['supported']
            assert 'canonical_wording' in repairs[0]
    finally:
        reviewer.close()
