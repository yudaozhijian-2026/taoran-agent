"""Private, durable diagnostics; public responses contain only an opaque reference."""
import json
import os
from pathlib import Path
from uuid import uuid4


def save_failure_evidence(settings, *, stage, candidate, details):
    database = getattr(settings, "database_path", None)
    if not database:
        return None
    evidence_id = "model-" + uuid4().hex
    try:
        directory = Path(database).resolve().parent / "model-failure-evidence"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Deliberately accept only parsed business candidates, never HTTP headers,
        # settings, provider envelopes, or exception response bodies.
        content = json.dumps({"stage": stage, "candidate": candidate, "details": details},
                             ensure_ascii=False, default=str)
        with os.fdopen(os.open(directory / (evidence_id + ".json"),
                               os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as file:
            file.write(content)
        return evidence_id
    except (OSError, TypeError, ValueError):
        # A diagnostic disk failure must not discard a usable model result.
        return None
