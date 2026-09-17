"""Promote only the isolated test service, preserving its current data/config."""

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

root = Path('/TAORAN agent/isolated-submit-test-20260916')
release = sys.argv[1] if len(sys.argv) > 1 else '1.0.6rc3'
previous = {
    '1.0.6rc3': '1.0.6rc2',
    '1.0.6rc4': '1.0.6rc3',
    '1.0.6rc5': '1.0.6rc4',
    '1.0.6rc6': '1.0.6rc5',
    '1.0.6rc7': '1.0.6rc6',
    '1.0.6rc8': '1.0.6rc7',
    '1.0.6rc9': '1.0.6rc8',
    '1.0.6rc10': '1.0.6rc9',
    '1.0.6rc11': '1.0.6rc10',
    '1.0.6rc12': '1.0.6rc11',
    '1.0.6rc13': '1.0.6rc12',
    '1.0.6rc14': '1.0.6rc13',
    '1.0.6rc15': '1.0.6rc14',
    '1.0.6rc16': '1.0.6rc15',
    '1.0.6rc17': '1.0.6rc16',
    '1.0.6rc18': '1.0.6rc17',
    '1.0.6rc19': '1.0.6rc18',
    '1.0.6rc20': '1.0.6rc19',
    '1.0.6rc21': '1.0.6rc20',
    '1.0.6rc22': '1.0.6rc21',
    '1.0.6rc23': '1.0.6rc22',
    '1.0.6rc24': '1.0.6rc23',
    '1.0.6rc25': '1.0.6rc24',
    '1.0.6rc26': '1.0.6rc25',
    '1.0.6rc27': '1.0.6rc26',
}[release]
old_date_suffix = (
    '20260917'
    if previous in {'1.0.6rc11', '1.0.6rc12', '1.0.6rc13', '1.0.6rc14', '1.0.6rc15', '1.0.6rc16', '1.0.6rc17', '1.0.6rc18', '1.0.6rc19', '1.0.6rc20', '1.0.6rc21', '1.0.6rc22', '1.0.6rc23', '1.0.6rc24', '1.0.6rc25', '1.0.6rc26'}
    else '20260916'
)
old = f'taoran-submit-test:{previous}-{old_date_suffix}'
date_suffix = (
    '20260917'
    if release in {'1.0.6rc11', '1.0.6rc12', '1.0.6rc13', '1.0.6rc14', '1.0.6rc15', '1.0.6rc16', '1.0.6rc17', '1.0.6rc18', '1.0.6rc19', '1.0.6rc20', '1.0.6rc21', '1.0.6rc22', '1.0.6rc23', '1.0.6rc24', '1.0.6rc25', '1.0.6rc26', '1.0.6rc27'}
    else '20260916'
)
new = f'taoran-submit-test:{release}-{date_suffix}'
info = json.loads(subprocess.check_output(['docker', 'inspect', 'taoran-submit-test-agent']))[0]
assert info['Config']['Image'] == old, 'Newer or unexpected test deployment: stop'
prod = json.loads(subprocess.check_output(['docker', 'inspect', 'dsm-taoran-v2-agent']))[0]
assert prod['Config']['Image'] == 'dsm-taoran-v2:1.0.5-contact-facts-20260914'
db = sqlite3.connect(root / 'data/taoran_agent.db')
assert db.execute('pragma integrity_check').fetchone()[0] == 'ok'
assert db.execute("select count(*) from evaluation_jobs where status in ('queued','running')").fetchone()[0] == 0
for (payload,) in db.execute('select payload_json from quick_check_recovery'):
    assert json.loads(payload)['status'] not in ('queued', 'running')
backup = root / f'backups/before-{release}'
backup.mkdir(parents=True, exist_ok=False)
db.backup(sqlite3.connect(backup / 'taoran_agent.db'))
shutil.copy2(root / 'compose.yaml', backup / 'compose.yaml')
shutil.copytree(root / 'runtime', backup / 'runtime')
compose = root / 'compose.yaml'
text = compose.read_text()
assert text.count(old) == 1
compose.write_text(text.replace(old, new))
subprocess.run(['docker', 'compose', '-f', str(compose), 'config', '-q'], check=True)
subprocess.run(['docker', 'compose', '-f', str(compose), 'up', '-d', '--no-deps', 'agent'], check=True)
print('Only isolated test service promoted; backup:', backup)
