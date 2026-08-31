"""Provision only the confirmed, previously absent TAORAN runtime file over SSH.

No keys are printed, written to a local export, passed in argv, or baked into images.
This command refuses to replace an existing remote configuration.
"""
from __future__ import annotations

import json
import subprocess

from taoran_agent.config import get_settings

settings = get_settings()
assert settings.llm_enabled and settings.llm_model == "glm-5.2"
assert settings.tenant_keys.get("tenant_demo")
assert settings.jiandaoyun_api_keys.get("tenant_demo")
assert settings.jiandaoyun_webhook_secret
names = [
    "tenant_keys_json", "jiandaoyun_api_keys_json", "jiandaoyun_webhook_secret",
    "jiandaoyun_base_url", "jiandaoyun_timeout_seconds", "precheck_budget_seconds",
    "llm_enabled", "llm_api_url", "llm_model", "llm_precheck_timeout_seconds",
    "llm_evaluation_timeout_seconds",
    "llm_max_concurrency", "llm_button_queue_capacity", "llm_button_queue_wait_seconds",
    "llm_max_input_chars", "llm_precheck_max_output_tokens", "llm_max_output_tokens",
    "llm_format_retries", "knowledge_snapshot_cache_seconds",
]
values = {name: getattr(settings, name) for name in names}
values["llm_api_key"] = settings.llm_api_key.get_secret_value()
lines = []
for name, value in values.items():
    value = str(value).lower() if isinstance(value, bool) else str(value)
    assert "${" not in value, "dotenv expansion not allowed"
    lines.append(f"DSM_TAORAN_{name.upper()}={json.dumps(value, ensure_ascii=False)}")
payload = ("\n".join(lines)+"\n").encode()
remote = (
    "test -d '/TAORAN agent/runtime' && test ! -L '/TAORAN agent/runtime' && "
    "test ! -e '/TAORAN agent/runtime/agent.env' && "
    "test ! -L '/TAORAN agent/runtime/agent.env' && "
    "umask 077 && install -o 10001 -g 10001 -m 600 /dev/stdin '/TAORAN agent/runtime/agent.env'"
)
result = subprocess.run([
    "ssh", "-i", "/Users/ydzj/.ssh/dsm_aliyun", "-o", "IdentitiesOnly=yes",
    "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=8",
    "root@47.119.191.148", remote,
], input=payload, capture_output=True, timeout=20, check=False)
if result.returncode:
    raise SystemExit("Runtime transfer refused or failed; no secret output was printed.")
print("TAORAN runtime provisioned via SSH; mode600, uid10001; existing keys preserved.")
