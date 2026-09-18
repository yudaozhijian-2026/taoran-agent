from copy import deepcopy

import pytest

from taoran_agent.front_v46.confirmation_shape import (
    ConfirmationShapeError,
    apply_patches,
    normalize,
    repair_paths,
    unsupported_role_requirement,
    valid_remainder,
)


def case():
    context = {'expected_key_result': '项目顺利实施', 'other_purpose': '协助项目实施',
               'process_description': '客户表示六台设备清单尚未整理，确认下周一发给销售。'}
    payload = {'analysis_points': [
        {'kind': 'customer_fact', 'text': '客户承诺下周一发清单。', 'proofs': []},
        {'kind': 'judgment_gap', 'text': '原文未记录负责人信息，不足以判断负责人是否明确。',
         'requires_followup': True, 'proofs': [{'field': 'process_description', 'quote': context['process_description']}]},
    ], 'items': [{'code': 'N', 'suggestion': '请具体说明下次希望取得的结果。',
                  'proofs': [{'field': 'expected_key_result', 'quote': '项目顺利实施'}]}],
        'confirmations': [{'kind': 'missing_field', 'field': 'process_description',
                           'question': '是否已确认负责人信息？', 'impact': '影响判断'}],
        'suggestion_status': 'has_suggestions', 'suggestion_reason': '需补充'}
    return context, payload


def test_original_failure_repairs_related_analysis_not_only_question():
    context, raw = case()
    original = deepcopy(raw)
    with pytest.raises(ConfirmationShapeError) as error:
        normalize(raw, context)
    paths = repair_paths(raw, error.value.validation_errors)
    assert set(paths) == {'analysis_points.1', 'confirmations.0'}
    patched = apply_patches(raw, {'patches': [{'path': p, 'value': None} for p in paths]}, paths)
    assert normalize(patched, context)['analysis_points'] == raw['analysis_points'][:1]
    assert patched['items'] == raw['items']
    assert raw == original
    partial = valid_remainder(raw, paths)
    assert partial['suggestion_status'] is None
    assert '负责人' not in str(partial['analysis_points'])
    assert not partial['confirmations']


@pytest.mark.parametrize('field', ['expected_key_result', 'other_purpose'])
def test_explicit_owner_objective_not_suppressed(field):
    context, _ = case()
    context[field] = '确认采购负责人及其决策权限'
    assert not unsupported_role_requirement('未记录负责人信息，不足以判断是否达成。', context)


def test_clear_customer_expression_does_not_need_name():
    context, _ = case()
    assert unsupported_role_requirement('请补充联系人姓名。', context)
    assert not unsupported_role_requirement('无需补充联系人姓名。', {'process_description': '联系人已确认订单。'})


def test_missing_field_and_actual_ambiguity():
    raw = {'analysis_points': [], 'confirmations': [{'field': 'process_description',
            'quote': '', 'question': '请补充过程。', 'impact': '无法判断结果'}]}
    assert normalize(raw, {'process_description': ''})['confirmations'][0]['kind'] == 'missing_field'
    with pytest.raises(ConfirmationShapeError):
        normalize(raw, {'process_description': '客户表示下周确认。'})
    raw['confirmations'][0].update(quote='他表示同意', question='他指客户还是销售？')
    fixed = normalize(raw, {'process_description': '沟通后他表示同意。'})
    assert fixed['confirmations'][0]['kind'] == 'source_ambiguity'


def test_normal_role_fact_not_blocked():
    assert not unsupported_role_requirement('采购负责人确认周五提供清单。', {})
    assert not unsupported_role_requirement('无需补充联系人姓名。', {})


def test_failed_repair_without_remaining_analysis_is_explicitly_partial():
    _, raw = case()
    partial = valid_remainder(raw, ['analysis_points.0', 'analysis_points.1', 'confirmations.0'])
    assert '尚未完成' in partial['analysis_points'][0]['text']
    assert partial['suggestion_status'] is None


@pytest.mark.parametrize(("root", "key", "text"), [
    ("analysis_points", "text", "N整体不达标。"),
    ("items", "suggestion", "请说明跨季度要求不适用的依据。"),
    ("confirmations", "question", "程序判定period_met为否，是否确认？"),
    ("confirmations", "impact", "潜力客户不要求客户共识，共识视为满足。"),
])
def test_internal_rule_language_requires_frontend_repair(root, key, text):
    context, raw = case()
    target = raw[root][0]
    target[key] = text
    with pytest.raises(ConfirmationShapeError) as error:
        normalize(raw, context)
    assert any(
        item["code"] == "salesperson_internal_rule_leak"
        for item in error.value.validation_errors
    )


@pytest.mark.parametrize('repair_ok', [True, False])
def test_provider_local_repair_and_safe_fallback(tmp_path, repair_ok):
    from test_suggestion_contract import execute

    _, raw = case()
    raw['items'][0]['code'] = 'R'
    raw['items'][0]['proofs'] = [
        {'field': 'expected_key_result', 'quote': '沟通订单和调价'}
    ]
    patch = {'patches': [{'path': 'analysis_points.1', 'value': None},
                         {'path': 'confirmations.0', 'value': None}]}
    result, text, calls = execute(tmp_path, raw, patch if repair_ok else {'patches': []})
    assert len(calls) == 2
    assert '负责人' not in text
    assert '客户承诺下周一发清单' in text
    assert '请具体说明下次希望取得的结果' in text
    assert result.recovered_after_retry == repair_ok
    assert (result.suggestion_status == 'incomplete') != repair_ok
