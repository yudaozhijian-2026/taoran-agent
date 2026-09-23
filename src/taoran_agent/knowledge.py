from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

ACTIVE_KNOWLEDGE_STATUSES = {"已批准", "已确认"}
DEFAULT_QUERY = "TAORAN"
REQUIRED_KNOWLEDGE_IDS = ("DSM-BS-01-06", "DSM-BS-01-07", "DSM-MP-01")
TAORAN_RUNTIME_KNOWLEDGE_IDS = (
    "DSM-BS-01-06",
    "DSM-BS-01-07",
    "DSM-MP-01",
)
APPROVED_FALLBACK_RESOURCE = "taoran_knowledge_approved_fallback_v1_18_0.json"
APPROVED_MASTER_VERSION = "1.18.0"
APPROVED_MASTER_SNAPSHOT_HASH = (
    "d42289b24083d8b917e008a71bd722cf72a09725d06082ea048286dd99935e3a"
)
APPROVED_RECORD_COUNT = 151
EXCLUDED_RECORD_COUNT = 7


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
    source_knowledge_base_version: str | None = None
    source_master_snapshot_hash: str | None = None
    included_records: int | None = Field(default=None, ge=1)
    excluded_records: int | None = Field(default=None, ge=0)
    generated_at: datetime | None = None
    approved_content_snapshot_hash: str | None = None

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


@dataclass(frozen=True)
class KnowledgeCompleteness:
    status: Literal["COMPLETE", "INCOMPLETE", "UNAVAILABLE"]
    reason: str | None = None
    missing_required_ids: tuple[str, ...] = ()
    empty_content_ids: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return self.status == "COMPLETE"


@dataclass(frozen=True)
class KnowledgeSelection:
    snapshot: TaoranKnowledgeSnapshot
    source: Literal["remote_api", "approved_bundled_snapshot"]
    remote_status: Literal["COMPLETE", "INCOMPLETE", "UNAVAILABLE"]
    fallback_active: bool
    fallback_reason: str | None
    last_error: str | None = None


class KnowledgeUnavailableError(RuntimeError):
    """Neither the remote snapshot nor the approved fallback is usable."""


def taoran_runtime_records(
    snapshot: TaoranKnowledgeSnapshot,
) -> list[KnowledgeRecord]:
    """Return the three required records from one selected complete snapshot."""
    records_by_id = {record.id: record for record in snapshot.records}
    return [
        records_by_id[record_id]
        for record_id in TAORAN_RUNTIME_KNOWLEDGE_IDS
        if record_id in records_by_id
    ]


def _canonical_sha256(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def assess_remote_knowledge(
    snapshot: TaoranKnowledgeSnapshot,
) -> KnowledgeCompleteness:
    records_by_id = {record.id: record for record in snapshot.records}
    missing = tuple(
        record_id for record_id in REQUIRED_KNOWLEDGE_IDS if record_id not in records_by_id
    )
    if missing:
        return KnowledgeCompleteness(
            status="INCOMPLETE",
            reason="required_record_missing",
            missing_required_ids=missing,
        )
    required_empty = tuple(
        record_id
        for record_id in REQUIRED_KNOWLEDGE_IDS
        if not records_by_id[record_id].content.strip()
    )
    if required_empty:
        return KnowledgeCompleteness(
            status="INCOMPLETE",
            reason="required_content_missing",
            empty_content_ids=required_empty,
        )
    empty = tuple(record.id for record in snapshot.records if not record.content.strip())
    if empty:
        return KnowledgeCompleteness(
            status="INCOMPLETE",
            reason="empty_content_present",
            empty_content_ids=empty,
        )
    invalid_status = tuple(
        record.id
        for record in snapshot.records
        if record.status not in ACTIVE_KNOWLEDGE_STATUSES
    )
    if invalid_status:
        return KnowledgeCompleteness(
            status="INCOMPLETE",
            reason="invalid_record_status",
        )
    invalid_version = tuple(
        record.id for record in snapshot.records if not record.version.strip()
    )
    if invalid_version:
        return KnowledgeCompleteness(
            status="INCOMPLETE",
            reason="invalid_record_version",
        )
    return KnowledgeCompleteness(status="COMPLETE")


def assess_approved_fallback(
    snapshot: TaoranKnowledgeSnapshot,
) -> KnowledgeCompleteness:
    if (
        snapshot.source != "approved_bundled_snapshot"
        or snapshot.source_knowledge_base_version != APPROVED_MASTER_VERSION
        or snapshot.source_master_snapshot_hash != APPROVED_MASTER_SNAPSHOT_HASH
        or snapshot.record_count != APPROVED_RECORD_COUNT
        or snapshot.included_records != APPROVED_RECORD_COUNT
        or snapshot.excluded_records != EXCLUDED_RECORD_COUNT
    ):
        return KnowledgeCompleteness(
            status="INCOMPLETE",
            reason="fallback_provenance_invalid",
        )
    completeness = assess_remote_knowledge(snapshot)
    if not completeness.complete:
        return KnowledgeCompleteness(
            status="INCOMPLETE",
            reason=f"fallback_{completeness.reason}",
            missing_required_ids=completeness.missing_required_ids,
            empty_content_ids=completeness.empty_content_ids,
        )
    for record in snapshot.records:
        actual = hashlib.sha256(record.content.encode("utf-8")).hexdigest()
        if actual != record.content_hash:
            return KnowledgeCompleteness(
                status="INCOMPLETE",
                reason="fallback_content_hash_mismatch",
            )
    canonical_records = [
        record.model_dump(mode="json")
        for record in sorted(snapshot.records, key=lambda item: item.id)
    ]
    if snapshot.approved_content_snapshot_hash != _canonical_sha256(canonical_records):
        return KnowledgeCompleteness(
            status="INCOMPLETE",
            reason="fallback_snapshot_hash_mismatch",
        )
    return KnowledgeCompleteness(status="COMPLETE")


def _remote_error_reason(error: Exception) -> tuple[str, str]:
    if isinstance(error, (httpx.TimeoutException, TimeoutError)):
        return "remote_timeout", type(error).__name__
    if isinstance(error, httpx.HTTPStatusError):
        return "remote_http_error", type(error).__name__
    if isinstance(error, (json.JSONDecodeError, ValidationError, ValueError)):
        return "remote_parse_error", type(error).__name__
    return "remote_unavailable", type(error).__name__


def select_knowledge_snapshot(
    remote_snapshot: TaoranKnowledgeSnapshot | None,
    fallback_snapshot: TaoranKnowledgeSnapshot,
    *,
    remote_error: Exception | None = None,
) -> KnowledgeSelection:
    if remote_snapshot is not None and remote_error is None:
        remote = assess_remote_knowledge(remote_snapshot)
        if remote.complete:
            return KnowledgeSelection(
                snapshot=remote_snapshot,
                source="remote_api",
                remote_status="COMPLETE",
                fallback_active=False,
                fallback_reason=None,
            )
        remote_status: Literal["INCOMPLETE", "UNAVAILABLE"] = "INCOMPLETE"
        fallback_reason = remote.reason
        last_error = None
    else:
        remote_status = "UNAVAILABLE"
        fallback_reason, last_error = _remote_error_reason(
            remote_error or RuntimeError("remote_unavailable")
        )

    fallback = assess_approved_fallback(fallback_snapshot)
    if not fallback.complete:
        raise KnowledgeUnavailableError(fallback.reason or "fallback_unavailable")
    return KnowledgeSelection(
        snapshot=fallback_snapshot,
        source="approved_bundled_snapshot",
        remote_status=remote_status,
        fallback_active=True,
        fallback_reason=fallback_reason,
        last_error=last_error,
    )


def load_taoran_knowledge_snapshot(path: str | Path | None = None) -> TaoranKnowledgeSnapshot:
    if path:
        raw = Path(path).read_text(encoding="utf-8")
    else:
        resource = files("taoran_agent.data").joinpath(APPROVED_FALLBACK_RESOURCE)
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

    def fetch_taoran_snapshot(self, limit: int = 50) -> TaoranKnowledgeSnapshot:
        # 知识服务OpenAPI约束单次查询最多50条，调用方传入更大值时安全收敛。
        limit = min(limit, 50)
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
            records_by_id: dict[str, dict[str, Any]] = {
                item["id"]: item
                for item in items
                if item.get("id")
                and item.get("content")
                and item.get("status") in ACTIVE_KNOWLEDGE_STATUSES
            }
            missing_ids = list(
                dict.fromkeys(
                    [
                        item["id"]
                        for item in items
                        if item.get("id") and item["id"] not in records_by_id
                    ]
                    + [
                        record_id
                        for record_id in REQUIRED_KNOWLEDGE_IDS
                        if record_id not in records_by_id
                    ]
                )
            )
            for record_id in missing_ids:
                detail = client.get(f"/v1/knowledge/{record_id}")
                if detail.status_code == 404 and record_id in REQUIRED_KNOWLEDGE_IDS:
                    continue
                detail.raise_for_status()
                payload: dict[str, Any] = detail.json()
                record = payload.get("item") or payload.get("data") or payload
                if (
                    record.get("id") == record_id
                    and record.get("status") in ACTIVE_KNOWLEDGE_STATUSES
                ):
                    records_by_id[record_id] = record
            retrieved_at = datetime.now(UTC)
            records = [
                self._normalize_record(record, retrieved_at)
                for record in records_by_id.values()
            ]
        return TaoranKnowledgeSnapshot(
            source=f"{self.base_url}/v1/knowledge/search",
            query=DEFAULT_QUERY,
            retrieved_at=retrieved_at,
            record_count=len(records),
            records=records,
        )

    @staticmethod
    def _normalize_record(
        record: dict[str, Any],
        retrieved_at: datetime,
    ) -> dict[str, Any]:
        """Accept the public knowledge API's compact release representation.

        The release API intentionally omits storage-only ``content_hash`` and
        ``updated_at`` fields.  They are snapshot metadata, not TAORAN business
        content, so derive a stable hash from the exact returned content and use
        the retrieval time only when the API exposes no update timestamp.
        """
        normalized = dict(record)
        normalized["id"] = str(
            normalized.get("id") or normalized.get("knowledge_id") or ""
        ).strip()
        normalized["version"] = str(
            normalized.get("version") or normalized.get("release_version") or ""
        ).strip()
        content = str(normalized.get("content") or "")
        normalized["content_hash"] = str(
            normalized.get("content_hash")
            or hashlib.sha256(content.encode("utf-8")).hexdigest()
        )
        normalized["updated_at"] = (
            normalized.get("updated_at")
            or normalized.get("published_at")
            or normalized.get("released_at")
            or retrieved_at
        )
        return normalized


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
