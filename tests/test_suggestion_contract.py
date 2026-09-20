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


def test_advice_stage_uses_compact_contract_and_keeps_validated_analysis(tmp_path):
    calls = []
    raw = {
        'items': [{
            'code': 'R',
            'covered_fields': ['process_description'],
            'suggestion': '请补充客户对调价的明确反馈。',
        }],
        'confirmations': [],
        'suggestion_status': 'has_suggestions',
        'suggestion_reason': '调价结果尚不具体。',
    }

    def provider(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{
            'message': {'content': json.dumps(raw, ensure_ascii=False)},
            'finish_reason': 'stop',
        }]})

    settings = Settings(_env_file=None, database_path=str(tmp_path/'db'), llm_model='test',
                        llm_api_url='https://example.test/chat', llm_api_key='test')
    reviewer = FrontReviewer(settings, None, transport=httpx.MockTransport(provider))
    try:
        result = generate(
            reviewer,
            [{'code': 'R'}],
            {
                'visit_analysis_context': {'process_description': '沟通订单和调价'},
                'decision_ledger': {'validated_analysis': '客户已沟通订单，调价结果尚不具体。'},
            },
            30,
        )
    finally:
        reviewer.close()

    assert len(calls) == 1
    assert result.visit_analysis == '客户已沟通订单，调价结果尚不具体。'
    assert result.items[0].suggestion == raw['items'][0]['suggestion']
    prompt = calls[0]['messages'][0]['content']
    assert '本阶段只生成AI改善建议' in prompt
    assert 'analysis_points' not in prompt
    assert '"present"' not in prompt
    assert '"covered_fields"' in prompt


def test_advice_stage_locally_strips_legacy_analysis_shape(tmp_path):
    calls = []
    raw = {
        'analysis_points': [{'kind': 'objective_result', 'text': '不应重复生成的分析。'}],
        'items': [{
            'code': 'R',
            'suggestion': '请补充客户对调价的明确反馈。',
            'present': [],
            'proofs': [{'field': 'process_description', 'quote': '沟通订单和调价', 'features': []}],
        }],
        'confirmations': [],
        'suggestion_status': 'has_suggestions',
        'suggestion_reason': '调价结果尚不具体。',
    }

    def provider(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{
            'message': {'content': json.dumps(raw, ensure_ascii=False)},
            'finish_reason': 'stop',
        }]})

    settings = Settings(_env_file=None, database_path=str(tmp_path/'db'), llm_model='test',
                        llm_api_url='https://example.test/chat', llm_api_key='test')
    reviewer = FrontReviewer(settings, None, transport=httpx.MockTransport(provider))
    try:
        result = generate(
            reviewer,
            [{'code': 'R'}],
            {
                'visit_analysis_context': {'process_description': '沟通订单和调价'},
                'decision_ledger': {'validated_analysis': '客户已沟通订单，调价结果尚不具体。'},
            },
            30,
        )
    finally:
        reviewer.close()

    assert len(calls) == 1
    assert result.status == 'completed'
    assert result.model_attempts[0]['local_format_repair'] is True
    assert result.visit_analysis == '客户已沟通订单，调价结果尚不具体。'


def test_advice_stage_receives_one_slot_with_every_required_field(tmp_path):
    calls = []
    raw = {
        'items': [{
            'code': 'N',
            'covered_fields': ['next_action_expected_result', 'next_contact_at'],
            'suggestion': '“保持联系”没有说清要取得什么结果，也未填写下一次联系时间，请一并补充。',
        }],
        'confirmations': [],
        'suggestion_status': 'has_suggestions',
        'suggestion_reason': '下一步结果和联系时间均需完善。',
    }

    def provider(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{
            'message': {'content': json.dumps(raw, ensure_ascii=False)},
            'finish_reason': 'stop',
        }]})

    settings = Settings(_env_file=None, database_path=str(tmp_path/'db'), llm_model='test',
                        llm_api_url='https://example.test/chat', llm_api_key='test')
    reviewer = FrontReviewer(settings, None, transport=httpx.MockTransport(provider))
    try:
        result = generate(reviewer, [{'code': 'N'}], {
            'visit_analysis_context': {
                'next_action_expected_result': '保持联系',
                'next_contact_at': None,
            },
            'required_advice': [
                {'code': 'N', 'field': 'next_action_expected_result', 'reason': 'obviously_not_specific'},
                {'code': 'N', 'field': 'next_contact_at', 'reason': 'not_filled'},
            ],
            'decision_ledger': {'validated_analysis': '下一步只写了保持联系，且未填写联系时间。'},
        }, 30)
    finally:
        reviewer.close()

    assert len(calls) == 1
    assert result.suggestion_status == 'has_suggestions'
    incoming = json.loads(calls[0]['messages'][1]['content'])
    assert incoming['coverage_plan'] == [{
        'slot_id': 'N',
        'code': 'N',
        'required_fields': ['next_action_expected_result', 'next_contact_at'],
        'fields': [
            {'field': 'next_action_expected_result', 'field_name': '下次拜访期望的关键结果',
             'reason': 'obviously_not_specific'},
            {'field': 'next_contact_at', 'field_name': '下一次联系客户时间安排', 'reason': 'not_filled'},
        ],
        'output': {
            'code': 'N',
            'covered_fields': ['next_action_expected_result', 'next_contact_at'],
            'suggestion': '<结合本次数据说明上述每个字段的具体问题和修改方向>',
        },
    }]


def test_empty_required_field_can_be_safely_bound_from_explicit_wording(tmp_path):
    calls = []
    raw = {
        'items': [{
            'code': 'N',
            'covered_fields': ['next_action_expected_result'],
            'suggestion': '请细化下一步期望结果，并补充下一次联系客户时间安排。',
        }],
        'confirmations': [],
        'suggestion_status': 'has_suggestions',
        'suggestion_reason': '下一步信息需完善。',
    }

    def provider(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{
            'message': {'content': json.dumps(raw, ensure_ascii=False)},
            'finish_reason': 'stop',
        }]})

    settings = Settings(_env_file=None, database_path=str(tmp_path/'db'), llm_model='test',
                        llm_api_url='https://example.test/chat', llm_api_key='test')
    reviewer = FrontReviewer(settings, None, transport=httpx.MockTransport(provider))
    try:
        result = generate(reviewer, [{'code': 'N'}], {
            'visit_analysis_context': {
                'next_action_expected_result': '保持联系',
                'next_contact_at': None,
            },
            'required_advice': [
                {'code': 'N', 'field': 'next_action_expected_result'},
                {'code': 'N', 'field': 'next_contact_at'},
            ],
            'decision_ledger': {'validated_analysis': '下一步信息尚需完善。'},
        }, 30)
    finally:
        reviewer.close()

    assert len(calls) == 1
    assert result.suggestion_status == 'has_suggestions'
    assert any(
        item.get('rule') == 'local_required_field_binding'
        and item.get('field') == 'next_contact_at'
        for item in result.semantic_observations
    )


def test_vague_time_wording_does_not_hide_missing_required_field(tmp_path):
    calls = []
    first = {
        'items': [{
            'code': 'N',
            'covered_fields': ['next_action_expected_result', 'next_contact_at'],
            'suggestion': '下一步还缺少具体时间或客户意向，请完善安排。',
        }],
        'confirmations': [],
        'suggestion_status': 'has_suggestions',
        'suggestion_reason': '下一步安排需完善。',
    }
    fixed = {
        'items': [{
            'code': 'N',
            'covered_fields': ['next_action_expected_result', 'next_contact_at'],
            'suggestion': '“保持联系”缺少具体期望结果，且未填写下一次联系客户时间安排。',
        }],
        'confirmations': [],
        'suggestion_status': 'has_suggestions',
        'suggestion_reason': '下一步结果和联系时间均需完善。',
    }

    def provider(request):
        calls.append(json.loads(request.content))
        value = first if len(calls) == 1 else fixed
        return httpx.Response(200, json={'choices': [{
            'message': {'content': json.dumps(value, ensure_ascii=False)},
            'finish_reason': 'stop',
        }]})

    settings = Settings(_env_file=None, database_path=str(tmp_path/'db'), llm_model='test',
                        llm_api_url='https://example.test/chat', llm_api_key='test')
    reviewer = FrontReviewer(settings, None, transport=httpx.MockTransport(provider))
    try:
        result = generate(reviewer, [{'code': 'N'}], {
            'visit_analysis_context': {
                'next_action_expected_result': '保持联系',
                'next_contact_at': None,
            },
            'required_advice': [
                {'code': 'N', 'field': 'next_action_expected_result'},
                {'code': 'N', 'field': 'next_contact_at'},
            ],
            'decision_ledger': {'validated_analysis': '下一步信息尚需完善。'},
        }, 30)
    finally:
        reviewer.close()

    assert len(calls) == 2
    assert result.recovered_after_retry
    assert result.suggestion_status == 'has_suggestions'
