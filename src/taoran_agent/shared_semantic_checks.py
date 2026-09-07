"""Only hard, source-grounded conflicts are gates; unknown matches stay unknown."""

from .experimental_business_semantic_state import build_business_state
from .experimental_semantic_invariants import validate_invariants
from .record_contract import field_claim_hits, goal_scope_hits

# Do not import frontend's vague-target/self-assessment presentation restrictions
# into formal scoring. These selected rules protect factual and goal boundaries.
FACT_RULES = frozenset(
    {
        "JOINT_AGREEMENT_ERASED",
        "TEMPORALITY_MISMATCH",
        "ACTOR_MISMATCH",
        "NOT_RECORDED_AS_NEGATIVE_FACT",
        "SUPPORTED_GOAL_DOWNGRADED",
        "CROSS_GOAL_CONTAMINATION",
        "PROCUREMENT_ATTITUDE_AS_COMPLETION_CONDITION",
    }
)


def semantic_hits(text, context, target):
    hits = field_claim_hits(text, context, target) + goal_scope_hits(text, context, target)
    for issue in validate_invariants(text, build_business_state(context)):
        if issue["code"] in FACT_RULES:
            hits.append(
                {
                    "rule": issue["code"],
                    "target": target,
                    "quote": issue["text"],
                    "scanned_text": text,
                    "goal_ids": issue.get("goal_ids", []),
                    "fact_ids": issue.get("fact_ids", []),
                }
            )
    return hits
