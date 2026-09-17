"""Read-only deployment checks; persist a secret-free isolated-test audit."""

import hashlib
import json
import sqlite3
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

root = Path('/TAORAN agent/isolated-submit-test-20260916')
db = sqlite3.connect(f'file:{root}/data/taoran_agent.db?mode=ro', uri=True)
# The server host runs Python 3.8, which does not expose datetime.UTC.
audit = {'checked_at': datetime.now(timezone.utc).isoformat(), 'integrity': db.execute('pragma integrity_check').fetchone()[0]}  # noqa: UP017
audit['jobs'] = []
for job, status, code, raw in db.execute('select job_id,status,visit_record_code,response_json from evaluation_jobs'):
    result = json.loads(raw) if raw else {}
    audit['jobs'].append({'job_id': job, 'status': status, 'record_code': code,
        'score': result.get('total_score'), 'writeback': result.get('writeback'),
        'phase_latency_ms': result.get('phase_latency_ms')})
audit['quick_checks'] = []
for cid, raw in db.execute('select check_id,payload_json from quick_check_recovery'):
    data = json.loads(raw)
    final = data.get('outcome', {}).get('final', {})
    audit['quick_checks'].append({'check_id': cid, 'status': data['status'],
        'full_feedback_ms': final.get('full_feedback_ms'), 'diagnostics': final.get('diagnostics')})
ids = subprocess.check_output(['docker', 'ps', '-q'], text=True).split()
containers = json.loads(subprocess.check_output(['docker', 'inspect', *ids]))
audit['containers'] = [{'name': d['Name'], 'id': d['Id'], 'image': d['Image'],
    'image_tag': d['Config']['Image'], 'restart_count': d['RestartCount'],
    'status': d['State']['Status'], 'health': d['State'].get('Health', {}).get('Status'),
    'revision': d['Config'].get('Labels', {}).get('org.opencontainers.image.revision')} for d in containers]
audit['configuration_sha256'] = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
    for p in [root/'compose.yaml', root/'runtime/agent.env', root/'runtime/tenant_registry.json', root/'runtime/field_mapping.json']}
audit['http'] = {}
for url in ['http://127.0.0.1:8031/health', 'https://taoran-test.yudaozhijian.top/health',
            'https://taoran-test.yudaozhijian.top/admin/tenants', 'https://taoran-test.yudaozhijian.top/.git/config']:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            audit['http'][url] = {'status': response.status}
            if url.endswith('/health'):
                audit['http'][url]['version'] = json.load(response).get('release_version')
    except urllib.error.HTTPError as error:
        audit['http'][url] = {'status': error.code}
version = audit['http']['http://127.0.0.1:8031/health']['version']
assert version in {'1.0.6rc2', '1.0.6rc3', '1.0.6rc4', '1.0.6rc5', '1.0.6rc6', '1.0.6rc7', '1.0.6rc8', '1.0.6rc9', '1.0.6rc10', '1.0.6rc11', '1.0.6rc12', '1.0.6rc13', '1.0.6rc14', '1.0.6rc15', '1.0.6rc16', '1.0.6rc17'}
audit['rollback'] = str(root / f'backups/before-{version}')
(root/f'acceptance-audit-{version}.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2))
print(json.dumps(audit, ensure_ascii=False, indent=2))
