"""
orchestrator/agents/__init__.py — routing targets.

Five capabilities are registered so the router has real destinations. Most
are still Phase-1 stubs (NEEDS_INPUT → the orchestrator falls back to the
existing assistant); the rag_agent is the first REAL Phase-2 agent, enabled
with JARVIS_RAG_AGENT=on and scoped to its granted collections through
core/rag_service (see config/rag_callers.json).

Phase 2 replaces each remaining stub by registering a real BaseAgent under
the SAME name — the router, contracts, and orchestrator are untouched.
"""
from __future__ import annotations

import os

from orchestrator.agents import stubs
from orchestrator.base import StubAgent
from orchestrator.contracts import TaskType
from orchestrator.registry import AgentRegistry

# Canonical registry names — Phase 2 agents must reuse these names.
CODING_AGENT = "coding_agent"
RESEARCH_AGENT = "research_agent"
TRADING_AGENT = "trading_agent"
RAG_AGENT = "rag_agent"
SYSTEM_AGENT = "system_agent"


def _rag_agent():
    """The real rag_agent (Phase 2), imported lazily so a broken core.rag
    dependency cannot take the whole registry down."""
    from orchestrator.agents.rag_agent import RagAgent
    return RagAgent()


def build_default_registry() -> AgentRegistry:
    """The registry: stubs under their canonical names, plus the real rag
    agent when JARVIS_RAG_AGENT=on (default off — shadow-mode convention)."""
    registry = AgentRegistry()
    registry.register(StubAgent(
        CODING_AGENT,
        "Code understanding, bug fixes, refactors, tests (Phase 2).",
        task_types=(TaskType.CODING,)))
    registry.register(StubAgent(
        RESEARCH_AGENT,
        "Web research, news, comparisons, summaries (Phase 2).",
        task_types=(TaskType.RESEARCH,)))
    registry.register(StubAgent(
        TRADING_AGENT,
        "Market analysis and paper trading. Human approval required "
        "for anything that moves money.",
        task_types=(TaskType.TRADING,),
        require_approval=True))
    if (os.environ.get("JARVIS_RAG_AGENT", "off").strip().lower() == "on"):
        try:
            registry.register(_rag_agent())
        except Exception as exc:
            print(f"[orchestrator] rag_agent unavailable ({exc}) — "
                  "stub stays registered")
            registry.register(StubAgent(
                RAG_AGENT,
                "Private documents / knowledge base Q&A (Phase 2).",
                task_types=(TaskType.RAG,)))
    else:
        registry.register(StubAgent(
            RAG_AGENT,
            "Private documents / knowledge base Q&A (Phase 2).",
            task_types=(TaskType.RAG,)))
    registry.register(StubAgent(
        SYSTEM_AGENT,
        "Device control, system status, app/process actions (Phase 2).",
        task_types=(TaskType.SYSTEM,),
        require_approval=True))
    return registry


__all__ = [
    "CODING_AGENT", "RESEARCH_AGENT", "TRADING_AGENT", "RAG_AGENT",
    "SYSTEM_AGENT", "StubAgent", "stubs", "build_default_registry",
]
