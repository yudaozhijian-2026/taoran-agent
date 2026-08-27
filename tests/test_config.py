from taoran_agent.config import Settings


def test_settings_loads_jiandaoyun_key_from_dotenv(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        'DSM_TAORAN_JIANDAOYUN_API_KEYS_JSON={"tenant_demo":"secret-from-env"}\n',
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.jiandaoyun_api_keys["tenant_demo"] == "secret-from-env"


def test_knowledge_api_key_is_redacted_from_settings_output() -> None:
    settings = Settings(
        _env_file=None,
        knowledge_api_key="synthetic-knowledge-secret",
    )

    assert settings.knowledge_api_key.get_secret_value() == "synthetic-knowledge-secret"
    assert "synthetic-knowledge-secret" not in repr(settings)
    assert "synthetic-knowledge-secret" not in settings.model_dump_json()
