"""Securely add or replace the TAORAN knowledge API key on the server.

The secret is read without terminal echo, sent through SSH standard input, and
never placed in argv, source files, command logs, or normal output.
"""
from __future__ import annotations

import getpass
import re
import shlex
import subprocess
import sys

REMOTE_CODE = r"""
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

path = Path('/TAORAN agent/runtime/agent.env')
backup = Path('/TAORAN agent/runtime/agent.env.before-0.11.0-20260827')
st = os.lstat(path)
if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
    raise SystemExit(2)
secret = sys.stdin.buffer.readline().rstrip(b'\r\n')
if (
    len(secret) < 20
    or not secret.startswith(b'dsmk_')
    or any(byte > 127 for byte in secret)
    or b'\x00' in secret
    or b'\n' in secret
    or b'\r' in secret
):
    raise SystemExit(3)
name = b'DSM_TAORAN_KNOWLEDGE_API_KEY='
lines = path.read_bytes().splitlines()
updated = []
found = False
for line in lines:
    if line.startswith(name):
        if found:
            continue
        updated.append(name + secret)
        found = True
    else:
        updated.append(line)
if not found:
    updated.append(name + secret)
if not backup.exists():
    shutil.copy2(path, backup)
    os.chown(backup, 10001, 10001)
    os.chmod(backup, 0o600)
fd, temporary = tempfile.mkstemp(prefix='.agent.env.', dir=str(path.parent))
try:
    os.write(fd, b'\n'.join(updated) + b'\n')
    os.fsync(fd)
    os.fchmod(fd, 0o600)
    os.fchown(fd, 10001, 10001)
finally:
    os.close(fd)
os.replace(temporary, path)
print('knowledge_api_key_configured=yes; backup_preserved=yes')
"""


def main() -> None:
    secret = (
        sys.stdin.readline().rstrip("\r\n")
        if "--stdin" in sys.argv[1:]
        else getpass.getpass("请粘贴DSM知识库API Key（输入不会显示）：")
    )
    # Chat/Markdown may escape underscores and the clipboard may include a label.
    normalized = secret.replace("\\_", "_")
    token = re.search(r"dsmk_[A-Za-z0-9_]{20,}", normalized)
    if token:
        secret = token.group(0)
    if (
        len(secret) < 20
        or not secret.startswith("dsmk_")
        or not secret.isascii()
        or "\n" in secret
        or "\r" in secret
        or "\x00" in secret
    ):
        raise SystemExit("Key格式无效，服务器未修改。")
    remote_command = "python3 -c " + shlex.quote(REMOTE_CODE)
    result = subprocess.run(
        [
            "ssh",
            "-i",
            "/Users/ydzj/.ssh/dsm_aliyun",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "root@47.119.191.148",
            remote_command,
        ],
        input=(secret + "\n").encode(),
        capture_output=True,
        timeout=20,
        check=False,
    )
    secret = ""
    if result.returncode:
        raise SystemExit("知识库Key配置失败；未输出敏感信息。")
    print(result.stdout.decode().strip())


if __name__ == "__main__":
    main()
