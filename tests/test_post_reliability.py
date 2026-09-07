from copy import deepcopy
import json
import time

import httpx
import pytest

from test_post_policy import visit
from test_post_repair import reviewer, valid_payload
from taoran_agent.llm import _read_chat_response, ModelCallError
from taoran_agent.post_claim_guards import claim_hits
from taoran_agent.models import Q34SemanticFacts
from taoran_agent.scoring import score_q34


@pytest.mark.parametrize('source,text,blocked', [
    ('等待客户进一步评估。','本次仅有等待客户评估和明年续标前再确定',False),
    ('等待客户进一步评估。','客户正在评估技术方案',True),
    ('等待客户进一步评估。','客户尚在评估中',True),
    ('计划客户评估技术。','客户已完成评估',True),
    ('客户正在评估技术。','客户已经完成评估',True),
    ('客户正在评估技术。','客户继续评估技术',False),
    ('客户已完成评估。','客户已经评估',False),
    ('收集劳保订单，本周不会再点单','客户收集劳保订单',True),
    ('收集劳保订单，本周不会再点单','记录写收集劳保订单，主体需核实',False),
    ('收集劳保订单，本周不会再点单','下次目的应承接本次客户本周不再点单的事实',True),
    ('客户表示本周不会再点单','本次客户本周不再点单',False),
    ('等待客户评估，给客户收货','等待客户评估且客户已签收',True),
    ('正在走合同流程','等待完成合同流程',False),
    ('正在走合同流程','合同流程已完成',True),
])
def test_pending_ongoing_completed_and_actor(source,text,blocked):
    assert bool(claim_hits(text,'facts.reason',{'source_text':source})) == blocked


def test_grounding_recheck_recomputes_score_from_new_facts(tmp_path,monkeypatch):
    r=reviewer(tmp_path)
    v=visit(process_description='收集劳保订单，本周不会再点单')
    initial=valid_payload(r,v)
    initial['facts']['reason']='客户收集劳保订单。'
    calls=[]
    def request(messages,precheck,timeout,lease,**kwargs):
        lease.release(); calls.append((messages,kwargs))
        if len(calls)==1:
            return deepcopy(initial),{}
        new=deepcopy(initial)
        new['facts']['reason']='记录为收集劳保订单，本周不会再点单；收集的主体未明确，建议核实。'
        new['facts']['purpose_achievement']='partially_achieved'
        return new,{}
    monkeypatch.setattr(r,'_request',request)
    try:
        result=r.review_q34(v)
        assert result.status=='completed'
        assert len(calls)==2 and not calls[1][1]['repair']
        assert '唯一一次受控重核' in calls[1][0][-1]['content']
        assert result.quality_audit['fact_grounding_reassessed']
        initial_facts=Q34SemanticFacts(provider='llm-chat',**initial['facts'])
        assert score_q34(v,result)[0].score!=score_q34(v,initial_facts)[0].score
        assert result.purpose_achievement=='partially_achieved'
        assert '客户收集劳保订单' not in result.reason
    finally:r.close()


def test_unlimited_backend_can_finish_after_legacy_deadline(tmp_path,monkeypatch):
    r=reviewer(tmp_path,llm_evaluation_timeout_seconds=0.01,llm_evaluation_retry_timeout_seconds=0.01)
    v=visit(); data=valid_payload(r,v);timeouts=[]
    def request(messages,precheck,timeout,lease,**kwargs):
        timeouts.append(timeout)
        try:time.sleep(0.04);return deepcopy(data),{}
        finally:lease.release()
    monkeypatch.setattr(r,'_request',request)
    try:
        parsed,_=r._analyze(v,False)
        assert parsed and timeouts==[None]
    finally:r.close()


@pytest.mark.parametrize('stream',[True,False])
def test_stream_reader_accepts_no_generation_deadline(stream):
    if stream:
        body='data: '+json.dumps({'choices':[{'delta':{'content':'{}'},'finish_reason':'stop'}]})+'\n\n'
    else:body='{"choices":[]}'
    response=httpx.Response(200,headers={'content-type':'text/event-stream' if stream else 'application/json'},content=body)
    envelope,_,_=_read_chat_response(response,started=time.monotonic()-10000,timeout=None,max_bytes=1000)
    assert 'choices' in envelope


def test_done_finishes_without_waiting_for_transport_close():
    class Response:
        headers={'content-type':'text/event-stream'}
        def iter_lines(self):
            yield 'data: '+json.dumps({'choices':[{'delta':{'content':'{}'},'finish_reason':'stop'}]})
            yield 'data: [DONE]'
            raise AssertionError('Must not wait for socket close after DONE')
    value,_,_=_read_chat_response(Response(),started=time.monotonic(),timeout=None,max_bytes=1000)
    assert value['choices'][0]['message']['content']=='{}'
