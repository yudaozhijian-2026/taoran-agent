// TAORAN AI检查：当前页面快照提交到已部署主程序。
const axios = require('axios');
const config = agentConf || {};
const draft = triggerConf || {};
const unavailable = (message) => ({ quick_check_status: 'unavailable', quick_check_id: '', quick_check_launch_url: '', quick_check_message: 'AI检查未能启动：' + message });
const tenant = String(config.tenant_id || config._widget_17885347802671 || '').trim();
const apiKey = String(config.api_key || '').trim();
const endpointUrl = String(config.endpoint_url || config._widget_17885347883461 || '').trim();
const publicBase = String(config.public_base_url || config._widget_17885347889461 || '').trim().replace(/[/]$/, '');
if (!tenant || !apiKey || !endpointUrl || !publicBase) return unavailable('服务配置不完整。');
let endpoint, launchBase;
try { endpoint = new URL(endpointUrl); launchBase = new URL(publicBase); } catch (_) { return unavailable('服务地址配置不正确。'); }
if (endpoint.protocol !== 'https:' || endpoint.username || endpoint.password || endpoint.search || endpoint.hash || endpoint.pathname !== '/api/v1/quick-check/tasks' || launchBase.protocol !== 'https:' || launchBase.username || launchBase.password || launchBase.search || launchBase.hash || endpoint.origin !== launchBase.origin) return unavailable('服务地址或授权配置不正确。');
const recordCode = String(draft.visit_record_code || '').trim();

if (draft.snapshot_mode !== 'experimental_current_page_v1') return unavailable('页面快照协议不匹配。');
let snapshot = {};
if (!recordCode) {
try { snapshot = JSON.parse(draft.page_snapshot_json); } catch (_) { return unavailable('完整页面快照格式错误。'); }
const fields = ['visit_date','employee_id','customer_id','customer_type_ii','visit_method','is_appointment','purpose_code','other_purpose','expected_key_result','process_description','self_assessment','next_action_purpose','next_action_other_purpose','next_action_expected_result','next_contact_at','actual_start_at','actual_end_at','duration_minutes','evidence_ids','participants','opportunities'];
if (!snapshot || typeof snapshot !== 'object' || Array.isArray(snapshot) || Object.keys(snapshot).length !== fields.length || fields.some(f => !Object.prototype.hasOwnProperty.call(snapshot,f))) return unavailable('当前页面快照不完整。');
try {
  for (const field of ['employee_id','evidence_ids','participants','opportunities']) {
    const value = snapshot[field];
    if (typeof value === 'string' && /^[\s]*[\[{]/.test(value)) snapshot[field] = JSON.parse(value);
  }
  for (const field of ['participants','opportunities']) {
    if (snapshot[field] === null || snapshot[field] === '') snapshot[field] = [];
    if (!Array.isArray(snapshot[field]) || snapshot[field].some(row => !row || typeof row !== 'object' || Array.isArray(row))) return unavailable('页面子表格式不正确。');
  }
} catch (_) { return unavailable('页面复杂字段格式不正确。'); }
}
const body = {record_code: recordCode, user_id: String(draft.user_id || 'jiandaoyun-user'), form_snapshot: snapshot};
// 编码仅在提交后生成：已提交记录手动检测读取服务端保存值。
if (recordCode) body.saved_record_check = true;
if (draft.data_id) body.data_id = String(draft.data_id);
try {
  const response = await axios({method: 'post', url: endpoint.toString(), headers: {'Content-Type':'application/json','X-Tenant-Id':tenant,'X-API-Key':apiKey}, data:body, timeout:10000, maxRedirects:0, validateStatus:status=>status===202});
  const task = response.data;
  if (!task || typeof task.check_id !== 'string' || typeof task.stream_token !== 'string' || !/^[A-Za-z0-9_-]{32}$/.test(task.opening_id || '') || !/^[a-f0-9]{64}$/.test(task.input_hash || '')) return unavailable('服务未返回有效检查任务。');
  return {quick_check_status:task.status, quick_check_id:task.check_id, quick_check_launch_url:publicBase + '/quick-check/interactive?check_id=' + encodeURIComponent(task.check_id) + '&stream_token=' + encodeURIComponent(task.stream_token) + '&opening_id=' + encodeURIComponent(task.opening_id) + '&input_hash=' + encodeURIComponent(task.input_hash)};
} catch (_) { return unavailable('服务连接失败或页面快照未被接受，请检查配置后重试。'); }
