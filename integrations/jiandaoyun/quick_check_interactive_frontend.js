// 简道云自建插件「前端扩展」：内容缓存版 Quick Check 交互入口。
// 由候选按钮调用；它不保存或提交表单。
const draft = triggerConf || {};
if (draft.snapshot_contract && draft.snapshot_contract !== 'experimental_current_page_v1') {
  throw new Error('页面快照协议配置不正确，未改用已保存记录。');
}
// 前端扩展无法读取通用参数；此 ID 仅指向同一候选插件的后端启动函数。
const backendFunctionId = 'func.6a9addad68630540c8908ec9';

const allowedFields = [
  'visit_date', 'employee_id', 'customer_id', 'customer_type_ii', 'opportunity_id', 'opportunity_stage',
  'visit_method', 'is_appointment', 'purpose_code', 'other_purpose', 'expected_key_result',
  'process_description', 'customer_feedback', 'self_assessment', 'deviation_reason',
  'next_action_purpose', 'next_action_other_purpose', 'next_action_expected_result', 'next_contact_at',
  'actual_start_at', 'actual_end_at', 'duration_minutes', 'evidence_ids', 'participants', 'opportunities',
  'visit_record_code', 'data_id', 'user_id',
];
const taskInput = {};
for (const field of allowedFields) {
  if (Object.prototype.hasOwnProperty.call(draft, field)) taskInput[field] = draft[field];
}
// 新建记录允许编码为空；内容指纹由已授权服务端计算。


// experimental: enable only after every page binding has been verified.
if (draft.snapshot_contract === "experimental_current_page_v1") {
  const fields = ["visit_date","employee_id","customer_id","customer_type_ii","visit_method","is_appointment","purpose_code","other_purpose","expected_key_result","process_description","self_assessment","next_action_purpose","next_action_other_purpose","next_action_expected_result","next_contact_at","actual_start_at","actual_end_at","duration_minutes","evidence_ids","participants","opportunities"];
  const page = {};
  for (const field of fields) page[field] = draft[field] === undefined ? null : draft[field];
  taskInput.snapshot_mode = "experimental_current_page_v1";
  taskInput.page_snapshot_json = JSON.stringify(page);
}
let pendingCheckId = '';
let pendingOpeningId = '';
let pendingInputHash = '';
let returnedFeedback = '';
let acceptingFeedback = false;

$g.ui.onmessage = (message) => {
  if (!acceptingFeedback || returnedFeedback) return;
  const payload = message && typeof message === 'object' && message.pluginMessage
    ? message.pluginMessage
    : message;
  if (!payload || payload.type !== 'taoran_quick_check_acknowledged') return;
  if (payload.check_id !== pendingCheckId) return;
  if (payload.opening_id !== pendingOpeningId || payload.input_hash !== pendingInputHash) return;
  if (typeof payload.feedback_text !== 'string' || !payload.feedback_text.trim() || payload.feedback_text.length > 20000) return;
  returnedFeedback = payload.feedback_text;
  acceptingFeedback = false;
  $g.utils.closeModal();
};

const callResult = await $g.utils.callFunction({ name: backendFunctionId, data: taskInput });
const launch = callResult && callResult.result && typeof callResult.result === 'object' ? callResult.result : callResult;
if (!launch || typeof launch.quick_check_id !== 'string' || typeof launch.quick_check_launch_url !== 'string' || !launch.quick_check_id || !launch.quick_check_launch_url) {
  const reason = launch && typeof launch.quick_check_message === 'string'
    ? launch.quick_check_message.trim()
    : '';
  throw new Error(reason || 'AI检查未能启动：候选调用未返回任务（返回字段：' + (launch && typeof launch === 'object' ? Object.keys(launch).join(',') : typeof launch) + '）。');
}
const launchUrl = new URL(launch.quick_check_launch_url);
pendingOpeningId = launchUrl.searchParams.get('opening_id') || '';
pendingInputHash = launchUrl.searchParams.get('input_hash') || '';
if (!/^[A-Za-z0-9_-]{32}$/.test(pendingOpeningId) || !/^[a-f0-9]{64}$/.test(pendingInputHash)) {
  throw new Error('AI检查插件与服务版本不一致，请管理员同步更新。');
}
pendingCheckId = launch.quick_check_id;
acceptingFeedback = true;
try {
  await $g.utils.openModal({ title: 'AI检查', url: launch.quick_check_launch_url });
} finally {
  acceptingFeedback = false;
}

if (!returnedFeedback) {
  throw new Error('本次AI检查未返回反馈。可关闭后重新点击AI检查。');
}

return { resText: returnedFeedback, quick_check_id: pendingCheckId };
