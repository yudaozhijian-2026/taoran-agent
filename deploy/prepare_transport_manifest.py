"""Prepare non-secret, reproducible release manifests from this Git worktree."""
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "build/transport-probe"
BASE = "cf7cccbde2ecdfec243776094a29ec0548f61096"


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT).decode().strip()


def main():
    DEST.mkdir(parents=True, exist_ok=True)
    source = {
        str(path.relative_to(ROOT / "src/taoran_agent")): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (ROOT / "src/taoran_agent").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    baseline = {}
    for name in git("ls-tree", "-r", "--name-only", BASE, "src/taoran_agent").splitlines():
        payload = subprocess.check_output(["git", "show", BASE + ":" + name], cwd=ROOT)
        baseline[name.removeprefix("src/taoran_agent/")] = hashlib.sha256(payload).hexdigest()
    for name, data in (("source-manifest.json", source), ("baseline-manifest.json", baseline)):
        (DEST / name).write_text(json.dumps(data, indent=2))
    (DEST / "git-commit.txt").write_text(git("rev-parse", "HEAD") + "\n")
    (DEST / "source-revision.txt").write_text(git("rev-parse", "HEAD^{tree}") + "\n")
    (DEST / "remote-main.txt").write_text(git("rev-parse", "origin/main") + "\n")
    print(f"Source files: {len(source)}; baseline files: {len(baseline)}")


if __name__ == "__main__":
    main()
