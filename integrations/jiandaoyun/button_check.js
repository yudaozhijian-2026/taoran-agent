// 简道云自建插件：后端函数 / Node.js 20。将本文件全文粘贴到代码编辑器。
// 配置见项目根目录《过程详细描述传参接入.md》。仅传递草稿，不读写业务记录。
const axios = require('axios');
const feedback = (message) => ({
  feedback_text: `【提交前TAORAN检查】\n本次检测未完成：${message}\n请重新点击“AI检测”，不要将上一次意见作为本次检查结果。`,
  check_status: 'unavailable',
});
const config = agentConf || {};
const draft = triggerConf || {};
const tenant = typeof config.tenant_id === 'string' ? config.tenant_id.trim() : '';
const key = typeof config.api_key === 'string' ? config.api_key.trim() : '';
let endpoint;
try {
  endpoint = new URL(config.endpoint_url);
} catch (_) {
  return feedback('服务地址配置不正确，请联系管理员。');
}
if (endpoint.protocol !== 'https:' || endpoint.username || endpoint.password ||
    endpoint.search || endpoint.hash ||
    endpoint.pathname !== '/api/v1/connectors/jiandaoyun/visit/button-check' ||
    !tenant || !key) {
  return feedback('服务地址或授权配置不正确，请联系管理员。');
}

// 未配置字段与用户主动清空必须区分；不能用已保存记录或默认文字补齐。
if (!Object.prototype.hasOwnProperty.call(draft, 'process_description') ||
    draft.process_description === undefined) {
  return feedback('未获取当前表单的“过程详细描述”，请管理员检查插件字段绑定。');
}
if (draft.process_description !== null && typeof draft.process_description !== 'string') {
  return feedback('“过程详细描述”的传递格式异常，请管理员将其绑定为文本字段值。');
}

const fields = [
  'visit_date', 'employee_id', 'customer_id', 'customer_type_ii',
  'visit_method', 'is_appointment', 'purpose_code', 'other_purpose',
  'expected_key_result', 'process_description', 'self_assessment',
  'next_action_purpose', 'next_action_other_purpose', 'next_action_expected_result',
  'next_contact_at', 'actual_start_at', 'actual_end_at', 'duration_minutes', 'evidence_ids',
];
const payload = { tenant_id: tenant };
const dateFields = new Set(['visit_date', 'actual_start_at', 'actual_end_at', 'next_contact_at']);
for (const field of fields) {
  if (Object.prototype.hasOwnProperty.call(draft, field)) {
    // 已绑定的空日期可能为 undefined；保留为 null，不把它误报为接口漏传。
    if (draft[field] !== undefined || dateFields.has(field)) {
      payload[field] = draft[field] === undefined ? null : draft[field];
    }
  }
}
// 通讯录字段经插件文本参数传入 JSON 数组；只转发稳定编号，不转发姓名。
try {
  let member = payload.employee_id;
  if (typeof member === 'string' && /^[\[{]/.test(member.trim())) member = JSON.parse(member);
  if (Array.isArray(member)) {
    if (member.length > 1) throw new Error('Expected single member');
    member = member[0] ?? null;
  }
  if (member && typeof member === 'object') {
    member = member.username ?? member.user_id ?? member.id ?? member._id;
    if (typeof member !== 'string' || !member.trim()) throw new Error('Missing member ID');
  }
  if (Object.prototype.hasOwnProperty.call(payload, 'employee_id')) payload.employee_id = member;
} catch (_) {
  return feedback('“销售代表（通讯录）”的成员编号传递异常，请管理员检查插件字段绑定。');
}
// 简道云空文本可能为 null；原文不 trim、不截断、不替换换行。
payload.process_description = draft.process_description === null ? '' : draft.process_description;
// 子表允许数组或JSON数组文本；只转发已核实映射的必要字段，不传姓名、电话和邮箱。
const subforms = {
  participants: {
    label: '联系人信息',
    children: { contact_id: ['_widget_1416718540131', '关联数据-主键', 'contact_id'] },
  },
  opportunities: {
    label: '关联商机阶段信息',
    children: {
      opportunity_id: ['_widget_1785314290802', '商机编号', 'opportunity_id'],
      historical_stage: ['_widget_1785314290798', '历史商机阶段', 'historical_stage'],
      current_stage: ['_widget_1785314290799', '最新商机阶段', 'current_stage'],
    },
  },
};
for (const [field, spec] of Object.entries(subforms)) {
  if (!Object.prototype.hasOwnProperty.call(draft, field) || draft[field] === undefined) continue;
  try {
    let rows = draft[field];
    if (typeof rows === 'string' && rows.trim()) rows = JSON.parse(rows);
    if (rows === null || rows === '') rows = [];
    if (!Array.isArray(rows)) throw new Error('Expected row array');
    payload[field] = rows.map((row) => {
      if (!row || typeof row !== 'object' || Array.isArray(row)) throw new Error('Invalid row');
      const selected = {};
      for (const [child, aliases] of Object.entries(spec.children)) {
        const name = aliases.find(name => Object.prototype.hasOwnProperty.call(row, name));
        if (name !== undefined) selected[child] = row[name];
      }
      if (Object.keys(row).length && !Object.keys(selected).length) throw new Error('Unmapped row');
      return selected;
    });
  } catch (_) {
    return feedback(`“${spec.label}”的传递格式或子字段绑定异常，请管理员核对；不能将其当成未填写。`);
  }
}
// 插件“文本”参数引用附件时可能得到 JSON 数组字符串，还原后交给原连接器。
if (typeof payload.evidence_ids === 'string' && payload.evidence_ids.trim().startsWith('[')) {
  try {
    const attachments = JSON.parse(payload.evidence_ids);
    if (!Array.isArray(attachments)) throw new Error('Invalid attachment list');
    payload.evidence_ids = attachments;
  } catch (_) {
    return feedback('“上传相关文件”的传递格式异常，请管理员检查插件字段绑定。');
  }
}

try {
  const response = await axios({
    method: 'post',
    url: endpoint.toString(),
    headers: { 'Content-Type': 'application/json', 'X-Tenant-Id': tenant, 'X-API-Key': key },
    data: payload,
    timeout: 12000,
    maxRedirects: 0, // 防止带密钥的请求跟随重定向到其他地址。
    maxBodyLength: 262144,
    maxContentLength: 262144,
    validateStatus: (status) => status === 200,
  });
  const result = response.data;
  if (response.status !== 200 || !result || result.stage !== 'pre_submit_advice' ||
      result.official_score_generated !== false || result.submission_blocked !== false ||
      !['passed', 'needs_revision', 'review'].includes(result.status) ||
      typeof result.feedback_text !== 'string' || !result.feedback_text.trim()) {
    return feedback('服务未返回有效的本次检查意见，请稍后重试。');
  }
  // 不返回分数、知识依据、分析方式、密钥或客户原始字段。
  return { feedback_text: result.feedback_text, check_status: result.status };
} catch (_) {
  // 不回显错误对象：HTTP错误中可能包含请求头和完整拜访内容。
  return feedback('服务连接失败、超时或请求未被接受，请稍后重试；持续失败请联系管理员。');
}
