import json
from copy import deepcopy

import httpx
import pytest
from test_post_policy import visit

from taoran_agent.agent import TaoranAgent
from taoran_agent.config import Settings
from taoran_agent.feedback import build_front_ai_suggestions_with_model
from taoran_agent.front_v46 import experimental_semantic_streaming_v22 as preview
from taoran_agent.front_v46.observed_feedback import generate
from taoran_agent.front_v46.reviewer import FrontReviewer
from taoran_agent.models import PrecheckRequest, RequestContext


@pytest.mark.parametrize('body', ['分析与必要核对事项', '<USER_FEEDBACK>分析与必要核对事项</USER_FEEDBACK>',
    '<USER_FEEDBACK>分析与必要核对事项</USER_FEEDBACK>这里才是实际正文'])
@pytest.mark.parametrize('persistent', [False, True])
def test_title_only_never_published_and_one_bounded_repair(tmp_path, monkeypatch, body, persistent):
    settings = Settings(_env_file=None, database_path=str(tmp_path/'db'), llm_enabled=True,
        llm_model='test', llm_api_url='https://example.test/chat', llm_api_key='test')
    calls = []
    def provider(request):
        calls.append(request)
        text = body if persistent or len(calls) == 1 else '原文仅记录沟通，客户是否实际确认尚不足以判断，请核对客户反馈。'
        events = [{'choices':[{'delta':{'content':text}}]}, {'choices':[{'delta':{},'finish_reason':'stop'}]}]
        return httpx.Response(200,text=''.join('data: '+json.dumps(e)+'\n\n' for e in events))
    real = httpx.Client
    monkeypatch.setattr(preview.httpx,'Client',lambda **k:real(transport=httpx.MockTransport(provider)))
    emitted = []
    result = preview.stream_semantic_preview_v22(settings,visit(),emitted.append,interactive=True)
    assert len(calls) == 2
    assert result['status'] == ('failed' if persistent else 'completed')
    assert '分析与必要核对事项' not in ''.join(emitted)
    assert bool(emitted) is not persistent
    assert all(a['diagnostic_evidence_id'] for a in result['model_attempts'])


@pytest.mark.parametrize('mode', ['advice', 'empty', 'confirmation', 'repair', 'persistent', 'contradiction'])
def test_suggestion_rendering_and_empty_decision(tmp_path, monkeypatch, mode):
    good = {'analysis_points':[{'kind':'objective_result','text':'当前原文仅记录收集信息，需核对实际信息。'}],
        'items':[{'code':'R','suggestion':'请补充实际收集的信息及客户反馈。', 'proofs':[{'field':'process_description','quote':'收集信息'}]}],
        'suggestion_status':'has_suggestions','suggestion_reason':'过程没有记录实际收集的信息。'}
    if mode == 'empty':
        good.update(items=[],suggestion_status='no_change_needed',suggestion_reason='原文记录清楚，无额外补充事项。')
        good['analysis_points'][0]['text']='已清楚记录客户预算未批准。'
    if mode == 'confirmation':
        good.update(items=[],suggestion_status='needs_confirmation',
            confirmations=[{'field':'process_description','quote':'收集信息','question':'实际收集到了什么信息？','impact':'目标达成判断'}])
    calls = []
    def provider(request):
        calls.append(json.loads(request.content))
        raw=deepcopy(good)
        if mode == 'persistent' or (mode == 'repair' and len(calls)==1):
            raw.update(items=[],suggestion_status=None,suggestion_reason='')
        if mode == 'contradiction':
            raw.update(items=[],suggestion_status='no_change_needed',suggestion_reason='无建议')
            raw['analysis_points'][0]['requires_followup']=True
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(raw)},'finish_reason':'stop'}]})
    settings=Settings(_env_file=None,database_path=str(tmp_path/'db'),llm_model='test',llm_api_url='https://example.test/chat',llm_api_key='test')
    reviewer=FrontReviewer(settings,None,transport=httpx.MockTransport(provider))
    monkeypatch.setattr(reviewer,'_experimental_audit_wording',lambda *a,**k:None)
    try:
        result=generate(reviewer,[{'code':'R'}],{'visit_analysis_context':{'process_description':'收集信息'}},30)
        request=PrecheckRequest(context=RequestContext(tenant_id='test',request_id='front',user_id='test'),visit=visit())
        precheck = TaoranAgent().precheck(request)
        text=build_front_ai_suggestions_with_model(precheck,result,experimental=True)
        from taoran_agent.api import _apply_knowledge_wording
        from taoran_agent.front_v46.experimental_final_diagnostics import audit
        applied = _apply_knowledge_wording(precheck, result, settings, experimental=True)
        diagnostics = audit(applied.semantic_review)
        assert diagnostics['suggestion_status'] == result.suggestion_status
        assert diagnostics['suggestion_count'] == sum(bool(i.suggestion.strip()) for i in result.items)
        assert result.status=='completed'
        assert len(calls)==(2 if mode in {'repair','persistent','contradiction'} else 1)
        if mode in {'advice','repair'}:
            assert result.items[0].specific is None
            assert good['items'][0]['suggestion'] in text
        elif mode=='empty':
            assert '本次无需额外补充填写' in text and good['suggestion_reason'] in text
        elif mode=='confirmation':
            assert text.count('实际收集到了什么信息？')==1
        else:
            assert result.suggestion_status=='incomplete'
            assert '不能据此认定无需补充' in text
    finally:
        reviewer.close()


def test_all_advice_sections_survive_without_inventing_specificity():
    from taoran_agent.models import KnowledgeWordingItem, KnowledgeWordingResult
    request = PrecheckRequest(context=RequestContext(tenant_id='test',request_id='front',user_id='test'),visit=visit())
    items = [KnowledgeWordingItem(code=code, suggestion='请按原文核对'+code, specific=None)
             for code in ['C','T','A1','O_KR','R','A2','N']]
    wording = KnowledgeWordingResult(status='completed', items=items, visit_analysis='已记录事实。',
                                    suggestion_status='has_suggestions',suggestion_reason='有核对事项')
    text=build_front_ai_suggestions_with_model(TaoranAgent().precheck(request),wording,experimental=True)
    for item in items:
        assert text.count(item.suggestion)==1
        assert item.specific is None
    assert '不具体：' not in text
    assert not any(label in text for label in (
        '客户类型：', '预约与拜访方式：', '拜访目的与关键结果：',
        '过程事实与结果：', '达成评价：', '下一步客户行动：',
        'T｜', 'A｜', 'O/KR｜', 'R｜', 'N｜',
    ))


def test_multiple_fields_in_one_dimension_render_as_one_advice_point():
    from taoran_agent.models import KnowledgeWordingItem, KnowledgeWordingResult

    request = PrecheckRequest(
        context=RequestContext(tenant_id='test', request_id='front', user_id='test'),
        visit=visit(),
    )
    wording = KnowledgeWordingResult(
        status='completed',
        items=[
            KnowledgeWordingItem(
                code='N', suggestion='请把“跟进客户”写成客户需确认的具体事项。'
            ),
            KnowledgeWordingItem(
                code='N', suggestion='请补充下一次联系客户时间安排。'
            ),
            KnowledgeWordingItem(
                code='R', suggestion='请补充客户的实际表达或动作。'
            ),
        ],
        visit_analysis='已记录本次拜访概况。',
        suggestion_status='has_suggestions',
        suggestion_reason='下一步内容和联系时间需完善。',
    )

    text = build_front_ai_suggestions_with_model(
        TaoranAgent().precheck(request), wording, experimental=True,
    )

    assert '请把“跟进客户”写成客户需确认的具体事项。请补充下一次联系客户时间安排。' in text
    assert text.count('\n1、') == 1 and text.count('\n2、') == 1
    assert '下一步客户行动：' not in text and '过程事实与结果：' not in text


def test_model_dimension_titles_are_removed_from_visible_advice():
    from taoran_agent.models import KnowledgeWordingItem, KnowledgeWordingResult

    request = PrecheckRequest(
        context=RequestContext(tenant_id='test', request_id='front', user_id='test'),
        visit=visit(),
    )
    wording = KnowledgeWordingResult(
        status='completed',
        items=[KnowledgeWordingItem(
            code='N',
            suggestion='TAORAN N｜下一步客户行动：请补充下一次联系客户时间安排。',
        )],
        visit_analysis='客户提出需先核对预算。',
        suggestion_status='has_suggestions',
        suggestion_reason='缺少下一次联系时间。',
    )

    text = build_front_ai_suggestions_with_model(
        TaoranAgent().precheck(request), wording, experimental=True,
    )

    assert '请补充下一次联系客户时间安排。' in text
    assert 'TAORAN N' not in text and '下一步客户行动：' not in text


def test_obviously_vague_goal_and_next_result_become_required_advice():
    from taoran_agent import api
    from taoran_agent.models import PrecheckResponse

    response = PrecheckResponse.model_construct(
        request_id='r', tenant_id='t', status='needs_revision', quality_score=0,
        issues=[], suggestions=[], field_completion={
            'expected_key_result': True, 'process_description': True,
            'next_action_expected_result': True,
        }, taoran_sections=[], knowledge_references=[], knowledge_snapshot_hash='k',
        standard_audit=None, semantic_review=None, feedback_text='', rule_feedback_text='',
        phase_latency_ms={}, engine_version='test',
    )
    checks = api._front_specificity_items(response, {
        'expected_key_result': '了解客户情况',
        'process_description': '客户表示设备经常卡纸，并要求下周提供维修和更换方案。',
        'next_action_expected_result': '跟进客户',
        'next_action_purpose': '提供方案',
    })
    states = {item['code']: item['local_specificity'] for item in checks}
    assert states['O_KR'] == 'not_specific'
    assert states['N'] == 'not_specific'
