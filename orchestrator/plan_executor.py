"""
orchestrator/plan_executor.py — executes an inspectable Plan, step by step.

The engine behind: Goal → Plan → Step 1 → Verify → Step 2 → Verify → … →
Final result. Core orchestration infrastructure, NOT another agent: every
step runs through components that already exist —

    agent steps  → the AgentRegistry + BaseAgent.handle() contract
    llm steps    → the Model Router (orchestrator.planner.LLMBackend)
    shell steps  → the planner's read-only allowlist
    noop steps   → a marker that always "passes"

Execution rules (fixed, checkpointed, honest):

  1. ORDER      steps run in the plan's topological order; a step runs only
                when all of its dependencies are done.
  2. APPROVAL   if any step requires_approval and the run is not approved,
                NOTHING runs: the plan is parked awaiting_approval and the
                rendered plan is returned for inspection. Approval is
                explicit per plan (never auto-granted); a deny cancels.
  3. VERIFY     every step gets a per-step verification check on its output
                ("contains:", "re:", or plain non-empty). Failing
                verification counts as a failed attempt and consumes retry
                budget exactly like an error.
  4. RETRY      up to the step's max_retries (clamped by the schema).
  5. FAIL       per-step on_fail policy: "abort" (default) stops the plan —
                remaining steps are skipped; "skip" drops this step AND its
                dependents; "continue" lets dependents run anyway.
  6. CHECKPOINT the full plan state is saved after EVERY step transition
                (crash-safe: resume continues from the last verified step).
  7. PROGRESS   every transition emits a PlanProgress event to the reporter
                (and the orchestrator journal, if wired).
  8. FINAL      when all steps are terminal the plan's success criteria are
                checked honestly; unmet criteria → plan FAILED, never faked.

execute() NEVER raises — every failure is a reported result plus a
checkpoint.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from orchestrator.base import BaseAgent
from orchestrator.checkpoint import PlanCheckpointStore
from orchestrator.contracts import (AgentRequest, AgentResult, TaskStatus,
                                    TaskType, new_task_id)
from orchestrator.planner import LLMBackend, RouterLLM, _sanitize_shell_command
from orchestrator.plans import MAX_RETRIES, Plan, PlanStep

log = logging.getLogger("jarvis.plan_engine")

STEP_TIMEOUT_S = 120.0            # per-step soft budget (steps are small)
SHELL_TIMEOUT_S = 15.0            # read-only commands answer fast


# ── progress reporting ──────────────────────────────────────────────────────
@dataclass
class PlanProgress:
    """One observable transition in a plan run."""
    plan_id: str
    level: str                     # plan | step
    event: str                     # plan_started | step_started | step_done |
                                   # step_retry | step_failed | step_skipped |
                                   # awaiting_approval | approved |
                                   # plan_completed | plan_failed
    step_id: str = ""
    detail: str = ""
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id, "level": self.level, "event": self.event,
            "step_id": self.step_id, "detail": self.detail[:200],
            "at": self.at.isoformat(),
        }


class ProgressReporter:
    """Protocol: report(event) — UIs, journals, and tests plug in here."""
    def report(self, event: PlanProgress) -> None:  # pragma: no cover
        raise NotImplementedError


class JournalReporter(ProgressReporter):
    """Bridges plan progress into the orchestrator's JSONL journal."""
    def __init__(self, journal: Any) -> None:
        self._journal = journal

    def report(self, event: PlanProgress) -> None:
        try:
            self._journal.record(f"plan.{event.event}",
                                 event.plan_id or "",
                                 step_id=event.step_id,
                                 detail=event.detail)
        except Exception:                          # journal is best-effort
            pass


# ── results ─────────────────────────────────────────────────────────────────
@dataclass
class PlanRunResult:
    """The outcome of one executor pass. Mirrors AgentResult semantics."""
    plan_id: str
    status: TaskStatus             # COMPLETED | FAILED | NEEDS_APPROVAL
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_agent_result(self, agent: str = "plan_engine") -> AgentResult:
        return AgentResult(task_id=self.plan_id, status=self.status,
                           text=self.text, data=self.data, agent=agent)


# ── the executor ────────────────────────────────────────────────────────────
class PlanExecutor:
    """Runs a validated Plan against registered agents + safe local tools."""

    def __init__(self, registry: Any, classifier: Any = None,
                 llm: LLMBackend | None = None,
                 store: PlanCheckpointStore | None = None,
                 reporter: ProgressReporter | None = None,
                 step_timeout_s: float = STEP_TIMEOUT_S,
                 clock: Callable[[], float] = time.monotonic,
                 allow_shell: bool = True) -> None:
        self.registry = registry
        self.classifier = classifier
        self.llm = llm
        self.store = store or PlanCheckpointStore()
        self.reporter = reporter
        self.step_timeout_s = float(step_timeout_s)
        self.allow_shell = allow_shell
        self._clock = clock

    # ── public API ────────────────────────────────────────────────────────
    def execute(self, plan: Plan, *, approved: bool = False,
                approved_by: str = "user") -> PlanRunResult:
        """Run (or continue) a plan. NEVER raises.

        approved=False on a plan with approval-gated steps → nothing runs,
        the plan is parked, and the rendered plan is returned for inspection.
        """
        try:
            return self._execute(plan, approved=approved,
                                 approved_by=approved_by)
        except Exception as exc:                    # last-resort containment
            log.exception("plan execution failed hard")
            try:
                plan.status = "failed"
                self._save(plan)
            except Exception:
                pass
            return PlanRunResult(
                plan_id=plan.plan_id, status=TaskStatus.FAILED,
                text=f"[plan_engine] internal error: "
                     f"{type(exc).__name__}: {exc}",
                data={"plan": plan.to_dict()})

    # ── execution ────────────────────────────────────────────────────────
    def _execute(self, plan: Plan, *, approved: bool,
                 approved_by: str) -> PlanRunResult:
        plan.validate()

        # ── APPROVAL gate: inspectable plans before dangerous execution ──
        if plan.approval_steps() and not approved:
            if plan.is_terminal:
                return self._terminal(plan)     # already resolved; nothing to do
            plan.status = "awaiting_approval"
            self._save(plan)
            self._report(plan, "awaiting_approval",
                         detail=f"{len(plan.approval_steps())} step(s) "
                                f"need human approval")
            return PlanRunResult(
                plan_id=plan.plan_id, status=TaskStatus.NEEDS_APPROVAL,
                text=self._approval_text(plan),
                data={"plan": plan.to_dict(include_runtime=True),
                      "approval_summary": self._approval_summary(plan),
                      "needs": "approval",
                      "approval_steps": [s.id for s in plan.approval_steps()]})

        # resume bookkeeping: awaiting_approval → user approved just now
        if plan.status == "awaiting_approval":
            self._report(plan, "approved", detail=f"by {approved_by}")
        elif plan.status != "running":           # crash-resume stays quiet
            self._report(plan, "plan_started", detail=plan.summary_line())
        plan.status = "running"
        self._save(plan)

        # ── the step loop (topological order; each step becomes terminal) ─
        aborted_at = ""
        for step in plan.ordered_steps():
            if step.status in ("done", "failed", "skipped", "denied"):
                continue                                   # already terminal
            if aborted_at:
                # an abort-failed step stops the WHOLE plan, not just its
                # dependents — every remaining step is skipped, honestly
                self._mark_step(plan, step, "skipped",
                                error=f"plan aborted at {aborted_at}")
                continue
            blocker = next((d for d in step.depends_on
                            if plan.steps_for(d).status in
                            ("failed", "skipped", "denied")), "")
            if blocker:
                policy = plan.steps_for(blocker).on_fail
                if policy == "continue":
                    pass                   # author opted in: run anyway
                elif policy == "skip":
                    self._mark_step(plan, step, "skipped",
                                    error=f"dependency {blocker} did not "
                                          f"complete")
                    continue
                else:                      # abort: park the remaining steps
                    self._mark_step(plan, step, "skipped",
                                    error=f"plan aborted at {blocker}")
                    continue
            self._run_step(plan, step)
            if step.status == "failed" and step.on_fail == "abort":
                aborted_at = step.id

        return self._final_verification(plan)

    # ── one step: run + verify + retry ───────────────────────────────────
    def _run_step(self, plan: Plan, step: PlanStep) -> None:
        max_retries = max(0, min(MAX_RETRIES, step.max_retries))
        attempts_allowed = max_retries + 1
        last_error = ""
        while step.attempts < attempts_allowed:
            step.attempts += 1
            step.status = "running"
            step.started_at = step.started_at or datetime.now(timezone.utc)
            self._save(plan)
            self._report(plan, "step_started", step.id,
                         detail=f"attempt {step.attempts}: {step.title}")
            try:
                ok, output, data = self._dispatch(plan, step)
            except Exception as exc:                    # tool blew up
                ok, output, data = False, "", {"error":
                                               f"{type(exc).__name__}: {exc}"}
            ok = ok and self._verify(step, output)
            if ok:
                step.status = "done"
                step.result_text = str(output)[:2000]
                step.result_data = data if isinstance(data, dict) else {}
                step.error = ""
                step.finished_at = datetime.now(timezone.utc)
                self._save(plan)
                self._report(plan, "step_done", step.id,
                             detail=str(output)[:120])
                return
            last_error = str((data or {}).get("error") or output or
                             "verification failed")[:500]
            if step.attempts <= max_retries:
                self._report(plan, "step_retry", step.id,
                             detail=f"attempt {step.attempts} failed: "
                                    f"{last_error[:80]}")
        # retries exhausted (or budget already spent before a resume)
        self._mark_step(plan, step, "failed", error=last_error or
                        "retries exhausted")

    def _mark_step(self, plan: Plan, step: PlanStep, status: str,
                   error: str = "") -> None:
        step.status = status
        step.error = error[:1000]
        step.finished_at = datetime.now(timezone.utc)
        self._save(plan)
        event = {"failed": "step_failed", "skipped": "step_skipped",
                 "denied": "step_failed"}.get(status, "step_done")
        self._report(plan, event, step.id, detail=error or status)

    # ── dispatch: tool/agent selection ───────────────────────────────────
    def _dispatch(self, plan: Plan, step: PlanStep) -> tuple[bool, str, dict]:
        """Route one step to its executor. Returns (ok, output, data)."""
        action = step.action
        params = step.params or {}
        if action == "agent":
            return self._run_agent_step(plan, step, params)
        if action == "llm":
            return self._run_llm_step(step, params)
        if action == "shell":
            return self._run_shell_step(step, params)
        if action == "noop":
            return True, "noop", {"noop": True}
        return False, "", {"error": f"unknown action {action!r}"}

    def _run_agent_step(self, plan: Plan, step: PlanStep,
                        params: dict) -> tuple[bool, str, dict]:
        text = str(params.get("text") or step.title)
        task_type = self._select_task_type(step, text)
        candidates = self._agents_for(task_type, text)
        if not candidates:
            return False, "", {"error": f"no agent registered for "
                                        f"task_type={task_type.value}"}
        agent = candidates[0]
        request = AgentRequest(
            task_id=new_task_id(prefix=f"step-{step.id}"),
            task_type=task_type, text=text,
            metadata={"plan_id": plan.plan_id, "plan_step": step.id,
                      "via": "plan_engine"})
        result = agent.handle(request)
        if not isinstance(result, AgentResult):
            return False, "", {"error": "agent returned a non-standard result"}
        if result.status is TaskStatus.COMPLETED:
            return True, result.text or "done", {"agent": agent.name,
                                                 "result_data": result.data}
        if result.status is TaskStatus.NEEDS_APPROVAL:
            # the sub-capability wants its own approval → surface it honestly
            return False, "", {"error":
                               f"agent {agent.name} requires approval",
                               "needs_approval": True,
                               "agent": agent.name,
                               "approval_summary":
                                   str(result.data.get("approval_summary")
                                       or result.text[:120])}
        return False, "", {"error": result.error or
                           f"agent {agent.name} returned "
                           f"{result.status.value}",
                           "agent": agent.name}

    def _select_task_type(self, step: PlanStep, text: str) -> TaskType:
        """Tool/agent selection: the plan's hint first, then the classifier."""
        hinted = str((step.params or {}).get("task_type") or "").strip()
        if hinted:
            try:
                return TaskType(hinted)
            except ValueError:
                pass
        if self.classifier is not None:
            try:
                return self.classifier.classify(text).task_type
            except Exception:
                pass
        return TaskType._FALLBACK

    def _agents_for(self, task_type: TaskType, text: str) -> list[Any]:
        try:
            serving = self.registry.by_task_type(task_type)
        except Exception:
            serving = []
        probe = AgentRequest(task_id="probe", task_type=task_type, text=text)
        return [a for a in serving if _can_handle(a, probe)]

    def _run_llm_step(self, step: PlanStep,
                      params: dict) -> tuple[bool, str, dict]:
        if self.llm is None:
            self.llm = RouterLLM()
        prompt = str(params.get("prompt") or step.title)
        text = self.llm.complete(
            prompt, system="You complete one small step of a JARVIS plan. "
                           "Answer with the step's result only.")
        if not text:
            return False, "", {"error": "no model available"}
        return True, text, {"chars": len(text)}

    def _run_shell_step(self, step: PlanStep,
                        params: dict) -> tuple[bool, str, dict]:
        if not self.allow_shell:
            return False, "", {"error": "shell steps are disabled"}
        command = _sanitize_shell_command(str(params.get("command") or ""))
        if not command:
            return False, "", {"error": "command not on the read-only "
                                        "allowlist"}
        try:
            proc = subprocess.run(command.split(), capture_output=True,
                                  text=True, timeout=SHELL_TIMEOUT_S)
            output = (proc.stdout or "") + (proc.stderr or "")
            data = {"returncode": proc.returncode}
            if proc.returncode != 0:
                return False, output[:2000], {**data,
                                              "error": f"exit "
                                                       f"{proc.returncode}"}
            return True, output[:2000], data
        except subprocess.TimeoutExpired:
            return False, "", {"error": f"timeout after {SHELL_TIMEOUT_S}s"}

    # ── per-step verification ────────────────────────────────────────────
    @staticmethod
    def _verify(step: PlanStep, output: str) -> bool:
        """Per-step verification: contains: → re: → plain non-empty."""
        verification = (step.verification or "").strip()
        output = output or ""
        if verification.startswith("re:"):
            try:
                return re.search(verification[3:], output) is not None
            except re.error:
                return bool(output.strip())
        if verification.startswith("contains:"):
            return verification[len("contains:"):].lower() in output.lower()
        return bool(output.strip())   # default: step produced something

    # ── final verification ───────────────────────────────────────────────
    def _final_verification(self, plan: Plan) -> PlanRunResult:
        """Success criteria: every step done. An aborted or failed plan says
        so honestly; skipped/failed steps are listed."""
        # defensive: nothing may still be pending here
        for step in plan.steps:
            if step.status in ("pending", "running", ""):
                self._mark_step(plan, step, "skipped",
                                error="left pending after the step loop")
        failed = [s for s in plan.steps if s.status == "failed"]
        skipped = [s for s in plan.steps if s.status in ("skipped", "denied")]
        criteria_ok = not failed and not skipped and \
            all(s.status == "done" for s in plan.steps)
        plan.status = "completed" if criteria_ok else "failed"
        self._save(plan)
        self._report(plan, "plan_completed" if criteria_ok else "plan_failed",
                     detail=plan.summary_line())
        return PlanRunResult(
            plan_id=plan.plan_id,
            status=TaskStatus.COMPLETED if criteria_ok else TaskStatus.FAILED,
            text=self._final_text(plan),
            data={"plan": plan.to_dict(include_runtime=True),
                  "progress": plan.progress,
                  "failed_steps": [s.id for s in failed],
                  "skipped_steps": [s.id for s in skipped]})

    def _terminal(self, plan: Plan) -> PlanRunResult:
        """execute() called on an already-terminal (or already-running) plan."""
        ok = plan.status == "completed"
        return PlanRunResult(
            plan_id=plan.plan_id,
            status=TaskStatus.COMPLETED if ok else TaskStatus.FAILED,
            text=self._final_text(plan) if ok else
            f"[plan_engine] plan {plan.plan_id} is {plan.status}; "
            f"nothing was re-run.",
            data={"plan": plan.to_dict(include_runtime=True),
                  "progress": plan.progress})

    # ── rendering (inspection) ───────────────────────────────────────────
    @staticmethod
    def render(plan: Plan, *, include_results: bool = True) -> str:
        """Human-readable plan — the artifact users inspect and approve."""
        lines = [f"Plan {plan.plan_id} — {plan.goal}  [{plan.status}]"]
        for s in plan.ordered_steps():
            flags = []
            if s.requires_approval:
                flags.append("APPROVAL")
            if s.on_fail != "abort":
                flags.append(f"on_fail={s.on_fail}")
            if s.max_retries:
                flags.append(f"retries={s.max_retries}")
            flag_txt = f" ({', '.join(flags)})" if flags else ""
            deps = f"  ← depends on {', '.join(s.depends_on)}" \
                if s.depends_on else ""
            lines.append(f"  {s.id}. {s.title}{flag_txt}{deps}")
            lines.append(f"     action: {s.action}"
                         + (f" {_json_params(s.params)}" if s.params else ""))
            if s.verification:
                lines.append(f"     verify: {s.verification}")
            if include_results and s.result_text:
                lines.append(f"     result: {s.result_text[:200]}")
            if include_results and s.error:
                lines.append(f"     error: {s.error[:200]}")
        for note in plan.risk_notes:
            lines.append(f"  risk: {note}")
        return "\n".join(lines)

    @staticmethod
    def _approval_text(plan: Plan) -> str:
        ids = ", ".join(s.id for s in plan.approval_steps())
        return ("[plan_engine] this plan needs your approval before it runs "
                f"(step(s) {ids}). Approve to execute; deny to cancel.\n\n"
                f"{PlanExecutor.render(plan, include_results=False)}")

    @staticmethod
    def _approval_summary(plan: Plan) -> str:
        gated = plan.approval_steps()
        titles = "; ".join(f"{s.id}: {s.title}" for s in gated)
        risks = "; ".join(plan.risk_notes[:3])
        return (f"plan {plan.plan_id}: {len(gated)} approval-gated step(s) "
                f"— {titles}" + (f" | risks: {risks}" if risks else ""))

    @staticmethod
    def _final_text(plan: Plan) -> str:
        counts = plan.progress
        parts = [f"{counts.get('done', 0)}/{len(plan.steps)} steps"]
        if counts.get("failed"):
            failed = ", ".join(s.id for s in plan.steps if s.status == "failed")
            parts.append(f"FAILED: {failed}")
        if counts.get("skipped") or counts.get("denied"):
            skipped = ", ".join(s.id for s in plan.steps
                                if s.status in ("skipped", "denied"))
            parts.append(f"skipped: {skipped}")
        for s in plan.steps:
            if s.status == "done" and s.result_text:
                parts.append(f"{s.id}: {s.result_text[:300]}")
        verdict = "Plan completed." if plan.status == "completed" \
            else "Plan FAILED."
        return f"[plan_engine] {verdict} (" + "; ".join(parts) + ")"

    # ── internals ────────────────────────────────────────────────────────
    def _report(self, plan: Plan, event: str, step_id: str = "",
                detail: str = "") -> None:
        if self.reporter is None:
            return
        try:
            self.reporter.report(PlanProgress(
                plan_id=plan.plan_id, level="step" if step_id else "plan",
                event=event, step_id=step_id, detail=detail))
        except Exception as exc:                    # progress never kills a run
            log.warning("progress reporter failed: %s", exc)

    def _save(self, plan: Plan) -> None:
        try:
            self.store.save(plan.to_dict(include_runtime=True))
        except Exception as exc:                    # checkpoint is best-effort
            log.warning("plan checkpoint failed: %s", exc)


def _json_params(params: dict) -> str:
    try:
        return json.dumps(params, ensure_ascii=False)[:160]
    except Exception:
        return str(params)[:160]


def _can_handle(agent: BaseAgent, request: AgentRequest) -> bool:
    try:
        return bool(agent.can_handle(request))
    except Exception:
        return False
