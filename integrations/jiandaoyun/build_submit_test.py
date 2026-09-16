"""Generate test-only plugin variants without altering the pilot templates."""

from pathlib import Path

ROOT = Path(__file__).parent


def replace_once(text, before, after):
    assert text.count(before) == 1, before
    return text.replace(before, after, 1)


front = (ROOT / "quick_check_interactive_frontend.js").read_text()
front = replace_once(front,
    "if (!payload || payload.type !== 'taoran_quick_check_acknowledged') return;",
    "if (!payload || !['taoran_quick_check_acknowledged','taoran_submit_cancelled'].includes(payload.type)) return;")
front = replace_once(front,
    "  if (typeof payload.feedback_text !== 'string'",
    "  if (payload.type === 'taoran_submit_cancelled') { acceptingFeedback = false; $g.utils.closeModal(); return; }\n"
    "  if (payload.submit_confirmed !== true) return;\n"
    "  if (typeof payload.feedback_text !== 'string'")
front = replace_once(front, "title: 'AI检查'", "title: 'AI检查 · 提交前确认'")
front = replace_once(front,
    "return { resText: draft.existing_feedback == null ? '' : draft.existing_feedback };",
    "return { resText: draft.existing_feedback == null ? '' : draft.existing_feedback, submit_decision: '返回修改' };")
front = replace_once(front,
    "return { resText: returnedFeedback, quick_check_id: pendingCheckId };",
    "return { resText: returnedFeedback, quick_check_id: pendingCheckId, submit_decision: '已确认提交' };")
back = (ROOT / "quick_check_interactive_launcher.js").read_text()
lines = back.splitlines(keepends=True)
overrides = {
    "const tenant =": "const tenant = 'tenant_433327714475';\n",
    "const endpointUrl =": "const endpointUrl = 'https://taoran-test.yudaozhijian.top/api/v1/quick-check/tasks';\n",
    "const publicBase =": "const publicBase = 'https://taoran-test.yudaozhijian.top';\n",
}
for prefix, replacement in overrides.items():
    assert sum(line.startswith(prefix) for line in lines) == 1
    lines = [replacement if line.startswith(prefix) else line for line in lines]
back = ''.join(lines)
destination = ROOT / 'submit_test'
destination.mkdir(exist_ok=True)
(destination / 'frontend.js').write_text(front)
(destination / 'launcher.js').write_text(back)
