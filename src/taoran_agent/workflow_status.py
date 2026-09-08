"""Business completion is separate from the durable worker lifecycle."""


def workflow_status(status, response):
    response = response or {}
    if status in {"queued", "running"}:
        return ("awaiting_writeback" if (response.get("semantic_facts") or {}).get("status") == "completed" else "analyzing")
    delivery = response.get("writeback") or {}
    if delivery.get("error_message") in {"WRITEBACK_SUPERSEDED", "WRITEBACK_SOURCE_CHANGED"}:
        return "superseded"
    if status == "failed" or (response.get("semantic_facts") or {}).get("status") != "completed":
        return "analysis_failed"
    if delivery.get("status") == "failed":
        return "writeback_failed"
    if delivery.get("status") == "succeeded":
        return "succeeded"
    return "awaiting_writeback"
