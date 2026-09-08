"""Source-based confirmation normalization and bounded, structural patch scope."""

from copy import deepcopy

from ..models import VisitDraftInput


class ConfirmationShapeError(ValueError):
    def __init__(self, errors):
        super().__init__("confirmation_source_shape")
        self.validation_errors = errors


def normalize(raw, context):
    value = deepcopy(raw)
    if not isinstance(value, dict) or not isinstance(value.get("confirmations", []), list):
        return value
    errors = []
    for index, item in enumerate(value.get("confirmations", [])):
        if not isinstance(item, dict) or not isinstance(item.get("field"), str):
            continue
        field = item["field"]
        source = context.get(field)
        # A nonexistent field must never be passed off as missing business data.
        known = field in VisitDraftInput.model_fields and not field.startswith("_")
        if not known:
            errors.append({"location": f"confirmations.{index}.field", "code": "unknown_source_field"})
        elif source is None or source == [] or (isinstance(source, str) and not source.strip()):
            item.update(kind="missing_field", quote="")
        elif isinstance(source, str) and isinstance(item.get("quote"), str) and item["quote"].strip() and item["quote"] in source:
            start = source.index(item["quote"])
            item.update(kind="source_ambiguity", quote=source[start:start + len(item["quote"])])
        else:
            errors.append({"location": f"confirmations.{index}.quote", "code": "unresolved_source_reference"})
    if errors:
        raise ConfirmationShapeError(errors)
    return value


def repair_paths(raw, errors):
    """Only malformed suggestion components qualify; analysis stays untouched."""
    if not isinstance(raw, dict) or not raw.get("analysis_points"):
        return []
    paths = []
    for error in errors:
        parts = error.get("location", "").split(".")
        root = parts[0]
        if root not in {"items", "confirmations", "suggestion_status", "suggestion_reason"}:
            return []
        path = root
        if root in {"items", "confirmations"} and len(parts) > 1 and parts[1].isdigit():
            index = int(parts[1])
            if not isinstance(raw.get(root), list) or index >= len(raw[root]):
                return []
            path += "." + parts[1]
        if path not in paths:
            paths.append(path)
    return [p for p in paths if not any(p.startswith(other + ".") for other in paths)]


def apply_patches(candidate, response, paths):
    patches = response.get("patches") if isinstance(response, dict) else None
    if (not isinstance(patches, list) or len(patches) != len(paths)
            or any(not isinstance(p, dict) or set(p) != {"path", "value"} for p in patches)
            or sorted(p["path"] for p in patches) != sorted(paths)):
        raise ValueError("invalid_local_shape_patch")
    raw = deepcopy(candidate)
    for patch in patches:
        parts = patch["path"].split(".")
        if len(parts) == 1:
            raw[parts[0]] = patch["value"]
        else:
            raw[parts[0]][int(parts[1])] = patch["value"]
    return raw


def valid_remainder(raw, paths):
    """Keep valid content visible if the single local repair fails."""
    value = deepcopy(raw)
    for root in ("items", "confirmations"):
        if root in paths:
            value[root] = []
        elif isinstance(value.get(root), list):
            value[root] = [v for i, v in enumerate(value[root]) if f"{root}.{i}" not in paths]
    value.update(suggestion_status=None, suggestion_reason="局部内容完整性核对未完成，待恢复。")
    return value
