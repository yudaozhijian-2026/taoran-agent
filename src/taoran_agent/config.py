from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


class JiandaoyunTenantConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: SecretStr | None = None
    webhook_secret: SecretStr | None = None
    mapping_path: str | None = None

    @field_validator("mapping_path", mode="before")
    @classmethod
    def normalize_mapping_path(cls, value):
        return value.strip() or None if isinstance(value, str) else value


class TenantConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    display_name: str | None = Field(default=None, max_length=100)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    access_keys: list[SecretStr] = Field(default_factory=list, max_length=2)
    jiandaoyun: JiandaoyunTenantConfig = Field(default_factory=JiandaoyunTenantConfig)

    @field_validator("access_keys")
    @classmethod
    def validate_access_keys(cls, values: list[SecretStr]) -> list[SecretStr]:
        normalized = [value.get_secret_value().strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("租户访问Key不能为空")
        if len(normalized) != len(set(normalized)):
            raise ValueError("租户访问Key不能重复")
        return [SecretStr(value) for value in normalized]


class TenantConfigRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    tenants: dict[str, TenantConfig] = Field(default_factory=dict)

    @field_validator("tenants")
    @classmethod
    def validate_tenant_ids(cls, tenants: dict[str, TenantConfig]) -> dict[str, TenantConfig]:
        if any(not tenant_id.strip() or tenant_id != tenant_id.strip() for tenant_id in tenants):
            raise ValueError("租户ID不能为空或包含首尾空格")
        return tenants


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DSM_TAORAN_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = "development"
    database_path: str = str(Path("data") / "taoran_agent.db")
    admin_enabled: bool = False
    admin_api_key: SecretStr | None = None
    admin_audit_path: str = str(Path("data") / "tenant_admin_audit.jsonl")
    tenant_registry_path: str | None = None
    tenant_registry_json: str = '{"version":1,"tenants":{}}'
    tenant_keys_json: str = "{}"
    jiandaoyun_api_keys_json: str = "{}"
    jiandaoyun_webhook_secret: str | None = None
    jiandaoyun_base_url: str = "https://api.jiandaoyun.com/api"
    jiandaoyun_timeout_seconds: float = Field(default=10.0, gt=0, le=30)
    semantic_endpoint: str | None = None
    semantic_api_key: str | None = None
    semantic_timeout_seconds: float = Field(default=6.0, gt=0, le=10)
    precheck_budget_seconds: float = Field(default=25.0, gt=0, le=28)
    llm_enabled: bool = False
    llm_api_url: str | None = None
    llm_api_key: SecretStr | None = None
    llm_model: str | None = None
    llm_precheck_timeout_seconds: float = Field(default=20.0, gt=0, le=20)
    llm_evaluation_timeout_seconds: float = Field(default=45.0, gt=0, le=90)
    llm_max_concurrency: int = Field(default=4, ge=1, le=8)
    llm_button_queue_capacity: int = Field(default=8, ge=0, le=50)
    llm_button_queue_wait_seconds: float = Field(default=12.0, gt=0, le=30)
    llm_max_input_chars: int = Field(default=24000, ge=1000, le=60000)
    llm_precheck_max_output_tokens: int = Field(default=2200, ge=1000, le=3000)
    llm_max_output_tokens: int = Field(default=3000, ge=500, le=6000)
    llm_format_retries: int = Field(default=1, ge=0, le=1)
    knowledge_api_base_url: str = "https://knowledge.api.yudaozhijian.top"
    knowledge_api_key: SecretStr | None = None
    knowledge_timeout_seconds: float = Field(default=10.0, gt=0, le=30)
    knowledge_snapshot_cache_seconds: float = Field(default=30.0, ge=0, le=300)
    knowledge_snapshot_path: str | None = None
    jiandaoyun_mapping_path: str | None = None
    enable_q40_integration: bool = False
    q40_service_id: str = "dsm-q40-agent"
    q40_service_keys_json: str = "{}"
    shared_store_url: str | None = None
    _tenant_registry_cache: TenantConfigRegistry = PrivateAttr(
        default_factory=TenantConfigRegistry
    )

    @field_validator("llm_api_url", "llm_api_key", "llm_model", mode="before")
    @classmethod
    def normalize_llm_settings(cls, value):
        return value.strip() or None if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_llm_settings(self) -> Settings:
        if self.llm_api_url:
            url = urlsplit(self.llm_api_url)
            if (
                url.scheme != "https" or not url.hostname or url.username
                or url.password or url.query or url.fragment
            ):
                raise ValueError("模型接口必须为HTTPS地址，不能在网址中携带密钥或查询参数")
        if self.llm_enabled:
            if not (self.llm_api_url and self.llm_api_key and self.llm_model):
                raise ValueError("启用大模型前必须配置TAORAN专用接口、模型名称和API Key")
            if self.semantic_endpoint:
                raise ValueError("直接大模型与旧版语义服务不能同时启用，请只配置一种")
        if self.admin_enabled:
            if not self.admin_api_key:
                raise ValueError("启用客户接入管理页前必须配置独立管理员Key")
            if not self.tenant_registry_path:
                raise ValueError("启用客户接入管理页前必须配置可写租户注册表路径")
        self._tenant_registry_cache = self._read_tenant_registry()
        return self

    @field_validator(
        "tenant_keys_json",
        "jiandaoyun_api_keys_json",
        "q40_service_keys_json",
    )
    @classmethod
    def validate_tenant_keys(cls, value: str) -> str:
        parsed = json.loads(value)
        if not isinstance(parsed, dict) or not all(
            isinstance(key, str) and isinstance(secret, str) for key, secret in parsed.items()
        ):
            raise ValueError("租户密钥配置必须是字符串到字符串的JSON对象")
        return value

    @property
    def tenant_keys(self) -> dict[str, str]:
        return json.loads(self.tenant_keys_json)

    @property
    def jiandaoyun_api_keys(self) -> dict[str, str]:
        return json.loads(self.jiandaoyun_api_keys_json)

    @property
    def q40_service_keys(self) -> dict[str, str]:
        return json.loads(self.q40_service_keys_json)

    @property
    def tenant_registry(self) -> TenantConfigRegistry:
        return self._tenant_registry_cache

    def tenant_config(self, tenant_id: str) -> TenantConfig | None:
        return self.tenant_registry.tenants.get(tenant_id)

    def tenant_access_keys_for(self, tenant_id: str) -> list[str]:
        tenant = self.tenant_config(tenant_id)
        if tenant is not None:
            return [key.get_secret_value() for key in tenant.access_keys]
        legacy_key = self.tenant_keys.get(tenant_id)
        return [legacy_key] if legacy_key else []

    @property
    def has_tenant_access_configuration(self) -> bool:
        return bool(self.tenant_keys) or bool(self.tenant_registry.tenants)

    def jiandaoyun_api_key_for(self, tenant_id: str) -> str | None:
        tenant = self.tenant_config(tenant_id)
        if tenant is not None:
            api_key = tenant.jiandaoyun.api_key
            return api_key.get_secret_value() if api_key else None
        return self.jiandaoyun_api_keys.get(tenant_id)

    def jiandaoyun_webhook_secret_for(self, tenant_id: str) -> str | None:
        tenant = self.tenant_config(tenant_id)
        if tenant is not None:
            secret = tenant.jiandaoyun.webhook_secret
            return secret.get_secret_value() if secret else None
        return self.jiandaoyun_webhook_secret

    def jiandaoyun_mapping_path_for(self, tenant_id: str) -> str | None:
        tenant = self.tenant_config(tenant_id)
        if tenant is not None:
            return tenant.jiandaoyun.mapping_path
        return self.jiandaoyun_mapping_path

    def tenant_configuration_source(self, tenant_id: str) -> str:
        return "registry" if self.tenant_config(tenant_id) else "legacy"

    def reload_tenant_registry(self) -> TenantConfigRegistry:
        self._tenant_registry_cache = self._read_tenant_registry()
        return self._tenant_registry_cache

    def _read_tenant_registry(self) -> TenantConfigRegistry:
        try:
            if self.tenant_registry_path:
                registry_path = Path(self.tenant_registry_path)
                if self.admin_enabled and not registry_path.exists():
                    return TenantConfigRegistry()
                raw = registry_path.read_text(encoding="utf-8")
            else:
                raw = self.tenant_registry_json
            return TenantConfigRegistry.model_validate_json(raw)
        except (OSError, ValueError) as exc:
            raise ValueError("租户注册表无法读取或格式无效") from exc


@lru_cache
def get_settings() -> Settings:
    return Settings()
