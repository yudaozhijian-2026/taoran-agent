"""Deploy only the isolated TAORAN submit-test container."""

import hashlib
import json
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime

ROOT = pathlib.Path("/TAORAN agent/isolated-submit-test-20260916")
RELEASE_NAME = "1.0.6rc61-20260921"
RELEASE = ROOT / "releases" / RELEASE_NAME
BACKUP = ROOT / "backups" / ("before-" + RELEASE_NAME + "-front-wording-v2")
COMPOSE = ROOT / "compose.yaml"
DATABASE = ROOT / "data" / "taoran_agent.db"
CONTAINER = "taoran-submit-test-agent"
OLD_IMAGE = "taoran-submit-test:1.0.6rc60-20260921"
OLD_REVISION = "f293d87"
NEW_IMAGE = "taoran-submit-test:1.0.6rc61-20260921"
NEW_REVISION = sys.argv[2]


def run(args, *, cwd=None):
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def inspect_container(name):
    return json.loads(run(["docker", "inspect", name]))[0]


def active_jobs():
    connection = sqlite3.connect("file:" + str(DATABASE) + "?mode=ro", uri=True)
    assert connection.execute("pragma integrity_check").fetchone()[0] == "ok"
    total = 0
    for table in ("evaluation_jobs", "quick_check_tasks"):
        columns = {
            row[1] for row in connection.execute("pragma table_info(" + table + ")")
        }
        if "status" not in columns:
            continue
        query = (
            "select count(*) from " + table
            + " where status in ('queued','running','processing','pending')"
        )
        total += connection.execute(query).fetchone()[0]
    rows = connection.execute("select payload_json from quick_check_recovery").fetchall()
    total += sum(json.loads(row[0]).get("status") in {"processing", "running", "pending"} for row in rows)
    connection.close()
    return total


def container_snapshot():
    names = run(["docker", "ps", "-a", "--format", "{{.Names}}"] ).splitlines()
    snapshot = {}
    for name in names:
        info = inspect_container(name)
        snapshot[name] = {
            "image": info["Config"]["Image"],
            "image_id": info["Image"],
            "started_at": info["State"]["StartedAt"],
            "restart_count": info["RestartCount"],
            "running": info["State"]["Running"],
        }
    return snapshot


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def assert_expected_current():
    info = inspect_container(CONTAINER)
    assert info["Config"]["Image"] == OLD_IMAGE, info["Config"]["Image"]
    revision = info["Config"]["Labels"].get("org.opencontainers.image.revision", "")
    assert revision.startswith(OLD_REVISION), revision
    assert info["State"]["Running"]
    assert info["State"]["Health"]["Status"] == "healthy"
    assert active_jobs() == 0
    return info


def preflight():
    RELEASE.mkdir(parents=True, exist_ok=True)
    info = assert_expected_current()
    snapshot = container_snapshot()
    (RELEASE / "containers-before.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2)
    )
    report = {
        "old_image": info["Config"]["Image"],
        "old_image_id": info["Image"],
        "old_revision": info["Config"]["Labels"].get(
            "org.opencontainers.image.revision"
        ),
        "active_jobs": 0,
        "database_integrity": "ok",
    }
    (RELEASE / "preflight.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2)
    )
    print(json.dumps(report, ensure_ascii=False))


def switch():
    current = assert_expected_current()
    subprocess.check_call([
        "docker", "build", "--pull=false",
        "--label", "org.opencontainers.image.version=1.0.6rc61",
        "--label", "org.opencontainers.image.revision=" + NEW_REVISION,
        "-t", NEW_IMAGE, ".",
    ], cwd=RELEASE)
    # Re-check after the potentially long build so a newer session cannot be overwritten.
    assert_expected_current()
    BACKUP.mkdir(parents=True, exist_ok=False)
    shutil.copy2(COMPOSE, BACKUP / "compose.yaml")
    shutil.copytree(ROOT / "runtime", BACKUP / "runtime")
    source = sqlite3.connect(str(DATABASE))
    target = sqlite3.connect(str(BACKUP / "taoran_agent.db"))
    source.backup(target)
    target.close()
    source.close()
    old_text = COMPOSE.read_text()
    assert old_text.count(OLD_IMAGE) == 1
    new_text = old_text.replace(OLD_IMAGE, NEW_IMAGE)
    temporary = COMPOSE.with_suffix(".yaml.rc61-new")
    temporary.write_text(new_text)
    temporary.replace(COMPOSE)
    try:
        subprocess.check_call([
            "docker", "compose", "-f", str(COMPOSE),
            "up", "-d", "--no-deps", "--pull", "never", "agent",
        ], cwd=ROOT)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            info = inspect_container(CONTAINER)
            health = (info["State"].get("Health") or {}).get("Status")
            if info["State"]["Running"] and health == "healthy":
                break
            time.sleep(2)
        else:
            raise RuntimeError("isolated_container_not_healthy")
    except Exception:
        shutil.copy2(BACKUP / "compose.yaml", COMPOSE)
        subprocess.call([
            "docker", "compose", "-f", str(COMPOSE),
            "up", "-d", "--no-deps", "--pull", "never", "agent",
        ], cwd=ROOT)
        raise
    rollback = BACKUP / "rollback.sh"
    rollback.write_text(
        "#!/bin/sh\nset -eu\n"
        "cp \"$(dirname \"$0\")/compose.yaml\" "
        "'/TAORAN agent/isolated-submit-test-20260916/compose.yaml'\n"
        "cd '/TAORAN agent/isolated-submit-test-20260916'\n"
        "docker compose -f compose.yaml up -d --no-deps --pull never agent\n"
    )
    rollback.chmod(0o700)
    print(json.dumps({
        "backup": str(BACKUP),
        "old_image_id": current["Image"],
    }, ensure_ascii=False))


def verify():
    info = inspect_container(CONTAINER)
    assert info["Config"]["Image"] == NEW_IMAGE
    assert info["Config"]["Labels"].get(
        "org.opencontainers.image.revision"
    ) == NEW_REVISION
    assert info["State"]["Health"]["Status"] == "healthy"
    assert info["RestartCount"] == 0
    assert active_jobs() == 0
    with urllib.request.urlopen("http://127.0.0.1:8031/health", timeout=10) as response:
        internal = json.loads(response.read())
    with urllib.request.urlopen(
        "https://taoran-test.yudaozhijian.top/health", timeout=15
    ) as response:
        external = json.loads(response.read())
    assert internal["release_version"] == "1.0.6rc61"
    assert external["release_version"] == "1.0.6rc61"
    before = json.loads((RELEASE / "containers-before.json").read_text())
    after = container_snapshot()
    for name, old in before.items():
        if name == CONTAINER:
            continue
        now = after[name]
        assert now == old, (name, old, now)
    manifest = {
        "release_version": "1.0.6rc61",
        "git_commit": NEW_REVISION,
        "git_tag": "submit-test-v1.0.6rc61-20260921",
        "image": NEW_IMAGE,
        "image_id": info["Image"],
        "container": CONTAINER,
        "health_internal": internal["status"],
        "health_external": external["status"],
        "database_integrity": "ok",
        "active_jobs": 0,
        "restart_count": info["RestartCount"],
        "agent_env_sha256": sha256(ROOT / "runtime" / "agent.env"),
        "field_mapping_sha256": sha256(ROOT / "runtime" / "field_mapping.json"),
        "tenant_registry_sha256": sha256(ROOT / "runtime" / "tenant_registry.json"),
        "other_containers_unchanged": True,
        "production_container_unchanged": True,
        "rollback_path": str(BACKUP),
        "verified_at": datetime.now(UTC).isoformat(),
    }
    (RELEASE / "deployment.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2)
    )
    print(json.dumps(manifest, ensure_ascii=False))


mode = sys.argv[1]
{"preflight": preflight, "switch": switch, "verify": verify}[mode]()
