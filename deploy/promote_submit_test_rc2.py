"""Upgrade only the isolated test container after backup and idle checks."""

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

root = Path('/TAORAN agent/isolated-submit-test-20260916')
db = sqlite3.connect(root / 'data/taoran_agent.db')
assert db.execute('pragma integrity_check').fetchone()[0] == 'ok'
assert db.execute("select count(*) from evaluation_jobs where status in ('queued','running')").fetchone()[0] == 0
for (payload,) in db.execute('select payload_json from quick_check_recovery'):
    assert json.loads(payload)['status'] not in ('queued', 'running')
backup = root / 'backups/before-1.0.6rc2'
backup.mkdir(parents=True, exist_ok=False)
db.backup(sqlite3.connect(backup / 'taoran_agent.db'))
shutil.copy2(root / 'compose.yaml', backup / 'compose.yaml')
shutil.copytree(root / 'runtime', backup / 'runtime')
compose = root / 'compose.yaml'
text = compose.read_text()
assert text.count('taoran-submit-test:1.0.6rc1-20260916') == 1
compose.write_text(text.replace('taoran-submit-test:1.0.6rc1-20260916', 'taoran-submit-test:1.0.6rc2-20260916'))
subprocess.run(['docker', 'compose', '-f', str(compose), 'config', '-q'], check=True)
subprocess.run(['docker', 'compose', '-f', str(compose), 'up', '-d', '--no-deps', 'agent'], check=True)
print('Isolated test upgraded; rollback compose and database preserved.')
