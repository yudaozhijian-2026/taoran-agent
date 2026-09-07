const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {test} = require('node:test');
const frontend = readFileSync('integrations/jiandaoyun/quick_check_interactive_frontend.js','utf8');
const launcher = readFileSync('integrations/jiandaoyun/quick_check_interactive_launcher.js','utf8');
async function front(draft) {
  let sent;
  const api = {ui:{},utils:{
    callFunction: async ({data}) => {sent=data; return {quick_check_id:'qc_page',quick_check_launch_url:'https://candidate.example/page'};},
    openModal: async () => api.ui.onmessage({type:'taoran_quick_check_acknowledged',check_id:'qc_page',feedback_text:'Final原文'}),
    closeModal: () => {},
  }};
  const result = await vm.runInNewContext('(async()=>{'+frontend+'})()', {triggerConf:draft,$g:api});
  return {sent,result};
}
test('verified page contract sends all 21 fields including explicit clears', async () => {
  const {sent,result} = await front({visit_record_code:'BFJL1',snapshot_contract:'experimental_current_page_v1',
    process_description:'',participants:[{contact_id:'c1'}]});
  const page = JSON.parse(sent.page_snapshot_json);
  assert.equal(Object.keys(page).length,21);
  assert.equal(page.process_description,'');
  assert.equal(page.next_contact_at,null);
  assert.deepEqual(page.participants,[{contact_id:'c1'}]);
  assert.equal(result.resText,'Final原文');
});
test('old button without page contract retains saved-record transport', async () => {
  const {sent} = await front({visit_record_code:'BFJL1'});
  assert.equal(sent.snapshot_mode,undefined);
  assert.equal(sent.page_snapshot_json,undefined);
});
test('unknown page contract fails instead of silently using saved records', async () => {
  await assert.rejects(front({visit_record_code:'BFJL1',snapshot_contract:'typo'}), /未改用已保存记录/);
});
test('launcher forwards page snapshot unchanged and keeps key server-side', async () => {
  let sent;
  const result = await vm.runInNewContext('(async()=>{'+launcher+'})()', {
    URL, agentConf:{tenant_id:'t',api_key:'server-only-test-key',
      endpoint_url:'https://candidate.example/api/v1/experimental/quick-check-interactive/tasks/current-record',
      public_base_url:'https://candidate.example'},
    triggerConf:{visit_record_code:'BFJL1',snapshot_mode:'experimental_current_page_v1',page_snapshot_json:'{"process_description":""}'},
    require: name => {assert.equal(name,'axios');return async req=>{sent=req;return {data:{check_id:'qc_page',stream_token:'short-token',status:'processing'}};};},
  });
  assert.equal(sent.data.page_snapshot.process_description,'');
  assert.equal(sent.data.snapshot_mode,'experimental_current_page_v1');
  assert.equal(JSON.stringify(result).includes('server-only-test-key'),false);
});
async function modalRun(messages, cancel = false) {
  const order = [];
  let resolveModal, opened;
  const ready = new Promise(resolve => { opened = resolve; });
  const api = {ui:{},utils:{
    callFunction: async () => ({quick_check_id:'qc_page',quick_check_launch_url:'https://candidate.example/page'}),
    openModal: () => { order.push('opened'); opened(); return new Promise(resolve => { resolveModal=resolve; }); },
    closeModal: () => {order.push('closed'); resolveModal();},
  }};
  const execution = vm.runInNewContext('(async()=>{'+frontend+'})()', {triggerConf:{visit_record_code:'BFJL1'},$g:api});
  await ready;
  for (const message of messages) api.ui.onmessage(message);
  if (cancel) resolveModal();
  try {
    const result = await execution;
    order.push('returned');
    return {result,order,api};
  } catch (error) { return {error,order,api}; }
}
const feedback = text => ({type:'taoran_quick_check_acknowledged',check_id:'qc_page',feedback_text:text});
test('modal return order is close then return, not a field-assignment receipt', async () => {
  const h = await modalRun([feedback('Final')]);
  assert.deepEqual(h.order,['opened','closed','returned']);
  assert.equal(h.result.resText,'Final');
});
test('first accepted feedback is immutable and preserves exact text', async () => {
  const text = '  Final原文\n';
  const h = await modalRun([feedback(text),feedback('重复消息替换')]);
  assert.equal(h.result.resText,text);
  assert.equal(h.order.filter(x=>x==='closed').length,1);
});
test('cancelled or wrong-task messages cannot close or fill the form', async () => {
  const h = await modalRun([{...feedback('其他任务'),check_id:'qc_other'},feedback('   ')],true);
  assert.match(h.error.message,/未返回反馈/);
  h.api.ui.onmessage(feedback('关闭后迟到反馈'));
  assert.deepEqual(h.order,['opened']);
});
