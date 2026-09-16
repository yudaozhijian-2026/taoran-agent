"""Server-side preparation. Never prints secrets or changes the pilot container."""

import json
import os
import subprocess
from pathlib import Path

ROOT = Path("/TAORAN agent/isolated-submit-test-20260916")
PROD = Path("/TAORAN agent")
TENANT = "tenant_433327714475"
ENTRY = "6a8408b7c5a0d9454090a5bc"
assert not (ROOT / "compose.yaml").exists(), "Candidate already prepared; inspect before retry"
os.umask(0o077)
for name in ["runtime", "data", "logs", "acme/.well-known/acme-challenge"]:
    (ROOT / name).mkdir(parents=True, exist_ok=True)
    os.chown(ROOT / name, 10001, 10001)
registry = json.loads((PROD / "tenant-config/tenant_registry.json").read_text())
tenant = registry["tenants"][TENANT]
source = Path(
    tenant["jiandaoyun"]["mapping_path"].replace(
        "/tenant-config/", str(PROD / "tenant-config") + "/", 1
    )
)
mapping = json.loads(source.read_text())
assert mapping["source_entry_id"] == ENTRY
assert mapping["source_application_id"] == "60fe7ad79ca2d000075dfab1"
secret_lines = (ROOT / "incoming-key.env").read_text().splitlines()
keys = [
    line.split("=", 1)[1].strip()
    for line in secret_lines
    if line.startswith("JIANDAOYUN_TEST_API_KEY=")
]
assert len(keys) == 1 and keys[0]
tenant["jiandaoyun"]["api_key"] = keys[0]
tenant["jiandaoyun"]["mapping_path"] = "/runtime/field_mapping.json"
tenant["display_name"] = "TAORAN独立提交测试"
(ROOT / "runtime/field_mapping.json").write_text(json.dumps(mapping, ensure_ascii=False))
(ROOT / "runtime/tenant_registry.json").write_text(
    json.dumps({"version": 1, "tenants": {TENANT: tenant}}, ensure_ascii=False)
)
# Select model/knowledge settings only, not production DB, administrator or alert settings.
extract = """import json
from taoran_agent.config import get_settings
s=get_settings(); d={}
for k in type(s).model_fields:
 if k.startswith(('llm_','frontend_model_','knowledge_')) and k != 'knowledge_snapshot_path':
  v=getattr(s,k)
  d['DSM_TAORAN_'+k.upper()]=v.get_secret_value() if hasattr(v,'get_secret_value') else v
print(json.dumps(d))"""
cfg = json.loads(
    subprocess.check_output(["docker", "exec", "dsm-taoran-v2-agent", "python", "-c", extract])
)
cfg.update(
    {
        "DSM_TAORAN_ENVIRONMENT": "isolated-submit-test",
        "DSM_TAORAN_DATABASE_PATH": "/data/taoran_agent.db",
        "DSM_TAORAN_TENANT_REGISTRY_PATH": "/runtime/tenant_registry.json",
        "DSM_TAORAN_JIANDAOYUN_MAPPING_PATH": "/runtime/field_mapping.json",
        "DSM_TAORAN_ISOLATED_TEST_ENTRY_ID": ENTRY,
        "DSM_TAORAN_SUBMIT_CONFIRMATION_ENABLED": True,
        "DSM_TAORAN_QUICK_CHECK_INTERACTIVE_ENABLED": True,
        "DSM_TAORAN_FEISHU_ALERTS_ENABLED": False,
        "DSM_TAORAN_ADMIN_ENABLED": False,
        "DSM_TAORAN_ENABLE_Q40_INTEGRATION": False,
        "DSM_TAORAN_LLM_MAX_CONCURRENCY": 2,
        "DSM_TAORAN_LLM_FRONTEND_RESERVED_CONCURRENCY": 1,
    }
)
(ROOT / "runtime/agent.env").write_text(
    "\n".join(k + "=" + json.dumps(v, ensure_ascii=False) for k, v in cfg.items() if v is not None)
    + "\n"
)
for p in (ROOT / "runtime").iterdir():
    os.chown(p, 10001, 10001)
    p.chmod(0o600)
compose = """name: taoran-submit-test
services:
  agent:
    image: taoran-submit-test:1.0.6rc1-20260916
    pull_policy: never
    container_name: taoran-submit-test-agent
    restart: unless-stopped
    command: [uvicorn, "taoran_agent.api:app", --host, "0.0.0.0", --port, "8030", --workers, "1", --no-access-log]
    user: "10001:10001"
    init: true
    read_only: true
    cpus: 0.5
    mem_limit: 768m
    pids_limit: 128
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    ports: ["127.0.0.1:8031:8030"]
    environment:
      DSM_TAORAN_ENVIRONMENT: isolated-submit-test
      DSM_TAORAN_DATABASE_PATH: /data/taoran_agent.db
    volumes:
      - "./runtime/agent.env:/app/.env:ro"
      - "./runtime:/runtime:ro"
      - "./data:/data"
    tmpfs: ["/tmp:rw,noexec,nosuid,size=64m,uid=10001,gid=10001,mode=1770"]
    logging:
      driver: json-file
      options: {max-size: "5m", max-file: "2"}
    healthcheck:
      test: [CMD, python, -c, "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8030/health', timeout=3).close()"]
      interval: 20s
      timeout: 5s
      retries: 3
      start_period: 30s
"""
(ROOT / "compose.yaml").write_text(compose)
print("Prepared isolated configuration for test form only; no production data copied.")
