import pytest
from fastapi import HTTPException

from taoran_agent import api
from taoran_agent.config import Settings
from taoran_agent.experimental_page_snapshot import PageSnapshotError, current_page_snapshot

MAPPING = {
    "source_application_id": "test_app", "source_entry_id": "test_entry",
    "record_fields": {"visit_record_code": {"widget_id": "record_code", "widget_type": "sn"}},
    "fields": {"process_description": {"widget_id": "process"}, "next_contact_at": "date"},
    "subforms": {"participants": {"field": "contacts", "children": {"contact_id": "id"}}},
}


def snapshot():
    return {"process_description": "未保存的新描述", "next_contact_at": None, "participants": []}


@pytest.mark.parametrize("payload", [None, {}, {**snapshot(), "process": "旧值"},
                                    {"process_description": "只有部分页面字段"}])
def test_partial_or_shadowed_snapshot_rejected(payload):
    with pytest.raises(PageSnapshotError):
        current_page_snapshot(payload, MAPPING)


def test_clear_and_empty_subform_never_become_saved_values():
    result = current_page_snapshot({**snapshot(), "process_description": "", "participants": None}, MAPPING)
    assert result == {"process_description": "", "next_contact_at": None, "participants": []}


def test_json_subform_and_attachment_text_normalization():
    mapping = {**MAPPING, "fields": {**MAPPING["fields"], "evidence_ids": "attachments"}}
    result = current_page_snapshot({**snapshot(), "participants": '[{"contact_id":"c1"}]',
                                    "evidence_ids": '[{"name":"附件1"}]'}, mapping)
    assert result["participants"] == [{"contact_id": "c1"}]
    assert result["evidence_ids"] == [{"name": "附件1"}]


@pytest.mark.parametrize("value", ['[broken', 'not an array', ["wrong row"]])
def test_invalid_subform_rejected(value):
    with pytest.raises(PageSnapshotError):
        current_page_snapshot({**snapshot(), "participants": value}, MAPPING)


def configure(monkeypatch):
    monkeypatch.setattr(api, "authorize", lambda *args: None)
    monkeypatch.setattr(api, "get_settings", lambda: Settings(_env_file=None, quick_check_interactive_enabled=True))
    monkeypatch.setattr(api, "tenant_mapping", lambda *args: MAPPING)


def test_page_route_uses_saved_identity_not_saved_business_values(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(api, "find_jiandaoyun_record_by_field", lambda *args: {
        "_id": "saved_id", "process": "数据库旧描述", "date": "2026-09-10", "contacts": [{"id": "old"}],
    })
    captured = []
    monkeypatch.setattr(api, "create_interactive_quick_check_task", lambda body, **kwargs: (
        captured.append(body) or {"check_id": "qc_page_test"}
    ))
    result = api.create_experimental_interactive_quick_check_current_record_task({
        "visit_record_code": "BFJL1", "snapshot_mode": "experimental_current_page_v1",
        "page_snapshot": snapshot(),
    }, "tenant_demo", "test-key")
    assert result["source"] == "current_page_snapshot"
    assert captured[0]["data_id"] == "saved_id"
    assert captured[0]["form_snapshot"] == snapshot()
    assert "process" not in captured[0]["form_snapshot"]


def test_bad_page_contract_fails_before_remote_read(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(api, "find_jiandaoyun_record_by_field", lambda *args: pytest.fail("must not read"))
    with pytest.raises(HTTPException) as caught:
        api.create_experimental_interactive_quick_check_current_record_task({
            "visit_record_code": "BFJL1", "snapshot_mode": "experimental_current_page_v1", "page_snapshot": {},
        }, "tenant_demo", "test-key")
    assert caught.value.status_code == 422


def test_changed_page_content_changes_hash_even_for_same_record():
    from taoran_agent.models import PrecheckRequest
    first = PrecheckRequest.model_validate({"context": {"tenant_id": "t", "request_id": "r", "user_id": "u", "source": "jiandaoyun"},
        "visit": {"visit_date": "2026-09-05", "employee_id": "e", "process_description": "旧描述"}})
    second = first.model_copy(update={"visit": first.visit.model_copy(update={"process_description": "新描述"})})
    assert api._quick_check_snapshot_hash(first, "BFJL1") != api._quick_check_snapshot_hash(second, "BFJL1")
