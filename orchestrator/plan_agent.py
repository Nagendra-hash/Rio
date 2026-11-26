"""
orchestrator/plan_agent.py — the Plan-and-Execute capability surface.

This is NOT another independent agent: it is the thin BaseAgent adapter over
the Plan-and-Execute engine (planner → inspectable plan → executor → final
verification), exposed so the existing orchestrator/router can route
multi-step goals through it and park approvals in the EXISTING ApprovalGate.

    Goal → Plan (inspectable, approval-gated) → Step 1 → Verify →
    Step 2 → Verify → … → Final result

Flow inside handle():

    1. PLAN       planner.make_goal_plan() → validated Plan (LLM planner,
                  heuristic floor). The plan is a PROPOSAL — nothing ran.
    2. APPROVAL   any requires_approval step parks the plan in the existing
                  ApprovalGate (NEEDS_APPROVAL). resolve_plan() executes on
                  approve and cancels on deny. The checkpoint IS the plan
                  (memory/plan_engine/<plan_id>.json) — always inspectable.
    3. EXECUTE    PlanExecutor runs steps in dependency order, verifying
                  each, retrying per policy, checkpointing every transition,
                  emitting progress events.
    4. RESULT     final verification → COMPLETED / FAILED AgentResult with
                  the full plan state in result.data["plan"].

No approvals needed → plan runs immediately (safe steps only).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from orchestrator.base import BaseAgent
from orchestrator.checkpoint import PlanCheckpointStore
from orchestrator.classifier import IntentClassifier
from orchestrator.contracts import (AgentRequest, AgentResult, TaskStatus,
                                    TaskType)
from orchestrator.plan_executor import (JournalReporter, PlanExecutor,
                                        PlanRunResult)
from orchestrator.planner import HeuristicPlanner, LLMPlanner
from orchestrator.plans import Plan, PlanValidationError, new_plan_id

log = logging.getLogger("jarvis.plan_engine")


class PlanAgent(BaseAgent):
    """The routed capability for multi-step goals. Core infrastructure:
    owns NO tools of its own — every step delegates to existing pieces."""

    name = "plan_agent"
    description = ("Plan-and-Execute: breaks a complex goal into small, "
                   "inspectable, dependency-ordered steps, gets approval "
                   "when steps are dangerous, executes sequentially with "
                   "per-step verification, retries, checkpoints, and a "
                   "final verification report.")
    task_types = (TaskType._FALLBACK,)   # deliberate: reached by explicit
                                         # override or ask, never stolen
                                         # from a specific lane

    def __init__(self, planner: LLMPlanner | None = None,
                 executor: PlanExecutor | None = None,
                 store: PlanCheckpointStore | None = None) -> None:
        self.store = store or PlanCheckpointStore()
        self.planner = planner or LLMPlanner()
        self.executor = executor          # built lazily in handle(): needs
                                          # the live registry

    # ── entry ────────────────────────────────────────────────────────────
    def handle(self, request: AgentRequest) -> AgentResult:
        try:
            return self._handle(request)
        except Exception as exc:                       # never break the router
            log.exception("plan_agent failed")
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.FAILED,
                text=f"[plan_agent] internal error: "
                     f"{type(exc).__name__}: {exc}",
                data={"fallback": False}, agent=self.name)

    def _handle(self, request: AgentRequest) -> AgentResult:
        meta = dict(request.metadata or {})

        # ── approval resolution path (existing ApprovalGate contract) ────
        if meta.get("plan_action") in ("approve", "deny"):
            return self._resolve(request, meta)

        # ── plan (or resume) path ────────────────────────────────────────
        goal = (request.text or "").strip()
        resume_id = str(meta.get("plan_resume") or "").strip()
        if not goal and not resume_id:
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.NEEDS_INPUT,
                text="What is the goal? Tell me what you want done and I "
                     "will plan it step by step.",
                data={"needs": "goal"}, agent=self.name)

        if resume_id:
            data = self.store.load(resume_id)
            if not data:
                return AgentResult(
                    task_id=request.task_id, status=TaskStatus.FAILED,
                    text=f"No plan checkpoint for '{resume_id}'.",
                    agent=self.name)
            try:
                plan = Plan.from_dict(data)
            except PlanValidationError as exc:
                return AgentResult(
                    task_id=request.task_id, status=TaskStatus.FAILED,
                    text=f"Plan checkpoint for '{resume_id}' is invalid: "
                         f"{exc}", agent=self.name)
        else:
            plan = self._make_plan(request, goal)

        run = self._executor(request).execute(
            plan, approved=bool(meta.get("plan_approved")))
        if run.status is TaskStatus.NEEDS_APPROVAL:
            self._park_approval(request, plan, run)
        return self._result(request, run, plan)

    # ── planning ─────────────────────────────────────────────────────────
    def _make_plan(self, request: AgentRequest, goal: str) -> Plan:
        try:
            plan = self.planner.plan(goal, list(request.context or []),
                                     created_by="plan_agent")
        except Exception as exc:                       # planner must not kill intake
            log.warning("planner crashed: %s", exc)
            plan = None
        if plan is None:
            plan = HeuristicPlanner().plan(goal, created_by="plan_agent")
        if plan is None:                                # goal empty after all
            plan = Plan(goal=goal or "(empty goal)", steps=[], status="draft")
        return plan

    def _executor(self, request: AgentRequest) -> PlanExecutor:
        if self.executor is None:
            registry = request.metadata.get("_registry") if isinstance(
                request.metadata, dict) else None
            if registry is None:
                try:
                    # the LIVE registry of a shared orchestrator, when wired
                    from orchestrator.integration import shared_orchestrator
                    registry = shared_orchestrator().registry
                except Exception:
                    registry = None
            if registry is None:
                try:
                    from orchestrator.agents import build_default_registry
                    registry = build_default_registry()
                except Exception:
                    registry = _RegistryAdapter()
            reporter = None
            try:
                from orchestrator.journal import OrchestrationJournal
                reporter = JournalReporter(OrchestrationJournal())
            except Exception:
                pass
            self.executor = PlanExecutor(
                registry=registry,
                classifier=IntentClassifier(),
                store=self.store,
                reporter=reporter,
                allow_shell=os.environ.get("JARVIS_PLAN_SHELL", "off")
                .strip().lower() == "on")
        return self.executor

    def _park_approval(self, request: AgentRequest, plan: Plan,
                       run: PlanRunResult) -> None:
        """Park the plan in the EXISTING ApprovalGate (the orchestrator also
        does this for any NEEDS_APPROVAL result; doing it here too keeps the
        state consistent when the agent is invoked directly)."""
        try:
            from orchestrator.approvals import ApprovalGate
            gate = request.metadata.get("_approval_gate")
            if isinstance(gate, ApprovalGate):
                gate.request(plan.plan_id, self.name,
                             str(run.data.get("approval_summary") or
                                 plan.summary_line()))
        except Exception:
            pass

    # ── approval resolution ──────────────────────────────────────────────
    def _resolve(self, request: AgentRequest, meta: dict) -> AgentResult:
        plan_id = str(meta.get("plan_id") or "").strip()
        data = self.store.load(plan_id)
        if not data:
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.DECLINED,
                text=f"[plan_agent] no pending plan '{plan_id}' — nothing "
                     f"was run.", data={"plan": "not_found"},
                agent=self.name)
        plan = Plan.from_dict(data)
        if meta.get("plan_action") == "deny":
            if plan.status in ("completed", "failed"):
                return AgentResult(
                    task_id=request.task_id, status=TaskStatus.DECLINED,
                    text=f"[plan_agent] plan {plan.plan_id} is already "
                         f"{plan.status} — nothing was run.",
                    data={"plan": "already_resolved"}, agent=self.name)
            plan.status = "failed"
            for step in plan.steps:
                if step.status in ("pending", "running"):
                    step.status = "denied"
            self.store.save(plan.to_dict(include_runtime=True))
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.DENIED,
                text=f"[plan_agent] plan {plan.plan_id} denied — nothing "
                     f"was run.",
                data={"plan": plan.to_dict(include_runtime=True)},
                agent=self.name)
        run = self._executor(request).execute(plan, approved=True,
                                              approved_by=str(
                                                  meta.get("approved_by")
                                                  or "user"))
        return self._result(request, run, plan)

    # ── result shaping ───────────────────────────────────────────────────
    @staticmethod
    def _result(request: AgentRequest, run: PlanRunResult,
                plan: Plan) -> AgentResult:
        data = dict(run.data)
        data["plan_id"] = plan.plan_id
        data["plan_checkpoint"] = str(PlanCheckpointStore().dir)
        return AgentResult(
            task_id=request.task_id,
            status=run.status,
            text=run.text,
            data=data,
            agent="plan_agent",
        )


class _RegistryAdapter:
    """Minimal registry surface when none was injected (tests, embedding)."""
    def __init__(self, agents: Any = None) -> None:
        self._agents = list(agents or [])

    def by_task_type(self, task_type):
        return [a for a in self._agents
                if task_type in getattr(a, "task_types", ())]


def resolve_plan(orchestrator, plan_id: str, approved: bool,
                 by: str = "user") -> AgentResult:
    """Module-level approval hook for the orchestrator/dashboard:
    resolves a parked plan through the plan_agent (approval gate contract)."""
    agent = orchestrator.registry.get("plan_agent")
    if agent is None:
        return AgentResult(
            task_id=plan_id, status=TaskStatus.DECLINED,
            text="[plan_agent] plan capability is not registered.",
            agent="plan_agent")
    request = AgentRequest(
        task_id=plan_id, task_type=TaskType._FALLBACK, text="(plan approval)",
        metadata={"plan_action": "approve" if approved else "deny",
                  "plan_id": plan_id, "approved_by": by})
    return agent.handle(request)
