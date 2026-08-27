import json

import pytest
from fastapi.testclient import TestClient

from taoran_agent import api
from taoran_agent.config import Settings, get_settings
from taoran_agent.mapping_sync import JiandaoyunSchemaSyncError
from taoran_agent.tenant_admin import (
    TenantFieldConfirmationRequest,
    TenantOnboardingRequest,
    confirm_tenant_fields,
    onboard_tenant,
)


@pytest.fixture(autouse=True)
def isolated_admin(tmp_path, monkeypatch):
    monkeypatch.setenv("DSM_TAORAN_DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DSM_TAORAN_ENVIRONMENT", "development")
    monkeypatch.setenv("DSM_TAORAN_ADMIN_ENABLED", "true")
    monkeypatch.setenv("DSM_TAORAN_ADMIN_API_KEY", "admin-test-key")
    monkeypatch.setenv(
        "DSM_TAORAN_ADMIN_AUDIT_PATH",
        str(tmp_path / "tenant_admin_audit.jsonl"),
    )
    monkeypatch.setenv(
        "DSM_TAORAN_TENANT_REGISTRY_PATH",
        str(tmp_path / "tenant_registry.json"),
    )
    monkeypatch.setenv("DSM_TAORAN_TENANT_KEYS_JSON", "{}")
    monkeypatch.setenv("DSM_TAORAN_JIANDAOYUN_API_KEYS_JSON", "{}")
    monkeypatch.setenv("DSM_TAORAN_LLM_ENABLED", "false")
    get_settings.cache_clear()
    api._stores.clear()
    api._agents.clear()
    yield tmp_path
    get_settings.cache_clear()
    api._stores.clear()
    api._agents.clear()


def onboarding_payload(**overrides) -> dict:
    payload = {
        "tenant_id": "customer_a",
        "display_name": "客户A",
        "enabled": True,
        "application_id": "app-a",
        "entry_id": "entry-a",
        "entry_name": "拜访记录A",
        "jiandaoyun_api_key": "jdy-a",
        "test_connection": False,
        "rotate_access_key": False,
        "rotate_webhook_secret": False,
    }
    payload.update(overrides)
    return payload


def test_admin_page_is_protected_and_serves_packaged_assets(monkeypatch) -> None:
    client = TestClient(api.app)

    page = client.get("/admin/tenants")
    unauthenticated = client.get("/api/v1/admin/status")
    authenticated = client.get(
        "/api/v1/admin/status",
        headers={"X-Admin-Key": "admin-test-key"},
    )
    script = client.get("/admin/assets/admin.js")

    assert page.status_code == 200
    assert "TAORAN 客户接入管理" in page.text
    assert "连接简道云" in page.text
    assert 'name="tenant_id"' not in page.text
    assert 'id="applicationSelect"' in page.text
    assert 'id="formSelect"' in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert unauthenticated.status_code == 401
    assert authenticated.status_code == 200
    assert authenticated.json()["tenant_count"] == 0
    assert script.status_code == 200

    monkeypatch.setenv("DSM_TAORAN_ADMIN_ENABLED", "false")
    monkeypatch.delenv("DSM_TAORAN_ADMIN_API_KEY")
    monkeypatch.setenv("DSM_TAORAN_TENANT_REGISTRY_PATH", "")
    get_settings.cache_clear()
    assert client.get("/admin/tenants").status_code == 404


def test_authorized_applications_and_forms_are_discovered(monkeypatch) -> None:
    monkeypatch.setattr(
        "taoran_agent.tenant_admin.discover_jiandaoyun_authorization",
        lambda *args: [
            {
                "app_id": "app-authorized",
                "name": "销售管理",
                "forms": [
                    {
                        "app_id": "app-authorized",
                        "entry_id": "form-authorized",
                        "name": "拜访记录",
                    }
                ],
            }
        ],
    )
    client = TestClient(api.app)

    response = client.post(
        "/api/v1/admin/jiandaoyun/authorization",
        headers={"X-Admin-Key": "admin-test-key"},
        json={"jiandaoyun_api_key": "jdy-key"},
    )

    assert response.status_code == 200
    assert response.json()["applications"][0]["forms"][0]["entry_id"] == "form-authorized"


def test_customer_id_is_generated_when_not_supplied(isolated_admin) -> None:
    client = TestClient(api.app)
    payload = onboarding_payload()
    payload.pop("tenant_id")

    response = client.post(
        "/api/v1/admin/tenants",
        headers={"X-Admin-Key": "admin-test-key"},
        json=payload,
    )

    assert response.status_code == 200
    tenant_id = response.json()["tenant"]["tenant_id"]
    assert tenant_id.startswith("tenant_")
    assert len(tenant_id) == 19
    registry = json.loads(
        (isolated_admin / "tenant_registry.json").read_text(encoding="utf-8")
    )
    assert tenant_id in registry["tenants"]


def test_recheck_existing_tenant_activates_after_fields_are_complete(
    isolated_admin,
    monkeypatch,
) -> None:
    settings = get_settings()
    created = onboard_tenant(
        settings,
        TenantOnboardingRequest.model_validate(onboarding_payload()),
    )
    tenant_id = created.tenant["tenant_id"]
    monkeypatch.setattr(
        "taoran_agent.tenant_admin.fetch_jiandaoyun_form_schema_with_key",
        lambda *args: {"widgets": []},
    )

    def complete_mapping(mapping, schema):
        mapping["status"] = "copy_widget_ids_synced"
        return mapping, {
            "matched_count": 25,
            "unresolved_count": 0,
            "matched": [],
            "unresolved": [],
            "available_fields": [],
        }

    monkeypatch.setattr("taoran_agent.tenant_admin.synchronize_mapping", complete_mapping)

    result = confirm_tenant_fields(
        settings,
        tenant_id,
        TenantFieldConfirmationRequest(assignments={}),
    )

    assert result.activated is True
    assert result.tenant["enabled"] is True
    registry = json.loads(
        (isolated_admin / "tenant_registry.json").read_text(encoding="utf-8")
    )
    assert registry["tenants"][tenant_id]["enabled"] is True


def test_manual_confirmation_endpoint_saves_selected_mapping(
    monkeypatch,
) -> None:
    client = TestClient(api.app)
    client.post(
        "/api/v1/admin/tenants",
        headers={"X-Admin-Key": "admin-test-key"},
        json=onboarding_payload(),
    )
    monkeypatch.setattr(
        "taoran_agent.tenant_admin.fetch_jiandaoyun_form_schema_with_key",
        lambda *args: {"widgets": []},
    )
    captured = {}

    def manual_mapping(mapping, schema, assignments):
        captured.update(assignments)
        mapping["status"] = "copy_widget_ids_partially_synced"
        return mapping, {
            "matched_count": 24,
            "unresolved_count": 1,
            "matched": [],
            "unresolved": [
                {
                    "path": "fields.visit_purpose",
                    "field_name": "拜访目的",
                    "location": "拜访记录",
                    "candidate_scope": "top_level",
                }
            ],
            "available_fields": [],
        }

    monkeypatch.setattr(
        "taoran_agent.tenant_admin.apply_manual_widget_assignments",
        manual_mapping,
    )

    response = client.post(
        "/api/v1/admin/tenants/customer_a/field-confirmation",
        headers={"X-Admin-Key": "admin-test-key"},
        json={"assignments": {"fields.visit_purpose": "_widget_visit_purpose"}},
    )

    assert response.status_code == 200
    assert captured == {"fields.visit_purpose": "_widget_visit_purpose"}
    assert response.json()["mapping_report"]["unresolved"][0]["field_name"] == "拜访目的"


def test_page_submission_creates_registry_and_returns_secrets_once(isolated_admin) -> None:
    client = TestClient(api.app)
    headers = {"X-Admin-Key": "admin-test-key"}

    created = client.post(
        "/api/v1/admin/tenants",
        headers=headers,
        json=onboarding_payload(),
    )

    assert created.status_code == 200
    body = created.json()
    assert body["activated"] is False
    assert body["one_time_credentials"]["access_key"].startswith("taor_")
    assert body["one_time_credentials"]["webhook_secret"]
    registry = json.loads(
        (isolated_admin / "tenant_registry.json").read_text(encoding="utf-8")
    )
    assert registry["tenants"]["customer_a"]["jiandaoyun"]["api_key"] == "jdy-a"
    assert registry["tenants"]["customer_a"]["enabled"] is False
    assert (isolated_admin / "tenants" / "customer_a").is_dir()
    assert (isolated_admin / "tenant_registry.json").stat().st_mode & 0o777 == 0o600

    listed = client.get("/api/v1/admin/tenants", headers=headers)
    listed_text = json.dumps(listed.json(), ensure_ascii=False)
    assert listed.status_code == 200
    assert listed.json()["tenants"][0]["display_name"] == "客户A"
    assert "jdy-a" not in listed_text
    assert "taor_" not in listed_text

    updated = client.post(
        "/api/v1/admin/tenants",
        headers=headers,
        json=onboarding_payload(
            display_name="客户A更新",
            jiandaoyun_api_key=None,
        ),
    )
    assert updated.status_code == 200
    assert updated.json()["one_time_credentials"] == {
        "access_key": None,
        "webhook_secret": None,
    }
    updated_registry = json.loads(
        (isolated_admin / "tenant_registry.json").read_text(encoding="utf-8")
    )
    assert updated_registry["tenants"]["customer_a"]["jiandaoyun"]["api_key"] == "jdy-a"


def test_failed_connection_does_not_write_registry(isolated_admin, monkeypatch) -> None:
    settings = get_settings()

    def fail_connection(*args, **kwargs):
        raise JiandaoyunSchemaSyncError("表单不可访问")

    monkeypatch.setattr(
        "taoran_agent.tenant_admin.fetch_jiandaoyun_form_schema_with_key",
        fail_connection,
    )
    request = TenantOnboardingRequest.model_validate(
        onboarding_payload(test_connection=True)
    )

    with pytest.raises(JiandaoyunSchemaSyncError):
        onboard_tenant(settings, request)

    assert not (isolated_admin / "tenant_registry.json").exists()
    assert settings.tenant_config("customer_a") is None


def test_successful_connection_and_complete_mapping_activates_tenant(
    isolated_admin,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "taoran_agent.tenant_admin.fetch_jiandaoyun_form_schema_with_key",
        lambda *args: {"widgets": []},
    )

    def complete_mapping(mapping, schema):
        mapping["status"] = "copy_widget_ids_synced"
        return mapping, {
            "matched_count": 18,
            "unresolved_count": 0,
            "matched": [],
            "unresolved": [],
        }

    monkeypatch.setattr(
        "taoran_agent.tenant_admin.synchronize_mapping",
        complete_mapping,
    )
    result = onboard_tenant(
        get_settings(),
        TenantOnboardingRequest.model_validate(
            onboarding_payload(test_connection=True, enabled=True)
        ),
    )

    assert result.connection_tested is True
    assert result.activated is True
    assert result.tenant["enabled"] is True
    assert result.mapping_report["matched_count"] == 18


def test_admin_settings_require_dedicated_key_and_writable_registry_path() -> None:
    with pytest.raises(ValueError, match="管理员Key"):
        Settings(
            _env_file=None,
            admin_enabled=True,
            admin_api_key=None,
            tenant_registry_path="",
        )
    with pytest.raises(ValueError, match="注册表路径"):
        Settings(
            _env_file=None,
            admin_enabled=True,
            admin_api_key="key",
            tenant_registry_path="",
        )
