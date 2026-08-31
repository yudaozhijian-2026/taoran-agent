import pytest
from pydantic import ValidationError

from taoran_agent.config import Settings, TenantConfig


def test_settings_loads_jiandaoyun_key_from_dotenv(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        'DSM_TAORAN_JIANDAOYUN_API_KEYS_JSON={"tenant_demo":"secret-from-env"}\n',
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.jiandaoyun_api_keys["tenant_demo"] == "secret-from-env"


def test_tenant_cannot_override_company_submission_timeliness_policy() -> None:
    with pytest.raises(ValidationError):
        TenantConfig.model_validate(
            {"enabled": True, "submission_timeliness_hours": 48}
        )


def test_knowledge_api_key_is_redacted_from_settings_output() -> None:
    settings = Settings(
        _env_file=None,
        knowledge_api_key="synthetic-knowledge-secret",
    )

    assert settings.knowledge_api_key.get_secret_value() == "synthetic-knowledge-secret"
    assert "synthetic-knowledge-secret" not in repr(settings)
    assert "synthetic-knowledge-secret" not in settings.model_dump_json()


def test_precheck_model_has_20_second_and_compact_output_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.llm_precheck_timeout_seconds == 20
    assert settings.llm_precheck_max_output_tokens == 2200
    assert settings.llm_max_output_tokens == 3000
    assert settings.knowledge_snapshot_cache_seconds == 30
