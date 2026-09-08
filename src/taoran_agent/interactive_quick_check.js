// experimental: transport/UX only. Final text is never rewritten here.
const params = new URLSearchParams(location.search);
const checkId = params.get('check_id'), token = sessionToken;
// A per-opening nonce is not a form field or cache identity.
const openingId = params.get('opening_id') || '';
const status = document.querySelector('#status'), content = document.querySelector('#content');
const finalPanel = document.querySelector('#finalPanel'), finalContent = document.querySelector('#finalContent');
const ack = document.querySelector('#ack');
const resume = document.querySelector('#resume');
const returnNotice = document.querySelector('#returnNotice');
let feedbackHandedOff = false;
// Refresh/reopen retains the redeemed task capability, not an expired launch URL.
if (typeof history !== 'undefined') {
  params.set('stream_token', token);
  history.replaceState(null, '', location.pathname + '?' + params.toString());
}
const base = publicPath + '/api/v1/quick-check/tasks/' + encodeURIComponent(checkId);
const query = '?stream_token=' + encodeURIComponent(token);
let done = false, source, pollTimer, retryDelay = 1000, polling = false, returning = false;
let disposed = false, returnTimer, lifecycle = 0;
let previewComplete = false, previewSucceeded = false;
let finalDone = false, finalFailed = false;
const activeRequests = new Set();
const versionedMode = typeof taskVersion === 'string';
const restartText = '请关闭当前弹窗，返回拜访记录界面重新点击“AI检测”。';
const waitingText = 'AI正在分析，请稍候；可关闭后重新打开查看进度。';
const dualMode = typeof frontPolicy === 'string' && ['front-v46-restored-20260908','front-v46-observe-20260908','front-v46-complete-20260908'].includes(frontPolicy);
const previewLabel = document.querySelector('#previewLabel');
if (versionedMode) {
  content.textContent = waitingText;
  previewComplete = !dualMode;
  if (previewLabel) previewLabel.textContent = 'AI实时分析';
}
function applyVersion(task) {
  if (!versionedMode) return true;
  if (task.input_hash !== taskVersion) { fail('record_version_mismatch','结果与当前记录版本不一致，已停止展示。',true); return false; }
  if (task.superseded) {
    content.textContent = waitingText;
    finalPanel.hidden = true;
    if (previewLabel) previewLabel.textContent = '历史版本';
    fail('superseded','拜访记录已更新。',true);
    return false;
  }
  return true;
}
const allowedParents = new Set(['https://www.jiandaoyun.com', 'https://jiandaoyun.com']);
let parentOrigin = '';
try { parentOrigin = new URL(document.referrer).origin; } catch (_) { /* fail closed */ }
function stage(text) { if (!disposed) status.textContent = text; }
function stopTransport() {
  if (source) source.close();
  clearTimeout(pollTimer);
}
function settle() {
  if (finalDone && previewComplete) { done = true; stopTransport(); }
}
function previewSnapshot(text, state) {
  if ((versionedMode && !dualMode) || previewComplete) return;
  if (typeof text === 'string' && (text || !dualMode)) {
    content.textContent = text;
    if (dualMode && previewLabel) previewLabel.textContent = 'AI实时分析';
  }
  previewSucceeded = state === 'completed';
  previewComplete = state === 'completed' || state === 'unavailable' || state === 'failed';
  if ((!content.textContent || content.textContent === waitingText) && previewComplete) content.textContent = '正在生成完整分析。';
  if (finalDone && !finalFailed) {
    if (previewComplete && !previewSucceeded && versionedMode) { content.textContent = finalContent.textContent; finalPanel.hidden = true; }
    stage(previewComplete ? 'AI检测完成' : '最终反馈已生成，AI实时分析仍在生成…');
  }
  settle();
}
function fail(code, message, terminal = false) {
  if (done || disposed) return;
  if (terminal) { done = true; stopTransport(); }
  finalDone = true;
  finalFailed = true;
  ack.hidden = true;
  ack.disabled = true;
  // Content-mode retries must capture the current form and effective versions.
  if (resume) resume.hidden = terminal || Boolean(openingId);
  const expired = code === 'task_or_token_expired' || code === 'task_expired';
  stage(expired
    ? '检测链接已失效或任务已过期。' + restartText
    : (message || '本次分析未完成。') + restartText);
  status.className = 'status error';
  settle();
}
function finish(text) {
  if (finalDone || disposed) return;
  if (typeof text !== 'string' || !text.trim()) return fail('empty_final_feedback');
  finalDone = true;
  finalContent.textContent = text;
  if (versionedMode && (!dualMode || (previewComplete && !previewSucceeded))) {
    content.textContent = text;
    if (previewLabel) previewLabel.textContent = 'AI实时分析';
    finalPanel.hidden = true;
  } else finalPanel.hidden = false;
  stage(previewComplete ? 'AI检测完成' : '最终反馈已生成，AI实时分析仍在生成…');
  ack.hidden = false;
  ack.disabled = false;
  settle();
}
async function requestJson(url, options = {}) {
  // Each network request is bounded; the overall AI wait has no response limit.
  const controller = new AbortController();
  activeRequests.add(controller);
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(url, {...options, cache: 'no-store', signal: controller.signal});
    // Keep the deadline active while reading the body, not just response headers.
    const data = response.ok ? await response.json() : null;
    return {ok: response.ok, status: response.status, data};
  }
  finally { clearTimeout(timeout); activeRequests.delete(controller); }
}
async function poll() {
  if (done || disposed || polling) return;
  polling = true;
  const generation = lifecycle;
  try {
    const response = await requestJson(base + query);
    if (disposed || generation !== lifecycle) return;
    if (response.status === 404 || response.status === 410) {
      return fail('task_or_token_expired', undefined, true);
    }
    if (response.status === 401 || response.status === 403) return fail('access_denied', undefined, true);
    if (!response.ok) throw new Error('retryable_transport');
    const task = response.data;
    if (!applyVersion(task)) return;
    if (task.check_id !== checkId) return fail('task_mismatch', undefined, true);
    previewSnapshot(task.preview_feedback_text, task.preview_status || (task.status === 'processing' ? 'processing' : 'unavailable'));
    if (task.status === 'completed') {
      finish(task.final_feedback_text);
      if (resume) resume.hidden = !task.recoverable;
      if (task.recoverable) stage('部分分析未完成，任务已保留，可恢复本次分析。');
    }
    else if (task.status === 'failed') { fail(task.failure_category || 'final_service_error'); if (resume) resume.hidden = !task.recoverable; }
    else if (task.status === 'expired') fail('task_expired', undefined, true);
    else stage(dualMode && previewSucceeded ? '实时分析已生成，最终反馈仍在后台生成，可重新打开查看。' : 'AI任务正在后台运行，可关闭后重新打开查看；请等待分析结果。');
  } catch (_) { stage('连接暂时中断，正在查询原任务；后台生成不会因此取消。'); }
  finally {
    polling = false;
    if (!done && !disposed) {
      pollTimer = setTimeout(poll, retryDelay);
      retryDelay = Math.min(5000, retryDelay * 1.5);
    }
  }
}
function recover() {
  if (done || disposed || polling) return;
  if (source) source.close();
  clearTimeout(pollTimer);
  poll();
}
function decode(event, callback) {
  if (done || disposed) return;
  try { callback(JSON.parse(event.data)); } catch (_) { recover(); }
}
source = new EventSource(base + '/events' + query);
source.addEventListener('stage', event => decode(event, data => { if (!finalDone) stage(data.text); }));
source.addEventListener('preview_snapshot', event => decode(event, data => {
  if (data.check_id !== checkId) return fail('task_mismatch', undefined, true);
  previewSnapshot(data.text, data.status);
}));
source.addEventListener('preview_delta', event => decode(event, data => {
  if (!previewComplete && typeof data.text === 'string') {
    if (content.textContent === waitingText) content.textContent = '';
    content.textContent += data.text;
  }
}));
source.addEventListener('preview_complete', event => decode(event, data => {
  previewComplete = true;
  previewSucceeded = data.status === 'completed';
  if (!finalDone) stage(data.status === 'unavailable'
    ? '实时预览暂不可用，仍在生成最终检测结果…'
    : 'AI实时分析已生成，正在完成最终检测…'
  );
  settle();
}));
source.addEventListener('final_completed', event => decode(event, data => {
  if (data.check_id !== checkId) return fail('task_mismatch', undefined, true);
  if (versionedMode) { recover(); return; }
  finish(data.feedback_text);
}));
source.addEventListener('final_failed', event => decode(event, data => {
  if (data.check_id !== checkId) return fail('task_mismatch', undefined, true);
  fail(data.code || 'final_service_error');
  if (resume) resume.hidden = data.recoverable === false;
}));
if (resume) resume.addEventListener('click', async () => {
  if (disposed || resume.disabled) return;
  resume.disabled = true;
  try {
    const response = await requestJson(base + '/resume' + query, {method:'POST'});
    if (!response.ok || response.data.check_id !== checkId) throw new Error('resume_failed');
    ack.hidden = true; ack.disabled = true;
    done = false; finalDone = false; finalFailed = false; previewComplete = false; previewSucceeded = false;
    content.textContent = versionedMode ? waitingText : ''; finalContent.textContent = ''; finalPanel.hidden = true;
    if (versionedMode) { previewComplete = !dualMode; if (previewLabel) previewLabel.textContent = 'AI实时分析'; }
    resume.hidden = true; status.className = 'status';
    stage('正在恢复本次分析，请稍候…');
    retryDelay = 1000; recover();
  } catch (_) { stage('恢复请求暂未确认。' + restartText); }
  finally { resume.disabled = false; }
});
// Both a named server failure and a broken SSE are resolved through the result API.
source.addEventListener('error', recover);
window.addEventListener('online', recover);
window.addEventListener('beforeunload', event => {
  if (!feedbackHandedOff && !finalFailed) { event.preventDefault(); event.returnValue = ''; }
});
window.addEventListener('pagehide', () => {
  disposed = true;
  lifecycle++;
  stopTransport();
  clearTimeout(returnTimer);
  for (const controller of activeRequests) controller.abort();
});
window.addEventListener('pageshow', event => {
  if (!event.persisted || !disposed) return;
  disposed = false;
  returning = false;
  if (finalDone && !ack.hidden) ack.disabled = false;
  // A restored page that already holds the authoritative Final only needs to
  // re-enable acknowledgement. Polling again can consume the acknowledgement
  // response slot and must not replace or delay the saved Final.
  if (!done && !finalDone) recover();
});
ack.addEventListener('click', async () => {
  if (disposed || returning || ack.disabled) return;
  if (window.parent === window || !allowedParents.has(parentOrigin)) {
    stage('无法确认简道云来源。' + restartText);
    return;
  }
  returning = true;
  ack.disabled = true;
  const generation = lifecycle;
  try {
    const response = await requestJson(base + '/acknowledge' + query, {method: 'POST'});
    if (disposed || generation !== lifecycle) return;
    if (!response.ok) throw new Error('ack_failed');
    const data = response.data;
    if (data.check_id !== checkId || typeof data.final_feedback_text !== 'string' || !data.final_feedback_text.trim()) {
      throw new Error('invalid_final');
    }
    window.parent.postMessage({pluginMessage: {
      type: 'taoran_quick_check_acknowledged', check_id: checkId, feedback_text: data.final_feedback_text,
      opening_id: openingId,
      ...(versionedMode ? {input_hash:taskVersion,generated_at:data.generated_at} : {}),
    }}, parentOrigin);
    feedbackHandedOff = true;
    if (returnNotice) returnNotice.textContent = 'AI反馈意见已交给简道云处理，请返回拜访记录录入界面核对并保存。';
    // Sending a message is not proof that Jiandaoyun has assigned the field.
    stage('最终反馈已交给简道云处理；请返回表单核对，记录尚未保存。');
    returnTimer = setTimeout(() => { returning = false; ack.disabled = false; }, 3000);
  } catch (_) {
    if (disposed || generation !== lifecycle) return;
    returning = false;
    ack.disabled = false;
    stage('最终反馈未能返回。' + restartText);
    status.className = 'status error';
  }
});

if (versionedMode) recover(); // Poll the retained task; the waiting message is not an AI result.
