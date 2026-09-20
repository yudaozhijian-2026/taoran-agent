// Isolated submit-test plugin contract: retry, bypass and return never affect pilot files.
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {test} = require('node:test');
const assert = require('node:assert/strict');
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
const front = new AsyncFunction('$g','triggerConf','URL',readFileSync(
  join(__dirname,'../integrations/jiandaoyun/submit_test/frontend.js'),'utf8'));
const tick = () => new Promise(resolve=>setImmediate(resolve));
const fingerprints = ['a'.repeat(64),'b'.repeat(64),'c'.repeat(64)];
function launch(index) {
  const opening=String(index+1).repeat(32);
  const check=`qc_${index+1}`;
  return {check,opening,fingerprint:fingerprints[index],result:{
    quick_check_id:check,
    quick_check_launch_url:`https://taoran-test.yudaozhijian.top/quick-check/interactive?check_id=${check}&opening_id=${opening}&input_hash=${fingerprints[index]}`,
  }};
}
function start(launches, fields={}) {
  let close;
  const opened=[];
  let callIndex=0;
  const g={ui:{},utils:{
    callFunction:async()=>({result:launches[callIndex++].result}),
    openModal:args=>{opened.push(args);return new Promise(resolve=>{close=resolve;});},
    closeModal:()=>close(),
  }};
  const promise=front(g,{snapshot_contract:'experimental_current_page_v1',...fields},URL);
  promise.catch(()=>{});
  return {g,promise,opened,get calls(){return callIndex;}};
}
function send(h, item, type, extra={}) {
  h.g.ui.onmessage({pluginMessage:{type,check_id:item.check,opening_id:item.opening,
    input_hash:item.fingerprint,...extra}});
}

test('retry closes failed opening and launches a fresh task for the same snapshot',async()=>{
  const first=launch(0), second=launch(1);
  const h=start([first,second],{process_description:'客户已确认试用范围'});
  await tick();
  assert.equal(h.calls,1);
  send(h,first,'taoran_submit_retry');
  await tick();
  assert.equal(h.calls,2);
  assert.equal(h.opened.length,2);
  send(h,second,'taoran_quick_check_acknowledged',{
    submit_confirmed:true,feedback_text:'本次拜访分析：客户已确认试用范围。',
  });
  assert.deepEqual(await h.promise,{
    resText:'本次拜访分析：客户已确认试用范围。',
    quick_check_id:second.check,
    submit_decision:'已确认提交',
  });
});

test('continue submit preserves the existing AI feedback after model failure',async()=>{
  const item=launch(0);
  const h=start([item],{existing_feedback:'原有已验证AI意见'});
  await tick();
  send(h,item,'taoran_submit_bypassed',{submit_confirmed:true});
  assert.deepEqual(await h.promise,{
    resText:'原有已验证AI意见',quick_check_id:item.check,submit_decision:'已确认提交',
  });
});

test('return or direct close never confirms submit and keeps the current value',async()=>{
  for (const directClose of [false,true]) {
    const item=launch(directClose?1:0);
    const h=start([item],{existing_feedback:'原意见'});
    await tick();
    if (directClose) h.g.utils.closeModal();
    else send(h,item,'taoran_submit_cancelled');
    assert.deepEqual(await h.promise,{resText:'原意见',submit_decision:'返回修改'});
  }
});

test('stale retry or bypass from a prior opening is ignored',async()=>{
  const item=launch(2);
  const h=start([item],{existing_feedback:'原意见'});
  await tick();
  send(h,launch(0),'taoran_submit_bypassed',{submit_confirmed:true});
  send(h,launch(1),'taoran_submit_retry');
  await tick();
  assert.equal(h.calls,1);
  send(h,item,'taoran_submit_cancelled');
  assert.deepEqual(await h.promise,{resText:'原意见',submit_decision:'返回修改'});
});

test('submit interaction has no plugin-owned auto-close deadline',()=>{
  assert.equal(front.toString().includes('decisionDeadlineMs'),false);
});
