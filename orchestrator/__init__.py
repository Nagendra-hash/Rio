"""
orchestrator/ — the central Jarvis Orchestrator (Phase 1).

Jarvis becomes the coordinator: every inbound ask is classified, routed to the
right capability agent, and answered — with a guaranteed safe fallback to the
existing assistant (Gemini live path / Hermes bridge) whenever no agent can
take the task.

    User -> Hermes -> Jarvis(Orchestrator) -> Router -> Agent -> Result
         -> Jarvis -> Hermes -> User

Phase 1 scope: intake, classification, routing, agent registry, standard
request/response contracts, conversation/context handoff, error handling,
logging, human-approval state, and five agent STUBS (interfaces only).
Full agents are Phase 2.

Design rules (non-negotiable):
  * stdlib-only and offline-safe — the orchestrator must never be the reason
    Jarvis fails to boot.
  * default mode is "shadow": classify + log only. The existing Gemini path
    is untouched until explicitly opted in (JARVIS_ORCHESTRATOR=on).
  * anything unknown, unimplemented, or erroring falls back to the existing
    assistant. The orchestrator never swallows a user turn.
  * no destructive actions anywhere in this package.
"""
from __future__ import annotations

from orchestrator.contracts import (
    AgentRequest,
    AgentResult,
    ApprovalState,
    OrchestratorError,
    TaskStatus,
    TaskType,
    new_task_id,
)
from orchestrator.orchestrator import AgentNotRegisteredError
from orchestrator.base import BaseAgent, StubAgent
from orchestrator.registry import AgentRegistry
from orchestrator.classifier import Classification, IntentClassifier
from orchestrator.router import RouteDecision, TaskRouter
from orchestrator.context import ContextStore, SessionContext
from orchestrator.journal import OrchestrationJournal, get_logger
from orchestrator.approvals import ApprovalGate
from orchestrator.orchestrator import JarvisOrchestrator
from orchestrator.agents import build_default_registry
from orchestrator import integration

__version__ = "1.0.0"

__all__ = [
    "AgentNotRegisteredError",
    "AgentRegistry",
    "AgentRequest",
    "AgentResult",
    "ApprovalGate",
    "ApprovalState",
    "BaseAgent",
    "Classification",
    "ContextStore",
    "IntentClassifier",
    "JarvisOrchestrator",
    "OrchestratorError",
    "OrchestrationJournal",
    "get_logger",
    "RouteDecision",
    "SessionContext",
    "StubAgent",
    "TaskRouter",
    "TaskStatus",
    "TaskType",
    "build_default_registry",
    "integration",
    "new_task_id",
    "__version__",
]
