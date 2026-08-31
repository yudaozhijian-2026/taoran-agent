from __future__ import annotations

import json

import httpx

from taoran_agent.knowledge import KnowledgeApiClient


def test_knowledge_client_respects_live_api_limit() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/knowledge/search":
            payload = json.loads(request.content)
            assert payload == {"query": "TAORAN", "limit": 50}
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "DSM-BS-TEST",
                        }
                    ]
                },
            )
        if request.url.path == "/v1/knowledge/DSM-BS-01-06":
            return httpx.Response(
                200,
                json={
                    "id": "DSM-BS-01-06",
                    "title": "拜访目的与关键结果标准",
                    "status": "已确认",
                    "version": "v1.0.0",
                    "summary": "拜访目的与关键结果标准。",
                    "content": "正式映射内容。",
                    "content_hash": "purpose-hash",
                    "updated_at": "2026-08-27T00:00:00Z",
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "DSM-BS-TEST",
                "title": "测试知识",
                "status": "已确认",
                "version": "V1",
                "summary": "测试摘要",
                "content": "测试内容",
                "content_hash": "abc123",
                "updated_at": "2026-08-27T00:00:00Z",
            },
        )

    snapshot = KnowledgeApiClient(
        "https://knowledge.example.test",
        "synthetic-key",
        transport=httpx.MockTransport(handler),
    ).fetch_taoran_snapshot(limit=100)

    assert snapshot.record_count == 2
    assert {record.id for record in snapshot.records} == {
        "DSM-BS-TEST",
        "DSM-BS-01-06",
    }
    assert [request.url.path for request in requests] == [
        "/v1/knowledge/search",
        "/v1/knowledge/DSM-BS-TEST",
        "/v1/knowledge/DSM-BS-01-06",
    ]


def test_knowledge_client_reuses_complete_search_items_without_detail_calls() -> None:
    requests: list[httpx.Request] = []
    records = [
        {
            "id": record_id,
            "title": record_id,
            "status": "已确认",
            "version": "V1",
            "summary": "摘要",
            "content": "受控知识内容",
            "content_hash": record_id + "-hash",
            "updated_at": "2026-08-31T00:00:00Z",
        }
        for record_id in ("DSM-BS-000", "DSM-BS-01-07", "DSM-BS-01-06")
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/v1/knowledge/search"
        return httpx.Response(200, json={"items": records})

    snapshot = KnowledgeApiClient(
        "https://knowledge.example.test",
        "synthetic-key",
        transport=httpx.MockTransport(handler),
    ).fetch_taoran_snapshot()

    assert snapshot.record_count == 3
    assert [request.url.path for request in requests] == ["/v1/knowledge/search"]
