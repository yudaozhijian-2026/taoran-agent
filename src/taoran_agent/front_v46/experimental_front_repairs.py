"""Lossless front structure handling and explicitly scoped business patches."""

from copy import deepcopy


def rendering_output_plan(contracts):
    """Reserve mandatory slots before optional background/process fragments."""
    required = [c["contract_id"] for c in contracts if c.get("required")]
    return {
        "required_contract_ids": required,
        "max_points": 4,
        "first_points": [
            {
                "contract_id": c["contract_id"],
                "goal_id": c["goal_id"],
                "allowed_claim_types": c["allowed_claim_types"],
            }
            for c in contracts
            if c.get("required")
        ],
        "optional_priority": ["C_PROCESS", "C_NEXT"],
        "omit_optional_background": True,
        "instruction": "先依次填写first_points中的每项，不得遗漏或用背景、过程替代目标判断。"
        "再用剩余名额补本次过程及下一步。C_PROCESS分段只能使用剩余名额；"
        "最多4项，不输出独立C_CONTEXT背景项。该顺序只分配结构，不提供业务结论。",
    }


def process_fragments(points):
    """Metadata grouping only: never merge prose, actors, states or evidence."""
    if not isinstance(points, list):
        return []
    rows = [
        (i, p)
        for i, p in enumerate(points)
        if isinstance(p, dict) and p.get("contract_id") == "C_PROCESS"
    ]
    if len(rows) < 2:
        return []
    return [
        {
            "kind": "process_fragment_group",
            "contract_id": "C_PROCESS",
            "point_indices": [i for i, _ in rows],
            "fragment_count": len(rows),
        }
    ]


def retry_scope(context):
    """Return precise patch positions; global/ambiguous findings need full review."""
    previous = (context or {}).get("previous_candidate", {})
    rejection = (context or {}).get("rejection", {})
    points, items = previous.get("analysis_points"), previous.get("items")
    if not isinstance(points, list) or not isinstance(items, list):
        return None
    if any(not isinstance(p, dict) for p in points + items):
        return None
    present_codes = {p.get("code") for p in items}
    if None in present_codes or len(present_codes) != len(items):
        return None
    indices, codes = set(), set()
    for repair in rejection.get("rendering_repairs", []):
        cid = repair.get("failed_contract_id")
        matches = {i for i, p in enumerate(points) if cid and p.get("contract_id") == cid}
        if not matches:
            return None
        indices.update(matches)
        codes.update(repair.get("suggestion_scope", []))
    for issue in rejection.get("semantic_issues", []):
        target = issue.get("target", "")
        if target.startswith("analysis:") and target[9:].isdigit():
            index = int(target[9:])
            if index >= len(points):
                return None
            indices.add(index)
        elif target.startswith("suggestion:"):
            suffix = target[11:]
            if suffix.isdigit():
                index = int(suffix)
                if index >= len(items):
                    return None
                codes.add(items[index]["code"])
            elif suffix in present_codes:
                codes.add(suffix)
            else:
                return None
        else:
            return None
    codes &= present_codes
    if not indices and not codes:
        return None
    return {"analysis_indices": sorted(indices), "item_codes": sorted(codes)}


def patch_schema(full_schema, scope):
    def array(item, count):
        return {"type": "array", "minItems": count, "maxItems": count, "items": item}

    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["analysis_updates", "item_updates"],
        "properties": {
            "analysis_updates": array(
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["index", "point"],
                    "properties": {
                        "index": {
                            "type": "integer",
                            **(
                                {"enum": scope["analysis_indices"]}
                                if scope["analysis_indices"]
                                else {}
                            ),
                        },
                        "point": deepcopy(full_schema["properties"]["analysis_points"]["items"]),
                    },
                },
                len(scope["analysis_indices"]),
            ),
            "item_updates": array(
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["code", "item"],
                    "properties": {
                        "code": {
                            "type": "string",
                            **({"enum": scope["item_codes"]} if scope["item_codes"] else {}),
                        },
                        "item": deepcopy(full_schema["properties"]["items"]["items"]),
                    },
                },
                len(scope["item_codes"]),
            ),
        },
    }


def merge_patch(context, patch, scope):
    if not isinstance(patch, dict) or set(patch) != {"analysis_updates", "item_updates"}:
        raise ValueError("wording_scoped_repair_invalid")
    result = deepcopy(context["previous_candidate"])
    for key, identity, allowed in [
        ("analysis_updates", "index", scope["analysis_indices"]),
        ("item_updates", "code", scope["item_codes"]),
    ]:
        updates = patch[key]
        if not isinstance(updates, list) or len(updates) != len(allowed):
            raise ValueError("wording_scoped_repair_invalid")
        seen = set()
        for update in updates:
            valuekey = "point" if identity == "index" else "item"
            if not isinstance(update, dict) or set(update) != {identity, valuekey}:
                raise ValueError("wording_scoped_repair_invalid")
            value = update[identity]
            if (
                type(value) is not (int if identity == "index" else str)
                or value not in allowed
                or value in seen
            ):
                raise ValueError("wording_scoped_repair_out_of_scope")
            seen.add(value)
            content = update[valuekey]
            if not isinstance(content, dict):
                raise ValueError("wording_scoped_repair_invalid")  # noqa: TRY004 - shared model-format failure boundary expects ValueError
            if identity == "index":
                if content.get("contract_id") != result["analysis_points"][value].get(
                    "contract_id"
                ):
                    raise ValueError("wording_scoped_repair_out_of_scope")
                result["analysis_points"][value] = deepcopy(content)
            else:
                if content.get("code") != value:
                    raise ValueError("wording_scoped_repair_out_of_scope")
                for i, item in enumerate(result["items"]):
                    if item.get("code") == value:
                        result["items"][i] = deepcopy(content)
    return result
