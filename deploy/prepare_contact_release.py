"""Build a scoped, secret-free offline release bundle."""
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "dist/contact-release"
OUT.mkdir(parents=True, exist_ok=True)
BASELINE = "c279c46b5062acdc2f364b63ba1e5ccb97475bee"


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def manifest(revision):
    prefix = "src/taoran_agent/"
    paths = git("ls-tree", "-r", "--name-only", revision, "--", prefix).decode().splitlines()
    return {p[len(prefix):]: hashlib.sha256(git("show", revision + ":" + p)).hexdigest()
            for p in paths}


commit = git("rev-parse", "HEAD").decode().strip()
for name, revision in [("baseline-manifest.json", BASELINE), ("source-manifest.json", commit)]:
    (OUT / name).write_text(json.dumps(manifest(revision)))
(OUT / "git-commit.txt").write_text(commit)
shutil.copytree(ROOT / "src", OUT / "src", dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
for name in ("pyproject.toml", "uv.lock"):
    shutil.copy2(ROOT / name, OUT / name)
shutil.copy2(ROOT / "deploy/release_contact_policy.py", OUT / "release.py")
shutil.copy2(ROOT / "dist/dsm_taoran_agent-1.0.4-py3-none-any.whl", OUT)
(OUT / "Dockerfile.release").write_text("""FROM dsm-taoran-v2:1.0.3-token-usage-20260911
USER root
WORKDIR /app
COPY dsm_taoran_agent-1.0.4-py3-none-any.whl /tmp/package.whl
RUN mv /tmp/package.whl /tmp/dsm_taoran_agent-1.0.4-py3-none-any.whl && uv pip install --python /app/.venv/bin/python --no-deps --no-index /tmp/dsm_taoran_agent-1.0.4-py3-none-any.whl
COPY src /app/src
COPY pyproject.toml uv.lock /app/
ARG GIT_COMMIT
LABEL org.opencontainers.image.revision=$GIT_COMMIT
LABEL top.yudaozhijian.taoran.release="1.0.4-contact-policy-20260914"
USER 10001:10001
""")
(OUT / "smoke.py").write_text("""import taoran_agent
from taoran_agent.models import VisitDraftInput
from taoran_agent.contact_policy import contact_policy,contact_policy_hits
from taoran_agent.api import app
assert taoran_agent.__version__ == '1.0.4'
assert any(r.path == '/health' for r in app.routes)
for kind, period in [('target','month'),('potential','quarter'),('opportunity','none')]:
    v=VisitDraftInput(visit_date='2026-09-10',employee_id='smoke',customer_type_ii=kind)
    p=contact_policy(v)
    assert p['period']==period and p['date_state']=='missing'
    if kind!='target':
        assert contact_policy_hits({'facts':{'reason':'须跨自然月'}},p)
print('isolated smoke passed; no data writes or model calls')
""")
print(OUT)
