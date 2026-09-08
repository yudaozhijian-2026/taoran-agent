// Deterministic browser transport tests: no model, network, or Jiandaoyun writes.
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {test} = require('node:test');
const code = readFileSync('src/taoran_agent/interactive_quick_check.js', 'utf8');
function harness(responses = [], referrer = 'https://www.jiandaoyun.com/dashboard', initial = {}) {
  const nodes = Object.fromEntries(['status','content','previewLabel','finalPanel','finalContent','ack','resume','timings','versionNote'].map(id => [id, {
    textContent: id === 'previewLabel' ? 'AI实时分析' : '', hidden: id === 'ack' || id === 'finalPanel', disabled: id === 'ack',
    addEventListener(name, fn) { this[name] = fn; },
  }]));
  let serial = 0, calls = 0;
  const timers = new Map(), messages = [], handlers = {};
  const parent = {postMessage: (...args) => messages.push(args)};
  class Source {
    constructor() { Source.instance = this; this.handlers = {}; }
    addEventListener(name, fn) { this.handlers[name] = fn; }
    close() { this.closed = true; }
    emit(name, data) { this.handlers[name]({data: JSON.stringify(data)}); }
  }
  const context = {
    ...initial, URL, URLSearchParams, AbortController, publicPath: '/taoran-027', sessionToken: 'session-token',
    location: {search: '?check_id=qc_test&stream_token=' + 'x'.repeat(32)},
    document: {referrer, querySelector: selector => nodes[selector.slice(1)]},
    window: {parent, addEventListener: (name, fn) => { handlers[name] = fn; }},
    EventSource: Source,
    setTimeout: (fn, delay) => { const id = ++serial; timers.set(id, {fn, delay}); return id; },
    clearTimeout: id => timers.delete(id),
    fetch: async (url, options) => {
      calls++;
      const next = responses.shift();
      if (typeof next === 'function') return next(url, options);
      if (next instanceof Error) throw next;
      if (!next) throw new Error('no response');
      return {ok: (next.http || 200) === 200, status: next.http || 200, json: async () => next};
    },
  };
  vm.runInNewContext(code, context);
  return {nodes, source: Source.instance, messages, timers, handlers, get calls() {return calls;},
    async tick(delay) {
      const [id, timer] = [...timers].find(([, timer]) => delay === undefined ? timer.delay < 15000 : timer.delay === delay) || [];
      if (timer) { timers.delete(id); await timer.fn(); }
      await new Promise(resolve => setImmediate(resolve));
    },
  };
}
const pending = () => ({check_id: 'qc_test', status: 'processing'});
test('Final first does not close the stream or block later Preview', () => {
  const h = harness();
  h.source.emit('preview_snapshot', {check_id: 'qc_test', text: '', status: 'processing'});
  h.source.emit('final_completed', {check_id: 'qc_test', feedback_text: '正式反馈'});
  assert.notEqual(h.source.closed, true);
  assert.equal(h.nodes.ack.disabled, false);
  h.source.emit('preview_snapshot', {check_id: 'qc_test', text: '后来生成的建议', status: 'completed'});
  assert.equal(h.nodes.content.textContent, '后来生成的建议');
  assert.equal(h.nodes.finalContent.textContent, '正式反馈');
  assert.equal(h.source.closed, true);
});
test('snapshot replay replaces the snapshot instead of duplicating text', () => {
  const h = harness();
  for (let i = 0; i < 3; i++) h.source.emit('preview_snapshot', {check_id: 'qc_test', text: '客户确认', status: 'processing'});
  h.source.emit('preview_snapshot', {check_id: 'qc_test', text: '客户确认采购计划', status: 'completed'});
  assert.equal(h.nodes.content.textContent, '客户确认采购计划');
});
test('polling continues after Final until Preview is complete', async () => {
  const h = harness([
    {...completed(), preview_status: 'processing', preview_feedback_text: '建议'},
    {...completed(), preview_status: 'completed', preview_feedback_text: '建议完整原文'},
  ]);
  h.source.emit('error');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.nodes.finalContent.textContent, '真实Final');
  assert.equal(h.nodes.content.textContent, '建议');
  await h.tick();
  assert.equal(h.nodes.content.textContent, '建议完整原文');
  assert.equal(h.timers.size, 0);
});
test('Final failure still receives independently completed Preview', () => {
  const h = harness();
  h.source.emit('final_failed', {check_id: 'qc_test', code: 'model_failed'});
  assert.notEqual(h.source.closed, true);
  h.source.emit('preview_snapshot', {check_id: 'qc_test', text: '仍可阅读的建议', status: 'completed'});
  assert.equal(h.nodes.content.textContent, '仍可阅读的建议');
  assert.equal(h.nodes.ack.hidden, true);
  assert.equal(h.source.closed, true);
});
const completed = () => ({check_id: 'qc_test', status: 'completed', final_feedback_text: '真实Final'});
test('polling continues beyond the previous three-attempt limit', async () => {
  const h = harness([pending(), pending(), pending(), pending(), completed()]);
  h.source.emit('error');
  await new Promise(resolve => setImmediate(resolve));
  for (let i = 0; i < 4; i++) await h.tick();
  assert.equal(h.calls, 5);
  assert.equal(h.nodes.finalContent.textContent, '真实Final');
  assert.equal(h.nodes.ack.hidden, false);
  assert.equal(h.timers.size, 0);
});
test('temporary errors recover without substituting preview for Final', async () => {
  const h = harness([new Error('offline'), {http: 503}, completed()]);
  h.source.emit('preview_delta', {text: '辅助预览'});
  h.source.emit('error');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.nodes.ack.hidden, true);
  await h.tick(); await h.tick();
  assert.equal(h.nodes.finalContent.textContent, '真实Final');
});
test('expired access ends polling with a traceable state', async () => {
  const h = harness([{http: 404}]); h.source.emit('error');
  await new Promise(resolve => setImmediate(resolve));
  assert.match(h.nodes.status.textContent, /qc_test.*task_or_token_expired/);
  assert.equal(h.nodes.ack.hidden, true); assert.equal(h.timers.size, 0);
});
test('Final failure cannot enable return', async () => {
  const h = harness([{check_id: 'qc_test', status: 'failed', failure_category: 'evidence_validation_failed'}]);
  h.source.emit('preview_delta', {text: '辅助预览'}); h.source.emit('error');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.nodes.ack.hidden, true);
  assert.match(h.nodes.status.textContent, /evidence_validation_failed/);
});
test('late Preview cannot overwrite Final', () => {
  const h = harness();
  h.source.emit('final_completed', {check_id: 'qc_test', feedback_text: '原文Final'});
  h.source.emit('preview_delta', {text: '迟到预览'});
  assert.equal(h.nodes.finalContent.textContent, '原文Final');
});
test('return uses an exact allowed parent origin and authoritative ack text', async () => {
  const h = harness([{check_id: 'qc_test', final_feedback_text: '原始Final'}]);
  h.source.emit('final_completed', {check_id: 'qc_test', feedback_text: '原始Final'});
  await h.nodes.ack.click();
  assert.equal(h.messages[0][1], 'https://www.jiandaoyun.com');
  assert.equal(h.messages[0][0].pluginMessage.feedback_text, '原始Final');
});
test('unknown parent cannot receive customer feedback', async () => {
  const h = harness([], 'https://attacker.example/');
  h.source.emit('final_completed', {check_id: 'qc_test', feedback_text: '原始Final'});
  await h.nodes.ack.click();
  assert.equal(h.messages.length, 0); assert.equal(h.calls, 0);
});
const hangingBody = (_, {signal}) => Promise.resolve({ok: true, status: 200,
  json: () => new Promise((resolve, reject) => {
    signal.addEventListener('abort', () => reject(new Error('aborted')), {once: true});
  }),
});
test('hung ack body times out, preserves Final and allows safe retry', async () => {
  const h = harness([hangingBody, completed()]);
  h.source.emit('final_completed', {check_id: 'qc_test', feedback_text: '真实Final'});
  const click = h.nodes.ack.click();
  await new Promise(resolve => setImmediate(resolve));
  await h.nodes.ack.click();
  assert.equal(h.calls, 1);
  await h.tick(15000); await click;
  assert.equal(h.nodes.ack.disabled, false);
  assert.equal(h.nodes.finalContent.textContent, '真实Final');
  assert.equal(h.messages.length, 0);
  await h.nodes.ack.click();
  assert.equal(h.messages.length, 1);
  assert.equal(h.timers.size, 1); // Only the return-button cooldown remains.
});
test('hung result body times out and recovery continues to Final', async () => {
  const h = harness([hangingBody, completed()]);
  h.source.emit('error');
  await new Promise(resolve => setImmediate(resolve));
  await h.tick(15000); await h.tick();
  assert.equal(h.nodes.finalContent.textContent, '真实Final');
  assert.equal(h.timers.size, 0);
});
test('Preview failure still permits a successful Final', () => {
  const h = harness();
  h.source.emit('preview_complete', {status: 'unavailable'});
  assert.equal(h.nodes.ack.hidden, true);
  h.source.emit('final_completed', {check_id: 'qc_test', feedback_text: '真实Final'});
  assert.equal(h.nodes.finalContent.textContent, '真实Final');
  assert.equal(h.nodes.ack.disabled, false);
});
test('ack for another task never returns or discards current Final', async () => {
  const h = harness([{check_id: 'qc_other', final_feedback_text: '其他反馈'}]);
  h.source.emit('final_completed', {check_id: 'qc_test', feedback_text: '真实Final'});
  await h.nodes.ack.click();
  assert.equal(h.messages.length, 0);
  assert.equal(h.nodes.finalContent.textContent, '真实Final');
  assert.equal(h.nodes.ack.disabled, false);
});
test('pagehide during an in-flight result request cannot restart polling', async () => {
  let release;
  const h = harness([() => new Promise(resolve => { release = resolve; })]);
  h.source.emit('error');
  h.handlers.pagehide();
  release({ok: true, status: 200, json: async () => pending()});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.timers.size, 0);
  assert.equal(h.nodes.ack.hidden, true);
  h.handlers.online();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.calls, 1);
});
test('pagehide during acknowledgement cannot send feedback to a closed page', async () => {
  let release;
  const h = harness([() => new Promise(resolve => { release = resolve; })]);
  h.source.emit('final_completed', {check_id: 'qc_test', feedback_text: '真实Final'});
  const returning = h.nodes.ack.click();
  h.handlers.pagehide();
  release({ok: true, status: 200, json: async () => completed()});
  await returning;
  assert.equal(h.messages.length, 0);
  assert.equal(h.timers.size, 0);
});
test('prolonged simulated offline preserves Preview and recovers exact Final', async () => {
  const h = harness([...Array.from({length: 20}, () => new Error('offline')), pending(), completed()]);
  h.source.emit('preview_delta', {text: '辅助预览'});
  h.source.emit('error');
  await new Promise(resolve => setImmediate(resolve));
  for (let i = 0; i < 19; i++) await h.tick();
  assert.equal(h.nodes.content.textContent, '辅助预览');
  assert.equal(h.nodes.ack.hidden, true);
  assert.equal(h.messages.length, 0);
  assert.ok([...h.timers.values()].every(timer => timer.delay <= 5000));
  await h.tick(); await h.tick();
  assert.equal(h.nodes.finalContent.textContent, '真实Final');
  assert.equal(h.nodes.ack.hidden, false);
  assert.equal(h.calls, 22);
  assert.equal(h.timers.size, 0);
});
test('restored page ignores an acknowledgement from the prior page lifecycle', async () => {
  let release;
  const h = harness([() => new Promise(resolve => { release = resolve; }), completed()]);
  h.source.emit('preview_complete', {status: 'completed'});
  h.source.emit('final_completed', {check_id: 'qc_test', feedback_text: '真实Final'});
  const oldReturn = h.nodes.ack.click();
  h.handlers.pagehide();
  h.handlers.pageshow({persisted: true});
  release({ok: true, status: 200, json: async () => completed()});
  await oldReturn;
  assert.equal(h.messages.length, 0);
  await h.nodes.ack.click();
  assert.equal(h.messages.length, 1);
});
test('restored pending page resumes result recovery after an aborted request', async () => {
  const h = harness([hangingBody, completed()]);
  h.source.emit('error');
  await new Promise(resolve => setImmediate(resolve));
  h.handlers.pagehide();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.timers.size, 0);
  h.handlers.pageshow({persisted: true});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.nodes.finalContent.textContent, '真实Final');
});

test('completed Preview remains unchanged after Final and return', async () => {
  const h = harness([{check_id: 'qc_test', final_feedback_text: '正式反馈'}]);
  h.source.emit('preview_delta', {text: '客户已明确'});
  h.source.emit('preview_delta', {text: '采购计划。'});
  h.source.emit('preview_complete', {status: 'completed'});
  h.source.emit('preview_delta', {text: '迟到内容不应追加'});
  assert.equal(h.nodes.content.textContent, '客户已明确采购计划。');
  assert.equal(h.nodes.finalPanel.hidden, true);
  h.source.emit('final_completed', {check_id: 'qc_test', feedback_text: '正式反馈'});
  assert.equal(h.nodes.content.textContent, '客户已明确采购计划。');
  assert.equal(h.nodes.previewLabel.textContent, 'AI实时分析');
  assert.equal(h.nodes.finalContent.textContent, '正式反馈');
  assert.equal(h.nodes.finalPanel.hidden, false);
  await h.nodes.ack.click();
  assert.equal(h.nodes.content.textContent, '客户已明确采购计划。');
  assert.equal(h.messages[0][0].pluginMessage.feedback_text, '正式反馈');
});
test('poll recovery preserves generated Preview separately from Final', async () => {
  const h = harness([completed()]);
  h.source.emit('preview_delta', {text: '已生成实时建议'});
  h.source.emit('preview_complete', {status: 'completed'});
  h.source.emit('error');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.nodes.content.textContent, '已生成实时建议');
  assert.equal(h.nodes.finalContent.textContent, '真实Final');
});
test('failed Final keeps Preview visible without allowing it to be returned', async () => {
  const h = harness([{check_id: 'qc_test', status: 'failed'}]);
  h.source.emit('preview_delta', {text: '实时建议原文'});
  h.source.emit('preview_complete', {status: 'completed'});
  h.source.emit('error');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.nodes.content.textContent, '实时建议原文');
  assert.equal(h.nodes.finalPanel.hidden, true);
  assert.equal(h.nodes.ack.hidden, true);
  assert.equal(h.messages.length, 0);
});


test('failed task resumes same identity and keeps waiting and generation distinct', async () => {
  const h = harness([
    (url, options) => {
      assert.match(url, /tasks\/qc_test\/resume\?stream_token=session-token$/);
      assert.equal(options.method, 'POST');
      return {ok:true,status:200,json:async()=>pending()};
    }, completed()
  ]);
  h.source.emit('final_failed', {check_id:'qc_test', code:'timeout',recoverable:true,
    phase_timings:{attempts:[{first_byte_wait_ms:1200,generation_ms:3800}]}});
  assert.equal(h.nodes.resume.hidden, false);
  assert.match(h.nodes.timings.textContent, /首字等待 1.2 秒，生成 3.8 秒/);
  await h.nodes.resume.click();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.nodes.finalContent.textContent, '真实Final');
  assert.equal(h.calls, 2);
});


test('offline page keeps truthful basic feedback and never claims AI success', async () => {
  const h=harness([new Error('offline')],undefined,{initialBasic:'基础检查：原文摘录（非 AI 分析）',taskVersion:'version-a'});
  await new Promise(resolve=>setImmediate(resolve));
  assert.match(h.nodes.content.textContent,/基础检查/);
  assert.equal(h.nodes.previewLabel.textContent,'基础检查');
  assert.equal(h.nodes.ack.hidden,true);
  assert.equal(h.nodes.finalPanel.hidden,true);
  assert.match(h.nodes.status.textContent,/原任务/);
});

test('completed AI replaces basic feedback only for the matching version', async () => {
  const h=harness([{...completed(),input_hash:'version-a',generated_at:'2026-09-07T09:00:00Z'}],undefined,
    {initialBasic:'基础检查',taskVersion:'version-a'});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(h.nodes.content.textContent,'真实Final');
  assert.equal(h.nodes.previewLabel.textContent,'AI实时分析');
  assert.match(h.nodes.versionNote.textContent,/2026-09-07/);
});

test('late old result cannot replace basic feedback of a newer record', async () => {
  const h=harness([{...completed(),input_hash:'old-version'}],undefined,
    {initialBasic:'基础检查：新记录原文',taskVersion:'new-version'});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(h.nodes.content.textContent,'基础检查：新记录原文');
  assert.equal(h.nodes.ack.hidden,true);
  assert.match(h.nodes.status.textContent,/版本不一致/);
});

test('superseded task cannot be returned as the latest result', async () => {
  const h=harness([{...completed(),input_hash:'version-a',superseded:true}],undefined,
    {initialBasic:'基础检查：历史记录',taskVersion:'version-a'});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(h.nodes.previewLabel.textContent,'历史版本基础检查');
  assert.equal(h.nodes.ack.hidden,true);
  assert.notEqual(h.nodes.content.textContent,'真实Final');
});

const restored = {initialBasic:'基础检查：记录原文',taskVersion:'version-a',frontPolicy:'front-v46-restored-20260908'};
test('restored V4.6 retains basic feedback offline and never claims AI success', async () => {
  const h=harness([new Error('offline')],undefined,restored);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(h.nodes.content.textContent,restored.initialBasic);
  assert.equal(h.nodes.ack.hidden,true);
});
test('restored V4.6 displays independent Preview and Final after reopening', async () => {
  const h=harness([{...completed(),input_hash:'version-a',preview_feedback_text:'V4.6实时意见',preview_status:'completed'}],undefined,restored);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(h.nodes.content.textContent,'V4.6实时意见');
  assert.equal(h.nodes.finalContent.textContent,'真实Final');
  assert.equal(h.nodes.finalPanel.hidden,false);
  assert.equal(h.nodes.ack.disabled,false);
});
test('restored V4.6 Final first remains usable while Preview completes later', async () => {
  const h=harness([
    {...completed(),input_hash:'version-a',preview_feedback_text:'',preview_status:'processing'},
    {...completed(),input_hash:'version-a',preview_feedback_text:'稍后完成的实时意见',preview_status:'completed'},
  ],undefined,restored);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(h.nodes.finalContent.textContent,'真实Final');
  assert.equal(h.nodes.content.textContent,restored.initialBasic);
  await h.tick(1000);
  assert.equal(h.nodes.content.textContent,'稍后完成的实时意见');
  assert.equal(h.nodes.finalContent.textContent,'真实Final');
});
test('restored V4.6 does not display either opinion for an old input version', async () => {
  const h=harness([{...completed(),input_hash:'old-version',preview_feedback_text:'旧意见',preview_status:'completed'}],undefined,restored);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(h.nodes.content.textContent,restored.initialBasic);
  assert.equal(h.nodes.finalPanel.hidden,true);
  assert.equal(h.nodes.ack.hidden,true);
});

test('semantic observation shows confirmation and remains a completed returnable result', async () => {
  const text='【AI反馈意见】\n本次拜访分析：当前不足以判断客户认可。\n需确认事项：请核对实际确认方。';
  const h=harness([{...completed(),input_hash:'version-a',final_feedback_text:text,
    preview_feedback_text:'原文未说明确认方，需确认实际确认方。',preview_status:'completed'}],undefined,
    {...restored,frontPolicy:'front-v46-observe-20260908'});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(h.nodes.finalContent.textContent,text);
  assert.equal(h.nodes.ack.disabled,false);
  assert.equal(h.nodes.finalPanel.hidden,false);
  assert.doesNotMatch(h.nodes.status.textContent,/失败|未完成/);
});
