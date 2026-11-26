"""
core/rag_strategies — PHASE 4: Adaptive, Agentic, CRAG and Self-RAG.

Six retrieval strategies (adaptive / agentic / corrective / self_rag +
their local offline twins) sharing ONE pipeline in base.py:

    detect complexity → decide retrieval → plan depth/sources → evaluate
    evidence → correct poor retrieval → answer → re-evaluate → retrieve
    again → cited answer

Security model is unchanged from Phase 3: strategies never touch
core/rag.py directly and never choose their own scope — core/rag_service
hands them a pre-scoped retriever closure. See docs/RAG_STRATEGIES.md.
"""
from core.rag_strategies.base import (Answer, Evidence, RetrievalPipeline,
                                      MAX_ATTEMPTS, MAX_ITERATIONS)
from core.rag_strategies.strategies import (REGISTRY, LLM_STRATEGIES,
                                            LOCAL_STRATEGIES,
                                            DEFAULT_STRATEGY, strategies,
                                            get)
from core.rag_strategies import heuristics

__all__ = [
    "Answer", "Evidence", "RetrievalPipeline", "MAX_ATTEMPTS",
    "MAX_ITERATIONS", "REGISTRY", "LLM_STRATEGIES", "LOCAL_STRATEGIES",
    "DEFAULT_STRATEGY", "strategies", "get", "heuristics",
]
