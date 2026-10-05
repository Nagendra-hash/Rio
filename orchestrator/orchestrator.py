"""
orchestrator/orchestrator.py — the central coordinator.

Owns one task end-to-end:

    intake → classify → route → (handoff context) → dispatch agent
          → result | approval | NEEDS_INPUT | error → safe fallback

The orchestrator is the ONLY component that invokes agents and the ONLY
place that decides to fall back to the existing assistant. Agents never see
each other; failures are contained per agent and per task.

Nothing in submit() may raise: a user turn must always produce either an
agent result or the safe fallback result, and never take the host process
down with it.
"""
from __future__ import annotations

import logging
from typing import Optional

from orchestrator.approvals import ApprovalGate
from orchestrator.base import BaseAgent
from orchestrator.classifier import Classification, IntentClassifier
from orchestrator.contracts import (
    AgentNotRegisteredError,
    AgentRequest,
    AgentResult,
    TaskStatus,
    TaskType,
    new_task_id,
)
from orchestrator.context import ContextStore
from orchestrator.journal import OrchestrationJournal
from orchestrator.registry import AgentRegistry
from orchestrator.router import RouteDecision, TaskRouter

log = logging.getLogger("jarvis.orchestrator")


def _result_for(result: AgentResult, task_id: str, agent: str) -> AgentResult:
    """Fill in identity fields the agent may have left blank."""
    if not result.task_id:
        result.task_id = task_id
    if not result.agent:
        result.agent = agent
    return result


class JarvisOrchestrator:
    """Central coordinator. See module docstring for the full loop."""

    def __init__(self, registry: AgentRegistry | None = None,
                 classifier: IntentClassifier | None = None,
                 context: ContextStore | None = None,
                 journal: OrchestrationJournal | None = None,
                 approvals: ApprovalGate | None = None,
                 fallback_agent: str = "gemini_assistant",
                 min_confidence: float = 0.5) -> None:
        self.registry = registry or AgentRegistry()
        self.classifier = classifier or IntentClassifier()
        self.context = context or ContextStore()
        self.journal = journal or OrchestrationJournal(enabled=False)
        self.approvals = approvals or ApprovalGate()
        self.router = TaskRouter(self.registry,
                                 fallback_agent=fallback_agent,
                                 min_confidence=min_confidence)
        self._last_result: dict[str, AgentResult] = {}

    # ── registry helpers ──────────────────────────────────────────────────
    def register_agent(self, agent: BaseAgent) -> BaseAgent:
        return self.registry.register(agent)

    def require_agent(self, name: str) -> BaseAgent:
        agent = self.registry.get(name)
        if agent is None:
            raise AgentNotRegisteredError(f"agent not registered: {name}")
        return agent

    # ── task intake ───────────────────────────────────────────────────────
    def submit(self, text, *, session_id: str = "default",
               channel: str = "", chat_id: str = "",
               attachments: list[str] | None = None,
               metadata: dict | None = None,
               task_id: str | None = None,
               classify_only: bool = False) -> AgentResult:
        """Intake for one user turn. NEVER raises.

        submit() is the single entry point used by Hermes-bridge hooks, the
        dashboard, and tests. Flow: classify → route → dispatch → result,
        with a guaranteed fallback result whenever no agent can (or may)
        take the task.
        """
        try:
            return self._submit(text, session_id=session_id, channel=channel,
                                chat_id=chat_id, attachments=attachments,
                                metadata=metadata, task_id=task_id,
                                classify_only=classify_only)
        except Exception as exc:                     # last-resort containment
            log.exception("orchestrator.submit failed")
            return AgentResult(
                task_id=task_id or new_task_id(),
                status=TaskStatus.DECLINED,
                text="[orchestrator] internal error; deferring to "
                     "existing assistant.",
                data={"fallback": True,
                      "reason": f"{type(exc).__name__}: {exc}"},
                agent=self.router.fallback_agent,
            )

    def _submit(self, text, *, session_id, channel, chat_id, attachments,
                metadata, task_id, classify_only) -> AgentResult:
        task_id = task_id or new_task_id()
        text = (text or "").strip() if isinstance(text, str) else ""
        channel = (channel or "").strip()
        chat_id = (chat_id or "").strip()
        meta = dict(metadata or {}) if isinstance(metadata, dict) else {}

        self._journal_safe("task_received", task_id, text, channel)

        # ── intent / task classification ─────────────────────────────────
        try:
            classification = self.classifier.classify(text)
        except Exception as exc:            # classifier must not kill intake
            self._journal_safe("error", task_id, "classify",
                               f"{type(exc).__name__}: {exc}")
            classification = Classification(TaskType._FALLBACK, 0.0,
                                            "classifier error")
        self._journal_safe("classified", task_id,
                           classification.task_type.value,
                           classification.confidence,
                           classification.reason)

        if classify_only:
            return AgentResult(task_id=task_id, status=TaskStatus.NEEDS_INPUT,
                               text=classification.task_type.value,
                               data={"classification": {
                                   "task_type": classification.task_type.value,
                                   "confidence": classification.confidence,
                                   "reason": classification.reason,
                               }},
                               agent="orchestrator")

        # Unknown ask → the existing assistant answers, exactly as today.
        if not classification.is_known:
            self._journal_safe("fallback", task_id, classification.reason)
            fallback = self._fallback_result(task_id, classification.reason)
            try:                      # the turn still happened — remember it
                self.context.record_user(session_id, text, channel, chat_id)
                self.context.record_assistant(session_id, fallback.text)
            except Exception:
                pass
            return fallback

        # ── standard request envelope ────────────────────────────────────
        request = AgentRequest(
            task_id=task_id,
            task_type=classification.task_type,
            text=text,
            attachments=[str(a) for a in (attachments or [])],
            metadata={**meta, "channel": channel, "chat_id": chat_id,
                      "session_id": session_id},
        )

        # Conversation/context handoff: attach the session's recent history.
        try:
            request = self.context.attach(request, session_id)
        except Exception as exc:
            self._journal_safe("error", task_id, "context_attach",
                               f"{type(exc).__name__}: {exc}")

        # ── routing + execution ──────────────────────────────────────────
        try:
            result = self.router.execute(request, classification)
        except TaskRouter.FallbackExhausted as exc:
            self._journal_safe("fallback", task_id, str(exc))
            return self._fallback_result(task_id, str(exc))
        except Exception as exc:            # routing itself blew up
            self._journal_safe("error", task_id, "route",
                               f"{type(exc).__name__}: {exc}")
            return self._fallback_result(task_id, f"router error: {exc}")

        # ── standard result handling ─────────────────────────────────────
        result = _result_for(result, task_id, result.agent or "unknown")
        self._last_result[task_id] = result
        self._journal_safe("agent_result", task_id, result.agent,
                           result.status.value,
                           (result.error or result.text)[:200])

        if result.status is TaskStatus.NEEDS_APPROVAL:
            summary = str(result.data.get("approval_summary",
                                          result.text[:120]))
            try:
                self.approvals.request(task_id, result.agent, summary)
            except Exception as exc:
                self._journal_safe("error", task_id, "approval_request",
                                   f"{type(exc).__name__}: {exc}")
            self._journal_safe("approval_requested", task_id, result.agent,
                               summary)

        # Remember the turn AFTER handling, so the agent's own question
        # (NEEDS_INPUT) is part of the next handoff too.
        try:
            self.context.record_user(session_id, text, channel, chat_id)
            self.context.record_assistant(session_id, result.text or "")
        except Exception as exc:
            self._journal_safe("error", task_id, "context_record",
                               f"{type(exc).__name__}: {exc}")
        return result

    # ── human approval ────────────────────────────────────────────────────
    def resolve_approval(self, task_id: str, approved: bool,
                         by: str = "user") -> AgentResult:
        """Resolve a pending approval for task_id.

        approved=True  → the parked task is released to the agent that
                         requested it (stub acknowledges; Phase 2 executes).
        approved=False → the task is denied; nothing ran, nothing will.
        Unknown/expired ids return a safe DECLINED result — never an error.
        """
        try:
            return self._resolve_approval(task_id, approved, by)
        except Exception as exc:
            self._journal_safe("error", str(task_id), "resolve_approval",
                               f"{type(exc).__name__}: {exc}")
            return AgentResult(
                task_id=str(task_id), status=TaskStatus.DECLINED,
                text="[orchestrator] error while resolving approval; "
                     "nothing was run.",
                data={"approval": "error"}, agent="orchestrator")

    def _resolve_approval(self, task_id: str, approved: bool,
                          by: str) -> AgentResult:
        agent_name = self._approval_agent_of(task_id)   # read BEFORE resolve pops it
        resolved = self.approvals.resolve(task_id, approved, by=by)
        self._journal_safe("approval_resolved", task_id, approved, by)
        if not resolved:
            return AgentResult(
                task_id=task_id,
                status=TaskStatus.DECLINED,
                text=f"[orchestrator] no pending approval for {task_id} "
                     f"(already resolved or expired). Nothing was run.",
                data={"approval": "not_pending"},
                agent="orchestrator",
            )
        if not approved:
            return AgentResult(
                task_id=task_id,
                status=TaskStatus.DENIED,
                text="[orchestrator] approval denied — task will not run.",
                data={"approval": "denied"},
                agent="orchestrator",
            )
        agent = None
        if agent_name:
            try:
                agent = self.require_agent(agent_name)
            except AgentNotRegisteredError:
                agent = None
        if agent is None:
            return self._fallback_result(task_id, "approval agent vanished")
        request = AgentRequest(
            task_id=task_id,
            task_type=agent.task_types[0] if agent.task_types
            else TaskType._FALLBACK,
            text="(approved task)",
            metadata={"approved": True, "by": by})
        try:
            result = _result_for(agent.handle(request), task_id, agent.name)
        except Exception as exc:
            self._journal_safe("error", task_id, "approved_dispatch",
                               f"{type(exc).__name__}: {exc}")
            return self._fallback_result(task_id, f"approved task failed: {exc}")
        self._last_result[task_id] = result
        self._journal_safe("agent_result", task_id, result.agent,
                           result.status.value,
                           (result.error or result.text)[:200])
        return result

    def _approval_agent_of(self, task_id: str) -> str:
        """The agent that requested approval for this task ("" if unknown)."""
        try:
            state = self.approvals.get(task_id)
            return state.agent if state else ""
        except Exception:
            return ""

    # ── results ───────────────────────────────────────────────────────────
    def result_of(self, task_id: str) -> Optional[AgentResult]:
        return self._last_result.get(task_id)

    # ── internals ─────────────────────────────────────────────────────────
    def _fallback_result(self, task_id: str, reason: str) -> AgentResult:
        """The guaranteed safe outcome: hand the turn to the existing
        assistant. Phase 1 only *names* it — the caller (dashboard / Hermes
        hook) keeps doing exactly what it does today."""
        return AgentResult(
            task_id=task_id,
            status=TaskStatus.DECLINED,
            text=f"[orchestrator] no routed agent for this task "
                 f"({reason}); deferring to existing assistant.",
            data={"fallback": True, "reason": reason},
            agent=self.router.fallback_agent,
        )

    def _journal_safe(self, method: str, *args) -> None:
        """Journal calls must never break a user turn."""
        try:
            getattr(self.journal, method)(*args)
        except Exception as exc:
            log.warning("journal.%s failed: %s", method, exc)
