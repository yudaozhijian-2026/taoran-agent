"""Validate the isolated submit-test plugin artifacts.

The submit-test frontend has a different interaction contract from the pilot
button plugin, so it is maintained as an explicit test-only artifact.  This
guard intentionally never rewrites the pilot templates.
"""

from pathlib import Path

ROOT = Path(__file__).parent
FRONTEND = ROOT / "submit_test" / "frontend.js"
LAUNCHER = ROOT / "submit_test" / "launcher.js"


def main() -> None:
    front = FRONTEND.read_text()
    launcher = LAUNCHER.read_text()
    for marker in (
        "taoran_submit_cancelled",
        "taoran_submit_retry",
        "taoran_submit_bypassed",
        "do {",
        "submit_decision: '已确认提交'",
        "decisionDeadlineMs = 50000",
    ):
        assert marker in front, f"submit-test frontend is missing {marker!r}"
    for marker in (
        "https://taoran-test.yudaozhijian.top/api/v1/quick-check/tasks",
        "https://taoran-test.yudaozhijian.top",
        "tenant_433327714475",
    ):
        assert marker in launcher, f"submit-test launcher is missing {marker!r}"
    assert "https://taoran.yudaozhijian.top/api/v1/quick-check/tasks" not in launcher
    print("submit-test plugin artifacts are isolated and internally consistent")


if __name__ == "__main__":
    main()
