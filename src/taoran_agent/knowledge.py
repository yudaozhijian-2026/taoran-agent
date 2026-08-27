from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

ACTIVE_KNOWLEDGE_STATUSES = {"已批准", "已确认"}
DEFAULT_QUERY = "TAORAN"


class KnowledgeRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    title: str
    status: str
    version: str
    summary: str
    content: str
    applicable_scope: str | None = None
    source_reference: str | None = None
    content_hash: str
    updated_at: datetime


class TaoranKnowledgeSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "DSM-TAORAN-KNOWLEDGE-SNAPSHOT-V1"
    source: str
    query: str = DEFAULT_QUERY
    retrieved_at: datetime
    record_count: int = Field(ge=1)
    records: list[KnowledgeRecord] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_snapshot(self) -> TaoranKnowledgeSnapshot:
        if self.record_count != len(self.records):
            raise ValueError("record_count 与 records 数量不一致")
        if len({record.id for record in self.records}) != len(self.records):
            raise ValueError("知识快照存在重复知识ID")
        inactive = [
            record.id
            for record in self.records
            if record.status not in ACTIVE_KNOWLEDGE_STATUSES
        ]
        if inactive:
            raise ValueError(f"知识快照包含未生效记录：{','.join(inactive)}")
        return self

    @property
    def snapshot_hash(self) -> str:
        digest_input = "|".join(
            f"{record.id}:{record.version}:{record.content_hash}"
            for record in sorted(self.records, key=lambda item: item.id)
        )
        return hashlib.sha256(digest_input.encode("utf-8")).hexdigest()


def load_taoran_knowledge_snapshot(path: str | Path | None = None) -> TaoranKnowledgeSnapshot:
    if path:
        raw = Path(path).read_text(encoding="utf-8")
    else:
        resource = files("taoran_agent.data").joinpath("taoran_knowledge_snapshot_v1.json")
        raw = resource.read_text(encoding="utf-8")
    return TaoranKnowledgeSnapshot.model_validate_json(raw)


class KnowledgeApiClient:
    """DSM知识服务只读客户端；运行时预检不依赖该客户端。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    def fetch_taoran_snapshot(self, limit: int = 100) -> TaoranKnowledgeSnapshot:
        with httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            transport=self.transport,
            headers={"X-API-Key": self.api_key, "Accept": "application/json"},
        ) as client:
            response = client.post(
                "/v1/knowledge/search",
                headers={"Content-Type": "application/json"},
                json={"query": DEFAULT_QUERY, "limit": limit},
            )
            response.raise_for_status()
            result = response.json()
            items = result.get("items", [])
            records = []
            for item in items:
                detail = client.get(f"/v1/knowledge/{item['id']}")
                detail.raise_for_status()
                payload: dict[str, Any] = detail.json()
                record = payload.get("item") or payload.get("data") or payload
                if record.get("status") in ACTIVE_KNOWLEDGE_STATUSES:
                    records.append(record)
        return TaoranKnowledgeSnapshot(
            source=f"{self.base_url}/v1/knowledge/search",
            query=DEFAULT_QUERY,
            retrieved_at=datetime.now(UTC),
            record_count=len(records),
            records=records,
        )


def write_snapshot(snapshot: TaoranKnowledgeSnapshot, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(snapshot.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def render_snapshot_markdown(snapshot: TaoranKnowledgeSnapshot) -> str:
    lines = [
        "# TAORAN权威知识基线",
        "",
        f"- 来源：{snapshot.source}",
        f"- 查询词：`{snapshot.query}`",
        f"- 同步时间：{snapshot.retrieved_at.isoformat()}",
        f"- 快照哈希：`{snapshot.snapshot_hash}`",
        f"- 正式记录数：{snapshot.record_count}",
        "",
        "> 本文件由原始JSON快照自动生成。评分规则不会因知识库变化自动启用，需审核后发布。",
        "",
    ]
    for record in snapshot.records:
        lines.extend(
            [
                f"## {record.id}｜{record.title}",
                "",
                f"- 状态：{record.status}",
                f"- 版本：{record.version}",
                f"- 适用范围：{record.applicable_scope or '未说明'}",
                f"- 内容哈希：`{record.content_hash}`",
                f"- 更新时间：{record.updated_at.isoformat()}",
                "",
                record.summary,
                "",
                record.content,
                "",
                f"来源依据：{record.source_reference or '未说明'}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"
