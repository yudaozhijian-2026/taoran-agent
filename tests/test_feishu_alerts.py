import json

import httpx
import pytest

from taoran_agent.config import Settings
from taoran_agent.feishu_alerts import AlertMonitor, failures, start_monitor
from taoran_agent.storage import AgentStore


def settings(**kwargs):
    return Settings(_env_file=None, feishu_alerts_enabled=True,
                    feishu_alert_routes={'t':{'webhook_url':'https://open.feishu.cn/open-apis/bot/v2/hook/test',
                                             'signing_secret':'secret'}}, **kwargs)


def task(store, identity='q1', status='failed', attempt=1, tenant='t', preview='processing'):
    payload = {'tenant_id':tenant, 'check_id':identity, 'record_code':'BF001',
               'status':status, 'attempt':attempt, 'outcome':{
                   'final':{'status':status,'feedback_text':'正文' if status=='completed' else ''},
                   'preview':{'status':preview}}}
    store.save_quick_check(identity,tenant,9999999999,payload)


def outbox(store):
    return [dict(r) for r in store._connection.execute('SELECT * FROM feishu_alert_outbox')]


class Client:
    def __init__(self, code=0, fail=False):
        self.code, self.fail, self.calls = code, fail, []

    def post(self, url, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise httpx.ReadTimeout('secret url must not reach logs')
        return httpx.Response(200, json={'code':self.code}, request=httpx.Request('POST',url))


def test_historical_baseline_pending_jobs_and_dedup_after_preview_finishes(tmp_path):
    store=AgentStore(tmp_path/'test.db')
    task(store,'old')
    task(store,'pending',status='processing')
    monitor=AlertMonitor(settings(),store)
    monitor.scan()
    assert not outbox(store)
    task(store,'pending')
    monitor.scan()
    task(store,'pending',preview='failed')
    monitor.scan()
    assert len(outbox(store))==1
    monitor=AlertMonitor(settings(),store)
    monitor.scan()
    assert len(outbox(store))==1
    task(store,'pending',attempt=2)
    monitor.scan()
    assert len(outbox(store))==2


def test_no_alert_for_success_or_closed_modal_and_new_preview_failure():
    store=AgentStore(':memory:')
    monitor=AlertMonitor(settings(),store)
    monitor.scan()
    task(store,status='completed')
    monitor.scan()
    assert not outbox(store)
    task(store,status='completed',preview='failed')
    monitor.scan()
    assert len(outbox(store))==1


def test_signed_delivery_business_error_retries_and_no_feedback_secrets():
    store=AgentStore(':memory:')
    client=Client(code=19021)
    monitor=AlertMonitor(settings(),store,client)
    monitor.scan()
    task(store)
    monitor.scan()
    monitor.deliver()
    assert outbox(store)[0]['status']=='pending'
    assert client.calls[0]['json']['sign']
    assert client.calls[0]['follow_redirects'] is False
    client.code=0
    store._connection.execute('UPDATE feishu_alert_outbox SET next_at=0')
    monitor.deliver()
    monitor.deliver()
    assert len(client.calls)==2
    assert outbox(store)[0]['status']=='sent'
    assert 'secret' not in outbox(store)[0]['payload_json']
    assert 'feedback_text' not in outbox(store)[0]['payload_json']


def test_persistent_notification_failure_is_bounded_without_modifying_task():
    store=AgentStore(':memory:')
    client=Client(fail=True)
    monitor=AlertMonitor(settings(),store,client)
    monitor.scan()
    task(store)
    original=store.get_quick_check('q1')
    monitor.scan()
    for _ in range(6):
        store._connection.execute('UPDATE feishu_alert_outbox SET next_at=0')
        monitor.deliver()
    assert len(client.calls)==3
    assert outbox(store)[0]['status']=='failed'
    assert outbox(store)[0]['last_error']=='ReadTimeout'
    assert store.get_quick_check('q1')==original


def test_tenant_route_isolation():
    store=AgentStore(':memory:')
    monitor=AlertMonitor(settings(),store,Client())
    monitor.scan()
    task(store,tenant='other')
    monitor.scan()
    monitor.deliver()
    assert not outbox(store)


@pytest.mark.parametrize('response,expected', [
    ({'semantic_facts':{'status':'completed'},'ai_opinion':'意见','total_score':0,
      'writeback':{'status':'succeeded'}}, []),
    ({'semantic_facts':{'status':'completed'},'ai_opinion':'意见','total_score':25,
      'writeback':{'status':'failed'}}, ['反馈意见或评分未成功回写并核验']),
    ({'semantic_facts':{'status':'failed'},'ai_opinion':'','total_score':None,
      'writeback':{'status':'failed'}}, ['后台AI分析未完成','后台AI反馈意见未生成',
                                       '正式评分未生成','反馈意见或评分未成功回写并核验']),
])
def test_backend_failure_classification(response,expected):
    assert failures('backend',{'status':'completed','request_json':json.dumps({'writeback_target':{'data_id':'1'}}),
                               'response_json':json.dumps(response)})==expected


def test_disabled_monitor_does_not_start():
    assert start_monitor(Settings(_env_file=None),AgentStore(':memory:')) is None


def test_enabled_monitor_starts_and_stops_without_sending_old_failures():
    store=AgentStore(':memory:')
    task(store)
    monitor=start_monitor(settings(),store)
    assert monitor is not None and monitor.thread.is_alive()
    monitor.close()
    assert not monitor.thread.is_alive()
    assert not outbox(store)


def test_backend_terminal_failure_is_detected_after_restart(tmp_path):
    store=AgentStore(tmp_path/'jobs.db')
    monitor=AlertMonitor(settings(),store)
    monitor.scan()
    with store._connection:
        store._connection.execute('''INSERT INTO evaluation_jobs
            (job_id,tenant_id,request_id,input_snapshot_hash,status,request_json,created_at,updated_at)
            VALUES ('j','t','r','h','failed','{}','2026-09-08','2026-09-08')''')
    monitor=AlertMonitor(settings(),store)
    monitor.scan()
    assert len(outbox(store))==1
    assert '后台分析或评分任务失败' in outbox(store)[0]['payload_json']


def test_frontend_return_failure_has_its_own_alert():
    store=AgentStore(':memory:')
    monitor=AlertMonitor(settings(),store)
    monitor.scan()
    task(store,status='completed')
    monitor.scan()
    payload=store.get_quick_check('q1')
    payload['return_failure']=True
    store.save_quick_check('q1','t',9999999999,payload)
    monitor.scan()
    assert len(outbox(store))==1
    assert '交付失败' in outbox(store)[0]['payload_json']


@pytest.mark.parametrize('url',['http://open.feishu.cn/open-apis/bot/v2/hook/a',
                              'https://example.org/open-apis/bot/v2/hook/a'])
def test_reject_non_feishu_destination(url):
    with pytest.raises(ValueError):
        Settings(_env_file=None,feishu_alert_routes={'t':{'webhook_url':url}})
