// 仅本地测试：用可观测的替身替换 axios，不访问网络。
const fs = require('node:fs');
const path = require('node:path');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const source = fs.readFileSync(path.join(__dirname, '../integrations/jiandaoyun/button_check.js'), 'utf8');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const calls = [];
const fakeAxios = async (options) => {
  calls.push(options);
  if (input.fail) throw new Error('Do not disclose simulated-secret or customer content');
  return input.response || {
    status: 200,
    data: {
      stage: 'pre_submit_advice', official_score_generated: false,
      submission_blocked: false, status: 'passed', feedback_text: '合成规则意见',
      rule_feedback_text: '合成规则意见',
      knowledge_feedback_text: '合成知识库意见',
      knowledge_status: 'passed',
    },
  };
};
new AsyncFunction('agentConf', 'triggerConf', 'require', source)(
  input.config, input.draft,
  (name) => { if (name !== 'axios') throw new Error('Unexpected dependency'); return fakeAxios; },
).then((result) => process.stdout.write(JSON.stringify({result, calls})))
  .catch(() => { process.stderr.write('Plugin test execution failed'); process.exitCode = 1; });
