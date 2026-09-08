// Runs real candidate plugin scripts with a deterministic platform adapter.
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {test} = require('node:test');
const assert = require('node:assert/strict');
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
const front = new AsyncFunction('$g','triggerConf','URL',readFileSync(join(__dirname,'../integrations/jiandaoyun/quick_check_interactive_frontend.js'),'utf8'));
const back = new AsyncFunction('require','agentConf','triggerConf','URL',readFileSync(join(__dirname,'../integrations/jiandaoyun/quick_check_interactive_launcher.js'),'utf8'));
const hash = 'a'.repeat(64);
const tick = () => new Promise(resolve=>setImmediate(resolve));
function launch(id='qc_same', opening='x'.repeat(32), fingerprint=hash) {
  return {quick_check_id:id,quick_check_launch_url:`https://taoran.yudaozhijian.top/quick-check/interactive?check_id=${id}&opening_id=${opening}&input_hash=${fingerprint}`};
}
function start(result=launch(), fields={}) {
  let close, input, modal;
  const g = {ui:{},utils:{
    callFunction:async args=>{input=args.data;return {result};},
    openModal:args=>{modal=args;return new Promise(resolve=>{close=resolve;});},
    closeModal:()=>close(),
  }};
  const promise = front(g,{snapshot_contract:'experimental_current_page_v1',...fields},URL);
  promise.catch(()=>{});
  return {g,promise,get input(){return input;},get modal(){return modal;},close:()=>close()};
}
function ack(h, id='qc_same', opening='x'.repeat(32), fingerprint=hash) {
  h.g.ui.onmessage({pluginMessage:{type:'taoran_quick_check_acknowledged',
    check_id:id,opening_id:opening,input_hash:fingerprint,feedback_text:'客户确认了试用安排'}});
}
test('new unsaved record launches; rich text and subtables are passed',async()=>{
  const fields={process_description:'<p>客户确认</p>',participants:[{contact_id:'c'}],opportunities:[{opportunity_id:'o',current_stage:'P3'}]};
  const h=start(launch(),fields);await tick();
  const snapshot=JSON.parse(h.input.page_snapshot_json);
  for(const key of Object.keys(fields))assert.deepEqual(snapshot[key],fields[key]);
  assert.equal(h.input.visit_record_code,undefined);
  assert.equal(h.input.draft_session_id,undefined);
  ack(h);assert.equal((await h.promise).resText,'客户确认了试用安排');
});
test('close without acknowledgement never writes; late messages ignored',async()=>{
  const h=start();await tick();h.close();
  await assert.rejects(h.promise,/未返回反馈/);
  ack(h); // Must not close another modal or return a result after disposal.
});
test('same cached task in a new opening rejects previous opening message',async()=>{
  const old=start();await tick();old.close();await assert.rejects(old.promise);
  const current=start(launch('qc_same','y'.repeat(32)));await tick();
  let finished=false;current.promise.then(()=>{finished=true;});
  ack(current);await tick();assert.equal(finished,false);
  ack(current,'qc_same','y'.repeat(32));assert.ok((await current.promise).resText);
});
test('changed content rejects old task and incorrect fingerprint',async()=>{
  const h=start(launch('qc_new','z'.repeat(32),'b'.repeat(64)));await tick();
  let finished=false;h.promise.then(()=>{finished=true;});
  ack(h);ack(h,'qc_new','z'.repeat(32));await tick();assert.equal(finished,false);
  ack(h,'qc_new','z'.repeat(32),'b'.repeat(64));assert.ok((await h.promise).resText);
});
test('plugin fails closed against old server without content contract',async()=>{
  const h=start({quick_check_id:'qc_old',quick_check_launch_url:'https://taoran.yudaozhijian.top/quick-check/interactive?check_id=qc_old'});
  await assert.rejects(h.promise,/版本不一致/);
  assert.equal(h.modal,undefined);
});
test('backend accepts empty code; forwards snapshot only without temporary ID',async()=>{
  const h=start();await tick();h.close();await assert.rejects(h.promise);
  let sent;
  const result=await back(name=>{assert.equal(name,'axios');return async request=>{
    sent=request.data;return {data:{check_id:'qc_same',stream_token:'t'.repeat(32),opening_id:'x'.repeat(32),input_hash:hash,status:'processing'}};
  };},{tenant_id:'test',api_key:'test-only',endpoint_url:'https://taoran.yudaozhijian.top/api/v1/quick-check/tasks',public_base_url:'https://taoran.yudaozhijian.top'},
  {...h.input,draft_session_id:'obsolete'},URL);
  assert.equal(sent.record_code,'');assert.equal(sent.draft_session_id,undefined);
  assert.equal(Object.keys(sent.form_snapshot).length,21);
  assert.equal(new URL(result.quick_check_launch_url).searchParams.get('input_hash'),hash);
});
