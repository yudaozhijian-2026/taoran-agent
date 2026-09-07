"""Evidence-bound original-goal review; models never supply a numeric score."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .experimental_business_semantic_state import decompose_goal


class GoalReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal_id: str
    status: Literal[
        "supported",
        "partially_supported",
        "contradicted",
        "insufficient_evidence",
        "not_assessable",
    ]
    evidence_ids: list[str] = Field(max_length=6)
    actor_ambiguity_material: StrictBool
    reason: str = Field(min_length=1, max_length=140)


GUIDANCE = """
goal_reviews必须逐项覆盖semantic_index.goal_items，使用程序goal_id，不合并、补造或改写原目标。
status分别为supported已支持、partially_supported部分支持、contradicted原文明确不满足、insufficient_evidence证据不足、not_assessable原目标无法解释。
每项填写简短reason及evidence_ids。原目标证据可界定核对范围；达成或反证必须另有过程描述或客户反馈，不能仅凭目标、自评或下次计划证明已发生。
支持或明确反证必须有实际原文证据；没有证据只能说明不足，不得称实际未发生。否定的需求现状可支持信息确认目标。
actor_ambiguity_material仅在主体歧义影响该子目标结论时为true；此时不能认定该子目标已支持，reason须说明受影响结论。
局部主体歧义不自动使其他子目标失败，不自动否定过程已有的清楚事实。
先完成逐项目标核对，再独立确定purpose_achievement及六项意见；全部supported才能认定全部达到，有部分成果时保留部分达到。
目标过于含糊时not_assessable，按既有标准检查目标质量，不将其写成实际业务已失败。
"""


def goals(data):
    return decompose_goal("expected_key_result", data.get("expected_key_result"))


def review_hits(reviews, data, catalog, achievement):
    expected = {g.goal_id: g for g in goals(data)}
    by_id = {e["evidence_id"]: e for e in catalog}
    hits = []

    def fail(rule, row=None):
        hits.append(
            {
                "rule": rule,
                "target": "goal_reviews",
                "goal_id": row.goal_id if row else "",
                "quote": row.reason if row else "",
                "scanned_text": row.reason if row else "",
            }
        )

    if len(reviews) != len(expected) or {r.goal_id for r in reviews} != set(expected):
        fail("original_goal_coverage")
        return hits
    for row in reviews:
        refs = [by_id.get(i) for i in row.evidence_ids]
        allowed = {"process_description", "customer_feedback", "expected_key_result"}
        if any(e is None or e["field"] not in allowed for e in refs):
            fail("goal_evidence_not_actual_source", row)
        if row.status in {"supported", "partially_supported", "contradicted"} and not any(
            e is not None and e["field"] in {"process_description", "customer_feedback"}
            for e in refs
        ):
            fail(
                "goal_evidence_not_actual_source" if refs else "goal_decision_without_evidence", row
            )
        if row.actor_ambiguity_material and row.status == "supported":
            fail("material_actor_unknown_as_supported", row)
        # A conservative keyword index cannot forbid new, valid goal semantics.
        # Only empty/placeholder targets are inherently unassessable.
    states = {r.status for r in reviews}
    if reviews and states == {"supported"} and achievement != "achieved":
        fail("goal_summary_contradiction")
    if reviews and achievement == "achieved" and states != {"supported"}:
        fail("goal_summary_contradiction")
    if "supported" in states and len(states) > 1 and achievement != "partially_achieved":
        fail("partial_goal_erased")
    return hits
