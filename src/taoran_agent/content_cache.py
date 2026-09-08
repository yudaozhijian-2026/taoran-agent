"""Content-based identities; no business record or persistent draft ID required."""

import json
from contextvars import ContextVar
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from time import monotonic, time

VERSION = "content-cache-v2"
knowledge_basis = ContextVar("interactive_knowledge_basis", default=None)


def run_with_knowledge_basis(callback, request, settings, basis):
    """Bind only the interactive Final worker, never submitted scoring."""
    token = knowledge_basis.set(basis)
    try:
        return callback(request, settings)
    finally:
        knowledge_basis.reset(token)


def pinned_snapshot(kind):
    from .knowledge import TaoranKnowledgeSnapshot

    basis = knowledge_basis.get()
    if basis is None:
        return None
    raw = basis.get(kind)
    if raw is None:
        raise ValueError("pinned_knowledge_unavailable")
    return TaoranKnowledgeSnapshot.model_validate(raw)


def digest(value):
    return sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


@lru_cache(maxsize=1)
def implementation_digest():
    # Immutable deployment: includes rules, prompts, model adapters and schemas.
    root = Path(__file__).parent
    return digest(
        {
            str(p.relative_to(root)): sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*"))
            if p.is_file() and p.suffix in {".py", ".json", ".md", ".txt"}
        }
    )


def fingerprint(
    snapshot,
    visit,
    *,
    tenant,
    credential,
    user,
    settings,
    mapping,
    local_knowledge_hash,
    live_knowledge_hash,
    implementation=None,
):
    # A client-provided user name never grants authority: authorization runs first.
    # Credential isolation is mandatory, user is an additional partition only.
    scope = digest(
        {
            "tenant": tenant,
            "credential": sha256(credential.encode()).hexdigest(),
            "user": user,
            "application": mapping.get("source_application_id"),
            "form": mapping.get("source_entry_id"),
        }
    )
    basis = digest(
        {
            "protocol": VERSION,
            "implementation": implementation or implementation_digest(),
            "settings": settings,
            "mapping": mapping,
            "local_knowledge": local_knowledge_hash,
            "live_knowledge": live_knowledge_hash,
        }
    )
    # Preserve raw fields and subtable contents as well as validated facts.
    # Do not trim/case-fold prose or sort arrays: different facts must never collide.
    content = digest({"snapshot": snapshot, "visit": visit})
    return digest({"scope": scope, "basis": basis, "content": content})


def reuse_allowed(task, response, now=None):
    now = monotonic() if now is None else now
    if task.get("expires_at", 0) <= now:
        return False
    if (
        task.get("cache_until", 0) <= time()
        and response.get("status") != "processing"
        and response.get("preview_status") != "processing"
    ):
        return False
    if response.get("status") in {"failed", "expired"} or response.get("recoverable"):
        return False
    # Final may finish while preview is still running; keep deduplicating the work.
    if response.get("status") == "processing" or response.get("preview_status") == "processing":
        return True
    if task.get("knowledge_basis") and task["knowledge_basis"].get("live") is None:
        return False
    return bool(
        response.get("status") == "completed"
        and response.get("content_complete")
        and response.get("final_status") == "completed"
        and response.get("final_feedback_text")
        and response.get("preview_feedback_text")
    )
