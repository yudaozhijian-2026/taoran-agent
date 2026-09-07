import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from taoran_agent import api
from taoran_agent.config import Settings
from taoran_agent.experimental_business_semantic_state import build_business_state
from taoran_agent.experimental_rendering_binding import (
    check_targeted_retry,
    experimental_retry_allowed,
    normalize_bindings,
    validate_bindings,
)
from taoran_agent.experimental_semantic_streaming_v22 import (
    _interactive_preview_safe,
    detect_unsupported_specific_facts,
)
from taoran_agent.llm import ChatModelReviewer


def state():
    return build_business_state({'expected_key_result':'客户确认现阶段合作需求，并同意下一次沟通中反馈具体条件',
                                 'process_description':'客户反馈目前没有原料药新的采购计划。'})


def test_unique_identifiers_are_filled_without_text_inference_or_state_mutation():
    s=state();before=deepcopy(s)
    points=[{'goal_id':'G2','claim_type':'unresolved','text':'当前记录未体现条件承诺。'},
            {'kind':'next_step','claim_type':'planned','text':'下一步确认预算。'},
            {'kind':'customer_fact','claim_type':'recorded_fact','text':'已反馈暂无采购计划。'},
            {'kind':'visit_context','claim_type':'context','text':'本次拜访。'},
            {'contract_id':'C_G1','claim_type':'supported','text':'需求已确认。'}]
    old=deepcopy(points);out=normalize_bindings(points,s)
    assert [p['contract_id'] for p in out]==['C_G2','C_NEXT','C_PROCESS','C_CONTEXT','C_G1']
    assert out[-1]['goal_id']=='G1'
    assert s==before and points==old
    assert [p['text'] for p in out]==[p['text'] for p in points]


@pytest.mark.parametrize('point',[
    {'kind':'objective_result','text':'G1需求已确认'},
    {'kind':'judgment_gap','text':'应补充事实'},
    {'goal_id':'G99','kind':'customer_fact','text':'需求'},
    {'contract_id':'invented','goal_id':'G1','text':'需求'},
])
def test_ambiguous_or_explicit_invalid_id_is_never_guessed(point):
    out=normalize_bindings([point],state())
    assert out==[point]
    errors=validate_bindings(out,state())
    assert any(e['code']=='UNKNOWN_CONTRACT' for e in errors)
    assert not experimental_retry_allowed({'rejection':{'binding_errors':errors,'rendering_repairs':[{'failed_contract_id':'C_G1'}]}})


def test_wrong_goal_and_claim_are_not_silently_replaced():
    p={'contract_id':'C_G1','goal_id':'G2','claim_type':'unresolved','text':'当前记录未体现需求确认。'}
    assert normalize_bindings([p],state())==[p]
    assert any(e['code']=='BINDING_STATE_MISMATCH' for e in validate_bindings([p],state()))


def test_retry_keeps_each_unidentified_position_and_rejects_unrelated_addition():
    previous={'analysis_points':[{'text':'第一项'}, {'text':'第二项'}, {'contract_id':'C_G2','text':'待修正'}], 'items':[]}
    ctx={'previous_candidate':previous,'rejection':{'rendering_repairs':[{'failed_contract_id':'C_G2','suggestion_scope':[]}]}}
    new=deepcopy(previous);new['analysis_points'][2]['text']='修正后'
    assert not check_targeted_retry(new,ctx)
    new['analysis_points'][0]['text']='被篡改'
    assert check_targeted_retry(new,ctx)[0]['point_index']==0
    new=deepcopy(previous);new['analysis_points'].append({'contract_id':'C_CONTEXT','text':'新增'})
    assert check_targeted_retry(new,ctx)


def test_retry_after_normalizing_missing_ids_has_no_none_collision():
    previous={'analysis_points':[{'goal_id':'G1','text':'需求确认'}, {'goal_id':'G2','text':'错误条件'}]}
    previous['analysis_points']=normalize_bindings(previous['analysis_points'],state())
    new=deepcopy(previous);new['analysis_points'][1]['text']='当前未体现条件承诺'
    ctx={'previous_candidate':previous,'rejection':{'rendering_repairs':[{'failed_contract_id':'C_G2','suggestion_scope':[]}]}}
    assert not check_targeted_retry(new,ctx)


@pytest.mark.parametrize('text',[
    '当前记录未体现客户独立行动或客户承诺。',
    '尚未记录客户行动和客户承诺。',
])
def test_preview_coordinated_absence_is_not_customer_commitment(text):
    assert not detect_unsupported_specific_facts(text,{},interactive=True)['failure_category']
    assert _interactive_preview_safe(text,{})


@pytest.mark.parametrize('text',[
    '当前记录未体现客户独立行动，客户承诺明年签约。',
    '当前记录未体现客户独立行动；客户承诺签约。',
    '并非未体现客户独立行动或客户承诺签约。',
    '客户已承诺签约。',
    '未体现客户独立行动，但客户承诺签约。',
])
def test_preview_negation_does_not_hide_affirmative_commitment(text):
    assert detect_unsupported_specific_facts(text,{},interactive=True)['failure_category']


@pytest.mark.parametrize('mode',['missing_contract','missing_goal_id','wrong_claim','invalid_contract','ambiguous','duplicate_process','long_process'])
def test_actual_glm_wire_schema_and_pipeline_retry(monkeypatch,mode):
    from test_experimental_semantic_audit import PIPELINE_CONTEXT, pipeline_provider
    calls=[];requests=[];ordinary=pipeline_provider(['pass'],calls)
    def provider(request):
        body=json.loads(request.content)
        if 'candidate' in json.loads(body['messages'][1]['content']):
            return ordinary(request)
        requests.append(body)
        assert body['response_format']=={'type':'json_object'} and 'tools' not in body
        instruction=body['messages'][0]['content']
        assert '只返回紧凑JSON：' not in instruction
        schema=json.loads(instruction.split('实验输出JSON Schema（JSON-object模式也必须遵循）：')[1].split('\n绑定结构示例')[0])
        required=schema['properties']['analysis_points']['items']['required']
        assert 'contract_id' in required and 'claim_type' in required and 'fact_ids' not in required
        response=ordinary(request);wire=response.json();payload=json.loads(wire['choices'][0]['message']['content'])
        for point in payload['analysis_points']:
            point.pop('fact_ids',None)
            if mode=='missing_contract':point.pop('contract_id',None)
            if mode=='missing_goal_id':point.pop('goal_id',None)
            if mode=='wrong_claim' and len(requests)==1:point['claim_type']='unresolved'
            if mode=='invalid_contract':point['contract_id']='invented'
            if mode=='ambiguous':
                point.pop('contract_id',None);point.pop('goal_id',None);point['kind']='objective_result'
        if mode in {'duplicate_process','long_process'}:
            process=deepcopy(payload['analysis_points'][0])
            process.update(contract_id='C_PROCESS',goal_id='',claim_type='recorded_fact',kind='customer_fact')
            process['proofs']=[p for p in process['proofs'] if p['field']=='process_description']
            if mode=='long_process':process['text'] = '客户表示审批暂缓。' * 9
            payload['analysis_points'].append(process)
            if mode=='duplicate_process' and len(requests)==1: payload['analysis_points'].append(deepcopy(process))
        wire['choices'][0]['message']['content']=json.dumps(payload)
        return httpx.Response(200,json=wire)
    reviewer=ChatModelReviewer(Settings(_env_file=None,llm_model='glm-test',llm_api_url='https://example.test/chat',llm_api_key='test'),None,transport=httpx.MockTransport(provider))
    monkeypatch.setattr(api,'get_store',lambda _:SimpleNamespace(get_feedback_artifact=lambda *a:None,save_feedback_artifact=lambda *a:a[-1]))
    monkeypatch.setattr(api,'_front_specificity_items',lambda *a:[{'code':'R','source_fields':PIPELINE_CONTEXT}])
    monkeypatch.setattr(api,'_cached_knowledge_wording',lambda _:None)
    monkeypatch.setattr(api,'_apply_knowledge_wording',lambda _,wording,*a,**k:wording)
    response=SimpleNamespace(knowledge_snapshot_hash='k',knowledge_references=['ref'],issues=[],tenant_id='t',input_snapshot_hash='i')
    try:
        result=api._enhance_front_suggestions(response,reviewer,reviewer.settings,5,{'visit_snapshot':PIPELINE_CONTEXT},experimental=True)
        assert len(requests)==(2 if mode in {'wrong_claim','duplicate_process'} else 1)
        assert result.status==('unavailable' if mode in {'invalid_contract','ambiguous'} else 'completed')
        if mode in {'invalid_contract','ambiguous'}:assert calls==['generate']
    finally:reviewer.close()


def test_duplicate_goal_contract_still_cannot_retry():
    ctx={'rejection':{'binding_errors':[{'code':'DUPLICATE_CONTRACT','contract_id':'C_G1'}],
                     'rendering_repairs':[{'failed_contract_id':'C_G1'}]}}
    assert not experimental_retry_allowed(ctx)
