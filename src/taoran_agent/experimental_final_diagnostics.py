"""Safe candidate diagnostics: no model text, quotes, credentials or raw errors."""
import re

from .experimental_semantic_audit import CHECKS


def safe_code(value):
    if value is None:
        return None
    if not isinstance(value, str):
        return "unknown_failure"
    if value in {"timeout", "queue_timeout", "queue_full", "invalid_json", "invalid_contract", "invalid_content", "output_truncated", "provider_http_error", "rate_limited", "authentication_failed", "access_denied", "invalid_response_or_network_error"}:
        return value
    if isinstance(value, str) and re.fullmatch(r"(?:wording|analysis_point)_[a-z_]{1,90}", value):
        return value
    return "unknown_failure"


def audit(review):
    return {
        "failure_reason": safe_code(getattr(review, "failure_reason", None)),
        "model_attempt_count": getattr(review, "attempt_count", None),
        "recovered_after_retry": getattr(review, "recovered_after_retry", False),
        "attempt_failure_codes": [safe_code(item.get("failure_reason")) for item in getattr(review, "model_attempts", [])][:2],
        "validation_codes": list(dict.fromkeys(safe_code(item.get("code")) for item in getattr(review, "validation_errors", [])))[:20],
        "experimental_semantic_audits": [safe_semantic_audit(item.get("experimental_semantic_audit"))
            for item in getattr(review, "model_attempts", [])[:2] if item.get("experimental_semantic_audit")],
    }


def safe_semantic_audit(value):
    if not isinstance(value, dict):
        return {}
    return {
        "status": value.get("status") if value.get("status") in {"passed", "rejected", "unavailable"} else "unavailable",
        "failed_checks": [code for code in value.get("failed_checks", []) if code in CHECKS][:6],
        "provider_failure": safe_code(value.get("provider_failure")),
        **{key: value[key] for key in ("latency_ms", "queue_ms", "prompt_tokens", "completion_tokens", "total_tokens", "review_attempt_count")
           if type(value.get(key)) is int and value[key] >= 0},
    }


def category(reason):
    if reason == "wording_experimental_binding_conflict":
        return "final_binding_conflict"
    if reason == "wording_experimental_invariant_conflict":
        return "final_invariant_conflict"
    if reason == "wording_experimental_record_state_conflict":
        return "final_record_state_conflict"
    if reason and reason.startswith("wording_experimental_audit_"):
        return "final_semantic_review_rejected" if reason.removeprefix("wording_experimental_audit_") in CHECKS else "final_semantic_review_unavailable"
    if reason == "wording_experimental_receipt_role_conflict":
        return "final_receipt_role_conflict"
    if reason == "wording_experimental_approved_goal_conflict":
        return "final_approved_goal_conflict"
    if reason == "wording_experimental_goal_inflation":
        return "final_goal_inflation"
    if reason == "wording_experimental_assessment_evidence_missing":
        return "final_assessment_evidence_missing"
    if reason == "wording_speaker_attribution_conflict":
        return "final_attribution_conflict"
    if reason == "wording_analysis_consistency_conflict":
        return "final_consistency_conflict"
    if reason in {"wording_experimental_visit_result_missing", "wording_experimental_analysis_missing"}:
        return "final_analysis_missing"
    if reason == "output_truncated":
        return "final_output_truncated"
    if reason in {"invalid_json", "invalid_contract", "invalid_content"}:
        return "final_format_error"
    if reason in {"timeout", "queue_timeout", "queue_full", "provider_http_error", "rate_limited", "authentication_failed", "access_denied", "invalid_response_or_network_error"}:
        return "final_upstream_error"
    return "final_analysis_unavailable"


def repair_instruction(reason):
    reason = safe_code(reason)
    if reason == "wording_experimental_binding_conflict":
        return "若错误为C_PROCESS的DUPLICATE_CONTRACT，将该契约的多条过程事实合并为一条，保留客户需求和销售回应以及对应连续原文证据；这不是否定这些事实，不要按forbidden_claim删除已记录事实。其他错误仅按rendering_repairs修复指定contract和suggestion_scope，保持其他点逐字不变。goal_id、claim_type必须与契约一致；fact_ids等机器元数据由程序补齐，不要求模型输出。正文与对应目标一致，证据用连续原文。仍输出完整JSON，只重试一次。"
    if reason == "wording_experimental_invariant_conflict":
        return "依据invariant_errors中的code、错句、goal_ids/fact_ids修正业务内容一次。语义索引仅供定位，原始记录才是事实依据：保留支持目标及对应事实、主体、时间、否定状态；占位和模糊不可说未填，未记录不可说未发生。分别陈述各子目标，禁止让无关反馈缺口污染已完成动作。只改违反不变量的推理，输出完整JSON；证据仍使用原始字段。"
    if reason == "wording_experimental_record_state_conflict":
        return "按state_errors定位原句：记录未体现联系时间不能写成实际未约定时间；目标占位时只能说无法判断，不能用目的或过程补目标。保留已有事实与原始字段，修正具体错误后返回完整JSON。"
    if reason and reason.startswith("wording_experimental_audit_"):
        focus = {
            "actor": "逐句区分销售、客户、转述人及不同部门；不得替换动作主体或预算层级。",
            "goal": "根据具体错误位置区分目标替换、额外验收条件和目标具体性建议。合理的目标具体化不等于扩大目标；不得用实际是否达成判目标文字是否具体。目标为占位内容时不能自行补目标。",
            "temporal": "计划、承诺、待审批不能写成已经完成；保留原文时态和否定条件。",
            "coverage": "概括本次主要实际结果和关键限制，不只保留背景、合作态度及下次计划。",
            "consistency": "分析与全部逐项建议用同一套事实；可以指出其他缺口，但不能否认已记录行动。",
            "assessment": "只用原目标与实际事实比较达成度，目标无法解释时保留无法判断。部分达成不等于全部完成；分别说明已有成果与未确认部分，不用未全部完成反驳部分达成。不把未证实写成实际没有。",
        }.get(reason.removeprefix("wording_experimental_audit_"), "核查主体、目标范围、时态、关键事实、自评和建议一致性。")
        return "独立语义复核未通过。" + focus + "重新生成完整JSON，保持逐项校验和连续原文证据，不补造事实，不以固定模板代替业务分析。"
    if reason == "wording_experimental_receipt_role_conflict":
        return "依据原定目标核对动作及主体；主体不明确保持未知，不补成销售或客户，也不增加原目标未要求的签收验收条件。保留清楚事实，只有实质影响目标判断的歧义才建议核实。"
    if reason == "wording_experimental_approved_goal_conflict":
        return "重新按当前原定目标与过程独立核对；信息沟通不能增加成交条件，不能以历史样例结论替代当前记录。只输出完整JSON，证据引用原文。"
    if reason == "wording_experimental_goal_inflation":
        return "上次擅自扩大了目标验收范围。确认当前预算卡点不要求全部审批信息；确认合同并承诺签署不要求已经实际签署。只比较输入目标和实际事实，已支持目标时省略assessment_gap，不造新差距；重新生成完整JSON。"
    if reason == "wording_experimental_assessment_evidence_missing":
        return "上次目标达成差距分析缺少证据。重新生成完整JSON：assessment_gap同时引用自评、关键结果、实际过程或客户反馈三类连续原文；不能用R是否合格代替达成判断。若事实已支持目标达成则不要虚构差距。"
    if reason == "wording_speaker_attribution_conflict":
        return "上次把原文发言归给了另一方。依据明确说话人分别概括客户表态与销售行动；无明确主体则使用中性描述，不指定主体。保持客户否定条件和原文证据，重新生成完整JSON。"
    if reason in {"wording_experimental_visit_result_missing", "wording_experimental_analysis_missing"}:
        return "上次缺少通过校验的实际拜访结果。请生成完整JSON，其中至少一个customer_fact或objective_result点概括实际过程；每点不超过55字，proofs从process_description或customer_feedback逐字选连续原文，不拼接、不改字；保留客户否定条件。"
    if reason == "wording_analysis_consistency_conflict":
        return "上次分析与原文或逐项结论冲突。重新核对客户动作、销售动作、期望目标、实际结果，生成一致的完整JSON；不得否认已记录动作或补造客户确认。"
    return "上次输出未通过结构或证据校验。重新生成完整JSON，保持原有字段和逐项建议要求；分析点每点不超过55字，proofs必须来自对应字段连续原文，不能拼接证据或补造事实。"
