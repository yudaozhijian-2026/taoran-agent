from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

EXPECTED_MASTER_VERSION = "1.18.0"
EXPECTED_MASTER_CANONICAL_HASH = "d42289b24083d8b917e008a71bd722cf72a09725d06082ea048286dd99935e3a"
EXPECTED_MASTER_RAW_SHA256 = "a6c211b778638af894c47c312c1b6f5822b8dda35781f4d1ed458c7e928cb577"
EXPECTED_MASTER_RECORDS = 158
EXPECTED_INCLUDED_RECORDS = 151
REQUIRED_IDS = {"DSM-BS-01-06", "DSM-BS-01-07", "DSM-MP-01"}


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_bytes(raw)


def fallback_record(record: dict[str, Any]) -> dict[str, Any]:
    content = str(record.get("content") or "")
    content_hash = sha256_bytes(content.encode("utf-8"))
    return {
        "id": record["id"],
        "title": record["title"],
        "status": record["contentReviewStatus"],
        "version": record["version"],
        "summary": record.get("summary") or "",
        "content": content,
        "applicable_scope": record.get("applicableScope"),
        "source_reference": record.get("sourceReference"),
        "content_hash": content_hash,
        "updated_at": record["updatedAt"],
        "content_review_status": record["contentReviewStatus"],
        "release_effective_status": record["releaseEffectiveStatus"],
        "release_version": record.get("releaseVersion"),
        "source_content_hash": record["contentHash"],
        "source_content_sha256": content_hash,
    }


def generate(master_path: Path, output_path: Path, manifest_path: Path) -> None:
    master_bytes = master_path.read_bytes()
    raw_hash = sha256_bytes(master_bytes)
    if raw_hash != EXPECTED_MASTER_RAW_SHA256:
        raise ValueError(f"master raw hash changed: {raw_hash}")

    master = json.loads(master_bytes)
    canonical_hash = canonical_sha256(master)
    if canonical_hash != EXPECTED_MASTER_CANONICAL_HASH:
        raise ValueError(f"master canonical hash changed: {canonical_hash}")

    metadata = master.get("metadata") or {}
    records = master.get("knowledge") or []
    if metadata.get("knowledge_base_version") != EXPECTED_MASTER_VERSION:
        raise ValueError("unexpected master version")
    if len(records) != EXPECTED_MASTER_RECORDS:
        raise ValueError("unexpected master record count")

    approved = [
        fallback_record(record)
        for record in records
        if record.get("contentReviewStatus") == "已确认"
        and record.get("releaseEffectiveStatus") == "已批准生效"
    ]
    approved.sort(key=lambda item: item["id"])
    if len(approved) != EXPECTED_INCLUDED_RECORDS:
        raise ValueError("unexpected approved record count")
    ids = {record["id"] for record in approved}
    if not REQUIRED_IDS <= ids:
        raise ValueError(f"required records missing: {sorted(REQUIRED_IDS - ids)}")
    if any(not record["content"].strip() for record in approved):
        raise ValueError("approved snapshot contains empty content")

    generated_at = datetime.now(UTC).isoformat()
    content_snapshot_hash = canonical_sha256(approved)
    snapshot = {
        "schema_version": "DSM-TAORAN-APPROVED-KNOWLEDGE-SNAPSHOT-V2",
        "source": "approved_bundled_snapshot",
        "query": ("contentReviewStatus=已确认 AND releaseEffectiveStatus=已批准生效"),
        "retrieved_at": generated_at,
        "record_count": len(approved),
        "records": approved,
        "source_knowledge_base_version": EXPECTED_MASTER_VERSION,
        "source_master_snapshot_hash": canonical_hash,
        "included_records": len(approved),
        "excluded_records": len(records) - len(approved),
        "generated_at": generated_at,
        "approved_content_snapshot_hash": content_snapshot_hash,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    artifact_hash = sha256_bytes(output_path.read_bytes())
    required = {
        record["id"]: {
            "version": record["version"],
            "content_length": len(record["content"]),
            "source_content_hash": record["source_content_hash"],
            "source_content_sha256": record["source_content_sha256"],
        }
        for record in approved
        if record["id"] in REQUIRED_IDS
    }
    manifest = {
        "status": "APPROVED_FALLBACK_SNAPSHOT_GENERATED",
        "source_knowledge_base_version": EXPECTED_MASTER_VERSION,
        "source_master_raw_sha256": raw_hash,
        "source_master_snapshot_hash": canonical_hash,
        "source_master_records": len(records),
        "included_records": len(approved),
        "excluded_records": len(records) - len(approved),
        "generated_at": generated_at,
        "approved_content_snapshot_hash": content_snapshot_hash,
        "snapshot_file_sha256": artifact_hash,
        "required_records": required,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("master", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    generate(args.master, args.output, args.manifest)


if __name__ == "__main__":
    main()
