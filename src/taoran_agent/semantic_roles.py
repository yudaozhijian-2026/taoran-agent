"""Local Chinese proposition scopes shared by the post and preview truth gates.

These roles describe how a clause is *used*.  They do not establish that a
business fact is true; evidence matching remains the caller's responsibility.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise


class SemanticRole(StrEnum):
    ASSERTED_FACT = "ASSERTED_FACT"
    NEGATED_FACT = "NEGATED_FACT"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    QUESTION_OR_PENDING_CONFIRMATION = "QUESTION_OR_PENDING_CONFIRMATION"
    SALES_RECOMMENDATION = "SALES_RECOMMENDATION"
    DESIRED_OUTCOME = "DESIRED_OUTCOME"
    EXAMPLE_OR_HYPOTHETICAL = "EXAMPLE_OR_HYPOTHETICAL"
    CONDITIONAL_FUTURE = "CONDITIONAL_FUTURE"
    CUSTOMER_INTENT = "CUSTOMER_INTENT"
    SUPPORTED_CUSTOMER_COMMITMENT = "SUPPORTED_CUSTOMER_COMMITMENT"
    UNSUPPORTED_CUSTOMER_COMMITMENT = "UNSUPPORTED_CUSTOMER_COMMITMENT"


@dataclass(frozen=True)
class SemanticSegment:
    text: str
    start: int
    end: int


_CLAUSE = re.compile(r"[^。；;\n，,]+")
# A contrast or a new explicit actor starts another proposition even without
# punctuation.  Coordinated example objects ("或客户…") remain in the example.
_INDEPENDENT_EVENT = re.compile(
    r"(?=(?:但|然而|不过|可是|同时|并且|而|且|和)(?:客户|对方|我方|销售))"
)
_EXAMPLE = re.compile(r"(?:例如|比如|譬如|举例|假设|假定)")
_INSUFFICIENT = re.compile(
    r"(?:不足以|尚不足以|没有证据|尚无证据|无法证明|不能证明|无法确认|"
    r"尚不能认定|不能认定|未见证据|缺少证据)"
)
_PENDING = re.compile(r"(?:是否|能否|待确认|待核实|需要确认|需确认|请确认|尚待确认)")
_NEGATED = re.compile(r"(?:尚未|未|没有|并未|不曾|尚无)(?:[^。；;\n，,]{0,12})(?:同意|确认|承诺|保证|约定|接受|完成|提供|付款|采购)")
_CONDITIONAL = re.compile(
    r"^(?:如果|假如|若|如(?!实|期))|"
    r"(?:如果|假如|若|如)(?:(?:实际|确实)?已|[^。；;\n，,]{0,40}(?:再|才|则|后|通过|成立|满足))"
)
_DESIRED = re.compile(r"(?:希望|期望|争取|推动|促使|期待|力争|拟推动|计划推动)")
_RECOMMENDATION = re.compile(
    r"(?:^|[：:])\s*(?:(?:销售|我方)[^。；;\n，,]{0,4}建议|建议|请|需要|应当|应|可|可以|需|宜|(?:并|再)?(?:将|把))"
)
_INTENT = re.compile(r"(?:客户|对方)(?:[^。；;\n，,]{0,16})(?:表示|考虑|意向|倾向|可能|拟)")
_SALES_PLAN = re.compile(r"(?:销售|我方)(?:[^。；;\n，,]{0,12})(?:计划|拟|准备|将)(?:[^。；;\n，,]{0,20})")


def semantic_segments(text: str) -> list[SemanticSegment]:
    """Split only at local proposition boundaries, preserving source offsets."""
    result: list[SemanticSegment] = []
    for clause in _CLAUSE.finditer(str(text or "")):
        starts = [0, *(match.start() for match in _INDEPENDENT_EVENT.finditer(clause.group()) if match.start())]
        starts.append(len(clause.group()))
        for left, right in pairwise(starts):
            raw = clause.group()[left:right]
            value = raw.strip()
            if value:
                offset = len(raw) - len(raw.lstrip())
                start = clause.start() + left + offset
                result.append(SemanticSegment(value, start, start + len(value)))
    return result


def semantic_scope(segment: str, event_start: int | None = None, event_end: int | None = None) -> SemanticRole:
    """Classify one local event using only its own proposition and leading scope.

    The priority is intentional: a quoted/example proposition is not an
    assertion; lack of evidence is not a customer refusal; a requested or
    hoped-for event is not an already agreed customer action.
    """
    value = str(segment or "").strip()
    start = len(value) if event_start is None else max(0, event_start)
    end = len(value) if event_end is None else min(len(value), event_end)
    before = value[:start]
    proposition = value[:end]
    if _EXAMPLE.search(before):
        return SemanticRole.EXAMPLE_OR_HYPOTHETICAL
    if _INSUFFICIENT.search(before):
        return SemanticRole.INSUFFICIENT_EVIDENCE
    if _PENDING.search(proposition):
        return SemanticRole.QUESTION_OR_PENDING_CONFIRMATION
    if _NEGATED.search(proposition):
        return SemanticRole.NEGATED_FACT
    if _CONDITIONAL.search(before) or _CONDITIONAL.search(proposition):
        return SemanticRole.CONDITIONAL_FUTURE
    if _DESIRED.search(before):
        return SemanticRole.DESIRED_OUTCOME
    if _RECOMMENDATION.search(before):
        return SemanticRole.SALES_RECOMMENDATION
    if _INTENT.search(proposition):
        return SemanticRole.CUSTOMER_INTENT
    if _SALES_PLAN.search(proposition):
        return SemanticRole.SALES_RECOMMENDATION
    return SemanticRole.ASSERTED_FACT
