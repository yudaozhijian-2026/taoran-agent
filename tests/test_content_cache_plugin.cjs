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
  assert.deepEqual(await h.promise, {resText:''});
  ack(h); // Must not close another modal or return a result after disposal.
});
test('same cached task in a new opening rejects previous opening message',async()=>{
  const old=start();await tick();old.close();assert.deepEqual(await old.promise, {resText:''});
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
  const h=start();await tick();h.close();assert.deepEqual(await h.promise, {resText:''});
  let sent;
  const result=await back(name=>{assert.equal(name,'axios');return async request=>{
    sent=request.data;return {data:{check_id:'qc_same',stream_token:'t'.repeat(32),opening_id:'x'.repeat(32),input_hash:hash,status:'processing'}};
  };},{tenant_id:'test',api_key:'test-only',endpoint_url:'https://taoran.yudaozhijian.top/api/v1/quick-check/tasks',public_base_url:'https://taoran.yudaozhijian.top'},
  {...h.input,draft_session_id:'obsolete'},URL);
  assert.equal(sent.record_code,'');assert.equal(sent.draft_session_id,undefined);
  assert.equal(Object.keys(sent.form_snapshot).length,21);
  assert.equal(new URL(result.quick_check_launch_url).searchParams.get('input_hash'),hash);
});

test('editing a saved record sends current unsaved values instead of requesting saved scoring',async()=>{
  const sent=[];
  const conf={tenant_id:'test',api_key:'test-only',endpoint_url:'https://taoran.yudaozhijian.top/api/v1/quick-check/tasks',public_base_url:'https://taoran.yudaozhijian.top'};
  for(const process_description of ['原内容','客户提出试用，尚未确认时间','']) {
    const h=start(launch(),{visit_record_code:'BFJL-existing',data_id:'existing-id',process_description});
    await tick();h.close();assert.deepEqual(await h.promise, {resText:''});
    const response=await back(()=>async request=>{
      sent.push(request.data);
      return {data:{check_id:'qc_current',stream_token:'t'.repeat(32),opening_id:'x'.repeat(32),input_hash:hash,status:'processing'}};
    },conf,h.input,URL);
    assert.equal(response.quick_check_id,'qc_current');
  }
  assert.deepEqual(sent.map(r=>r.form_snapshot.process_description),['原内容','客户提出试用，尚未确认时间','']);
  for(const request of sent){
    assert.equal(request.saved_record_check,undefined);
    assert.equal(request.record_code,'BFJL-existing');
    assert.equal(request.data_id,'existing-id');
    assert.equal(Object.keys(request.form_snapshot).length,21);
  }
});

test('saved record with malformed page snapshot fails closed without falling back to saved data',async()=>{
  let calls=0;
  const conf={tenant_id:'test',api_key:'test-only',endpoint_url:'https://taoran.yudaozhijian.top/api/v1/quick-check/tasks',public_base_url:'https://taoran.yudaozhijian.top'};
  for(const page_snapshot_json of [undefined,'{}','not-json']){
    const result=await back(()=>async()=>{calls++;},conf,{visit_record_code:'BFJL-existing',snapshot_mode:'experimental_current_page_v1',page_snapshot_json},URL);
    assert.equal(result.quick_check_status,'unavailable');
    assert.equal(result.quick_check_launch_url,'');
  }
  assert.equal(calls,0);
});

test('same platform instance: cancel twice then acknowledge; no cancelled output or stale handler',async()=>{
  let close, calls=0, oldHandler;
  const inputs=[];
  const g={ui:{},utils:{
    callFunction:async args=>{
      inputs.push(args.data);
      calls++;
      return {result:launch(calls===3?'qc_changed':'qc_same',String(calls).repeat(32),calls===3?'b'.repeat(64):hash)};
    },
    openModal:()=>new Promise(resolve=>{close=resolve;}),
    closeModal:()=>close(),
  }};
  let existing='原有AI意见';
  for(let i=1;i<=3;i++){
    const p=front(g,{existing_feedback:existing,snapshot_contract:'experimental_current_page_v1',process_description:i===3?'修改后的客户事实':'原客户事实'},URL);
    await tick();
    if(i===1)oldHandler=g.ui.onmessage;
    if(i<3)close();
    else {
      oldHandler({type:'taoran_quick_check_acknowledged',check_id:'qc_same',opening_id:'1'.repeat(32),input_hash:hash,feedback_text:'过期意见'});
      g.ui.onmessage({type:'taoran_quick_check_acknowledged',check_id:'qc_changed',opening_id:'3'.repeat(32),input_hash:'b'.repeat(64),feedback_text:'最新客户事实建议'});
    }
    const output=await p;
    if(Object.hasOwn(output,'resText'))existing=output.resText;
    assert.equal(existing,i===3?'最新客户事实建议':'原有AI意见');
    if(i<3)assert.deepEqual(output,{resText:'原有AI意见'});
  }
  assert.equal(calls,3);
  assert.equal(inputs[0].page_snapshot_json,inputs[1].page_snapshot_json);
  assert.notEqual(inputs[1].page_snapshot_json,inputs[2].page_snapshot_json);
  for(const input of inputs) {
    assert.equal(input.existing_feedback,undefined);
    assert.equal(JSON.parse(input.page_snapshot_json).existing_feedback,undefined);
  }
});

test('modal failures still reject and remove listener; next invocation can succeed',async()=>{
  const g={ui:{},utils:{callFunction:async()=>({result:launch()}),openModal:async()=>{throw new Error('platform failure');}}};
  await assert.rejects(front(g,{},URL),/platform failure/);
  assert.doesNotThrow(()=>g.ui.onmessage({}));
  const h=start();await tick();ack(h);assert.ok((await h.promise).resText);
});
