"""Download Linux wheels from uv.lock, verify hashes, and prepare an offline build.

No dependency resolution or version changes; only locked PyPI artifacts are used.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import tomllib
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.tags import compatible_tags, cpython_tags
from packaging.utils import parse_wheel_filename

parser = argparse.ArgumentParser()
parser.add_argument("output", type=Path)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
output = args.output.resolve()
output.mkdir(parents=True, exist_ok=True)
wheels = output / "wheels"
wheels.mkdir(exist_ok=True)
lock = tomllib.loads((root / "uv.lock").read_text())
export = subprocess.run([
    "/Users/ydzj/.local/bin/uv", "export", "--frozen", "--no-dev", "--no-emit-project",
    "--no-hashes", "--format", "requirements-txt",
], cwd=root, check=True, capture_output=True, text=True).stdout
env = default_environment()
env.update(os_name="posix", sys_platform="linux", platform_system="Linux",
           platform_machine="x86_64", python_version="3.12", python_full_version="3.12.12")
platforms = [f"manylinux_2_{i}_x86_64" for i in range(36, 16, -1)]
platforms += ["manylinux2014_x86_64", "manylinux2010_x86_64", "manylinux1_x86_64", "linux_x86_64"]
tags = list(cpython_tags((3, 12), ["cp312"], platforms))
tags += list(compatible_tags((3, 12), "cp312", platforms))
rank = {tag: index for index, tag in enumerate(tags)}
selected = []
for line in export.splitlines():
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    req = Requirement(line)
    if req.marker and not req.marker.evaluate(env):
        continue
    package = next(p for p in lock["package"] if p["name"] == req.name and p["version"] in req.specifier)
    candidates = []
    for wheel in package["wheels"]:
        filename = urlparse(wheel["url"]).path.rsplit("/", 1)[-1]
        wheel_tags = parse_wheel_filename(filename)[3]
        matching = [rank[tag] for tag in wheel_tags if tag in rank]
        if matching:
            candidates.append((min(matching), filename, wheel))
    assert candidates, f"No locked Linux wheel for {req.name}"
    _, filename, wheel = min(candidates, key=lambda item: item[0])
    selected.append((req.name, package["version"], filename, wheel))


def download(item):
    name, version, filename, wheel = item
    assert urlparse(wheel["url"]).hostname == "files.pythonhosted.org"
    digest = wheel["hash"].removeprefix("sha256:")
    target = wheels / filename
    if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
        with urllib.request.urlopen(wheel["url"], timeout=30) as response:
            payload = response.read()
        assert hashlib.sha256(payload).hexdigest() == digest, f"Hash mismatch: {name}"
        target.write_bytes(payload)
    return f"{name}=={version} --hash=sha256:{digest}"


with ThreadPoolExecutor(max_workers=4) as pool:
    requirements = list(pool.map(download, selected))
app_wheel = root / "dist/dsm_taoran_agent-0.7.0-py3-none-any.whl"
shutil.copy2(app_wheel, wheels / app_wheel.name)
requirements.append("dsm-taoran-agent==0.7.0 --hash=sha256:" + hashlib.sha256(app_wheel.read_bytes()).hexdigest())
(output / "requirements.lock.txt").write_text("\n".join(requirements)+"\n")
print(f"Verified {len(selected)} locked runtime wheels and project wheel; offline files prepared.")
