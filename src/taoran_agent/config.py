from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DSM_TAORAN_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = "development"
    database_path: str = str(Path("data") / "taoran_agent.db")
    tenant_keys_json: str = "{}"
    jiandaoyun_api_keys_json: str = "{}"
    jiandaoyun_webhook_secret: str | None = None
    jiandaoyun_base_url: str = "https://api.jiandaoyun.com/api"
    jiandaoyun_timeout_seconds: float = Field(default=10.0, gt=0, le=30)
    semantic_endpoint: str | None = None
    semantic_api_key: str | None = None
    semantic_timeout_seconds: float = Field(default=6.0, gt=0, le=10)
    precheck_budget_seconds: float = Field(default=12.0, gt=0, le=14)
    llm_enabled: bool = False
    llm_api_url: str | None = None
    llm_api_key: SecretStr | None = None
    llm_model: str | None = None
    llm_precheck_timeout_seconds: float = Field(default=6.0, gt=0, le=10)
    llm_evaluation_timeout_seconds: float = Field(default=45.0, gt=0, le=90)
    llm_max_concurrency: int = Field(default=2, ge=1, le=8)
    llm_max_input_chars: int = Field(default=24000, ge=1000, le=60000)
    llm_max_output_tokens: int = Field(default=3000, ge=500, le=6000)
    llm_format_retries: int = Field(default=1, ge=0, le=1)
    knowledge_api_base_url: str = "https://knowledge.api.yudaozhijian.top"
    knowledge_api_key: str | None = None
    knowledge_timeout_seconds: float = Field(default=10.0, gt=0, le=30)
    knowledge_snapshot_path: str | None = None
    jiandaoyun_mapping_path: str | None = None
    enable_q40_integration: bool = False
    q40_service_id: str = "dsm-q40-agent"
    q40_service_keys_json: str = "{}"
    shared_store_url: str | None = None

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
