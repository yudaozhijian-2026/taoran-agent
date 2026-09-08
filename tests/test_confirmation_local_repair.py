from copy import deepcopy

import pytest
from test_suggestion_contract import candidate, execute


def test_empty_field_confirmation_needs_no_model_retry(tmp_path):
    raw = candidate(['R'])
    raw['confirmations'] = [{'field': 'customer_feedback', 'quote': '',
                            'question': '请补充客户反馈。', 'impact': '客户意向判断'}]
    original = deepcopy(raw)
    result, text, calls = execute(tmp_path, raw)
    assert len(calls) == 1 and raw == original
    assert result.suggestion_status == 'needs_confirmation'
    assert '客户反馈未填写' in text and '原文「」' not in text


def test_nonempty_field_cannot_be_claimed_missing(tmp_path):
    raw = candidate(['R'])
    raw['confirmations'][0].update(kind='missing_field', quote='')
    replacement = dict(raw['confirmations'][0], kind='source_ambiguity', quote='沟通订单和调价')
    result, text, calls = execute(tmp_path, raw, {'patches': [
        {'path': 'confirmations.0', 'value': replacement}],
        'analysis_points': [{'text': '不可替换'}]})
    assert len(calls) == 2 and result.recovered_after_retry
    assert result.visit_analysis == raw['analysis_points'][0]['text'].rstrip('。')
    assert '不可替换' not in text and '未填写' not in text
    assert result.items[0].suggestion == raw['items'][0]['suggestion']


@pytest.mark.parametrize('response', [None, {'patches': []}, {'patches': [
    {'path': 'analysis_points', 'value': []}]}])
def test_local_repair_failure_keeps_valid_content_and_is_incomplete(tmp_path, response):
    raw = candidate(['R'])
    raw['confirmations'][0]['quote'] = '不在原文中'
    result, text, calls = execute(tmp_path, raw, response)
    assert len(calls) == 2
    assert result.status == 'completed' and result.suggestion_status == 'incomplete'
    assert not result.recovered_after_retry
    assert raw['items'][0]['suggestion'] in text
    assert result.visit_analysis == raw['analysis_points'][0]['text'].rstrip('。')
    assert '完整性核对未完成' in text and '不在原文中' not in text


def test_invalid_suggestion_repairs_only_bad_item(tmp_path):
    raw = candidate(['R', 'R'])
    raw['items'][1]['suggestion'] = None
    fixed = dict(raw['items'][1], suggestion='请补充调价结果。')
    result, text, calls = execute(tmp_path, raw, {'patches': [
        {'path': 'items.1', 'value': fixed}]})
    assert len(calls) == 2 and result.recovered_after_retry
    assert result.items[0].suggestion == raw['items'][0]['suggestion']
    assert '请补充调价结果。' in text


def test_unknown_field_is_not_rendered_as_missing(tmp_path):
    raw = candidate(['R'])
    raw['confirmations'][0].update(field='nonexistent', quote='')
    result, text, calls = execute(tmp_path, raw, {'patches': []})
    assert len(calls) == 2 and result.suggestion_status == 'incomplete'
    assert 'nonexistent' not in text and '未填写' not in text
