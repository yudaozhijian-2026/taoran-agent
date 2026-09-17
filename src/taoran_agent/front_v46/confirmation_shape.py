"""Source-based confirmation normalization and bounded, structural patch scope."""

import re
from copy import deepcopy

from ..models import VisitDraftInput


class ConfirmationShapeError(ValueError):
    def __init__(self, errors):
        super().__init__("confirmation_source_shape")
        self.validation_errors = errors


ROLE = re.compile(r"负责人|联系人|姓名|职务|决策人|采购人")
DEMAND = re.compile(r"未(?:记录|明确|确认|取得|提供)|缺少|不足以判断|是否.{0,12}(?:确认|明确|取得)|(?:补充|填写|核实).{0,12}(?:负责人|联系人|姓名|职务)")


def unsupported_role_requirement(text, context):
    """Narrow guard: absence of role facts is not a new completion condition.

    An explicit role objective or role-bearing source remains eligible for normal
    analysis; this is deliberately not a blanket ban on mentioning people.
    """
    relevant = "\n".join(str(context.get(k) or "") for k in (
        "visit_purpose", "other_purpose", "expected_key_result",
        "process_description", "customer_feedback", "next_action_expected_result"))
    clauses = re.split(r"[。；;！!\n]", text)
    return not ROLE.search(relevant) and any(
        ROLE.search(clause) and DEMAND.search(clause)
        and not re.search(r"(?:无需|不必|不要求|不强制).{0,10}(?:补充|填写|确认|提供)?", clause)
        for clause in clauses)


def normalize(raw, context):
    value = deepcopy(raw)
    if not isinstance(value, dict) or not isinstance(value.get("confirmations", []), list):
        return value
    errors = []
    for root, key in (("analysis_points", "text"), ("items", "suggestion"), ("confirmations", "question")):
        for index, item in enumerate(value.get(root, [])):
            if isinstance(item, dict) and unsupported_role_requirement(str(item.get(key, "")), context):
                errors.append({"location": f"{root}.{index}", "code": "unsupported_role_requirement"})
    from .feedback_consistency import candidate_errors
    errors.extend(candidate_errors(value, context))
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
            # Repair the dependent gap as well, not just its malformed question.
            for j, point in enumerate(value.get("analysis_points", [])):
                if (isinstance(point, dict) and point.get("requires_followup")
                        and any(p.get("field") == field for p in point.get("proofs", []) if isinstance(p, dict))):
                    errors.append({"location": f"analysis_points.{j}", "code": "dependent_confirmation_gap"})
    if errors:
        raise ConfirmationShapeError(errors)
    return value


def repair_paths(raw, errors):
    """Only malformed suggestion components qualify; analysis stays untouched."""
    if not isinstance(raw, dict) or not isinstance(raw.get("analysis_points"), list) or not raw["analysis_points"]:
        return []
    paths = []
    for error in errors:
        parts = error.get("location", "").split(".")
        root = parts[0]
        if root == "analysis_points" and (len(parts) < 2 or not parts[1].isdigit()):
            return []
        if root not in {"analysis_points", "items", "confirmations", "suggestion_status", "suggestion_reason"}:
            return []
        path = root
        if root in {"analysis_points", "items", "confirmations"} and len(parts) > 1 and parts[1].isdigit():
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
    for root in ("analysis_points", "items", "confirmations"):
        if isinstance(raw.get(root), list):
            raw[root] = [v for v in raw[root] if v is not None]
    return raw


def valid_remainder(raw, paths):
    """Keep valid content visible if the single local repair fails."""
    value = deepcopy(raw)
    for root in ("analysis_points", "items", "confirmations"):
        if root in paths:
            value[root] = []
        elif isinstance(value.get(root), list):
            value[root] = [v for i, v in enumerate(value[root]) if f"{root}.{i}" not in paths]
    if not value.get("analysis_points"):
        value["analysis_points"] = [{"kind": "visit_context", "text": "本次拜访分析尚未完成，暂不能给出完整结论。", "proofs": []}]
    value.update(suggestion_status=None, suggestion_reason="局部内容完整性核对未完成，待恢复。")
    return value
