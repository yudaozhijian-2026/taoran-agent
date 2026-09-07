"""Post-only authoritative context and auditable output conflict checks."""
import re

from .post_claim_guards import advice_hits
from .purpose_mapping import (
    purpose_mapping_record,
    purpose_policy_for_visit,
    structure_purpose_mapping,
)
from .rules import normalized_text


def assessment_summary_hits(text, mismatch):
    """Reject an affirmative objectivity claim when scored achievement differs."""
    if not mismatch:
        return []
    hits = []
    for match in re.finditer(r"[^。；\n]+", text):
        clause = match.group()
        if re.search(r"自评.{0,24}(?:客观|合理|准确|一致)", clause) and not re.search(
            r"(?:不客观|不合理|不准确|不一致|并不|并非|不能|无法|缺乏|不足|未能)", clause
        ):
            hits.append({"target":"facts.reason", "path":"facts.reason",
                "rule":"summary_contradicts_self_assessment_gap", "quote":clause,
                "start":match.start(), "end":match.end(), "scanned_text":text})
    return hits


class PostFeedbackConflict(ValueError):
    def __init__(self, hits, feedback):
        super().__init__("POST_FEEDBACK_CONFLICT")
        self.details = {"hits": hits, "feedback": feedback}


def quality_context(visit, snapshot):
    result = {"purpose": {"status": "unavailable", "matches": None},
              "field_states": {},
              "source_text": "\n".join(str(getattr(visit, f, "") or "") for f in (
                  "process_description", "customer_feedback"))}
    if visit.next_contact_date is not None:
        current, following = visit.visit_date, visit.next_contact_date
        result["calendar"] = {
            "visit_date": current.isoformat(), "next_contact_date": following.isoformat(),
            "after_visit": following > current,
            "different_month": (current.year, current.month) != (following.year, following.month),
            "different_quarter": (current.year, (current.month-1)//3) != (following.year, (following.month-1)//3),
        }
    for field in ("expected_key_result", "process_description", "customer_feedback",
                  "next_action_purpose", "next_action_expected_result", "next_contact_at"):
        value = getattr(visit, field, None)
        result["field_states"][field] = "empty" if value in (None, "", [], {}) else "present"
    supplied = visit.metadata.get("source_supplied_fields")
    if isinstance(supplied, list):
        for field in result["field_states"]:
            if field not in supplied:
                result["field_states"][field] = "not_received"
    try:
        record = purpose_mapping_record(snapshot)
        policy = purpose_policy_for_visit(structure_purpose_mapping(record), visit)
        if policy:
            result["purpose"] = {"status": policy.status, "matches": None,
                "selected": visit.purpose_code, "allowed": policy.allowed_purposes,
                "stages": policy.opportunity_stages, "version": policy.policy_version}
            if policy.status == "active" and visit.purpose_code:
                result["purpose"]["matches"] = normalized_text(visit.purpose_code) in {
                    normalized_text(p) for p in policy.allowed_purposes}
    except (ValueError, AttributeError, KeyError):
        # Unavailable mapping cannot be converted into a salesperson's mismatch.
        pass
    return result


def quality_hits(text, target, context):
    hits = advice_hits(text, target)
    for match in re.finditer(r"[^。；\n]+", text):
        clause = match.group()
        rule = None
        if context.get("purpose", {}).get("matches") is True and target in {"T", "O_KR", "facts.reason"}:
            stage_scope = re.search(r"(?:P[1-6]|商机阶段|当前阶段|客户类型|阶段目的|允许目的)", clause)
            if stage_scope and (re.search(r"(?:拜访目的|本次目的|目的与|目的和).{0,22}(?:不匹配|不符合|错位|不一致)", clause) or re.search(r"将拜访目的.{0,12}(?:改为|调整为|校准为)", clause)):  # noqa: SIM102
                if not re.search(r"(?:并非|不能认为|不存在|不属于).{0,12}(?:不匹配|错位)", clause):
                    rule = "purpose_contradicts_authoritative_mapping"
        if re.search(r"(?:拜访目的|收集信息|强化关系).{0,10}(?:[一二三四五六七八九十0-9]+个?字|字数)", clause):
            rule = "irrelevant_purpose_word_count"
        if re.search(r"(?:项目实施|项目交付|实施工作).{0,5}(?:尚未发生|尚未开始|没有发生|未开展|未启动|尚未完成|未完成|没有完成)", clause):
            source = context.get("source_text", "")
            explicit = re.search(r"(?:实施|交付).{0,5}(?:尚未|未开始|未开展|未启动|没有开始|未完成|没有完成)", source)
            uncertainty = re.search(r"(?:不能|无法|不足以|未能).{0,6}(?:证明|确认|判断)|(?:未记录|未体现)", clause)
            if not explicit and not uncertainty:
                rule = "absence_of_evidence_is_not_nonoccurrence"
        if re.search(r"(?:仅|只有|只有短短).{0,5}(?:一句话|几个字|字数少)", clause) and re.search(r"(?:不足|缺少|不充分|不达标)", clause):
            rule = "length_is_not_evidence_quality"
        labels = {"customer_feedback":"客户反馈", "process_description":"过程详细描述"}
        for field, label in labels.items():
            if context.get("field_states", {}).get(field) != "not_received" and re.search(label+r".{0,8}(?:未传入|未获取|接口未传|没有传入)", clause):
                rule = "empty_is_not_transport_missing"
        if rule:
            hits.append({"rule":rule, "target":target, "quote":clause,
                         "start":match.start(), "end":match.end(), "scanned_text":text})
    return hits


def collect_repair_hits(payload, data, allowed_fields):
    """Collect independently observable conflicts before the one allowed repair.

    This is not a replacement validator. All evidence, schema, fact and scope
    checks still run on the merged result; no generated assertion is corrected
    or accepted by this function.
    """
    from .post_review_policy import requirement_hits
    if not isinstance(payload, dict) or not isinstance(payload.get("sections"), list):
        return []
    facts = payload.get("facts")
    if not isinstance(facts, dict):
        return []
    context = data.get("_authoritative_checks", {})
    hits = []
    texts = [("facts.reason", "facts.reason", facts.get("reason", ""))]
    labels = {"achieved": "达到目的", "partially_achieved": "部分达到", "not_achieved": "未达到"}
    mismatch = labels.get(facts.get("purpose_achievement")) != data.get("self_assessment")
    if isinstance(facts.get("reason"), str):
        hits.extend(assessment_summary_hits(facts["reason"], mismatch))
    for section in payload["sections"]:
        if not isinstance(section, dict) or section.get("code") not in allowed_fields:
            continue
        code = section["code"]
        valid = allowed_fields[code] & data.keys()
        paths = section.get("field_paths")
        if isinstance(paths, list) and all(isinstance(f, str) for f in paths) and not set(paths) <= valid:
            hits.append({"target": code, "path": f"sections.{code}.field_paths",
                         "rule": "invalid_field_reference", "allowed_fields": sorted(valid)})
        if code == "A2" and section.get("verdict") == "met" and mismatch:
            hits.append({"target": code, "path": "sections.A2.verdict", "rule": "assessment_fact_conflict"})
        for field in ("reason", "suggestion"):
            texts.append((code, f"sections.{code}.{field}", section.get(field, "")))
    for target, path, text in texts:
        if not isinstance(text, str):
            continue
        hits.extend({**h, "path": path} for h in quality_hits(text, target, context))
        hits.extend({**h, "target": target, "path": path}
                    for h in requirement_hits(text, str(data.get("customer_type_ii", ""))))
    return hits


POST_EVIDENCE_GUIDANCE = {
    "T": {"mandatory": "客户类型与本次目的匹配使用authoritative_checks，不得推翻允许清单。"},
    "A1": {"mandatory": "如实记录是否预约及拜访方式；视频必须预约，商机客户原则上预约，目标客户优先预约，不加单次预约评分门槛。",
           "optional_evidence_types":["预约对象", "预约时间"]},
    "O_KR": {"mandatory": "按原目标检查具体事项与可验证性；允许目的不等于关键结果具体。不要增加原目标未要求的订单、验收或联系方式。若原目标是确认采购负责人，须检查是否取得所需负责人信息，不能以姓名或职务通常可选免除目标检查。"},
    "R": {"mandatory": "有足以支撑当前判断的客户表达或动作；观点有事实依据。",
          "optional_evidence_types": ["姓名或职务", "确认事项", "异议", "条件", "承诺", "销售判断或假设的区分"],
          "boundary": "不是每类都必填。真实下单是客户动作，不因没有姓名、异议或原话而否定。只有来源歧义影响本项判断时才核实具体事实来源。局部歧义不否定其他明确事实，不影响判断则不追加填写要求。"},
    "A2": {"mandatory": "独立确定实际达成，再比较销售自评；不得借修改原目标或实际达成消除自评差异。"},
    "N": {"mandatory": "对象默认当前客户，目的、时间、期望结果承接本次事实；共识仅商机客户适用。"},
}
