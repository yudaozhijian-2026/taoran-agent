import json
from pathlib import Path

from taoran_agent.config import Settings
from taoran_agent.mapping_sync import (
    discover_jiandaoyun_authorization,
    fetch_jiandaoyun_form_schema,
    synchronize_mapping,
)


def test_mapping_is_synchronized_by_labels_and_subform_children() -> None:
    mapping = json.loads(
        Path("config/jiandaoyun_field_mapping.example.json").read_text(encoding="utf-8")
    )
    schema = {
        "dataModifyTime": "2026-08-18T12:00:00.000Z",
        "widgets": [
            {
                "label": "拜访日期",
                "name": "_widget_visit_date",
                "widgetName": "_widget_visit_date",
                "type": "datetime",
            },
            {
                "label": "联系人信息",
                "name": "_widget_contacts",
                "widgetName": "_widget_contacts",
                "type": "subform",
                "items": [
                    {
                        "label": "关联数据-主键",
                        "name": "_widget_contact_id",
                        "widgetName": "_widget_contact_id",
                        "type": "text",
                    }
                ],
            },
            {
                "label": "AI评分",
                "name": "_widget_1787037882562",
                "widgetName": "_widget_1787037882562",
                "type": "number",
            },
            {
                "label": "AI反馈意见（规则反馈）",
                "name": "_widget_1787037882560",
                "widgetName": "_widget_1787037882560",
                "type": "textarea",
            },
            {
                "label": "AI反馈意见（知识库反馈）",
                "name": "_widget_1787803259012",
                "widgetName": "_widget_1787803259012",
                "type": "textarea",
            },
            {
                "label": "AI反馈意见（大模型反馈）",
                "name": "_widget_1787803259013",
                "widgetName": "_widget_1787803259013",
                "type": "textarea",
            },
        ],
    }

    updated, report = synchronize_mapping(mapping, schema)

    assert updated["fields"]["visit_date"]["widget_id"] == "_widget_visit_date"
    assert updated["subforms"]["participants"]["field"]["widget_id"] == "_widget_contacts"
    assert (
        updated["subforms"]["participants"]["children"]["contact_id"]["widget_id"]
        == "_widget_contact_id"
    )
    assert updated["output_fields"]["total_score"]["widget_type"] == "number"
    assert updated["output_fields"]["knowledge_feedback"]["widget_id"] == "_widget_1787803259012"
    assert updated["output_fields"]["model_feedback"]["widget_id"] == "_widget_1787803259013"
    assert updated["status"] == "copy_widget_ids_partially_synced"
    assert report["matched_count"] == 7
    assert report["unresolved_count"] > 0


def test_form_schema_is_fetched_from_v5_endpoint(monkeypatch) -> None:
    captured = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"widgets": []}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return Response()

    monkeypatch.setattr("taoran_agent.mapping_sync.httpx.post", fake_post)
    settings = Settings(jiandaoyun_api_keys_json='{"tenant_demo":"secret"}')

    result = fetch_jiandaoyun_form_schema(
        settings,
        "tenant_demo",
        "60fe7ad79ca2d000075dfab1",
        "6a8408b7c5a0d9454090a5bc",
    )

    assert result == {"widgets": []}
    assert captured["url"].endswith("/v5/app/entry/widget/list")
    assert captured["kwargs"]["json"]["entry_id"] == "6a8408b7c5a0d9454090a5bc"
    assert captured["kwargs"]["headers"]["Authorization"] == "Bearer secret"


def test_authorization_discovery_uses_visible_apps_and_forms(monkeypatch) -> None:
    captured = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self.payload

    def fake_post(url, **kwargs):
        captured.append((url, kwargs))
        if url.endswith("/v5/app/list"):
            return Response({"apps": [{"app_id": "app-a", "name": "销售管理"}]})
        return Response(
            {
                "forms": [
                    {
                        "app_id": "app-a",
                        "entry_id": "entry-a",
                        "name": "拜访记录",
                    }
                ]
            }
        )

    monkeypatch.setattr("taoran_agent.mapping_sync.httpx.post", fake_post)

    result = discover_jiandaoyun_authorization(
        "https://api.jiandaoyun.com/api",
        10,
        "secret",
    )

    assert result[0]["app_id"] == "app-a"
    assert result[0]["forms"][0]["entry_id"] == "entry-a"
    assert captured[0][1]["headers"]["Authorization"] == "Bearer secret"
    assert captured[0][1]["json"] == {"limit": 100, "skip": 0}
    assert captured[1][1]["json"] == {"app_id": "app-a", "limit": 100, "skip": 0}
