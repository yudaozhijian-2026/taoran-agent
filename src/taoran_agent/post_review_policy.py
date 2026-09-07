"""Versioned, post-submit-only interpretation policy and input boundary checks."""
from __future__ import annotations

import hashlib
import json
import re

POLICY_VERSION = "TAORAN-POST-REVIEW-20260907-V4.7"
from .record_contract import GUIDANCE as RECORD_GUIDANCE

POLICY = RECORD_GUIDANCE + (
    "后台依据最新提交记录独立判断并计分，不沿用历史或首次模型评分。事实冲突允许一次受控重核，全部事实与意见重新判断；局部文字修复只能保留已通过核对的当前事实。"
    "不得用获得新信息冒充原目标已经达成；原目标未达成也不自动说明拜访无价值。"
    "记录中的保密、临时议题变化、暂未取得信息等只是待核验说明，不能自动判达标、免除公司标准或编造替代事实。"
    "主体歧义确实影响具体结论时，允许指出具体事实来源缺口；只有缺姓名职务但客户表达明确时不要求补填。"
    "下一步对象默认当前客户，不强制补具体联系人。原目标要求负责人信息时，不能用联系人可选的保护规则免除该目标检查。"
    "自评固定选项与独立判断的原目标达成程度比较；一致且依据清楚时不因文字不详细要求修改自评。"
    "目的与客户类型阶段是否匹配、日期先后及跨月跨季度关系均使用程序结果，模型不能覆盖。"
    "商机客户的下一步需客户明确共识，日期晚于本次；目标客户需跨自然月，潜力客户需跨自然季度，后二者不额外强制客户承诺。"
    "下一步同主题但不具体时承认主题联系，只指出真实缺口，不笼统判无衔接。"
    "T只解释类型阶段目的映射，O_KR只判断原定目标具体性，不重复要求修改已允许的目的。"
    "advice_basis.gap_kind的missing_value仅限整个字段为空；有值但不具体使用insufficient_specificity，来源歧义使用fact_source_unclear并明确影响的结论。"
    "补充本次目标只能核实原定、原计划或事先目标；新目标应明确用于下次，不得按实际结果改写本次目标。"
    "未取得SWAS、打卡或关联表单数据时，不声称它们缺失、不一致或未更新。AI意见不代表经理已检查或确认。"
)
# Local implementation interpretation, not a verbatim knowledge-library record.
POLICY_BASIS = "DSM-MP-01-04（2026-09-04审查原文）及公司已确认边界"
REQUIRED_FIELDS = (
    "customer_type_ii", "visit_method", "is_appointment", "purpose_code",
    "expected_key_result", "process_description", "self_assessment",
    "next_action_purpose", "next_action_expected_result", "next_contact_at",
)


class PostInputNotReceived(ValueError):
    """Missing transport evidence must never become a salesperson's failing score."""


def digest(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def requirement_hits(text: str, customer_type: str) -> list[dict]:
    """Conservative explicit-requirement checks with exact character locations."""
    hits = []
    for clause in re.finditer(r"[^。；，,\n]+", text):
        sentence = clause.group()
        if re.search(r"(?:不要求|无需|不必|不构成缺口|不适用|不是必填|不强制|不能要求|不得要求)", sentence):
            continue
        # Only reject explicit demands that the next-action target be a person.
        # Do not infer a forbidden requirement from missing-role wording alone:
        # it may concern an actual evidence-source gap or the stated visit goal.
        match = re.search(
            r"(?:下一步|下次)(?:客户)?(?:行动)?对象.{0,10}"
            r"(?:必须|应当|需要|应|须|缺少|缺失|未填写|未明确|补充|补齐).{0,10}"
            r"(?:具体)?(?:联系人|姓名|职务|客户角色)", sentence)
        if match:
            hits.append({"rule": "next_target_person_required", "start": clause.start()+match.start(),
                         "end": clause.start()+match.end(), "quote": match.group()})
        if customer_type in {"potential", "target", "潜力客户", "目标客户"}:
            match = re.search(r"(?:缺少|缺失|尚无|未获得|需要|须有|补充).{0,16}(?:下一步.{0,8}(?:承诺|共识)|客户共识)", sentence)
            if match:
                hits.append({"rule": "consensus_not_applicable", "start": clause.start()+match.start(),
                             "end": clause.start()+match.end(), "quote": match.group()})
        if customer_type in {"opportunity", "商机客户"}:
            match = re.search(r"(?:须|必须|应|填写|安排|要求).{0,25}跨(?:自然)?(?:月|季度)", sentence)
            if match:
                hits.append({"rule": "opportunity_has_no_period_gate",
                             "start": clause.start()+match.start(), "end": clause.start()+match.end(),
                             "quote": match.group()})
    return hits


def unsupported_requirement(text: str, customer_type: str) -> bool:
    return bool(requirement_hits(text, customer_type))


def knowledge_manifest(snapshot) -> dict:
    """Embed exact enabled records, rather than trusting mutable version labels."""
    records = [r.model_dump(mode="json") for r in sorted(snapshot.records, key=lambda r: r.id)]
    return {
        "schema_version": "TAORAN-KNOWLEDGE-AUDIT-V1",
        "source": snapshot.source,
        "retrieved_at": snapshot.retrieved_at.isoformat(),
        "snapshot_hash": snapshot.snapshot_hash,
        "actual_records_hash": digest(records),
        "enabled_records": records,
        "post_policy": {
            "version": POLICY_VERSION, "basis": POLICY_BASIS,
            "kind": "project_interpretation", "text": POLICY, "hash": digest(POLICY),
        },
        "activation": "packaged_baseline_with_project_policy",
    }


def compare_snapshots(baseline, candidate) -> list[dict]:
    old = {r.id: r for r in baseline.records}
    new = {r.id: r for r in candidate.records}
    changes = []
    for record_id in sorted(old.keys() | new.keys()):
        a, b = old.get(record_id), new.get(record_id)
        if a is None:
            kind = "new_record"
        elif b is None:
            kind = "not_returned"  # A search omission is not proof of deletion.
        elif digest(a.model_dump(mode="json")) == digest(b.model_dump(mode="json")):
            continue
        elif a.content != b.content and a.version == b.version:
            kind = "same_version_content_changed"
        else:
            kind = "record_changed"
        changes.append({"id": record_id, "kind": kind,
                        "old": a.model_dump(mode="json") if a else None,
                        "candidate": b.model_dump(mode="json") if b else None,
                        "activation": "review_required"})
    return changes


def input_boundary(visit) -> dict:
    from .record_contract import visit_contract
    presence = visit_contract(visit)['presence']
    return {field: presence[field] for field in REQUIRED_FIELDS}


def require_evaluation_input(visit) -> dict:
    states = input_boundary(visit)
    missing = [field for field, state in states.items() if state == "not_received"]
    if missing:
        # Fail before scoring/model calls: the worker's existing failure path prevents writeback.
        from .field_labels import display_field_name
        labels = "、".join(display_field_name(field) for field in missing)
        raise PostInputNotReceived(
            "POST_INPUT_NOT_RECEIVED:" + ",".join(missing)
            + f"；接口未获取：{labels}。请核对字段映射，补取数据后重试；本次未发布评分。"
        )
    return states
