import json

import pytest

from taoran_agent.config import Settings


def test_submit_mode_is_not_allowed_on_production():
    with pytest.raises(ValueError, match="独立测试"):
        Settings(_env_file=None, environment="production", submit_confirmation_enabled=True)


def test_isolated_registry_must_match_form(tmp_path):
    mapping = tmp_path / "mapping.json"
    mapping.write_text(
        json.dumps(
            {"source_application_id": "60fe7ad79ca2d000075dfab1", "source_entry_id": "test-form"}
        )
    )
    registry = json.dumps({"tenants": {"test": {"jiandaoyun": {"mapping_path": str(mapping)}}}})
    args = {
        "_env_file": None,
        "environment": "isolated-submit-test",
        "submit_confirmation_enabled": True,
        "isolated_test_entry_id": "test-form",
        "tenant_registry_json": registry,
    }
    assert Settings(**args).submit_confirmation_enabled
    mapping.write_text(json.dumps({"source_entry_id": "production-form"}))
    with pytest.raises(ValueError, match="白名单"):
        Settings(**args)


def test_empty_registry_cannot_start_isolated_service():
    with pytest.raises(ValueError, match="一个测试租户"):
        Settings(_env_file=None, isolated_test_entry_id="test-form")
