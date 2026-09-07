"""Compatibility shim: never infer an actor from a known example."""

def receipt_role_hint(context: dict) -> str:
    return ""


def proxy_receipt_goal_conflict(text: str, context: dict) -> bool:
    return False
