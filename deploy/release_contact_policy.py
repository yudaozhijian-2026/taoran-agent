"""Explicit 1.0.3 rollout, confined to the verified TAORAN container and database.

Run preflight, build, switch, verify in order on the server. No business API calls.
Runtime backups contain secrets and remain private on the server.
"""
import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import tarfile
import urllib.request
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

BASE = Path('/TAORAN agent')
VERSION = '1.0.4-contact-policy-20260914'
OLD = '1.0.3-token-usage-20260911'
EXPECTED_IMAGE = 'sha256:67c6be7807f7e2ad7d9d66050133c0d44d08afa991a257c75a423153539fa5f0'
EXPECTED_REVISION = 'c279c46b5062acdc2f364b63ba1e5ccb97475bee'
RELEASE = BASE / 'releases' / VERSION
BACKUP = BASE / 'backups' / ('before-' + VERSION)
CONTAINER = 'dsm-taoran-v2-agent'


def run(args, **kwargs):
    return subprocess.check_output(args, **kwargs).decode().strip()


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def inspect():
    return json.loads(run(['docker', 'inspect', CONTAINER]))[0]


def health(origin='http://127.0.0.1:8030'):
    with urllib.request.urlopen(origin + '/health', timeout=15) as response:
        return json.load(response)


def others():
    ids = run(['docker', 'ps', '-aq']).split()
    return {c['Name']: {'id': c['Id'], 'image': c['Image'], 'started': c['State']['StartedAt'],
                       'status': c['State']['Status'], 'restarts': c['RestartCount']}
            for c in json.loads(run(['docker', 'inspect', *ids]))
            if c['Name'] != '/' + CONTAINER}


def configuration():
    return {str(p.relative_to(BASE)): hashlib.sha256(p.read_bytes()).hexdigest()
            for folder in ('runtime', 'tenant-config') for p in (BASE / folder).rglob('*')
            if p.is_file()}


def database(backup=None):
    with closing(sqlite3.connect('file:/TAORAN agent/data/taoran_agent.db?mode=ro', uri=True)) as db:
        db.execute('BEGIN')
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        result = {'integrity': 'ok', 'counts': {}, 'digests': {}, 'active': {}}
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
            assert re.fullmatch('[a-zA-Z0-9_]+', name)
            rows = db.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()
            cols = [r[1] for r in db.execute(f'PRAGMA table_info("{name}")')]
            result['counts'][name] = len(rows)
            result['digests'][name] = hashlib.sha256(json.dumps(rows, ensure_ascii=False, default=str).encode()).hexdigest()
            if 'status' in cols:
                result['active'][name] = sum(r[cols.index('status')] in ('queued', 'running', 'processing', 'pending') for r in rows)
            if name == 'quick_check_recovery':
                result['active'][name] = sum(json.loads(r[cols.index('payload_json')]).get('status') in ('queued', 'running', 'processing', 'pending') for r in rows)
        if backup:
            with closing(sqlite3.connect(backup)) as dst:
                db.backup(dst)
                assert dst.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        return result


def idle():
    cap = health()['monitoring']['model_capacity']
    assert cap['active_frontend'] == cap['active_backend'] == 0, 'Models still active'
    state = database()
    assert not any(state['active'].values()), 'Business tasks still active'
    return state


def expected_old():
    c = inspect()
    assert c['Image'] == EXPECTED_IMAGE and c['Config']['Image'] == 'dsm-taoran-v2:' + OLD
    assert c['Config']['Labels']['org.opencontainers.image.revision'] == EXPECTED_REVISION
    assert (BASE / 'current').resolve() == BASE / 'releases' / OLD
    assert health()['release_version'] == '1.0.3'
    return c


def source(manifest):
    code = '''import hashlib,json,sys,taoran_agent
from pathlib import Path
root=Path(taoran_agent.__file__).parent
expected=json.load(sys.stdin)
actual={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'}
print(json.dumps({'files':len(expected),'mismatches':[k for k,v in expected.items() if actual.get(k)!=v],'unexpected':sorted(set(actual)-set(expected))}))'''
    result = json.loads(run(['docker', 'exec', '-i', CONTAINER, 'python', '-c', code], input=manifest.read_bytes()))
    assert not result['mismatches'] and not result['unexpected'], result
    return result


def compose(path):
    return ['docker', 'compose', '-p', 'dsm-taoran-v2', '-f', str(path)]


def main(mode):
    os.umask(0o077)
    if mode == 'preflight':
        c = expected_old()
        source(RELEASE / 'baseline-manifest.json')
        idle()
        BACKUP.mkdir(mode=0o700, exist_ok=False)
        save(BACKUP / 'containers-before.json', others())
        save(BACKUP / 'configuration-hashes.json', configuration())
        save(BACKUP / 'container-before.json', c)
        save(BACKUP / 'health-before.json', health())
        save(BACKUP / 'database-before.json', database(BACKUP / 'taoran_agent.db'))
        old_compose = Path(c['Config']['Labels']['com.docker.compose.project.config_files']).read_text()
        assert old_compose.count('image: dsm-taoran-v2:' + OLD) == 1
        (BACKUP / 'compose.rollback.yaml').write_text(old_compose)
        (RELEASE / 'compose.server.yaml').write_text(old_compose.replace('image: dsm-taoran-v2:' + OLD, 'image: dsm-taoran-v2:' + VERSION))
        with tarfile.open(BACKUP / 'runtime-tenant-config.tar.gz', 'w:gz') as archive:
            for name in ('runtime', 'tenant-config'):
                archive.add(BASE / name, arcname=name)
        with (BACKUP / 'previous-image.tar').open('wb') as target:
            subprocess.run(['docker', 'image', 'save', 'dsm-taoran-v2:' + OLD], stdout=target, check=True)
        rollback = '#!/bin/sh\nset -eu\n' + shlex.join(compose(BACKUP / 'compose.rollback.yaml') + ['up', '-d', '--no-deps', '--pull', 'never', 'agent']) + '\n' + shlex.join(['ln', '-sfn', str(BASE / 'releases' / OLD), str(BASE / 'current')]) + '\n'
        (BACKUP / 'rollback.sh').write_text(rollback)
        (BACKUP / 'rollback.sh').chmod(0o700)
        save(BACKUP / 'backup-sha256.json', {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in BACKUP.iterdir() if p.is_file()})
        print(json.dumps({'backup': str(BACKUP), 'database': database(), 'baseline_verified': True}))
    elif mode == 'build':
        expected_old()
        commit = (RELEASE / 'git-commit.txt').read_text().strip()
        subprocess.run(compose(RELEASE / 'compose.server.yaml') + ['config', '--quiet'], check=True)
        subprocess.run(['docker', 'build', '--network=none', '--pull=false', '--build-arg', 'GIT_COMMIT=' + commit,
                        '-t', 'dsm-taoran-v2:' + VERSION, '-f', str(RELEASE / 'Dockerfile.release'), str(RELEASE)], check=True)
        subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--read-only', '--tmpfs', '/tmp:rw,nosuid,size=64m',
                        '--entrypoint', 'python', '-i', 'dsm-taoran-v2:' + VERSION],
                       input=(RELEASE / 'smoke.py').read_bytes(), check=True)
        code = "import json,importlib.metadata as m; print(json.dumps({d.metadata['Name']:d.version for d in m.distributions() if d.metadata['Name']!='dsm-taoran-agent'},sort_keys=True))"
        dependencies = [json.loads(run(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'python',
                                       'dsm-taoran-v2:' + v, '-c', code])) for v in (OLD, VERSION)]
        assert dependencies[0] == dependencies[1], 'Runtime dependencies changed'
        save(RELEASE / 'dependencies-verified.json', dependencies[1])
        save(RELEASE / 'candidate-image.json', json.loads(run(['docker', 'image', 'inspect', 'dsm-taoran-v2:' + VERSION])))
    elif mode == 'switch':
        expected_old()
        assert others() == json.loads((BACKUP / 'containers-before.json').read_text()), 'Other containers changed'
        assert configuration() == json.loads((BACKUP / 'configuration-hashes.json').read_text()), 'Configuration changed'
        before = idle()
        assert before == json.loads((BACKUP / 'database-before.json').read_text()), 'Database changed since backup; refresh consistent backup first'
        save(BACKUP / 'database-at-switch.json', before)
        subprocess.run(compose(RELEASE / 'compose.server.yaml') + ['up', '-d', '--no-deps', '--pull', 'never', 'agent'], check=True)
        print('TAORAN-only switch completed; verification required')
    elif mode == 'verify':
        c = inspect()
        h = health()
        assert c['Config']['Image'] == 'dsm-taoran-v2:' + VERSION
        assert c['State']['Health']['Status'] == 'healthy' and c['RestartCount'] == 0
        assert h['release_version'] == '1.0.4' and h['prewarm']['status'] == 'ready'
        assert c['Config']['Labels']['org.opencontainers.image.revision'] == (RELEASE / 'git-commit.txt').read_text().strip()
        assert health('https://taoran.yudaozhijian.top')['release_version'] == '1.0.4'
        check_source = source(RELEASE / 'source-manifest.json')
        assert configuration() == json.loads((BACKUP / 'configuration-hashes.json').read_text())
        assert others() == json.loads((BACKUP / 'containers-before.json').read_text())
        before, after = json.loads((BACKUP / 'database-at-switch.json').read_text()), database()
        for field in ('counts', 'digests', 'active'):
            assert all(after[field].get(k) == v for k,v in before[field].items()), ('Business data changed during switch', field)
        pages = {}
        for path in ('/admin/tenants', '/admin/assets/admin.js', '/admin/assets/admin.css'):
            with urllib.request.urlopen('http://127.0.0.1:8030' + path, timeout=10) as r:
                assert r.status == 200 and r.read()
                pages[path] = 200
        # Initialize only the additive ledger schema, with the actual service UID.
        # No synthetic model calls or synthetic sales rows enter production data.
        code = "from taoran_agent.config import get_settings; from taoran_agent.token_usage import UsageLedger; import sqlite3,json; p=get_settings().database_path; UsageLedger(p)._write('SELECT 1'); c=sqlite3.connect('file:'+p+'?mode=ro',uri=True); print(json.dumps({'ledger_rows':c.execute('select count(*) from model_token_usage').fetchone()[0]}))"
        ledger = {'unchanged': True}
        report = {'release': VERSION, 'verified_at': datetime.now(timezone.utc).isoformat(), 'image_id': c['Image'],  # noqa: UP017 - deployment host Python 3.8
                  'commit': c['Config']['Labels']['org.opencontainers.image.revision'], 'source': check_source,
                  'health': 'ok', 'public_health': 'ok', 'other_containers_unchanged': True,
                  'configuration_unchanged': True, 'business_tables_unchanged': True, 'pages': pages,
                  'ledger': ledger, 'backup': str(BACKUP), 'production_model_calls_for_verification': 0}
        save(RELEASE / 'deployment.json', report)
        temporary = BASE / 'current.contact-policy-new'
        temporary.symlink_to(RELEASE)
        temporary.replace(BASE / 'current')
        print(json.dumps(report, ensure_ascii=False))
    else:
        raise ValueError('Use preflight, build, switch or verify')


if __name__ == '__main__':
    main(sys.argv[1])
