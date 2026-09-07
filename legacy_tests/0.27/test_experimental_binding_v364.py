import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from queue import Queue
from threading import Event
from types import SimpleNamespace

import httpx
import pytest

from taoran_agent import api
from taoran_agent.config import Settings
from taoran_agent.experimental_business_semantic_state import build_business_state
from taoran_agent.experimental_rendering_binding import (
    check_targeted_retry,
    experimental_retry_allowed,
    repair_targets,
    validate_bindings,
)
from taoran_agent.experimental_semantic_invariants import field_claims, validate_invariants
from taoran_agent.llm import ChatModelReviewer


@pytest.mark.parametrize('text,expected', [
    ('下次拜访日期尚未填写。', []),
    ('下一次拜访日期尚未填写。', []),
    ('下次拜访目的尚未填写。', []),
    ('下一步行动目的尚未填写。', []),
    ('本次拜访日期尚未填写。', ['visit_date']),
    ('本次拜访目的尚未填写。', ['purpose_code']),
    ('本次目标尚未填写，下次期望结果也尚未填写。', ['expected_key_result']),
    ('下次期望结果未填写；本次预期结果未填写。', ['expected_key_result']),
    ('本次目标已填，下次目标未填写。', []),
    ('本次目标未填写，下次联系时间尚未填写。', ['expected_key_result']),
])
def test_field_scope_by_local_phrase(text, expected):
    s=build_business_state({'visit_date':'2026-08-24','purpose_code':'收集信息','expected_key_result':'办公物资'})
    errors=validate_invariants(text,s)
    assert sorted({e['field'] for e in errors if e['code']=='FIELD_STATE_CONTRADICTION'})==sorted(expected)


def test_scoped_and_ambiguous_labels_have_no_double_owner():
    assert list(field_claims('下次拜访期望的关键结果未填写'))==[('next_action_expected_result','下次拜访期望的关键结果未填写')]
    assert not list(field_claims('预期结果未填写'))
    s=build_business_state({'expected_key_result':'1','next_action_expected_result':'客户确认预算'})
    assert not validate_invariants('本次目标已填写占位值，下次目标已填写。',s)
    assert validate_invariants('本次目标未具体填写，下次目标已填写。',s)


@pytest.mark.parametrize('text,blocked',[
    ('客户未就此作出明确承诺。',True),
    ('当前记录尚未体现客户同意反馈具体条件，客户未就此作出明确承诺。',True),
    ('客户没有承诺反馈具体条件。',True),
    ('当前记录尚未体现客户承诺反馈具体条件。',False),
    ('无法判断客户未就此作出明确承诺。',False),
    ('不能说客户未就此作出明确承诺。',False),
])
def test_clause_local_denial(text, blocked):
    s=build_business_state({'expected_key_result':'客户同意反馈具体条件','process_description':'销售介绍产品'})
    assert bool(validate_invariants(text,s)) is blocked


def test_recorded_refusal_and_ambiguous_reference_not_rejected():
    s=build_business_state({'expected_key_result':'客户同意反馈具体条件','process_description':'客户明确拒绝反馈具体条件'})
    assert not validate_invariants('客户明确拒绝反馈具体条件。',s)
    assert not validate_invariants('客户未就此作出明确承诺。',s)
    s=build_business_state({'expected_key_result':'客户确认合同最终版本，并承诺完成签署','process_description':'销售找商品'})
    assert not validate_invariants('客户未就此作出明确承诺。',s)  # Referent not unique.


def test_bound_goal_denial_uses_bound_target_not_other_goal():
    from test_experimental_rendering_guidance import bound, composite
    s=composite();points=[bound(s,'C_G1','已确认需求。'),bound(s,'C_G2','当前记录尚未体现条件反馈，客户未就此作出明确承诺。')]
    assert any(e['code']=='NOT_RECORDED_AS_NEGATIVE_FACT' and e['contract_id']=='C_G2' for e in validate_bindings(points,s))


def test_mixed_proof_repair_requires_known_target_and_keeps_other_points():
    s=build_business_state({'expected_key_result':'现场收货','process_description':'现场给客户收货','next_action_expected_result':'客户确认预算'})
    points=[{'contract_id':'C_G1','goal_id':'G1','claim_type':'supported','text':'销售已代收货。','proofs':[{'field':'process_description','quote':'现场给客户收货'}]},
            {'contract_id':'C_NEXT','claim_type':'','text':'计划跟进预算。','proofs':[{'field':'process_description','quote':'现场给客户收货'}]}]
    errors=validate_bindings(points,s);context={'previous_candidate':{'analysis_points':points},'rejection':{'binding_errors':errors,'rendering_repairs':repair_targets(errors,s)}}
    assert experimental_retry_allowed(context)
    assert all(t['failed_contract_id']=='C_NEXT' for t in context['rejection']['rendering_repairs'])
    assert context['rejection']['rendering_repairs'][-1]['allowed_source_fields']
    fixed=deepcopy(points);fixed[1].update(claim_type='planned',proofs=[{'field':'next_action_expected_result','quote':'客户确认预算'}])
    assert not validate_bindings(fixed,s)
    assert not check_targeted_retry({'analysis_points':fixed},context)
    fixed[0]['text']='改变未授权目标'
    assert check_targeted_retry({'analysis_points':fixed},context)
    context['rejection']['binding_errors'].append({'code':'UNKNOWN_CONTRACT','contract_id':None})
    assert not experimental_retry_allowed(context)


@pytest.mark.parametrize('drop_proof',[False,True])
def test_mixed_error_pipeline_repairs_once_and_requires_evidence(monkeypatch,drop_proof):
    from test_experimental_semantic_audit import PIPELINE_CONTEXT, pipeline_provider
    visit={**PIPELINE_CONTEXT,'next_action_expected_result':'客户确认预算'}
    calls=[];requests=[];ordinary=pipeline_provider(['pass'],calls)
    def provider(request):
        body=json.loads(request.content)
        if 'candidate' in json.loads(body['messages'][1]['content']):return ordinary(request)
        requests.append(body)
        instruction=body['messages'][0]['content']
        schema=json.loads(instruction.split('实验输出JSON Schema（JSON-object模式也必须遵循）：')[1].split('\n绑定结构示例')[0])
        prop=schema['properties']['analysis_points']['items']['properties']['claim_type']
        assert prop['minLength']==1 and 'planned' in prop['enum']
        assert '非目标结构示例' in instruction
        response=ordinary(request);wire=response.json();payload=json.loads(wire['choices'][0]['message']['content'])
        next_point={'contract_id':'C_NEXT','claim_type':'planned','kind':'next_step','text':'下一步计划请客户确认预算。','proofs':[{'field':'next_action_expected_result','quote':'客户确认预算'}]}
        if len(requests)==1:
            next_point['claim_type']='';next_point['proofs']=[{'field':'process_description','quote':PIPELINE_CONTEXT['process_description']}]
        elif drop_proof:next_point['proofs']=[]
        payload['analysis_points'].append(next_point)
        wire['choices'][0]['message']['content']=json.dumps(payload)
        return httpx.Response(200,json=wire)
    settings=Settings(_env_file=None,llm_model='glm-test',llm_api_url='https://example.test/chat',llm_api_key='test',frontend_model_format_retries=1)
    reviewer=ChatModelReviewer(settings,None,transport=httpx.MockTransport(provider))
    monkeypatch.setattr(api,'get_store',lambda _:SimpleNamespace(get_feedback_artifact=lambda *a:None,save_feedback_artifact=lambda *a:a[-1]))
    monkeypatch.setattr(api,'_front_specificity_items',lambda *a:[{'code':'R','source_fields':visit}])
    monkeypatch.setattr(api,'_cached_knowledge_wording',lambda _:None)
    monkeypatch.setattr(api,'_apply_knowledge_wording',lambda _,wording,*a,**k:wording)
    response=SimpleNamespace(knowledge_snapshot_hash='k',knowledge_references=['ref'],issues=[],tenant_id='t',input_snapshot_hash='i')
    try:
        result=api._enhance_front_suggestions(response,reviewer,settings,5,{'visit_snapshot':visit},experimental=True)
        assert len(requests)==2
        assert result.status==('unavailable' if drop_proof else 'completed'),result.model_dump()
        assert calls==(['generate','generate'] if drop_proof else ['generate','generate','audit'])
    finally:reviewer.close()


@pytest.mark.parametrize('preview_first',[False,True])
def test_stage_order_is_controlled_and_late_preview_cannot_overwrite_final(monkeypatch,preview_first):
    from test_interactive_quick_check import _canonical, _settings
    began,release,finished=Event(),Event(),Event()
    events=Queue()
    with ThreadPoolExecutor(max_workers=1) as preview_pool, ThreadPoolExecutor(max_workers=1) as final_pool:
        class CapturedExecutor:
            future=None
            def submit(self,fn,*args):
                self.future=preview_pool.submit(fn,*args)
                return self.future
        captured=CapturedExecutor()
        def preview(*args,**kwargs):
            args[2]('先发片段');began.set()
            if not preview_first:assert release.wait(3)
            args[2]('后发片段');finished.set()
            return {'status':'completed'}
        def final(*args):
            assert began.wait(3)
            if preview_first:
                assert finished.wait(3)
                # submit may be returning while the task is already complete.
                # Wait for completion via the pool itself, not a timing guess.
                preview_pool.submit(lambda:None).result(timeout=3)
            return {'status':'completed','feedback_text':'最终结果'}
        monkeypatch.setattr(api,'_quick_check_preview_executor',captured)
        monkeypatch.setattr(api,'_quick_check_final_executor',final_pool)
        monkeypatch.setattr(api,'stream_semantic_preview_v22',preview)
        monkeypatch.setattr(api,'_quick_check_run_final',final)
        try:
            result=api._quick_check_run(_canonical(),_settings(),events)
            assert result['preview']['status']==('completed' if preview_first else 'superseded')
            assert result['final']['feedback_text']=='最终结果'
        finally:release.set()
        captured.future.result(timeout=3)
        emitted=[]
        while not events.empty():emitted.append(events.get_nowait())
        if not preview_first:assert [e['text'] for e in emitted if e['type']=='preview_delta']==['先发片段']
        assert result['final']['feedback_text']=='最终结果'


@pytest.mark.parametrize('text,blocked', [
    ('当前记录未体现客户的独立行动或客户承诺。',False),
    ('尚未记录客户的行动和客户承诺。',False),
    ('当前记录未体现客户的独立行动，客户承诺签约。',True),
    ('并非未体现客户的独立行动或客户承诺签约。',True),
    ('未体现客户的独立行动；客户承诺签约。',True),
])
def test_coordinated_nominal_possessive_does_not_expand_negation(text,blocked):
    from taoran_agent.experimental_semantic_streaming_v22 import detect_unsupported_specific_facts
    assert bool(detect_unsupported_specific_facts(text,{},interactive=True)['failure_category']) is blocked


@pytest.mark.parametrize('text,blocked',[
    ('不能视为客户已下单。',False),
    ('尚不能认为客户已完成签署。',False),
    ('不能认定客户已确认订单。',False),
    ('不能视为客户已下单，客户已承诺签约。',True),
    ('并非不能视为客户已下单。',True),
    ('客户已下单。',True),
])
def test_negated_inference_is_not_affirmative_customer_event(text,blocked):
    from taoran_agent.experimental_semantic_streaming_v22 import detect_unsupported_specific_facts
    assert bool(detect_unsupported_specific_facts(text,{},interactive=True)['failure_category']) is blocked


@pytest.mark.parametrize('text,blocked',[
    ('建议明确验收标准，如希望客户同意安排某项对接。',False),
    ('希望客户同意参与下一次沟通。',False),
    ('例如客户同意对接可以作为验收标准。',False),
    ('希望后续取得参与，客户同意安排对接。',True),
    ('建议明确验收标准，客户已同意安排对接。',True),
    ('客户同意安排对接。',True),
])
def test_proposed_customer_action_is_not_asserted_fact(text,blocked):
    from taoran_agent.experimental_semantic_streaming_v22 import detect_unsupported_specific_facts
    assert bool(detect_unsupported_specific_facts(text,{},interactive=True)['failure_category']) is blocked
