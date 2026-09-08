"""Semantic findings are scoped observations, never task or scoring gates."""

import logging

GUIDANCE = (
    "按最新原始记录逐项核对原定目标，区分已发生、未来计划、明确否定和无法确认。"
    "未记录不等于未发生；收到订单不追加收款、发货条件。"
    "不强制姓名或职务，只在主体歧义影响具体结论时说明谁做了什么尚无法确认。"
    "信息不足时正常回答不足以判断，不补造主体、结果或肯定结论，不因此否定整条拜访。"
    "本次目标是确认负责人时仍检查负责人信息。"
    "只对影响结论的原文缺口提出需确认事项，不能把AI自身的推断错误归责于销售。"
    "后续建议不能替换原定目标；过程原文明确标注的未来计划可用于下一步分析，不能算作已完成事实。"
    "历史AI意见和校验观察不能作为本次事实；不自动改写记录。"
)


def observe(check, *args, scope, **kwargs):
    """Observer outages cannot invalidate a usable generation."""
    try:
        return [{**hit, "scope": scope, "policy": "observe_only"}
                for hit in check(*args, **kwargs)]
    except Exception:
        logging.getLogger(__name__).exception("Semantic observer unavailable: %s", scope)
        return [{"rule": "observer_unavailable", "scope": scope, "policy": "observe_only"}]


def final_feedback_observations(reason, suggestions, context):
    from .post_quality import quality_hits

    findings = observe(quality_hits, reason, "facts.reason", context, scope="facts.reason")
    for index, suggestion in enumerate(suggestions):
        findings += observe(quality_hits, suggestion, "suggestion", context,
                            scope=f"suggestions.{index}")
    return {"status": "observed", "policy": "observe_only", "hits": findings}
