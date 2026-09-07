"""Source revision and bounded in-process locks for the supported single worker."""
from hashlib import sha256
from threading import RLock

from .rules import canonical_hash

_locks = tuple(RLock() for _ in range(128))


def source_lock(tenant_id, target):
    key = (tenant_id, target.app_id, target.entry_id, target.data_id) if target else (tenant_id,)
    index = int.from_bytes(sha256(repr(key).encode()).digest()[:4], "big") % len(_locks)
    return _locks[index]


def business_revision(record, mapping):
    """Exclude only known AI outputs and mutable platform audit timestamps."""
    outputs = {
        spec if isinstance(spec, str) else spec.get("widget_id")
        for spec in mapping.get("output_fields", {}).values()
        if isinstance(spec, (str, dict))
    }
    return canonical_hash({k: v for k, v in record.items()
                           if k not in outputs and k not in {"updateTime", "updater"}})
