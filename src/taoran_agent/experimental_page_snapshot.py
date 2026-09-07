"""Experimental complete-page contract. Never merge saved business values."""

import json
from typing import Any


class PageSnapshotError(ValueError):
    """Safe error: contains no customer values."""


def current_page_snapshot(payload: Any, mapping: dict[str, Any]) -> dict[str, Any]:
    required = set(mapping.get("fields", {})) | set(mapping.get("subforms", {}))
    if not isinstance(payload, dict) or not required or set(payload) != required:
        raise PageSnapshotError("page_snapshot_incomplete")
    # Canonical names only: widget aliases cannot shadow a cleared page value.
    # Null/empty are intentional clears, never a reason to fall back to storage.
    snapshot = dict(payload)
    for field in {"employee_id", "evidence_ids"} | set(mapping.get("subforms", {})):
        value = snapshot.get(field)
        if isinstance(value, str) and value.lstrip().startswith(("[", "{")):
            try:
                snapshot[field] = json.loads(value)
            except ValueError:
                raise PageSnapshotError("page_json_invalid") from None
    for field in mapping.get("subforms", {}):
        if snapshot[field] in (None, ""):
            snapshot[field] = []
        if not isinstance(snapshot[field], list) or any(
            not isinstance(row, dict) for row in snapshot[field]
        ):
            raise PageSnapshotError("page_subform_invalid")
    return snapshot
