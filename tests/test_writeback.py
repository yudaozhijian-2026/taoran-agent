import json

from test_agent import complete_precheck_payload

from taoran_agent import TaoranAgent
from taoran_agent.config import Settings
from taoran_agent.models import PostEvaluationRequest
from taoran_agent.writeback import writeback_evaluation


def evaluation_request() -> PostEvaluationRequest:
    payload = complete_precheck_payload("writeback-001")
    payload["visit"].update(
        {
            "actual_start_at": "2026-08-18T09:00:00+08:00",
            "actual_end_at": "2026-08-18T10:00:00+08:00",
            "submitted_at": "2026-08-18T11:00:00+08:00",
        }
    )
    return PostEvaluationRequest.model_validate(
        {
            "context": payload["context"],
            "visit_record_code": "BFJL001",
            "visit": payload["visit"],
            "opportunity_updated": True,
            "writeback_target": {
                "app_id": "app-1",
                "entry_id": "entry-1",
                "data_id": "data-1",
            },
        }
    )


def test_evaluation_is_written_to_configured_jiandaoyun_fields(tmp_path, monkeypatch) -> None:
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(
        json.dumps(
            {
                "fields": {},
                "output_fields": {
                    "q33_score": "_widget_q33",
                    "q34_score": "_widget_q34",
                    "total_score": "_widget_total",
                    "ai_opinion": "_widget_opinion",
                },
            }
        ),
        encoding="utf-8",
    )
    captured = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return Response()

    monkeypatch.setattr("taoran_agent.writeback.httpx.post", fake_post)
    request = evaluation_request()
    evaluation = TaoranAgent().evaluate(request, "job-writeback")
    settings = Settings(
        database_path=":memory:",
        jiandaoyun_api_keys_json='{"tenant_demo":"secret"}',
        jiandaoyun_mapping_path=str(mapping_path),
    )

    result = writeback_evaluation(settings, request, evaluation)

    assert result.status == "succeeded"
    assert result.written_fields == [
        "_widget_opinion",
        "_widget_q33",
        "_widget_q34",
        "_widget_total",
    ]
    assert captured["url"].endswith("/v5/app/entry/data/update")
    body = captured["kwargs"]["json"]
    assert body["data_id"] == "data-1"
    assert body["data"]["_widget_total"]["value"] == 100
    assert body["data"]["_widget_q33"]["value"] == 50
    assert body["data"]["_widget_q34"]["value"] == 50
    assert body["is_start_trigger"] is False


def test_writeback_reports_missing_tenant_key() -> None:
    request = evaluation_request()
    evaluation = TaoranAgent().evaluate(request, "job-writeback-missing-key")

    result = writeback_evaluation(
        Settings(
            database_path=":memory:",
            jiandaoyun_api_keys_json="{}",
            _env_file=None,
        ),
        request,
        evaluation,
    )

    assert result.status == "failed"
    assert "API密钥" in (result.error_message or "")


def test_writeback_rejects_placeholder_output_fields(tmp_path) -> None:
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(
        json.dumps(
            {
                "fields": {},
                "output_fields": {"total_score": "_widget_replace_ai_total_score"},
            }
        ),
        encoding="utf-8",
    )
    request = evaluation_request()
    evaluation = TaoranAgent().evaluate(request, "job-writeback-placeholder")
    settings = Settings(
        database_path=":memory:",
        jiandaoyun_api_keys_json='{"tenant_demo":"secret"}',
        jiandaoyun_mapping_path=str(mapping_path),
    )

    result = writeback_evaluation(settings, request, evaluation)

    assert result.status == "failed"
    assert "占位ID" in (result.error_message or "")


def test_writeback_rejects_new_copy_field_names_without_widget_ids(tmp_path) -> None:
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(
        json.dumps(
            {
                "fields": {},
                "output_fields": {
                    "total_score": {"field_name": "AI评分", "widget_id": None},
                    "ai_opinion": {"field_name": "AI反馈意见", "widget_id": None},
                },
            }
        ),
        encoding="utf-8",
    )
    request = evaluation_request()
    evaluation = TaoranAgent().evaluate(request, "job-writeback-copy-no-widget")
    settings = Settings(
        database_path=":memory:",
        jiandaoyun_api_keys_json='{"tenant_demo":"secret"}',
        jiandaoyun_mapping_path=str(mapping_path),
    )

    result = writeback_evaluation(settings, request, evaluation)

    assert result.status == "failed"
    assert "widget ID尚未配置" in (result.error_message or "")


def test_active_copy_mapping_writes_only_confirmed_ai_fields(monkeypatch) -> None:
    captured = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

    def fake_post(url, **kwargs):
        captured["body"] = kwargs["json"]
        return Response()

    monkeypatch.setattr("taoran_agent.writeback.httpx.post", fake_post)
    request = evaluation_request()
    request = request.model_copy(
        update={
            "writeback_target": request.writeback_target.model_copy(
                update={
                    "app_id": "60fe7ad79ca2d000075dfab1",
                    "entry_id": "6a8408b7c5a0d9454090a5bc",
                }
            )
        }
    )
    evaluation = TaoranAgent().evaluate(request, "job-active-copy-writeback")
    settings = Settings(
        database_path=":memory:",
        jiandaoyun_api_keys_json='{"tenant_demo":"secret"}',
        jiandaoyun_mapping_path="config/jiandaoyun_field_mapping.example.json",
    )

    result = writeback_evaluation(settings, request, evaluation)

    assert result.status == "succeeded"
    assert set(captured["body"]["data"]) == {
        "_widget_1787037882560",
        "_widget_1787037882562",
    }
    assert captured["body"]["data"]["_widget_1787037882562"]["value"] == "100"
    assert isinstance(
        captured["body"]["data"]["_widget_1787037882560"]["value"], str
    )
