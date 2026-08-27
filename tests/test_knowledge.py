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

    assert snapshot.record_count == 1
    assert [request.url.path for request in requests] == [
        "/v1/knowledge/search",
        "/v1/knowledge/DSM-BS-TEST",
    ]
