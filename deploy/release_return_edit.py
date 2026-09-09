"""TAORAN reliable analysis and delivery release guard; run on the server with preflight/switch/verify.

Refuses changed server revisions, active jobs, changed data/configuration, and
changes to other containers. Never stops or restarts another service.
"""

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

BASE = Path("/TAORAN agent")
VERSION = "1.0.1-return-edit-20260909"
OLD = "1.0.0-taoran-v1-20260909"
EXPECTED_IMAGE = "sha256:0f4769e290653d594331ef84722ac98f6d8bc5099200171d7131350e4eeabed5"
RELEASE = BASE / "releases" / VERSION
BACKUP = BASE / "backups" / ("before-" + VERSION)
CONTAINER = "dsm-taoran-v2-agent"
DB = BASE / "data/taoran_agent.db"


def run(args, **kwargs):
    return subprocess.check_output(args, **kwargs).decode().strip()


def save(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2))


def inspect():
    return json.loads(run(["docker", "inspect", CONTAINER]))[0]


def health():
    with urllib.request.urlopen("http://127.0.0.1:8030/health", timeout=10) as r:
        return json.load(r)


def others():
    ids = run(["docker", "ps", "-aq"]).split()
    return {
        v["Name"]: {
            "id": v["Id"],
            "image": v["Image"],
            "started": v["State"]["StartedAt"],
            "status": v["State"]["Status"],
            "restarts": v["RestartCount"],
        }
        for v in json.loads(run(["docker", "inspect", *ids]))
        if v["Name"] != "/" + CONTAINER
    }


def config_hashes():
    return {
        str(p.relative_to(BASE)): hashlib.sha256(p.read_bytes()).hexdigest()
        for directory in ["runtime", "tenant-config"]
        for p in (BASE / directory).rglob("*")
        if p.is_file()
    }


def database(backup=False):
    with sqlite3.connect("file:" + str(DB) + "?mode=ro", uri=True) as conn:
        conn.execute("BEGIN")
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        assert integrity == "ok", integrity
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        counts = {}
        digests = {}
        active = {}
        for table in tables:
            assert re.fullmatch(r"[a-zA-Z0-9_]+", table)
            rows = conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
            counts[table] = len(rows)
            digests[table] = hashlib.sha256(
                json.dumps(rows, ensure_ascii=False, default=str).encode()
            ).hexdigest()
            columns = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
            if table == "quick_check_recovery":
                active[table] = sum(
                    json.loads(r[columns.index("payload_json")]).get("status")
                    in ("queued", "running", "processing", "pending")
                    for r in rows
                )
            if "status" in columns:
                active[table] = conn.execute(
                    f"SELECT count(*) FROM \"{table}\" WHERE status IN ('queued','running','processing','pending')"
                ).fetchone()[0]
        if backup:
            with sqlite3.connect(BACKUP / "taoran_agent.db") as dst:
                conn.backup(dst)
                assert dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        return {"integrity": integrity, "counts": counts, "digests": digests, "active": active}


def activity():
    h = health()
    cap = h["monitoring"].get("model_capacity", {})
    assert cap.get("active_frontend") == 0 and cap.get("active_backend") == 0, (
        "Model requests active"
    )
    db = database()
    assert not any(db["active"].values()), "Database jobs active"
    # Also check all established sockets in the container namespace, excluding our completed health call.
    pid = inspect()["State"]["Pid"]
    established = []
    for family in ["tcp", "tcp6"]:
        for row in Path(f"/proc/{pid}/net/{family}").read_text().splitlines()[1:]:
            cols = row.split()
            if cols[3] == "01":
                established.append(
                    {
                        "local_port": int(cols[1].split(":")[1], 16),
                        "remote_port": int(cols[2].split(":")[1], 16),
                    }
                )
    assert not established, "Established service/provider sockets need idle verification"
    return {"model_capacity": cap, "database": db, "established_sockets": len(established)}


def verify_source(manifest):
    code = """import json,hashlib,sys;from pathlib import Path;import taoran_agent
root=Path(taoran_agent.__file__).parent
expected=json.load(sys.stdin)
bad=[name for name,digest in expected.items() if not (root/name).is_file() or hashlib.sha256((root/name).read_bytes()).hexdigest()!=digest]
print(json.dumps({'files':len(expected),'mismatches':bad}))
"""
    result = json.loads(
        run(["docker", "exec", "-i", CONTAINER, "python", "-c", code], input=manifest.read_bytes())
    )
    assert not result["mismatches"], result
    return result


def main(mode):
    os.umask(0o077)
    if mode in {"preflight", "switch"}:
        assert inspect()["Config"]["Labels"].get("org.opencontainers.image.revision") == (
            "2a958dbe19deefa21f571b4b2c79e4a298ae1017"
        ), "Server deployment commit changed; synchronize before deployment"
    if mode == "preflight":
        assert inspect()["Config"]["Image"] == "dsm-taoran-v2:" + OLD
        assert inspect()["Image"] == EXPECTED_IMAGE, (
            "Server image changed; synchronize before deployment"
        )
        assert (BASE / "current").resolve() == BASE / "releases" / OLD, "Server release changed"
        assert (RELEASE / "git-commit.txt").read_text().strip() == (
            RELEASE / "remote-main.txt"
        ).read_text().strip(), "Candidate is not remote main"
        assert health()["release_version"] == "1.0.0"
        baseline = verify_source(RELEASE / "baseline-manifest.json")
        idle = activity()
        BACKUP.mkdir(mode=0o700, exist_ok=False)
        save(BACKUP / "activity-before.json", idle)
        save(BACKUP / "database-before.json", database(backup=True))
        save(BACKUP / "containers-before.json", others())
        save(BACKUP / "configuration-hashes.json", config_hashes())
        save(BACKUP / "container-before.json", inspect())
        save(BACKUP / "health-before.json", health())
        (BACKUP / "previous-current.txt").write_text(str((BASE / "current").resolve()))
        compose = Path(inspect()["Config"]["Labels"]["com.docker.compose.project.config_files"])
        (BACKUP / "compose.rollback.yaml").write_bytes(compose.read_bytes())
        (BACKUP / "agent.env").write_bytes((BASE / "runtime/agent.env").read_bytes())
        with tarfile.open(BACKUP / "runtime-tenant-config.tar.gz", "w:gz") as archive:
            for name in ["runtime", "tenant-config", "deploy"]:
                archive.add(BASE / name, arcname=name)
        with open(BACKUP / "previous-image.tar", "wb") as f:
            subprocess.run(
                ["docker", "image", "save", "dsm-taoran-v2:" + OLD], stdout=f, check=True
            )
        (BACKUP / "rollback.sh").write_text(
            "#!/bin/sh\nset -eu\ndocker image inspect dsm-taoran-v2:"
            + OLD
            + ' >/dev/null 2>&1 || docker image load -i "'
            + str(BACKUP / "previous-image.tar")
            + '"\ndocker compose -p dsm-taoran-v2 -f "'
            + str(BACKUP / "compose.rollback.yaml")
            + '" up -d --no-deps --pull never agent\nln -sfn "'
            + str((BASE / "current").resolve())
            + '" "/TAORAN agent/current"\n'
        )
        rollback = BACKUP / "rollback.sh"
        rollback.write_text(rollback.read_text().replace(
            'set -eu\n', 'set -eu\ncp "' + str(BACKUP / 'agent.env')
            + '" "' + str(BASE / 'runtime/agent.env') + '"\n'))
        (BACKUP / "rollback.sh").chmod(0o700)
        save(
            BACKUP / "backup-sha256.json",
            {
                p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in BACKUP.iterdir()
                if p.is_file()
            },
        )
        print(
            json.dumps(
                {"baseline_source": baseline, "activity": idle, "backup": str(BACKUP)},
                ensure_ascii=False,
            )
        )
    elif mode == "switch":
        assert inspect()["Config"]["Image"] == "dsm-taoran-v2:" + OLD
        assert inspect()["Image"] == EXPECTED_IMAGE, (
            "Server image changed; synchronize before deployment"
        )
        assert (BASE / "current").resolve() == BASE / "releases" / OLD, "Server release changed"
        assert (RELEASE / "git-commit.txt").read_text().strip() == (
            RELEASE / "remote-main.txt"
        ).read_text().strip(), "Candidate is not remote main"
        idle = activity()
        save(BACKUP / "activity-at-switch.json", idle)
        assert others() == json.loads((BACKUP / "containers-before.json").read_text())
        assert config_hashes() == json.loads((BACKUP / "configuration-hashes.json").read_text())
        assert idle["database"] == json.loads((BACKUP / "database-before.json").read_text()), (
            "Database changed; refresh consistent backup before switch"
        )
        save(RELEASE / "expected-configuration-hashes.json", config_hashes())
        subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                "dsm-taoran-v2",
                "-f",
                str(RELEASE / "compose.server.yaml"),
                "up",
                "-d",
                "--no-deps",
                "--pull",
                "never",
                "agent",
            ],
            check=True,
        )
        print("TAORAN-only replacement started")
    elif mode == "verify":
        h = health()
        assert h["release_version"] == "1.0.1" and h["prewarm"]["status"] == "ready"
        assert inspect()["State"]["Health"]["Status"] == "healthy"
        assert inspect()["Config"]["Image"] == "dsm-taoran-v2:" + VERSION
        assert inspect()["RestartCount"] == 0, "Unexpected restarts"
        commit = (RELEASE / "git-commit.txt").read_text().strip()
        assert inspect()["Config"]["Labels"]["org.opencontainers.image.revision"] == commit
        with urllib.request.urlopen(
            "https://taoran.yudaozhijian.top/health", timeout=20
        ) as response:
            external = json.load(response)
        assert external["release_version"] == h["release_version"] and external["status"] == "ok"
        pages = {}
        for origin in ["http://127.0.0.1:8030", "https://taoran.yudaozhijian.top"]:
            for path in ["/admin/tenants", "/admin/assets/admin.js", "/admin/assets/admin.css"]:
                with urllib.request.urlopen(origin + path, timeout=20) as response:
                    content = response.read()
                    assert response.status == 200 and content
                    pages[origin + path] = {
                        "status": response.status,
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
        source = verify_source(RELEASE / "source-manifest.json")
        db = database()
        before = json.loads((BACKUP / "database-before.json").read_text())
        assert db["integrity"] == "ok"
        for field in ("counts", "digests", "active"):
            assert all(db[field].get(k) == v for k, v in before[field].items()), (
                "Existing data changed"
            )
        added = set(db["counts"]) - set(before["counts"])
        assert added <= {"feishu_alert_seen", "feishu_alert_meta", "feishu_alert_outbox"}, (
            "Unexpected migration data"
        )
        assert h['monitoring']['feishu_alerts'].get('enabled') is True
        assert h['monitoring']['feishu_alerts'].get('configured') is True
        assert others() == json.loads((BACKUP / "containers-before.json").read_text()), (
            "Other containers changed"
        )
        assert config_hashes() == json.loads((RELEASE / "expected-configuration-hashes.json").read_text()), (
            "Config changed"
        )
        save(RELEASE / "health-after.json", h)
        save(RELEASE / "database-after.json", db)
        save(RELEASE / "containers-after.json", others())
        result = {
            "version": VERSION,
            "git_commit": commit,
            "git_tag": VERSION,
            "remote_main": (RELEASE / "remote-main.txt").read_text().strip(),
            "restart_count": inspect()["RestartCount"],
            "external_health": external,
            "management_pages": pages,
            "compose_sha256": hashlib.sha256(
                (RELEASE / "compose.server.yaml").read_bytes()
            ).hexdigest(),
            "configuration_hashes": config_hashes(),
            "source_revision": (RELEASE / "source-revision.txt").read_text().strip(),
            "image_id": inspect()["Image"],
            "source": source,
            "database": db,
            "configuration_change": "none",
            "other_containers_unchanged": True,
            "backup": str(BACKUP),
        }
        save(RELEASE / "deployment.json", result)
        temp = BASE / ("current-" + VERSION + "-new")
        temp.symlink_to(RELEASE)
        temp.replace(BASE / "current")
        print(json.dumps(result, ensure_ascii=False))
    else:
        raise ValueError(mode)


if __name__ == "__main__":
    main(sys.argv[1])
