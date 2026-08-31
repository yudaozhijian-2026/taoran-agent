import json
import shutil
import subprocess
from pathlib import Path

import pytest


def run_button_plugin(draft: dict, **overrides) -> dict:
    node = shutil.which('node')
    if not node:
        pytest.skip('Jiandaoyun plugin tests require Node.js')
    payload = {
        'config': {
            'endpoint_url': 'https://taoran.example.test/api/v1/connectors/jiandaoyun/visit/button-check',
            'tenant_id': 'tenant_demo',
            'api_key': 'synthetic-test-key',
        },
        'draft': draft,
        **overrides,
    }
    result = subprocess.run(
        [node, str(Path(__file__).with_name('run_button_plugin.cjs'))],
        input=json.dumps(payload), text=True, capture_output=True, check=True, timeout=10,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize('text', [
    '客户确认8月28日安排验证。\n判断：需要进一步核实预算。',
    '引号"、反斜杠\\、制表符\t、换行\r\n、中文与emoji🙂',
    '客户确认下一步安排。\n' * 3000,
    '',
])
def test_plugin_preserves_current_multiline_text(text):
    result = run_button_plugin({'process_description': text, 'AI评分': '200'})
    assert len(result['calls']) == 1
    call = result['calls'][0]
    assert call['data'] == {'tenant_id': 'tenant_demo', 'process_description': text}
    assert call['headers']['X-API-Key'] == 'synthetic-test-key'
    assert call['timeout'] == 30000
    assert call['maxRedirects'] == 0
    assert 'synthetic-test-key' not in call['url']
    assert result['result'] == {
        'feedback_text': '合成规则意见',
        'rule_feedback_text': '合成规则意见',
        'knowledge_feedback_text': '合成知识库意见',
        'model_feedback_text': '合成大模型意见',
        'check_status': 'passed',
        'knowledge_check_status': 'passed',
        'model_check_status': 'passed',
    }


def test_plugin_one_click_returns_three_feedback_fields():
    result = run_button_plugin(
        {'process_description': '客户确认测试安排。'},
    )
    assert 'feedback_mode' not in result['calls'][0]['data']
    assert result['result']['rule_feedback_text'] == '合成规则意见'
    assert result['result']['knowledge_feedback_text'] == '合成知识库意见'
    assert result['result']['model_feedback_text'] == '合成大模型意见'


def test_plugin_distinguishes_missing_and_cleared_field():
    missing = run_button_plugin({})
    assert missing['calls'] == []
    assert '未获取当前表单的“过程详细描述”' in missing['result']['feedback_text']
    cleared = run_button_plugin({'process_description': None})
    assert cleared['calls'][0]['data']['process_description'] == ''


def test_plugin_preserves_other_inputs_and_decodes_attachment_list():
    draft = {
        'process_description': '客户确认合成测试安排。',
        'visit_date': '2026-08-25T16:00:00.000Z',
        'employee_id': 'TEST-EMP',
        'is_appointment': '否',
        'duration_minutes': 0,
        'next_contact_at': None,
        'evidence_ids': '[{"name":"synthetic.pdf","url":"https://example.test/file"}]',
        'tenant_id': 'untrusted-override',
        'request_id': 'stale-id',
        'source_record_id': 'stale-record',
    }
    forwarded = run_button_plugin(draft)['calls'][0]['data']
    for field in ('visit_date', 'employee_id', 'is_appointment', 'duration_minutes', 'next_contact_at'):
        assert forwarded[field] == draft[field]
    assert forwarded['evidence_ids'] == json.loads(draft['evidence_ids'])
    assert forwarded['tenant_id'] == 'tenant_demo'
    assert 'request_id' not in forwarded
    assert 'source_record_id' not in forwarded


def test_plugin_invalid_attachment_json_does_not_send_request():
    result = run_button_plugin({'process_description': '', 'evidence_ids': '[broken'})
    assert result['calls'] == []
    assert '上传相关文件' in result['result']['feedback_text']


@pytest.mark.parametrize('member', [
    '[{"username":"PLUGIN-EMP","name":"not-forwarded"}]',
    {'username': 'PLUGIN-EMP', 'name': 'not-forwarded'},
])
def test_plugin_member_text_uses_stable_id(member):
    data = run_button_plugin({'process_description': '', 'employee_id': member})['calls'][0]['data']
    assert data['employee_id'] == 'PLUGIN-EMP'
    assert 'not-forwarded' not in json.dumps(data)


def test_plugin_empty_member_is_not_json_array_identifier():
    data = run_button_plugin({'process_description': '', 'employee_id': '[]'})['calls'][0]['data']
    assert data['employee_id'] is None


@pytest.mark.parametrize('member', ['[bad', '[{"name":"not-an-id"}]', '[{"id":"a"},{"id":"b"}]'])
def test_plugin_invalid_member_returns_configuration_feedback(member):
    result = run_button_plugin({'process_description': '', 'employee_id': member})
    assert result['calls'] == []
    assert '成员编号传递异常' in result['result']['feedback_text']


@pytest.mark.parametrize('value', [42, ['客户确认'], {'value': '客户确认'}])
def test_plugin_rejects_invalid_process_value(value):
    result = run_button_plugin({'process_description': value})
    assert result['calls'] == []
    assert '传递格式异常' in result['result']['feedback_text']


@pytest.mark.parametrize('url', [
    'http://taoran.example.test/api/v1/connectors/jiandaoyun/visit/button-check',
    'https://taoran.example.test/api/v1/visit/evaluations',
    'https://taoran.example.test/api/v1/connectors/jiandaoyun/visit/button-check?key=bad',
    'https://user:pass@taoran.example.test/api/v1/connectors/jiandaoyun/visit/button-check',
    'not-a-url',
])
def test_plugin_rejects_unsafe_configuration(url):
    result = run_button_plugin(
        {'process_description': '合成测试'},
        config={'endpoint_url': url, 'tenant_id': 'tenant_demo', 'api_key': 'synthetic-test-key'},
    )
    assert result['calls'] == []
    assert result['result']['check_status'] == 'unavailable'


def test_plugin_network_failure_returns_new_chinese_feedback_without_secrets():
    result = run_button_plugin({'process_description': '合成测试'}, fail=True)
    assert len(result['calls']) == 1
    assert result['result']['check_status'] == 'unavailable'
    assert 'AI调用异常' in result['result']['feedback_text']
    assert 'simulated-secret' not in result['result']['feedback_text']
    assert 'customer content' not in result['result']['feedback_text']


@pytest.mark.parametrize('response', [
    {'status': 200, 'data': '<html>tunnel unavailable</html>'},
    {'status': 200, 'data': {'feedback_text': '不完整响应'}},
    {'status': 401, 'data': {'error': 'synthetic-test-key'}},
    {'status': 200, 'data': {'stage': 'post_submit', 'total_score': 200}},
])
def test_plugin_rejects_non_precheck_response(response):
    result = run_button_plugin({'process_description': '合成测试'}, response=response)
    assert result['result']['check_status'] == 'unavailable'
    assert set(result['result']) == {
        'feedback_text', 'rule_feedback_text', 'knowledge_feedback_text',
        'model_feedback_text', 'check_status', 'knowledge_check_status',
        'model_check_status',
    }


@pytest.mark.parametrize('as_json', [False, True])
def test_plugin_transmits_subform_ids_and_stages_without_private_contact_details(as_json):
    participants = [{'_widget_1416718540131': {'value': 'SYNTHETIC-C1'},
                     '手机': 'not-forwarded', '邮箱': 'not-forwarded'}]
    opportunities = [{'商机编号': 'SYNTHETIC-O1', '历史商机阶段': 'P2', '最新商机阶段': 'P3'},
                     {'opportunity_id': 'SYNTHETIC-O2', 'current_stage': 'P4'}]
    result = run_button_plugin({
        'process_description': '经理确认合成安排。\n客户提出验证要求。',
        'participants': json.dumps(participants) if as_json else participants,
        'opportunities': json.dumps(opportunities) if as_json else opportunities,
    })
    data = result['calls'][0]['data']
    assert data['participants'] == [{'contact_id': {'value': 'SYNTHETIC-C1'}}]
    assert data['opportunities'][0] == {'opportunity_id': 'SYNTHETIC-O1',
                                       'historical_stage': 'P2', 'current_stage': 'P3'}
    assert len(data['opportunities']) == 2
    assert 'not-forwarded' not in json.dumps(data)


@pytest.mark.parametrize('field', ['participants', 'opportunities'])
@pytest.mark.parametrize('value', ['not json', '{}', {'rows': []}, [12], [{'unknown': 'value'}]])
def test_plugin_rejects_invalid_subform_without_sending(field, value):
    result = run_button_plugin({'process_description': '', field: value})
    assert not result['calls']
    assert result['result']['check_status'] == 'unavailable'
    assert '不能将其当成未填写' in result['result']['feedback_text']


@pytest.mark.parametrize('value', [None, '', [], '[]'])
def test_plugin_subform_clearing_and_omission_are_distinct(value):
    base = {'process_description': '合成记录'}
    assert 'participants' not in run_button_plugin(base)['calls'][0]['data']
    cleared = run_button_plugin({**base, 'participants': value})
    assert cleared['calls'][0]['data']['participants'] == []
