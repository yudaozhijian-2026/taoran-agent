from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock
from typing import Any

from pydantic import BaseModel, Field, SecretStr, field_validator

from .config import Settings, TenantConfigRegistry
from .connector import load_jiandaoyun_mapping
from .mapping_sync import (
    JiandaoyunSchemaSyncError,
    apply_manual_widget_assignments,
    discover_jiandaoyun_authorization,
    fetch_jiandaoyun_form_schema_with_key,
    synchronize_mapping,
)

_TENANT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")
_ADMIN_WRITE_LOCK = Lock()


class TenantOnboardingRequest(BaseModel):
    tenant_id: str | None = Field(default=None, min_length=3, max_length=64)
    display_name: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    application_id: str = Field(min_length=1, max_length=100)
    entry_id: str = Field(min_length=1, max_length=100)
    entry_name: str = Field(min_length=1, max_length=100)
    jiandaoyun_api_key: SecretStr | None = None
    test_connection: bool = True
    rotate_access_key: bool = False
    rotate_webhook_secret: bool = False

    @field_validator(
        "tenant_id",
        "display_name",
        "application_id",
        "entry_id",
        "entry_name",
        mode="before",
    )
    @classmethod
    def strip_text(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("tenant_id")
    @classmethod
    def validate_tenant_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _TENANT_ID_PATTERN.fullmatch(value):
            raise ValueError("租户ID只能使用小写字母、数字、下划线和短横线，且以字母开头")
        return value


class JiandaoyunAuthorizationRequest(BaseModel):
    jiandaoyun_api_key: SecretStr


class JiandaoyunAuthorizationResponse(BaseModel):
    applications: list[dict[str, Any]]


class TenantFieldConfirmationRequest(BaseModel):
    assignments: dict[str, str] = Field(default_factory=dict, max_length=100)

    @field_validator("assignments")
    @classmethod
    def validate_assignments(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for path, widget_id in value.items():
            clean_path = path.strip()
            clean_widget_id = widget_id.strip()
            if not clean_path or not clean_widget_id.startswith("_widget_"):
                raise ValueError("手动映射必须包含有效字段路径和简道云字段ID")
            normalized[clean_path] = clean_widget_id
        return normalized


class OneTimeCredentials(BaseModel):
    access_key: str | None = None
    webhook_secret: str | None = None


class TenantOnboardingResult(BaseModel):
    tenant: dict[str, Any]
    connection_tested: bool
    mapping_report: dict[str, Any]
    activated: bool
    warnings: list[str]
    one_time_credentials: OneTimeCredentials


def list_tenants(settings: Settings) -> list[dict[str, Any]]:
    registry = settings.reload_tenant_registry()
    visible: list[dict[str, Any]] = []
    seen_forms: set[tuple[str, str]] = set()
    tenant_ids = sorted(
        registry.tenants,
        key=lambda tenant_id: (
            registry.tenants[tenant_id].created_at or datetime.max.replace(tzinfo=UTC),
            tenant_id,
        ),
    )
    for tenant_id in tenant_ids:
        identity = _tenant_form_identity(registry.tenants[tenant_id])
        if identity and identity in seen_forms:
            continue
        if identity:
            seen_forms.add(identity)
        visible.append(_tenant_summary(settings, tenant_id))
    return visible


def discover_authorized_forms(
    settings: Settings,
    request: JiandaoyunAuthorizationRequest,
) -> JiandaoyunAuthorizationResponse:
    api_key = request.jiandaoyun_api_key.get_secret_value().strip()
    if not api_key:
        raise ValueError("请填写简道云API Key")
    applications = discover_jiandaoyun_authorization(
        settings.jiandaoyun_base_url,
        settings.jiandaoyun_timeout_seconds,
        api_key,
    )
    if not any(application["forms"] for application in applications):
        raise JiandaoyunSchemaSyncError("该API Key的授权应用中没有可访问表单")
    return JiandaoyunAuthorizationResponse(
        applications=_annotate_connected_forms(settings, applications)
    )


def discover_tenant_authorized_forms(
    settings: Settings,
    tenant_id: str,
) -> JiandaoyunAuthorizationResponse:
    if not _TENANT_ID_PATTERN.fullmatch(tenant_id):
        raise ValueError("客户编号格式无效")
    registry = settings.reload_tenant_registry()
    tenant = registry.tenants.get(tenant_id)
    if tenant is None:
        raise ValueError("客户不存在或尚未完成首次接入")
    api_key = (
        tenant.jiandaoyun.api_key.get_secret_value()
        if tenant.jiandaoyun.api_key
        else None
    )
    if not api_key:
        raise ValueError("客户尚未配置简道云API Key")
    applications = discover_jiandaoyun_authorization(
        settings.jiandaoyun_base_url,
        settings.jiandaoyun_timeout_seconds,
        api_key,
    )
    if not any(application["forms"] for application in applications):
        raise JiandaoyunSchemaSyncError("该客户的授权应用中没有可访问表单")
    return JiandaoyunAuthorizationResponse(
        applications=_annotate_connected_forms(
            settings,
            applications,
            current_tenant_id=tenant_id,
        )
    )


def onboard_tenant(
    settings: Settings,
    request: TenantOnboardingRequest,
) -> TenantOnboardingResult:
    if not settings.tenant_registry_path:
        raise ValueError("未配置可写租户注册表路径")
    with _ADMIN_WRITE_LOCK:
        registry = settings.reload_tenant_registry()
        tenant_id = request.tenant_id or _generate_tenant_id(registry)
        existing = registry.tenants.get(tenant_id)
        requested_identity = (request.application_id, request.entry_id)
        current_identity = _tenant_form_identity(existing) if existing else None
        if current_identity != requested_identity:
            owner = _find_form_owner(
                registry,
                request.application_id,
                request.entry_id,
                exclude_tenant_id=tenant_id,
            )
            if owner:
                owner_id, owner_config = owner
                owner_name = owner_config.display_name or owner_id
                raise ValueError(
                    f"该表单已接入客户“{owner_name}”，同一个表单只能接入一次。"
                    "请在现有客户记录中修改配置。"
                )
        existing_api_key = (
            existing.jiandaoyun.api_key.get_secret_value()
            if existing and existing.jiandaoyun.api_key
            else None
        )
        supplied_api_key = (
            request.jiandaoyun_api_key.get_secret_value().strip()
            if request.jiandaoyun_api_key
            else None
        )
        api_key = supplied_api_key or existing_api_key
        if not api_key:
            raise ValueError("首次接入必须填写该客户的简道云API Key")

        access_keys = (
            [key.get_secret_value() for key in existing.access_keys] if existing else []
        )
        generated_access_key = None
        if existing is None or request.rotate_access_key:
            generated_access_key = "taor_" + secrets.token_urlsafe(32)
            access_keys = [generated_access_key, *access_keys[:1]]

        existing_webhook_secret = (
            existing.jiandaoyun.webhook_secret.get_secret_value()
            if existing and existing.jiandaoyun.webhook_secret
            else None
        )
        generated_webhook_secret = None
        if existing is None or request.rotate_webhook_secret:
            generated_webhook_secret = secrets.token_urlsafe(36)
        webhook_secret = generated_webhook_secret or existing_webhook_secret
        if not webhook_secret:
            raise ValueError("Webhook Secret生成失败")

        mapping = _base_mapping(settings, existing)
        mapping.update(
            {
                "source_application_id": request.application_id,
                "source_entry_id": request.entry_id,
                "source_entry_name": request.entry_name,
            }
        )
        mapping_report: dict[str, Any] = {
            "matched_count": 0,
            "unresolved_count": 0,
            "matched": [],
            "unresolved": [],
        }
        warnings: list[str] = []
        if request.test_connection:
            schema = fetch_jiandaoyun_form_schema_with_key(
                settings.jiandaoyun_base_url,
                settings.jiandaoyun_timeout_seconds,
                api_key,
                request.application_id,
                request.entry_id,
            )
            mapping, mapping_report = synchronize_mapping(mapping, schema)
            mapping.update(
                {
                    "source_application_id": request.application_id,
                    "source_entry_id": request.entry_id,
                    "source_entry_name": request.entry_name,
                }
            )
            if mapping_report["unresolved_count"]:
                warnings.append("存在未匹配字段，租户已保存为停用状态，请确认字段后再启用。")
        else:
            mapping["status"] = "pending_connection_test"
            warnings.append("尚未测试简道云连接，租户已保存为停用状态。")

        activated = bool(
            request.enabled
            and request.test_connection
            and mapping_report["unresolved_count"] == 0
        )
        mapping_path = _versioned_mapping_path(settings, tenant_id, mapping)
        _atomic_write_json(mapping_path, mapping)

        now = datetime.now(UTC)
        registry_document = _registry_document(registry)
        registry_document["tenants"][tenant_id] = {
            "enabled": activated,
            "display_name": request.display_name,
            "created_at": (
                existing.created_at.isoformat() if existing and existing.created_at else now.isoformat()
            ),
            "updated_at": now.isoformat(),
            "access_keys": access_keys,
            "jiandaoyun": {
                "api_key": api_key,
                "webhook_secret": webhook_secret,
                "mapping_path": str(mapping_path),
            },
        }
        validated = TenantConfigRegistry.model_validate(registry_document)
        _atomic_write_json(Path(settings.tenant_registry_path), _registry_document(validated))
        settings.reload_tenant_registry()
        _append_audit(
            Path(settings.admin_audit_path),
            {
                "timestamp": now.isoformat(),
                "action": "create" if existing is None else "update",
                "tenant_id": tenant_id,
                "enabled": activated,
                "connection_tested": request.test_connection,
                "matched_count": mapping_report["matched_count"],
                "unresolved_count": mapping_report["unresolved_count"],
                "access_key_rotated": generated_access_key is not None,
                "webhook_secret_rotated": generated_webhook_secret is not None,
            },
        )
        return TenantOnboardingResult(
            tenant=_tenant_summary(settings, tenant_id),
            connection_tested=request.test_connection,
            mapping_report=mapping_report,
            activated=activated,
            warnings=warnings,
            one_time_credentials=OneTimeCredentials(
                access_key=generated_access_key,
                webhook_secret=generated_webhook_secret,
            ),
        )


def confirm_tenant_fields(
    settings: Settings,
    tenant_id: str,
    request: TenantFieldConfirmationRequest,
) -> TenantOnboardingResult:
    if not settings.tenant_registry_path:
        raise ValueError("未配置可写租户注册表路径")
    if not _TENANT_ID_PATTERN.fullmatch(tenant_id):
        raise ValueError("客户编号格式无效")
    with _ADMIN_WRITE_LOCK:
        registry = settings.reload_tenant_registry()
        existing = registry.tenants.get(tenant_id)
        if existing is None:
            raise ValueError("客户不存在或尚未完成首次接入")
        api_key = (
            existing.jiandaoyun.api_key.get_secret_value()
            if existing.jiandaoyun.api_key
            else None
        )
        mapping_path = existing.jiandaoyun.mapping_path
        if not api_key or not mapping_path or not Path(mapping_path).is_file():
            raise ValueError("客户的简道云连接或字段映射配置不完整")
        mapping = load_jiandaoyun_mapping(mapping_path)
        application_id = mapping.get("source_application_id")
        entry_id = mapping.get("source_entry_id")
        if not application_id or not entry_id:
            raise ValueError("客户映射中缺少应用ID或表单ID")
        schema = fetch_jiandaoyun_form_schema_with_key(
            settings.jiandaoyun_base_url,
            settings.jiandaoyun_timeout_seconds,
            api_key,
            application_id,
            entry_id,
        )
        if request.assignments:
            updated, mapping_report = apply_manual_widget_assignments(
                mapping,
                schema,
                request.assignments,
            )
        else:
            updated, mapping_report = synchronize_mapping(mapping, schema)
        updated.update(
            {
                "source_application_id": application_id,
                "source_entry_id": entry_id,
                "source_entry_name": mapping.get("source_entry_name"),
            }
        )
        activated = mapping_report["unresolved_count"] == 0
        new_mapping_path = _versioned_mapping_path(settings, tenant_id, updated)
        _atomic_write_json(new_mapping_path, updated)
        now = datetime.now(UTC)
        registry_document = _registry_document(registry)
        tenant_document = registry_document["tenants"][tenant_id]
        tenant_document["enabled"] = activated
        tenant_document["updated_at"] = now.isoformat()
        tenant_document["jiandaoyun"]["mapping_path"] = str(new_mapping_path)
        validated = TenantConfigRegistry.model_validate(registry_document)
        _atomic_write_json(Path(settings.tenant_registry_path), _registry_document(validated))
        settings.reload_tenant_registry()
        _append_audit(
            Path(settings.admin_audit_path),
            {
                "timestamp": now.isoformat(),
                "action": "confirm_field_mappings" if request.assignments else "recheck_fields",
                "tenant_id": tenant_id,
                "enabled": activated,
                "assignment_count": len(request.assignments),
                "matched_count": mapping_report["matched_count"],
                "unresolved_count": mapping_report["unresolved_count"],
            },
        )
        warnings = []
        if not activated:
            warnings.append("仍有待确认字段，客户保持停用；请继续映射或在简道云修改字段后重新检查。")
        return TenantOnboardingResult(
            tenant=_tenant_summary(settings, tenant_id),
            connection_tested=True,
            mapping_report=mapping_report,
            activated=activated,
            warnings=warnings,
            one_time_credentials=OneTimeCredentials(),
        )


def _generate_tenant_id(registry: TenantConfigRegistry) -> str:
    for _ in range(20):
        tenant_id = f"tenant_{secrets.token_hex(6)}"
        if tenant_id not in registry.tenants:
            return tenant_id
    raise RuntimeError("无法生成唯一客户编号")


def _tenant_form_identity(tenant) -> tuple[str, str] | None:
    if tenant is None or not tenant.jiandaoyun.mapping_path:
        return None
    mapping_path = Path(tenant.jiandaoyun.mapping_path)
    if not mapping_path.is_file():
        return None
    mapping = load_jiandaoyun_mapping(str(mapping_path))
    application_id = str(mapping.get("source_application_id") or "").strip()
    entry_id = str(mapping.get("source_entry_id") or "").strip()
    return (application_id, entry_id) if application_id and entry_id else None


def _find_form_owner(
    registry: TenantConfigRegistry,
    application_id: str,
    entry_id: str,
    *,
    exclude_tenant_id: str | None = None,
):
    matches = []
    for tenant_id, tenant in registry.tenants.items():
        if tenant_id == exclude_tenant_id:
            continue
        if _tenant_form_identity(tenant) == (application_id, entry_id):
            matches.append((tenant_id, tenant))
    if not matches:
        return None
    return min(
        matches,
        key=lambda item: (
            item[1].created_at or datetime.max.replace(tzinfo=UTC),
            item[0],
        ),
    )


def _annotate_connected_forms(
    settings: Settings,
    applications: list[dict[str, Any]],
    *,
    current_tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    registry = settings.reload_tenant_registry()
    current = registry.tenants.get(current_tenant_id) if current_tenant_id else None
    current_identity = _tenant_form_identity(current)
    annotated = json.loads(json.dumps(applications, ensure_ascii=False))
    for application in annotated:
        application_id = str(application.get("app_id") or "")
        for form in application.get("forms", []):
            identity = (application_id, str(form.get("entry_id") or ""))
            owner = None
            if identity != current_identity:
                owner = _find_form_owner(
                    registry,
                    *identity,
                    exclude_tenant_id=current_tenant_id,
                )
            form["already_connected"] = owner is not None
            if owner:
                owner_id, owner_config = owner
                form["connected_tenant_id"] = owner_id
                form["connected_display_name"] = owner_config.display_name or owner_id
    return annotated


def _base_mapping(settings: Settings, existing) -> dict[str, Any]:
    if existing and existing.jiandaoyun.mapping_path:
        existing_path = Path(existing.jiandaoyun.mapping_path)
        if existing_path.is_file():
            return load_jiandaoyun_mapping(str(existing_path))
    return load_jiandaoyun_mapping(settings.jiandaoyun_mapping_path)


def _versioned_mapping_path(
    settings: Settings,
    tenant_id: str,
    mapping: dict[str, Any],
) -> Path:
    registry_path = Path(settings.tenant_registry_path or "")
    content = json.dumps(mapping, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    return registry_path.parent / "tenants" / tenant_id / f"field_mapping.{digest}.json"


def _tenant_summary(settings: Settings, tenant_id: str) -> dict[str, Any]:
    tenant = settings.tenant_config(tenant_id)
    if tenant is None:
        raise ValueError("租户不存在")
    mapping: dict[str, Any] = {}
    mapping_path = tenant.jiandaoyun.mapping_path
    if mapping_path and Path(mapping_path).is_file():
        mapping = load_jiandaoyun_mapping(mapping_path)
    return {
        "tenant_id": tenant_id,
        "display_name": tenant.display_name or tenant_id,
        "enabled": tenant.enabled,
        "created_at": tenant.created_at.isoformat() if tenant.created_at else None,
        "updated_at": tenant.updated_at.isoformat() if tenant.updated_at else None,
        "access_key_count": len(tenant.access_keys),
        "jiandaoyun": {
            "api_key_configured": bool(tenant.jiandaoyun.api_key),
            "webhook_secret_configured": bool(tenant.jiandaoyun.webhook_secret),
            "mapping_configured": bool(mapping_path),
            "application_id": mapping.get("source_application_id"),
            "entry_id": mapping.get("source_entry_id"),
            "entry_name": mapping.get("source_entry_name"),
            "mapping_version": mapping.get("mapping_version"),
            "mapping_status": mapping.get("status"),
        },
    }


def _registry_document(registry: TenantConfigRegistry) -> dict[str, Any]:
    tenants: dict[str, Any] = {}
    for tenant_id, tenant in registry.tenants.items():
        tenants[tenant_id] = {
            "enabled": tenant.enabled,
            "display_name": tenant.display_name,
            "created_at": tenant.created_at.isoformat() if tenant.created_at else None,
            "updated_at": tenant.updated_at.isoformat() if tenant.updated_at else None,
            "access_keys": [key.get_secret_value() for key in tenant.access_keys],
            "jiandaoyun": {
                "api_key": (
                    tenant.jiandaoyun.api_key.get_secret_value()
                    if tenant.jiandaoyun.api_key
                    else None
                ),
                "webhook_secret": (
                    tenant.jiandaoyun.webhook_secret.get_secret_value()
                    if tenant.jiandaoyun.webhook_secret
                    else None
                ),
                "mapping_path": tenant.jiandaoyun.mapping_path,
            },
        }
    return {"version": registry.version, "tenants": tenants}


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            json.dump(payload, temp_file, ensure_ascii=False, indent=2)
            temp_file.write("\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temporary_path = Path(temp_file.name)
        temporary_path.chmod(0o600)
        temporary_path.replace(path)
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()


def _append_audit(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as audit_file:
        audit_file.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    path.chmod(0o600)


__all__ = [
    "JiandaoyunAuthorizationRequest",
    "JiandaoyunAuthorizationResponse",
    "JiandaoyunSchemaSyncError",
    "TenantFieldConfirmationRequest",
    "TenantOnboardingRequest",
    "TenantOnboardingResult",
    "confirm_tenant_fields",
    "discover_authorized_forms",
    "discover_tenant_authorized_forms",
    "list_tenants",
    "onboard_tenant",
]
