// Transport/UX only: omit the duplicate heading for display, preserve returned feedback.
const params = new URLSearchParams(location.search);
const checkId = params.get('check_id'), token = sessionToken;
// A per-opening nonce is not a form field or cache identity.
const openingId = params.get('opening_id') || '';
const status = document.querySelector('#status'), content = document.querySelector('#content');
const finalPanel = document.querySelector('#finalPanel'), finalContent = document.querySelector('#finalContent');
const finalLabel = document.querySelector('#finalLabel');
const ack = document.querySelector('#ack');
const resume = document.querySelector('#resume');
const returnNotice = document.querySelector('#returnNotice');
const submitMode = typeof submitConfirmation !== 'undefined' && submitConfirmation === true;
const cancelSubmit = document.querySelector('#cancelSubmit');
const cachedOpening = typeof reusedOpening !== 'undefined' && reusedOpening === true;
function setConfirmReady(ready) {
  if (!ack) return;
  ack.hidden = submitMode ? false : !ready;
  ack.disabled = !ready;
}
if (submitMode) {
  ack.textContent = '确认提交';
  setConfirmReady(false);
  if (returnNotice) returnNotice.textContent = '记录尚未提交。阅读AI意见后可确认提交，不要求全部达标；返回修改或直接关闭均不提交。';
  if (cancelSubmit) {
    cancelSubmit.hidden = false;
    cancelSubmit.addEventListener('click', () => {
      if (disposed || returning || !allowedParents.has(parentOrigin)) return;
      feedbackHandedOff = true;
      window.parent.postMessage({pluginMessage:{type:'taoran_submit_cancelled',
        check_id:checkId,opening_id:openingId,input_hash:taskVersion}},parentOrigin);
    });
  }
}
let feedbackHandedOff = false;
const viewStarted = typeof performance !== 'undefined' ? performance.now() : Date.now();
const clientTimings = {};
function markTiming(key) {
  if (clientTimings[key] === undefined) clientTimings[key] = Math.max(0, Math.round(
    (typeof performance !== 'undefined' ? performance.now() : Date.now()) - viewStarted));
}
function markVisible(key) {
  if (typeof requestAnimationFrame === 'function') requestAnimationFrame(() => requestAnimationFrame(() => markTiming(key)));
  else markTiming(key);
}
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
let finalContentComplete = false;
const activeRequests = new Set();
const versionedMode = typeof taskVersion === 'string';
const restartText = submitMode ? '请关闭当前弹窗，返回填写页面重新点击“提交”。' : '请关闭当前弹窗，返回拜访记录界面重新点击“AI检测”。';
const waitingText = 'AI正在分析，请稍候。';
const finalWaitingText = '正在生成AI改善建议';
const suggestionWaitingText = '改善建议生成中';
const validationWaitingText = '正在校验并完善AI意见……';
const finalStreamMode = typeof frontPolicy === 'string' && ['front-v46-final-analysis-stream-v1-20260917','front-v46-final-analysis-typewriter-v1-20260917','front-v46-final-analysis-typewriter-v2-20260917','front-v46-taoran-advice-v3-20260917','front-v46-taoran-advice-v4-20260917','front-v46-taoran-advice-v5-20260918','front-v46-taoran-advice-v6-20260918'].includes(frontPolicy);
const typewriterMode = typeof frontPolicy === 'string' && ['front-v46-final-analysis-typewriter-v1-20260917','front-v46-final-analysis-typewriter-v2-20260917','front-v46-taoran-advice-v3-20260917','front-v46-taoran-advice-v4-20260917','front-v46-taoran-advice-v5-20260918','front-v46-taoran-advice-v6-20260918'].includes(frontPolicy);
const suggestionStreamMode = typeof frontPolicy === 'string' && ['front-v46-taoran-advice-v4-20260917','front-v46-taoran-advice-v5-20260918','front-v46-taoran-advice-v6-20260918'].includes(frontPolicy);
const dualMode = typeof frontPolicy === 'string' && ['front-v46-restored-20260908','front-v46-observe-20260908','front-v46-complete-20260908','front-v46-no-output-cap-20260908','front-v46-async-observation-20260908','front-v46-suggestion-contract-20260908','front-v46-grounded-confirmation-20260916'].includes(frontPolicy);
const previewLabel = document.querySelector('#previewLabel');
let analysisTarget = '', analysisQueue = '', analysisTypingTimer;
let suggestionTarget = '', suggestionQueue = '', suggestionTypingTimer;
let suggestionComplete = false, finalResultReceived = false;
let pendingSuggestionText = null;
let pendingFinalComplete = false;
let validationInProgress = false;
const analysisCharacterDelay = 18;
function stopAnalysisTyping() {
  clearTimeout(analysisTypingTimer);
  analysisTypingTimer = undefined;
}
function stopSuggestionTyping() {
  clearTimeout(suggestionTypingTimer);
  suggestionTypingTimer = undefined;
}
function analysisDisplayComplete() {
  return !analysisQueue && content.textContent === analysisTarget;
}
function finishSuggestionDisplay() {
  if (!suggestionStreamMode || !suggestionComplete || suggestionQueue || !finalResultReceived) return;
  finalDone = true;
  finalContentComplete = pendingFinalComplete;
  if (!finalContent.textContent || finalContent.textContent === suggestionWaitingText) {
    finalContent.textContent = pendingSuggestionText || '本次没有需要补充的AI改善建议或需确认事项。';
  }
  markVisible('final_visible_ms');
  markVisible('first_text_visible_ms');
  stage('AI检测完成');
  setConfirmReady(finalContentComplete);
  settle();
}
function typeSuggestionCharacter() {
  suggestionTypingTimer = undefined;
  if (disposed || !suggestionQueue || !analysisDisplayComplete()) return;
  const character = Array.from(suggestionQueue)[0];
  suggestionQueue = suggestionQueue.slice(character.length);
  if (finalContent.textContent === suggestionWaitingText) finalContent.textContent = '';
  finalContent.textContent += character;
  if (character.trim()) markVisible('final_visible_ms');
  if (suggestionQueue) suggestionTypingTimer = setTimeout(typeSuggestionCharacter, analysisCharacterDelay);
  else finishSuggestionDisplay();
}
function startSuggestionTyping() {
  if (!suggestionStreamMode || !analysisDisplayComplete()) return;
  finalPanel.hidden = false;
  if (!suggestionQueue) {
    if (!suggestionTarget) finalContent.textContent = suggestionWaitingText;
    finishSuggestionDisplay();
    return;
  }
  if (finalContent.textContent === suggestionWaitingText) finalContent.textContent = '';
  if (suggestionTypingTimer === undefined) typeSuggestionCharacter();
}
function syncSuggestionTarget(text, state = 'processing') {
  if (!suggestionStreamMode || typeof text !== 'string') return false;
  if (state === 'completed' || state === 'unavailable' || state === 'failed') suggestionComplete = true;
  if (text !== suggestionTarget) {
    if (text.startsWith(suggestionTarget)) suggestionQueue += text.slice(suggestionTarget.length);
    else {
      // A validated final correction replaces the draft once without blanking
      // and replaying the already visible advice.
      stopSuggestionTyping();
      suggestionQueue = '';
      finalContent.textContent = text;
    }
    suggestionTarget = text;
  }
  if (analysisDisplayComplete()) startSuggestionTyping();
  return true;
}
function typeAnalysisCharacter() {
  analysisTypingTimer = undefined;
  if (disposed || !analysisQueue) return;
  const character = Array.from(analysisQueue)[0];
  analysisQueue = analysisQueue.slice(character.length);
  if (content.textContent === waitingText) content.textContent = '';
  content.textContent += character;
  if (character.trim()) markVisible('first_text_visible_ms');
  if (analysisQueue) analysisTypingTimer = setTimeout(typeAnalysisCharacter, analysisCharacterDelay);
  else completeAnalysisDisplay();
}
function startAnalysisTyping() {
  if (analysisTypingTimer === undefined && analysisQueue) typeAnalysisCharacter();
}
function syncAnalysisTarget(text) {
  if (!typewriterMode || typeof text !== 'string') return false;
  if (text === analysisTarget) return true;
  if (text.startsWith(analysisTarget)) analysisQueue += text.slice(analysisTarget.length);
  else if (analysisTarget && text) {
    // A validated retry/final normalization is an atomic correction. Keeping
    // the old paragraph visible until now avoids blanking and replaying it.
    flushAnalysisText(text);
    completeAnalysisDisplay();
    return true;
  }
  else {
    stopAnalysisTyping();
    content.textContent = '';
    analysisQueue = text;
  }
  analysisTarget = text;
  startAnalysisTyping();
  return true;
}
function appendAnalysisText(text) {
  if (!typewriterMode) return false;
  analysisTarget += text;
  analysisQueue += text;
  startAnalysisTyping();
  return true;
}
function flushAnalysisText(text) {
  stopAnalysisTyping();
  analysisTarget = text;
  analysisQueue = '';
  content.textContent = text;
}
if (versionedMode) {
  content.textContent = waitingText;
  previewComplete = !(dualMode || finalStreamMode);
  if (previewLabel) previewLabel.textContent = finalStreamMode ? '本次拜访分析' : 'AI实时分析';
  if (finalStreamMode && finalLabel) finalLabel.textContent = 'AI改善建议';
  if (typewriterMode) {
    finalContent.textContent = '';
    finalPanel.hidden = true;
  }
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
function stage(text) {
  if (!disposed) status.textContent = validationInProgress
    ? validationWaitingText
    : (previewSucceeded && !finalDone ? finalWaitingText : text);
}
function setValidationInProgress(active) {
  validationInProgress = active === true;
  if (validationInProgress && !disposed && !finalDone) {
    status.textContent = validationWaitingText;
    setConfirmReady(false);
  }
}
function showFinalWaiting() {
  if (disposed || finalDone || !previewSucceeded) return;
  if (typewriterMode && (analysisQueue || content.textContent !== analysisTarget)) {
    finalPanel.hidden = true;
    return;
  }
  if (suggestionStreamMode) {
    if (!suggestionTarget && !suggestionQueue) finalContent.textContent = suggestionWaitingText;
    finalPanel.hidden = false;
    startSuggestionTyping();
    stage(finalWaitingText);
    return;
  }
  finalContent.textContent = typewriterMode ? suggestionWaitingText : finalWaitingText;
  finalPanel.hidden = false;
  stage(finalWaitingText);
}
function revealTypewriterFinal() {
  if (!typewriterMode || pendingSuggestionText === null || analysisQueue || content.textContent !== analysisTarget) return;
  const suggestionText = pendingSuggestionText;
  pendingSuggestionText = null;
  finalDone = true;
  finalContentComplete = pendingFinalComplete;
  finalContent.textContent = suggestionText;
  finalPanel.hidden = false;
  markVisible('final_visible_ms');
  markVisible('first_text_visible_ms');
  stage('AI检测完成');
  setConfirmReady(finalContentComplete);
  settle();
}
function revealCachedFinal(text, contentComplete = true) {
  const parts = splitFinalText(text);
  stopAnalysisTyping();
  stopSuggestionTyping();
  analysisTarget = parts.analysis;
  analysisQueue = '';
  content.textContent = parts.analysis || '本次拜访分析已完成。';
  previewComplete = true;
  previewSucceeded = true;
  finalDone = true;
  finalContentComplete = contentComplete;
  pendingSuggestionText = null;
  finalContent.textContent = parts.tail || '本次没有需要补充的AI改善建议或需确认事项。';
  finalPanel.hidden = false;
  markVisible('preview_complete_visible_ms');
  markVisible('final_visible_ms');
  markVisible('first_text_visible_ms');
  stage('AI检测完成');
  setConfirmReady(finalContentComplete);
  settle();
}
function completeAnalysisDisplay() {
  if (!typewriterMode || analysisQueue || content.textContent !== analysisTarget) return;
  if (suggestionStreamMode) {
    showFinalWaiting();
    return;
  }
  if (pendingSuggestionText !== null) revealTypewriterFinal();
  else if (previewComplete && previewSucceeded) showFinalWaiting();
}
function finalDisplayText(text) {
  return normalizeFinalFeedbackHeadings(text.replace(/^\s*【AI反馈意见】\s*/, ''));
}
function normalizeFinalFeedbackHeadings(text) {
  let clean = String(text || '');
  const suggestionMarker = /(?:AI改善建议|智能填写建议)：/;
  const confirmationMarker = /(?:需确认补充事项|需确认事项)：/;
  if (suggestionMarker.test(clean)) {
    clean = clean.replace(suggestionMarker, 'AI改善建议：');
    clean = clean.replace(confirmationMarker, '需确认事项：');
  } else if (confirmationMarker.test(clean)) {
    clean = clean.replace(confirmationMarker, 'AI改善建议：\n需确认事项：');
  }
  return clean;
}
function splitFinalText(text) {
  const clean = finalDisplayText(text).trim();
  const marker = '本次拜访分析：';
  let body = clean.startsWith(marker) ? clean.slice(marker.length).trim() : clean;
  let cut = body.length;
  for (const tail of ['AI改善建议：', 'AI最终意见：', '智能填写建议：', '需确认补充事项：', '需确认事项：']) {
    const index = body.indexOf(tail);
    if (index >= 0) cut = Math.min(cut, index);
  }
  const tail = body.slice(cut).trim().replace(/^(?:AI改善建议|AI最终意见)：\s*/, '');
  return {analysis: body.slice(0, cut).trim(), tail};
}
function stopTransport() {
  if (source) source.close();
  clearTimeout(pollTimer);
  stopAnalysisTyping();
  stopSuggestionTyping();
}
function settle() {
  if (finalDone && previewComplete) { done = true; stopTransport(); }
}
function previewSnapshot(text, state, validating = false) {
  if ((versionedMode && !(dualMode || finalStreamMode)) || previewComplete) return;
  setValidationInProgress(validating);
  if (typeof text === 'string') {
    // An empty snapshot can retract an incomplete attempt before format retry.
    if (!syncAnalysisTarget(text)) {
      content.textContent = text || (dualMode ? waitingText : '');
      if (text.trim()) markVisible('first_text_visible_ms');
    }
    if ((dualMode || finalStreamMode) && previewLabel) previewLabel.textContent = finalStreamMode ? '本次拜访分析' : 'AI实时分析';
  }
  previewSucceeded = state === 'completed';
  if (previewSucceeded) markVisible('preview_complete_visible_ms');
  previewComplete = state === 'completed' || state === 'unavailable' || state === 'failed';
  showFinalWaiting();
  if ((!content.textContent || content.textContent === waitingText) && previewComplete) content.textContent = '正在生成完整分析。';
  if (finalDone && !finalFailed) {
    if (previewComplete && !previewSucceeded && versionedMode) {
      content.textContent = finalContent.textContent;
      if (previewLabel) previewLabel.textContent = 'AI改善建议';
      finalPanel.hidden = true;
    }
    stage(previewComplete ? 'AI检测完成' : '最终反馈已生成，AI实时分析仍在生成…');
  }
  settle();
}
function suggestionSnapshot(text, state) {
  if (!suggestionStreamMode || cachedOpening || finalDone) return;
  syncSuggestionTarget(typeof text === 'string' ? text : '', state || 'processing');
}
function fail(code, message, terminal = false) {
  if (done || disposed) return;
  if (terminal) { done = true; stopTransport(); }
  finalDone = true;
  finalFailed = true;
  finalContentComplete = false;
  pendingSuggestionText = null;
  if ([finalWaitingText, suggestionWaitingText].includes(finalContent.textContent)) { finalContent.textContent = ''; finalPanel.hidden = true; }
  setConfirmReady(false);
  // Content-mode retries must capture the current form and effective versions.
  if (resume) resume.hidden = terminal || Boolean(openingId);
  const expired = code === 'task_or_token_expired' || code === 'task_expired';
  stage(expired
    ? '检测链接已失效或任务已过期。' + restartText
    : (message || '本次分析未完成。') + restartText);
  status.className = 'status error';
  settle();
}
function finish(text, contentComplete = true) {
  if (finalDone || disposed) return;
  if (typeof text !== 'string' || !text.trim()) return fail('empty_final_feedback');
  if (cachedOpening && finalStreamMode) {
    revealCachedFinal(text, contentComplete);
    return;
  }
  finalResultReceived = true;
  setValidationInProgress(false);
  finalContentComplete = contentComplete;
  if (finalStreamMode) {
    const parts = splitFinalText(text);
    previewComplete = true;
    previewSucceeded = true;
    if (typewriterMode) {
      pendingSuggestionText = parts.tail || '本次没有需要补充的AI改善建议或需确认事项。';
      pendingFinalComplete = contentComplete;
      if (parts.analysis) syncAnalysisTarget(parts.analysis);
      if (suggestionStreamMode) {
        suggestionComplete = true;
        syncSuggestionTarget(pendingSuggestionText, 'completed');
      } else finalPanel.hidden = true;
      completeAnalysisDisplay();
    } else {
      if (parts.analysis) flushAnalysisText(parts.analysis);
      finalDone = true;
      finalContent.textContent = parts.tail || '本次没有需要补充的AI改善建议或需确认事项。';
      finalPanel.hidden = false;
      markVisible('final_visible_ms');
      markVisible('first_text_visible_ms');
      stage('AI检测完成');
      setConfirmReady(finalContentComplete);
      settle();
    }
    return;
  }
  finalDone = true;
  finalContent.textContent = finalDisplayText(text);
  markVisible('final_visible_ms');
  markVisible('first_text_visible_ms');
  if (versionedMode && (!dualMode || (previewComplete && !previewSucceeded))) {
    content.textContent = finalDisplayText(text);
    if (previewLabel) previewLabel.textContent = 'AI改善建议';
    finalPanel.hidden = true;
  } else finalPanel.hidden = false;
  stage(previewComplete ? 'AI检测完成' : '最终反馈已生成，AI实时分析仍在生成…');
  setConfirmReady(finalContentComplete);
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
    markTiming('task_received_ms');
    if (!applyVersion(task)) return;
    if (task.check_id !== checkId) return fail('task_mismatch', undefined, true);
    const usableSubmitResult = submitMode && task.final_usable === true;
    if (cachedOpening && task.status === 'completed') {
      finish(task.final_feedback_text, task.content_complete !== false || usableSubmitResult);
      if (resume) resume.hidden = !task.recoverable;
      return;
    }
    previewSnapshot(task.preview_feedback_text, task.preview_status || (task.status === 'processing' ? 'processing' : 'unavailable'), task.validation_in_progress === true);
    suggestionSnapshot(task.suggestion_feedback_text, task.suggestion_status || 'processing');
    // Healthy active previews refresh each second; only failures/finished
    // previews back off, otherwise several generated sentences arrive at once.
    if ((dualMode || finalStreamMode) && !previewComplete) retryDelay = 1000;
    if (task.status === 'completed') {
      finish(task.final_feedback_text, task.content_complete !== false || usableSubmitResult);
      if (resume) resume.hidden = !task.recoverable;
      if (task.recoverable && !usableSubmitResult) stage('部分分析未完成，任务已保留，可恢复本次分析。');
    }
    else if (task.status === 'failed') { fail(task.failure_category || 'final_service_error'); if (resume) resume.hidden = !task.recoverable; }
    else if (task.status === 'expired') fail('task_expired', undefined, true);
    else stage((dualMode || finalStreamMode) && previewSucceeded ? finalWaitingText : 'AI任务正在运行，请等待分析结果。');
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
  if (cachedOpening) return;
  previewSnapshot(data.text, data.status, data.validating === true);
}));
source.addEventListener('preview_delta', event => decode(event, data => {
  if (cachedOpening) return;
  if (!previewComplete && typeof data.text === 'string') {
    if (!appendAnalysisText(data.text)) {
      if (content.textContent === waitingText) content.textContent = '';
      content.textContent += data.text;
    }
    if (finalStreamMode) stage('正在分析本次拜访，并核对填写建议…');
    if (!typewriterMode && data.text.trim()) markVisible('first_text_visible_ms');
  }
}));
source.addEventListener('preview_complete', event => decode(event, data => {
  if (cachedOpening) return;
  previewComplete = true;
  previewSucceeded = data.status === 'completed';
  if (previewSucceeded) markVisible('preview_complete_visible_ms');
  showFinalWaiting();
  if (!finalDone) stage(data.status === 'unavailable'
    ? (finalStreamMode ? '本次拜访分析暂未完成，仍在核对最终意见…' : '实时预览暂不可用，仍在生成最终检测结果…')
    : (finalStreamMode ? '本次拜访分析已生成，正在核对填写建议…' : 'AI实时分析已生成，正在完成最终检测…')
  );
  settle();
}));
source.addEventListener('suggestion_snapshot', event => decode(event, data => {
  if (data.check_id !== checkId) return fail('task_mismatch', undefined, true);
  suggestionSnapshot(data.text, data.status);
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
    setConfirmReady(false);
    done = false; finalDone = false; finalFailed = false; finalContentComplete = false; previewComplete = false; previewSucceeded = false;
    finalResultReceived = false; suggestionComplete = false;
    pendingSuggestionText = null;
    stopAnalysisTyping(); analysisTarget = ''; analysisQueue = '';
    stopSuggestionTyping(); suggestionTarget = ''; suggestionQueue = '';
    content.textContent = versionedMode ? waitingText : '';
    finalContent.textContent = '';
    finalPanel.hidden = true;
    if (versionedMode) { previewComplete = !(dualMode || finalStreamMode); if (previewLabel) previewLabel.textContent = finalStreamMode ? '本次拜访分析' : 'AI实时分析'; }
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
  if (finalDone && !finalFailed) setConfirmReady(finalContentComplete);
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
    markTiming('acknowledge_clicked_ms');
    const response = await requestJson(base + '/acknowledge' + query, {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(clientTimings)});
    if (disposed || generation !== lifecycle) return;
    if (!response.ok) throw new Error('ack_failed');
    const data = response.data;
    if (data.check_id !== checkId || typeof data.final_feedback_text !== 'string' || !data.final_feedback_text.trim()) {
      throw new Error('invalid_final');
    }
    window.parent.postMessage({pluginMessage: {
      type: 'taoran_quick_check_acknowledged', check_id: checkId, feedback_text: finalDisplayText(data.final_feedback_text),
      ...(submitMode ? {submit_confirmed:true} : {}),
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
    // Best-effort failure reporting; a notification outage must not block retry.
    requestJson(base + '/return-failure' + query, {method:'POST'}).catch(() => {});
  }
});

if (versionedMode) recover(); // Poll the retained task; the waiting message is not an AI result.
