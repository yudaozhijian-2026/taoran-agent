import httpx
import pytest

from taoran_agent.config import Settings
from taoran_agent.jiandaoyun_api import JiandaoyunReadError, get_jiandaoyun_record


def test_get_jiandaoyun_record_uses_single_record_endpoint(monkeypatch) -> None:
    captured = {}

    def fake_post(url, *, headers, json, timeout):
        captured.update(url=url, headers=headers, json=json, timeout=timeout)
        return httpx.Response(
            200,
            json={"data": {"_id": "DATA001", "createTime": "2026-08-19T00:00:00Z"}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("taoran_agent.jiandaoyun_api.httpx.post", fake_post)
    settings = Settings(
        jiandaoyun_api_keys_json='{"tenant_demo":"jdy-secret"}',
    )

    record = get_jiandaoyun_record(
        settings,
        "tenant_demo",
        "APP001",
        "ENTRY001",
        "DATA001",
    )

    assert record["_id"] == "DATA001"
    assert captured["url"].endswith("/v5/app/entry/data/get")
    assert captured["headers"]["Authorization"] == "Bearer jdy-secret"
    assert captured["json"] == {
        "app_id": "APP001",
        "entry_id": "ENTRY001",
        "data_id": "DATA001",
    }


def test_get_jiandaoyun_record_rejects_mismatched_response(monkeypatch) -> None:
    def fake_post(url, *, headers, json, timeout):
        return httpx.Response(
            200,
            json={"data": {"_id": "OTHER"}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("taoran_agent.jiandaoyun_api.httpx.post", fake_post)
    settings = Settings(
        jiandaoyun_api_keys_json='{"tenant_demo":"jdy-secret"}',
    )

    with pytest.raises(JiandaoyunReadError, match="数据ID不匹配"):
        get_jiandaoyun_record(
            settings,
            "tenant_demo",
            "APP001",
            "ENTRY001",
            "DATA001",
        )
