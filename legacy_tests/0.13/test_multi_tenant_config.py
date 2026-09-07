import hashlib
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from taoran_agent import api
from taoran_agent.config import Settings, get_settings
from taoran_agent.connector import load_jiandaoyun_mapping
from taoran_agent.field_labels import display_field_name, use_field_mapping
from taoran_agent.gateway import verify_tenant_access


def _registry(mapping_a, mapping_b) -> dict:
    return {
        "version": 1,
        "tenants": {
            "tenant_a": {
                "enabled": True,
                "access_keys": ["tenant-a-current", "tenant-a-previous"],
                "jiandaoyun": {
                    "api_key": "jdy-a",
                    "webhook_secret": "webhook-a",
                    "mapping_path": str(mapping_a),
                },
            },
            "tenant_b": {
                "enabled": True,
                "access_keys": ["tenant-b-current"],
                "jiandaoyun": {
                    "api_key": "jdy-b",
                    "webhook_secret": "webhook-b",
                    "mapping_path": str(mapping_b),
                },
            },
        },
    }


def _mapping_file(tmp_path, tenant: str):
    mapping = load_jiandaoyun_mapping()
    mapping["mapping_version"] = f"mapping-{tenant}"
    mapping["source_application_id"] = f"app-{tenant}"
    mapping["source_entry_id"] = f"entry-{tenant}"
    mapping["source_entry_name"] = f"拜访记录-{tenant}"
    mapping["fields"]["next_contact_at"]["field_name"] = f"{tenant}下次联系日期"
    path = tmp_path / f"mapping-{tenant}.json"
    path.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    return path


def test_registry_overrides_legacy_settings_and_supports_key_rotation(tmp_path) -> None:
    mapping_a = _mapping_file(tmp_path, "客户A")
    mapping_b = _mapping_file(tmp_path, "客户B")
    registry_path = tmp_path / "tenants.json"
    registry_path.write_text(
        json.dumps(_registry(mapping_a, mapping_b), ensure_ascii=False),
        encoding="utf-8",
    )
    settings = Settings(
        _env_file=None,
        tenant_registry_path=str(registry_path),
        tenant_keys_json='{"tenant_a":"legacy-tenant-key"}',
        jiandaoyun_api_keys_json='{"tenant_a":"legacy-jdy-key"}',
        jiandaoyun_webhook_secret="legacy-webhook",
        jiandaoyun_mapping_path="legacy-mapping.json",
    )

    assert settings.tenant_access_keys_for("tenant_a") == [
        "tenant-a-current",
        "tenant-a-previous",
    ]
    assert settings.jiandaoyun_api_key_for("tenant_a") == "jdy-a"
    assert settings.jiandaoyun_webhook_secret_for("tenant_a") == "webhook-a"
    assert settings.jiandaoyun_mapping_path_for("tenant_b") == str(mapping_b)
    assert settings.tenant_configuration_source("tenant_a") == "registry"
    assert settings.tenant_configuration_source("legacy_only") == "legacy"

    verify_tenant_access(settings, "tenant_a", "tenant_a", "tenant-a-previous")
    with pytest.raises(HTTPException) as exc_info:
        verify_tenant_access(settings, "tenant_b", "tenant_b", "tenant-a-current")
    assert exc_info.value.status_code == 401


def test_registered_tenant_never_falls_back_to_legacy_customer_configuration() -> None:
    settings = Settings(
        _env_file=None,
        environment="development",
        tenant_registry_json=json.dumps({
            "version": 1,
            "tenants": {"tenant_registered": {"enabled": True}},
        }),
        tenant_keys_json='{"tenant_registered":"legacy-key"}',
        jiandaoyun_api_keys_json='{"tenant_registered":"legacy-jdy"}',
        jiandaoyun_webhook_secret="legacy-webhook",
        jiandaoyun_mapping_path="legacy-mapping.json",
    )

    assert settings.tenant_access_keys_for("tenant_registered") == []
    assert settings.jiandaoyun_api_key_for("tenant_registered") is None
    assert settings.jiandaoyun_webhook_secret_for("tenant_registered") is None
    assert settings.jiandaoyun_mapping_path_for("tenant_registered") is None
    with pytest.raises(HTTPException) as mapping_error:
        api.tenant_mapping(settings, "tenant_registered")
    assert mapping_error.value.status_code == 503
    with pytest.raises(HTTPException) as exc_info:
        verify_tenant_access(
            settings,
            "tenant_registered",
            "tenant_registered",
            "legacy-key",
        )
    assert exc_info.value.status_code == 401


def test_disabled_registry_tenant_is_rejected() -> None:
    settings = Settings(
        _env_file=None,
        tenant_registry_json=json.dumps({
            "version": 1,
            "tenants": {
                "tenant_disabled": {
                    "enabled": False,
                    "access_keys": ["disabled-key"],
                }
            },
        }),
    )

    with pytest.raises(HTTPException) as exc_info:
        verify_tenant_access(
            settings,
            "tenant_disabled",
            "tenant_disabled",
            "disabled-key",
        )
    assert exc_info.value.status_code == 403


def test_registry_rejects_unknown_fields_and_duplicate_keys() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            tenant_registry_json=json.dumps({
                "version": 1,
                "tenants": {
                    "tenant_a": {
                        "access_keys": ["same-key", "same-key"],
                        "unexpected": True,
                    }
                },
            }),
        )


def test_tenant_mapping_labels_do_not_leak_between_requests(tmp_path) -> None:
    mapping_a = _mapping_file(tmp_path, "客户A")
    mapping_b = _mapping_file(tmp_path, "客户B")

    with use_field_mapping(str(mapping_a)):
        assert display_field_name("next_contact_at") == "客户A下次联系日期"
    with use_field_mapping(str(mapping_b)):
        assert display_field_name("next_contact_at") == "客户B下次联系日期"


def test_api_uses_isolated_mapping_authentication_and_webhook_secret(
    tmp_path, monkeypatch
) -> None:
    mapping_a = _mapping_file(tmp_path, "客户A")
    mapping_b = _mapping_file(tmp_path, "客户B")
    registry_path = tmp_path / "tenants.json"
    registry_path.write_text(
        json.dumps(_registry(mapping_a, mapping_b), ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("DSM_TAORAN_DATABASE_PATH", str(tmp_path / "tenant-test.db"))
    monkeypatch.setenv("DSM_TAORAN_ENVIRONMENT", "production")
    monkeypatch.setenv("DSM_TAORAN_TENANT_REGISTRY_PATH", str(registry_path))
    monkeypatch.setenv("DSM_TAORAN_TENANT_KEYS_JSON", "{}")
    get_settings.cache_clear()
    api._stores.clear()
    api._agents.clear()
    client = TestClient(api.app)

    response_a = client.get(
        "/api/v1/connectors/jiandaoyun/mapping",
        params={"tenant_id": "tenant_a"},
        headers={"X-Tenant-Id": "tenant_a", "X-Api-Key": "tenant-a-current"},
    )
    response_b = client.get(
        "/api/v1/connectors/jiandaoyun/mapping",
        params={"tenant_id": "tenant_b"},
        headers={"X-Tenant-Id": "tenant_b", "X-Api-Key": "tenant-b-current"},
    )
    cross_tenant = client.get(
        "/api/v1/connectors/jiandaoyun/mapping",
        params={"tenant_id": "tenant_b"},
        headers={"X-Tenant-Id": "tenant_b", "X-Api-Key": "tenant-a-current"},
    )

    assert response_a.json()["source_application_id"] == "app-客户A"
    assert response_b.json()["source_application_id"] == "app-客户B"
    assert cross_tenant.status_code == 401

    summary = client.get(
        "/api/v1/tenants/tenant_a/configuration",
        headers={"X-Tenant-Id": "tenant_a", "X-Api-Key": "tenant-a-current"},
    )
    summary_text = json.dumps(summary.json(), ensure_ascii=False)
    assert summary.status_code == 200
    assert summary.json()["configuration_source"] == "registry"
    assert summary.json()["access_key_count"] == 2
    assert summary.json()["jiandaoyun"]["application_id"] == "app-客户A"
    assert "tenant-a-current" not in summary_text
    assert "jdy-a" not in summary_text
    assert "webhook-a" not in summary_text

    payload = '{"op":"connection_test"}'
    signature = hashlib.sha1(f"n:{payload}:webhook-a:1".encode()).hexdigest()
    accepted = client.post(
        "/api/v1/connectors/jiandaoyun/visit/webhook",
        params={"tenant_id": "tenant_a", "nonce": "n", "timestamp": "1"},
        content=payload,
        headers={"X-JDY-Signature": signature, "Content-Type": "application/json"},
    )
    rejected = client.post(
        "/api/v1/connectors/jiandaoyun/visit/webhook",
        params={"tenant_id": "tenant_b", "nonce": "n", "timestamp": "1"},
        content=payload,
        headers={"X-JDY-Signature": signature, "Content-Type": "application/json"},
    )
    assert accepted.status_code == 202
    assert rejected.status_code == 401

    get_settings.cache_clear()
    api._stores.clear()
    api._agents.clear()
