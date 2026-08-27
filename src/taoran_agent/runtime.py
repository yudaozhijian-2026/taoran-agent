from __future__ import annotations

from .agent import TaoranAgent
from .config import Settings
from .knowledge import TaoranKnowledgeSnapshot, load_taoran_knowledge_snapshot
from .llm import ChatModelReviewer
from .precheck_engine import TaoranPrecheckEngine
from .semantic import HeuristicSemanticReviewer, HttpSemanticReviewer


def build_agent(
    settings: Settings,
    snapshot: TaoranKnowledgeSnapshot | None = None,
) -> TaoranAgent:
    snapshot = snapshot or load_taoran_knowledge_snapshot(settings.knowledge_snapshot_path)
    if settings.llm_enabled:
        reviewer = ChatModelReviewer(settings, snapshot)
    elif settings.semantic_endpoint:
        reviewer = HttpSemanticReviewer(
            settings.semantic_endpoint,
            settings.semantic_api_key,
            settings.semantic_timeout_seconds,
        )
    else:
        reviewer = HeuristicSemanticReviewer()
    return TaoranAgent(reviewer, TaoranPrecheckEngine(snapshot))
