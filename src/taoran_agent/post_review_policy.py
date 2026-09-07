"""Versioned, post-submit-only interpretation policy and input boundary checks."""
from __future__ import annotations

import hashlib
import json
import re

POLICY_VERSION = "TAORAN-POST-REVIEW-20260907-V4.2"
POLICY = (
    "先核对动作主体、动作内容、完成状态及原文依据，再判断达成。"
    "给客户、替客户、协助客户做某事，不等于客户本人已做或已确认；"
    "主体不清时保留原文动作，指出影响判断的主体歧义，不能改写成客户已完成。"
    "原文省略主语时继续保留主语未明确的状态；例如现场给客户收货不能改成客户已收货、"
    "客户已实际接收货物或客户已经签收，也不能以同义改写补造主体和完成状态。"
    "正在走流程、准备、等待、计划均不能写成已经完成；同一句内多个动作分别核对状态，"
    "不能用完成一词同时修饰已完成下单与仍在进行的合同流程。"
    "原定关键结果是本次评价基准，禁止按事后结果改写原目标来支持自评。"
    "原目标模糊时只建议核实并补充本次原定事项，保留原目标和修改痕迹；"
    "补充关键结果的建议必须明确写出原定、原计划或事先目标，不能用已取得结果充当原定事项。"
    "新的目标建议必须明确用于下次拜访，不能建议将本次关键结果改为、校准为已取得的结果。"
    "下一步与本次同一主题有关但不具体时，承认主题联系，指出缺少的具体事项或客户反馈；"
    "不用与本次无衔接或不衔接的笼统结论代替缺口分析。"
    "T只解释类型阶段目的映射，O/KR只解释原关键结果；映射问题已在T建议后，"
    "不要在O/KR再次建议修改目的。"
    "advice_basis.gap_kind的missing_value仅指整个字段为空；字段已填但缺少客户事实、"
    "确认内容或细节时必须选insufficient_specificity，不能选missing_value。"
    "记录未能证明某事件发生，不等于该事件未发生。合同流程不能单独证明项目未实施或未完成；"
    "缺少直接事实时使用记录尚不能证明，不擅自断言实际业务未发生或未完成。"
    "例如记录仅有客户下单和合同流程，应写记录尚不能证明项目顺利实施已完成，"
    "不能写项目实施尚未开始、尚未完成或断言实际实施状态。"
    "销售记录中的等待客户评估不是客户已承诺评估；不得将销售安排改写成客户表达。"
    "不要点评拜访目的有几个字，不以字数判断质量。结论简洁，分项与总述避免重复。"
    "分别评价原定关键结果达成程度与本次拜访实际价值。未达成原目标不能自动推断拜访无效；"
    "仅在记录有证据时说明新客户信息、客户表达或动作、判断得到纠正、卡点或下一步。"
    "不得为了肯定而编造成果，也不得用获得新信息冒充原目标已经达成。"
    "facts.reason先用具体客户事实说明本次发生了什么，再说明与原目标的关系和下一步；"
    "有价值且无明显缺口时简短肯定，不拼凑改善建议。"
    "已填写但不具体要指出缺少的具体事项，不说未填写。"
    "销售观点必须有客户事实支撑；计划、条件式承诺、未知均不得当作已发生事实。"
    "记录中明确的保密、临时议题变化、暂未取得信息等只属于待核验的例外说明，"
    "根据已有替代事实评价；不能自动判达标、豁免公司评分门槛或要求编造客户事实。"
    "对未知信息说明证据缺口和可行核验动作；不得把接口未取得数据归责为销售未填写。"
    "未取得SWAS、打卡和关联表单数据时，不声称它们缺失、不一致或未更新。"
    "下一步行动对象默认当前客户，不强制补具体联系人。"
    "过程事实不要求每条填写姓名或职务；但不清楚是谁表达、确认了什么确实影响判断时，"
    "允许指出具体事实来源缺口并建议核实，不能仅因没有姓名或职务否定已有客户事实。"
    "若明确目标就是确认采购负责人等人员信息，应正常检查是否取得目标所需的信息，"
    "不能用联系人可选的保护规则免除该目标检查，也不能增加目标未要求的联系方式等条件。"
    "客户下一步明确共识仅适用于商机客户；潜力客户及目标客户缺少下一步承诺不构成缺口，"
    "facts.reason和各项建议同样必须遵守此边界。"
    "潜力客户日期按自然季度、目标客户按自然月核对，不能将潜力客户描述为仅需跨月。"
    "商机客户的下次联系日期仅须晚于拜访日期，不增加跨自然月或跨自然季度要求。"
    "AI意见不代表经理已检查、已确认或已复查。"
    "本次目的匹配由程序的authoritative_checks确定，不得推翻允许清单；"
    "只针对影响当前目标判断的真实缺口建议补充，可选证据不是逐项必填清单，"
    "空值不等于接口未传入，模型达标项不再追加泛化改善建议。"
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
    supplied = visit.metadata.get("source_supplied_fields")
    supplied = set(supplied) if isinstance(supplied, list) else set(visit.model_fields_set)
    data = visit.model_dump(mode="json")
    return {field: (
        "not_received" if field not in supplied else
        "empty" if data.get(field) in (None, "", [], {}) else "present"
    ) for field in REQUIRED_FIELDS}


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
