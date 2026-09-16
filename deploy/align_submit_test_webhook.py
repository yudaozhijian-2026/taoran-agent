"""Preserve the test form's existing signing secret; never change production."""

import json
import shutil
from pathlib import Path

root = Path('/TAORAN agent/isolated-submit-test-20260916')
target = root / 'runtime/tenant_registry.json'
source = json.loads(Path('/TAORAN agent/tenant-config/tenant_registry.json').read_text())
registry = json.loads(target.read_text())
assert set(registry['tenants']) == {'tenant_433327714475'}
backup = root / 'runtime/tenant_registry.before-webhook.json'
assert not backup.exists()
shutil.copy2(target, backup)
registry['tenants']['tenant_433327714475']['jiandaoyun']['webhook_secret'] = (
    source['tenants']['tenant_fe64542a2b07']['jiandaoyun']['webhook_secret']
)
target.write_text(json.dumps(registry, ensure_ascii=False))
print('Isolated test webhook aligned; production registry unchanged.')
