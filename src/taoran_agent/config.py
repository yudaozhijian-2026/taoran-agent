from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
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
    jiandaoyun_base_url: str = "https://api.jiandaoyun.com/api"
    jiandaoyun_timeout_seconds: float = Field(default=10.0, gt=0, le=30)
    semantic_endpoint: str | None = None
    semantic_api_key: str | None = None
    semantic_timeout_seconds: float = Field(default=6.0, gt=0, le=10)
    precheck_budget_seconds: float = Field(default=12.0, gt=0, le=14)
    jiandaoyun_mapping_path: str | None = None
    enable_q40_integration: bool = False
    q40_service_id: str = "dsm-q40-agent"
    q40_service_keys_json: str = "{}"
    shared_store_url: str | None = None

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
