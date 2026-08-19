from __future__ import annotations

import secrets

from fastapi import HTTPException

from .config import Settings


def verify_tenant_access(
    settings: Settings,
    tenant_id: str,
    header_tenant_id: str | None,
    api_key: str | None,
) -> None:
    if header_tenant_id and header_tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="tenant header does not match request")
    tenant_keys = settings.tenant_keys
    if not tenant_keys and settings.environment == "development":
        return
    if not tenant_keys:
        raise HTTPException(status_code=503, detail="tenant authentication is not configured")
    expected = tenant_keys.get(tenant_id)
    if expected is None or api_key is None or not secrets.compare_digest(expected, api_key):
        raise HTTPException(status_code=401, detail="invalid tenant credentials")


def verify_q40_service_access(
    settings: Settings,
    tenant_id: str,
    service_id: str | None,
    service_key: str | None,
) -> None:
    if not settings.enable_q40_integration:
        raise HTTPException(status_code=404, detail="q40 integration is disabled")
    if service_id != settings.q40_service_id:
        raise HTTPException(status_code=401, detail="invalid q40 service identity")
    service_keys = settings.q40_service_keys
    if not service_keys:
        raise HTTPException(status_code=503, detail="q40 service authentication is not configured")
    expected = service_keys.get(tenant_id)
    if expected is None or service_key is None or not secrets.compare_digest(
        expected, service_key
    ):
        raise HTTPException(status_code=401, detail="invalid q40 service credentials")
