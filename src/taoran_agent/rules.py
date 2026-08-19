from __future__ import annotations

import hashlib
import json
import re
from importlib.resources import files
from typing import Any

from pydantic import BaseModel


def load_rule_catalog() -> dict[str, Any]:
    path = files("taoran_agent.data").joinpath("taoran_rules_v1.json")
    return json.loads(path.read_text(encoding="utf-8"))


def canonical_hash(value: BaseModel | dict[str, Any]) -> str:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalized_text(value: str | None) -> str:
    return re.sub(r"[\s，。,.!！?？;；:：/]+", "", value or "").casefold()


def is_meaningful(value: str | None, vague_phrases: set[str]) -> bool:
    text = normalized_text(value)
    if not text or text in vague_phrases:
        return False
    return not (re.fullmatch(r"[xX]+", text) or re.fullmatch(r"\d{1,3}", text))
