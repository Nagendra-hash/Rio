"""
orchestrator/router.py — task routing.

Takes a Classification + registry and produces a RouteDecision: which agent
runs, in what order candidates are tried, and — critically — when to fall
back to the existing assistant. The router never executes agents itself; the
orchestrator owns execution and error recovery.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from orchestrator.classifier import Classification
from orchestrator.contracts import AgentRequest, AgentResult, TaskStatus, TaskType
from orchestrator.registry import AgentRegistry

log = logging.getLogger("jarvis.orchestrator")


class _Declined(Exception):
    """Internal: every candidate refused the task."""


@dataclass
class RouteDecision:
    chosen_agent: Optional[str]
    candidates: list[str] = field(default_factory=list)
    fallback: bool = False
    reason: str = ""

    @property
    def to_fallback(self) -> bool:
        return self.fallback or self.chosen_agent is None


class TaskRouter:
    """Chooses an agent for a classified task; defaults to safe fallback.

    Args:
        fallback_agent: the existing assistant, represented as a registry
            name (e.g. "gemini_assistant"). The router only names it; the
            orchestrator owns the actual invocation.
        min_confidence: below this the router does not even try an agent.
            Conservative by design — an unknown ask must reach the assistant
            that already handles it, not a stub that says "not implemented".
    """

    def __init__(self, registry: AgentRegistry, fallback_agent: str = "gemini_assistant",
                 min_confidence: float = 0.5) -> None:
        self.registry = registry
        self.fallback_agent = fallback_agent
        self.min_confidence = min_confidence

    # ── decision ──────────────────────────────────────────────────────────
    def route(self, classification: Classification,
              request: AgentRequest) -> RouteDecision:
        if not classification.is_known:
            return RouteDecision(None, [], True,
                                 f"unknown intent: {classification.reason}")

        if classification.confidence < self.min_confidence:
            return RouteDecision(
                None, [], True,
                f"low confidence {classification.confidence} < "
                f"{self.min_confidence}: {classification.reason}")

        candidates = self._candidates(classification.task_type, request)
        if not candidates:
            return RouteDecision(
                None, [], True,
                f"no registered agent serves {classification.task_type.value}")

        return RouteDecision(candidates[0], candidates, False,
                             f"routed to {candidates[0]} "
                             f"({classification.reason})")

    def _candidates(self, task_type: TaskType,
                    request: AgentRequest) -> list[str]:
        """Registered agents for the lane, ordered; explicit overrides first."""
        order: list[str] = []

        requested = request.metadata.get("agent_override")
        if requested:
            agent = self.registry.get(str(requested))
            if agent is not None and agent.can_handle(request):
                order.append(agent.name)

        # Agents serving this task type, most specific declaration first.
        serving = self.registry.by_task_type(task_type)
        serving.sort(key=lambda a: -len(a.task_types))  # placeholder policy
        for a in serving:
            if a.name not in order and a.can_handle(request):
                order.append(a.name)
        return order

    # ── execution loop (called by the orchestrator) ───────────────────────
    def execute(self, request: AgentRequest,
                classification: Classification) -> AgentResult:
        """Try candidates in order; honor DECLINE re-routing; never raise.

        NEEDS_APPROVAL / NEEDS_INPUT are surfaced to the orchestrator as-is —
        they are not failures.
        """
        decision = self.route(classification, request)
        if decision.to_fallback:
            raise _Declined(decision.reason)

        tried: list[str] = []
        last_error = ""
        for name in decision.candidates:
            agent = self.registry.get(name)
            if agent is None:
                continue
            tried.append(name)
            try:
                result = agent.handle(request)
                if not isinstance(result, AgentResult):
                    raise TypeError(f"agent {name} returned {type(result).__name__}")
            except Exception as exc:                       # agent blew up
                last_error = f"{type(exc).__name__}: {exc}"
                log.exception("agent %s failed task %s", name, request.task_id)
                continue
            if result.status is TaskStatus.DECLINED:
                log.info("agent %s declined task %s — trying next candidate",
                         name, request.task_id)
                continue
            result.agent = result.agent or name
            return result

        # Every candidate either raised or declined → safe fallback.
        raise _Declined(last_error or
                        f"all candidates declined: {', '.join(tried)}")

    # Expose for orchestrator error handling
    FallbackExhausted = _Declined
