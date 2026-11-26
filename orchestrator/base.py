"""
orchestrator/base.py — the agent interface.

Phase 1 ships interfaces + stubs only. A Phase-2 agent implements
BaseAgent.handle() and registers itself; nothing else in the system changes.
"""

from __future__ import annotations

import logging
from typing import Any

from orchestrator.contracts import AgentRequest, AgentResult, TaskStatus

# ── Telemetry ────────────────────────────────────────────────────────────────
try:
    from core.telemetry import trace_agent_call
except Exception:
    trace_agent_call = None

log = logging.getLogger("jarvis.orchestrator")


class BaseAgent:
    """Contract every routed agent implements.

    Subclasses set name/description/task_types and implement handle().
    """

    name: str = "base"
    description: str = ""
    # Task types this agent is willing to serve (its "home" types plus any
    # extras it can genuinely cover — e.g. trading also covers live data).
    task_types: tuple = ()

    def can_handle(self, request: AgentRequest) -> bool:
        """Default: serves any of its declared task types."""
        return request.task_type in self.task_types

    def handle(self, request: AgentRequest) -> AgentResult:
        """Main entry point. Subclasses implement this.
        Telemetry wrapper captures latency, success/failure, task_type."""
        if trace_agent_call:
            # Use task_type from request for telemetry
            task_type = request.task_type.value if request.task_type else "unknown"
            cm = trace_agent_call(self.name, task_type)
        else:
            from contextlib import nullcontext

            cm = nullcontext()
        with cm:
            return self._handle_impl(request)

    def _handle_impl(self, request: AgentRequest) -> AgentResult:
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"<Agent {self.name} task_types={self.task_types}>"


class StubAgent(BaseAgent):
    """A routed-but-not-yet-built agent.

    Behavior is deliberately boring and safe:
      * NEEDS_APPROVAL  → returns a pending approval record (proves the
        human-approval state end-to-end without doing anything).
      * otherwise       → NEEDS_INPUT saying the capability exists but is not
        implemented yet, so the orchestrator falls back to the existing
        assistant instead of dead-ending the user.
    Never performs work. Never errors on purpose.
    """

    def __init__(
        self,
        name: str,
        description: str,
        task_types: tuple = (),
        *,
        require_approval: bool = False,
    ) -> None:
        self.name = name
        self.description = description
        self.task_types = tuple(task_types)
        self._require_approval = require_approval

    def handle(self, request: AgentRequest) -> AgentResult:
        if self._require_approval:
            if request.metadata.get("approved"):
                # The human said yes (orchestrator re-dispatches with
                # approved=True). Phase 1: acknowledge only — real execution
                # arrives with the Phase 2 agent.
                return AgentResult(
                    task_id=request.task_id,
                    status=TaskStatus.COMPLETED,
                    text=f"[{self.name}] approved — acknowledged (execution "
                    f"lands with the Phase 2 agent).",
                    data={"approved": True},
                    agent=self.name,
                )
            return AgentResult(
                task_id=request.task_id,
                status=TaskStatus.NEEDS_APPROVAL,
                text=f"[{self.name}] task '{request.text[:80]}' requires "
                f"human approval before execution.",
                data={"approval_summary": f"{self.name}: {request.text[:120]}"},
                agent=self.name,
            )
        return AgentResult(
            task_id=request.task_id,
            status=TaskStatus.NEEDS_INPUT,
            text=f"[{self.name}] capability is registered but not implemented "
            f"yet (Phase 1 stub); falling back to the main assistant.",
            data={"stub": True, "agent": self.name},
            agent=self.name,
        )

    # Convenience for Phase 2: record the last request for debugging.
    last_request: AgentRequest | None = None
    last_result: AgentResult | None = None

    async def ahandle(self, request: AgentRequest) -> AgentResult:  # pragma: no cover
        """Async sibling for future event-loop integrations."""
        return self.handle(request)

    def handle_logged(self, request: AgentRequest) -> AgentResult:
        """handle() + debug breadcrumb; used by tests and the orchestrator."""
        self.last_request = request
        result = self.handle(request)
        self.last_result = result
        log.debug(
            "agent %s handled %s → %s", self.name, request.task_id, result.status.value
        )
        return result


def passthrough_result(
    request: AgentRequest, text: str, data: dict[str, Any] | None = None
) -> AgentResult:
    """Shorthand for agents that complete immediately."""
    return AgentResult(
        task_id=request.task_id,
        status=TaskStatus.COMPLETED,
        text=text,
        data=data or {},
        agent="orchestrator",
    )
