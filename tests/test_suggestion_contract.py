import json
from copy import deepcopy

import httpx
import pytest
from test_post_policy import visit

from taoran_agent.agent import TaoranAgent
from taoran_agent.config import Settings
from taoran_agent.feedback import build_front_ai_suggestions_with_model
from taoran_agent.front_v46.observed_feedback import generate
from taoran_agent.front_v46.reviewer import FrontReviewer
from taoran_agent.models import PrecheckRequest, RequestContext


def candidate(codes):
    return {'analysis_points': [{'kind': 'objective_result', 'text': '已记录沟通，具体结果尚待确认。'}],
            'items': [{'code': c, 'suggestion': t,
                       'proofs': [{'field': 'process_description', 'quote': '沟通订单和调价'}]}
                      for c, t in zip(codes, ['请补充订单是否完成。', '请补充调价结果。'])],
            'confirmations': [{'field': 'process_description', 'quote': '沟通订单和调价',
                               'question': '下次何时联系？', 'impact': '下一步安排'}],
            'suggestion_status': 'needs_confirmation', 'suggestion_reason': '订单和调价结果待确认。'}


def execute(tmp_path, first, second=None):
    calls = []

    def provider(request):
        calls.append(json.loads(request.content))
        body = first if len(calls) == 1 else second
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(body)},
                                                     'finish_reason': 'stop'}]})

    settings = Settings(_env_file=None, database_path=str(tmp_path/'db'), llm_model='test',
                        llm_api_url='https://example.test/chat', llm_api_key='test')
    reviewer = FrontReviewer(settings, None, transport=httpx.MockTransport(provider))
    try:
        result = generate(reviewer, [{'code': 'R'}],
                          {'visit_analysis_context': {'process_description': '沟通订单和调价',
                                                      'expected_key_result': '沟通订单和调价'}}, 30)
    finally:
        reviewer.close()
    request = PrecheckRequest(context=RequestContext(tenant_id='test', request_id='test', user_id='test'), visit=visit())
    text = build_front_ai_suggestions_with_model(TaoranAgent().precheck(request), result, experimental=True)
    return result, text, calls


def test_unknown_codes_repair_only_classification_and_preserve_every_suggestion(tmp_path):
    first = candidate(['R_ORDER', 'R_PRICE'])
    second = {'item_codes': [{'index': 1, 'code': 'R'}, {'index': 0, 'code': 'R'}],
              'analysis_points': [{'text': '不应采用的新结论'}], 'items': []}
    original = deepcopy(first)
    result, text, calls = execute(tmp_path, first, second)
    assert first == original and len(calls) == 2
    assert result.visit_analysis == first['analysis_points'][0]['text'].rstrip('。')
    assert [i.suggestion for i in result.items] == [i['suggestion'] for i in first['items']]
    assert all(i.suggestion in text for i in result.items)
    assert result.suggestion_status == 'needs_confirmation'
    assert result.recovered_after_retry and all(i.code == 'R' for i in result.items)
    assert '不应采用' not in text and '无需重复补充' not in text
    assert '"enum": ["R"]' in calls[0]['messages'][0]['content']
    repair = json.loads(calls[1]['messages'][1]['content'])
    assert repair['candidate_items'] == first['items']
    assert '仅修复建议所属检查项编号' in calls[1]['messages'][0]['content']
    assert all(a['experimental_semantic_audit']['status'] == 'disabled'
               for a in result.model_attempts)


@pytest.mark.parametrize('mapping', [[], [{'index': 0, 'code': 'R'}],
    [{'index': 0, 'code': 'R'}, {'index': 0, 'code': 'R'}],
    [{'index': 0, 'code': 'OTHER'}, {'index': 1, 'code': 'R'}],
    [{'index': True, 'code': 'R'}, {'index': 0, 'code': 'R'}], ['bad']])
def test_failed_number_repair_keeps_text_visible_and_marks_incomplete(tmp_path, mapping):
    first = candidate(['unknown_order', 'unknown_price'])
    result, text, calls = execute(tmp_path, first, {'item_codes': mapping})
    assert len(calls) == 2 and result.status == 'completed'
    assert result.suggestion_status == 'incomplete' and not result.recovered_after_retry
    assert all(i['suggestion'] in text for i in first['items'])
    assert '完整性核对未完成' in text and '无需重复补充' not in text
    assert 'unknown_' not in text and 'UNMAPPED' not in text


def test_different_suggestions_under_same_field_are_not_discarded(tmp_path):
    result, text, calls = execute(tmp_path, candidate(['R', 'R']))
    assert len(calls) == 1 and len(result.items) == 2
    assert all(i.suggestion in text for i in result.items)


@pytest.mark.parametrize('same_quote', [True, False])
def test_merge_requires_identical_text_and_source_binding(tmp_path, same_quote):
    first = candidate(['R'])
    first['items'][0]['suggestion'] = first['confirmations'][0]['question']
    first['items'][0]['proofs'][0]['quote'] = '沟通订单和调价' if same_quote else '调价'
    result, text, calls = execute(tmp_path, first)
    assert len(calls) == 1
    assert len(result.items) == (0 if same_quote else 1)
    assert text.count('下次何时联系？') == (1 if same_quote else 2)
