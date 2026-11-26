"""
orchestrator/reflexion.py — PLAN → ACT → OBSERVE → REFLECT → ADJUST → RETRY.

Reflexion is the ACTION-QUALITY loop: it evaluates what an attempt actually
did, decides why it failed, adjusts the next plan, and retries within safe
limits. Built directly on the Phase-9 Plan-and-Execute engine — the plan is
the artifact, the executor runs it, the checkpoint stores it:

    PLAN      an initial Plan (from the planner or the caller)
    ACT       PlanExecutor.execute(plan) — steps run, verify, checkpoint
    OBSERVE   read the run's honest outcome (failed steps, skips, errors)
    REFLECT   why did it fail? → one structured lesson (LLM or heuristic)
    ADJUST    patch the plan: fix failed steps, add steps, reorder
    RETRY     re-execute the adjusted plan — bounded by RetryPolicy

Loop guarantees:
  * attempts are hard-capped (RetryPolicy, clamped to ≤3 retries total);
  * every attempt consumes budget (tokens, wall clock) checked BEFORE it;
  * an attempt that made no progress (same failures twice) stops the loop;
  * every attempt is checkpointed, so "what did attempt 2 change?" always
    has an answer;
  * lessons are stored masked (no secrets) and capped (no memory bloat).

Nothing here executes steps itself — ACT is always the real executor, so
approval gates and permissions apply to every retry exactly as on the
first run.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from orchestrator.agents.coding_tools import mask_secrets
from orchestrator.checkpoint import PlanCheckpointStore
from orchestrator.contracts import TaskStatus
from orchestrator.plans import Plan, PlanStep, PlanValidationError
from orchestrator.reflection import (_clip, _extract_json, _now, LLMBackend,
                                     _DefaultLLM)
from orchestrator.retry_policy import (BudgetState, RetryPolicy,
                                       backoff_delay, rough_tokens)


def _llm_wanted() -> bool:
    """LLM backends are opt-in via env, like every phase's shadow mode."""
    import os as _os
    return _os.environ.get("JARVIS_REFLECT_LLM", "off").strip().lower() == "on"

log = logging.getLogger("jarvis.reflexion")

MAX_LESSONS = 50                    # stored per plan (and in the library)
MAX_LESSON_CHARS = 600              # per lesson
MAX_ATTEMPTS_KEPT = 5               # per-attempt records kept per run


# ── lessons (stored WITHOUT secrets, capped) ────────────────────────────────
def sanitize_lesson(text: str) -> tuple[str, list[str]]:
    """Mask secrets in a lesson and cap its length. Returns (text, kinds)."""
    masked, kinds = mask_secrets(str(text or ""))
    masked = masked.strip()[:MAX_LESSON_CHARS]
    return masked, kinds


class LessonStore:
    """Durable, redacted lesson library — 'what we learned', not secrets.

    Backed by memory/reflexion/lessons.json (JARVIS_HOME-aware). Lessons are
    keyed by an optional goal tag so later plans can recall relevant ones.
    """

    def __init__(self, path: str | os.PathLike | None = None,
                 max_entries: int = 200) -> None:
        if path is None:
            path = _default_path()
        self.path = os.fspath(path)
        self.max_entries = max(1, int(max_entries))
        self._cache: list[dict] | None = None

    def _load(self) -> list[dict]:
        if self._cache is not None:
            return self._cache
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            self._cache = data if isinstance(data, list) else []
        except Exception:
            self._cache = []
        return self._cache

    def _save(self, entries: list[dict]) -> None:
        entries = entries[-self.max_entries:]      # cap in cache AND on disk
        self._cache = entries
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(entries, fh, indent=1,
                          ensure_ascii=False, default=str)
            os.replace(tmp, self.path)
        except Exception as exc:                     # lessons are best-effort
            log.warning("lesson store save failed: %s", exc)

    def add(self, lesson: str, *, goal: str = "", source: str = "reflexion",
            score: float | None = None) -> dict:
        text, kinds = sanitize_lesson(lesson)
        entry = {"lesson": text, "goal": _clip(goal, 200), "source": source,
                 "masked_kinds": kinds, "at": _now()}
        if score is not None:
            entry["score"] = round(float(score), 2)
        entries = self._load()
        entries.append(entry)
        self._save(entries)
        return entry

    def recall(self, goal: str = "", limit: int = 5) -> list[str]:
        """Most recent relevant lessons, newest first (never raises)."""
        entries = self._load()
        goal_l = (goal or "").lower()
        picked: list[dict] = []
        for entry in reversed(entries):
            if len(picked) >= max(1, int(limit)):
                break
            tag = str(entry.get("goal") or "").lower()
            if goal_l and tag and (goal_l in tag or tag in goal_l):
                picked.append(entry)
        if not picked:                       # fall back to newest overall
            picked = list(reversed(entries))[:max(1, int(limit))]
        return [str(e.get("lesson") or "") for e in picked
                if str(e.get("lesson") or "")]

    def __len__(self) -> int:
        return len(self._load())


def _default_path() -> str:
    home = os.environ.get("JARVIS_HOME", "").strip()
    base = os.fspath(home) if home else \
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "memory", "reflexion", "lessons.json")


# ── observation (why did the attempt fail?) ─────────────────────────────────
@dataclass
class Observation:
    """Honest read-out of one ACT phase."""
    attempt: int
    plan_status: str                    # completed | failed
    failed_steps: list[dict] = field(default_factory=list)
    skipped_steps: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    progress_signature: str = ""        # which steps failed — stall detector

    @property
    def ok(self) -> bool:
        return self.plan_status == "completed"

    def to_dict(self) -> dict[str, Any]:
        return {"attempt": self.attempt, "plan_status": self.plan_status,
                "failed_steps": self.failed_steps,
                "skipped_steps": self.skipped_steps,
                "errors": [e[:300] for e in self.errors],
                "progress_signature": self.progress_signature}


def observe(plan: Plan, attempt: int) -> Observation:
    """Read the executor's verdict off the plan itself — no guessing."""
    failed, skipped, errors = [], [], []
    for step in plan.steps:
        rec = {"id": step.id, "title": step.title[:120],
               "action": step.action, "attempts": step.attempts,
               "error": (step.error or "")[:300]}
        if step.status == "failed":
            failed.append(rec)
            if step.error:
                errors.append(f"{step.id}: {step.error}")
        elif step.status in ("skipped", "denied"):
            skipped.append(rec)
    # signature covers WHICH steps failed AND why (first error of each) so
    # a fixed-but-still-failing step counts as progress, not a stall
    signature = ",".join(
        f"{s['id']}:{hash(s['error']) & 0xffff:04x}" if s["error"]
        else s["id"]
        for s in sorted(failed, key=lambda x: x["id"])) or "none"
    return Observation(attempt=attempt,
                       plan_status=plan.status or "failed",
                       failed_steps=failed, skipped_steps=skipped,
                       errors=errors, progress_signature=signature)


# ── reflection (why it failed → lesson) ─────────────────────────────────────
REFLECT_SYSTEM = (
    "You are the JARVIS reflexion critic. Given what an attempted plan did "
    "and how it failed, you name the LIKELY CAUSE and one concrete, "
    "actionable lesson for the next attempt. You never propose running "
    "anything dangerous, never mention credentials, and you answer with "
    "ONE JSON object only.")

REFLECT_PROMPT = """A plan execution failed. Diagnose it.

GOAL:
{goal}

ATTEMPT {attempt} OUTCOME:
failed steps: {failed}
skipped steps: {skipped}
errors: {errors}

PRIOR LESSONS (already known — do not repeat them):
{lessons}

REPLY WITH ONE JSON OBJECT, NOTHING ELSE:
{{
  "cause": "the single most likely reason this attempt failed (1 sentence)",
  "lesson": "what the next attempt should do differently (1 sentence)",
  "adjust": "replace|patch|extend",
  "fix_hint": "what to change in the failed step(s), concretely"
}}

RULES:
- One cause, one lesson — no lists of maybes.
- "replace" = rewrite the failed steps entirely; "patch" = adjust params;
  "extend" = add a missing prerequisite step before the failures.
- The lesson must be checkable next attempt, not a platitude.
"""


def heuristic_reflection(obs: Observation, goal: str,
                         prior_lessons: list[str]) -> dict:
    """Offline cause/lesson/adjust derivation from the error text."""
    blob = " ".join(obs.errors).lower()
    if "no agent registered" in blob:
        cause = "the plan delegated to a capability that is not registered"
        lesson = "route steps only to registered task types, or add a " \
                 "fallback step that handles the work directly"
        adjust = "patch"
    elif "allowlist" in blob or "not on the read-only" in blob:
        cause = "a step used a command outside the read-only allowlist"
        lesson = "plan only allowlisted commands; replace the rest with " \
                 "agent steps"
        adjust = "patch"
    elif "verification" in blob or "empty" in blob:
        cause = "steps completed but their verification found no evidence"
        lesson = "add an intermediate step that produces the evidence the " \
                 "verification expects"
        adjust = "extend"
    elif "timeout" in blob:
        cause = "a step exceeded its time budget"
        lesson = "split oversized steps into smaller ones that finish fast"
        adjust = "patch"
    elif "requires approval" in blob:
        cause = "a step needs human approval that was not granted"
        lesson = "mark approval-needing steps requires_approval so the " \
                 "plan parks cleanly instead of failing mid-run"
        adjust = "patch"
    elif not obs.failed_steps and obs.skipped_steps:
        cause = "an earlier abort skipped the remaining steps"
        lesson = "set on_fail=skip on optional steps so one failure does " \
                 "not abort the plan"
        adjust = "patch"
    else:
        cause = "steps failed for reasons not identifiable from the output"
        lesson = "add a diagnostic step before the failing step to expose " \
                 "the failing condition"
        adjust = "extend"
    return {"cause": cause, "lesson": lesson, "adjust": adjust,
            "fix_hint": "", "source": "heuristic"}


def _heuristic_critic_factory(llm: LLMBackend | None):
    """Reflexion's offline floor: the heuristic reflection."""
    class _H:
        def reflect(self, obs, goal, prior_lessons):
            return heuristic_reflection(obs, goal, prior_lessons)
    return _H()


class ReflexionCritic:
    """LLM-backed cause/lesson; heuristic floor on any failure.

    Constructed only when the caller injects an LLM or sets
    JARVIS_REFLECT_LLM=on — otherwise the heuristic critic is used and the
    loop never touches the network.
    """

    def __init__(self, llm: LLMBackend | None = None) -> None:
        self.llm = llm or _DefaultLLM()

    def reflect(self, obs: Observation, goal: str,
                prior_lessons: list[str]) -> dict:
        prompt = REFLECT_PROMPT.format(
            goal=_clip(goal, 800), attempt=obs.attempt,
            failed=json.dumps([s["id"] + ": " + s["error"][:120]
                               for s in obs.failed_steps]) or "[]",
            skipped=json.dumps([s["id"] for s in obs.skipped_steps]) or "[]",
            errors=_clip("; ".join(obs.errors) or "(none)", 1500),
            lessons=_clip("\n".join(f"- {l}" for l in prior_lessons) or
                          "(none)", 1200))
        raw = self.llm.complete(prompt, system=REFLECT_SYSTEM)
        data = _extract_json(raw or "")
        if not data:
            return heuristic_reflection(obs, goal, prior_lessons)
        out = {
            "cause": _clip(str(data.get("cause") or ""), 400),
            "lesson": _clip(str(data.get("lesson") or ""), MAX_LESSON_CHARS),
            "adjust": str(data.get("adjust") or "patch").lower(),
            "fix_hint": _clip(str(data.get("fix_hint") or ""), 400),
            "source": "llm",
        }
        if out["adjust"] not in ("replace", "patch", "extend"):
            out["adjust"] = "patch"
        if not out["lesson"]:
            return heuristic_reflection(obs, goal, prior_lessons)
        # lessons/cause travel through the same secret choke point as every
        # stored artifact — a leaky model answer still cannot leak.
        for key in ("cause", "lesson", "fix_hint"):
            out[key], kinds = sanitize_lesson(out[key])
            if kinds:
                out.setdefault("masked_kinds", []).extend(kinds)
                out["masked_kinds"] = sorted(set(out["masked_kinds"]))
        return out


# ── adjustment (patch the plan) ─────────────────────────────────────────────
def adjust_plan(plan: Plan, obs: Observation, reflection: dict) -> Plan:
    """Apply the reflection's adjust directive to the plan, in place.

    * reset failed/skipped steps to pending with a fresh attempt budget;
    * "patch": record the fix hint on the failed steps (params/verification
      stay machine-owned — the hint lands in the step title context and the
      checkpoint trail so a human reviewing the plan sees the change);
    * "extend": insert a noop diagnostic step before the first failure;
    * "replace": clear the failed steps' params so the re-plan pass (or an
      LLM planner on the next attempt) rebuilds them.
    """
    mode = reflection.get("adjust") or "patch"
    hint = str(reflection.get("fix_hint") or
               reflection.get("lesson") or "")[:200]
    fixed: list[str] = []
    for step in plan.steps:
        if step.status in ("failed", "skipped", "denied"):
            step.status = "pending"
            step.attempts = 0
            step.error = ""
            step.started_at = None
            step.finished_at = None
            step.result_text = ""
            step.result_data = {}
            fixed.append(step.id)
            if mode == "patch":
                # keep the check, note the intended fix for reviewers
                step.title = (step.title.split(" ⟲")[0] +
                              f" ⟲ {hint[:80]}").strip()
            elif mode == "replace":
                step.params = {}
                step.title = f"re-plan: {step.title.split(' ⟲')[0]}"
    if mode == "extend" and obs.failed_steps:
        first = obs.failed_steps[0]["id"]
        try:
            target = plan.steps_for(first)
        except KeyError:
            target = plan.steps[0]
        # a stable, collision-safe diagnostic id across attempts: count
        # existing diagnostics for this step instead of reusing "<id>x"
        n = sum(1 for s in plan.steps
                if s.id.startswith(f"{first}x"))
        diag_id = f"{first}x{n + 1}"
        # a diagnostic that ALREADY exists with the same hint is reused,
        # not duplicated (idempotent extension)
        existing = next((s for s in plan.steps
                         if s.id == diag_id), None)
        if existing is None:
            diag = PlanStep(
                id=diag_id,
                title=f"diagnose before {first}: {hint[:100]}",
                action="noop", depends_on=list(target.depends_on),
                verification="", on_fail="continue", max_retries=0,
                created_by="reflexion")
            target.depends_on = [diag.id]
            idx = plan.steps.index(target)
            plan.steps.insert(idx, diag)
        else:
            target.depends_on = [diag_id]   # heal the edge after a reset
    plan.status = "draft"
    plan.risk_notes = list(plan.risk_notes) + \
        [f"reflexion attempt: {reflection.get('cause', '')[:160]}"][:3]
    try:
        plan.validate()
    except PlanValidationError as exc:
        log.warning("adjusted plan invalid (%s); reverting statuses", exc)
        raise
    return plan


# ── result ──────────────────────────────────────────────────────────────────
@dataclass
class ReflexionResult:
    goal: str
    success: bool
    attempts_used: int
    final_plan: Plan | None
    observations: list[dict] = field(default_factory=list)
    lessons: list[str] = field(default_factory=list)
    budget_exhausted: str = ""          # "" | "attempts" | "tokens" | "time"
    stalled: bool = False
    tokens_used: int = 0
    wall_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"goal": _clip(self.goal, 500), "success": self.success,
                "attempts_used": self.attempts_used,
                "final_plan": self.final_plan.to_dict()
                if self.final_plan else None,
                "observations": self.observations,
                "lessons": self.lessons,
                "budget_exhausted": self.budget_exhausted,
                "stalled": self.stalled,
                "tokens_used": self.tokens_used,
                "wall_seconds": round(self.wall_seconds, 2)}


# ── the reflexion loop ──────────────────────────────────────────────────────
class ReflexionLoop:
    """PLAN → ACT → OBSERVE → REFLECT → ADJUST → RETRY, bounded. Never raises."""

    def __init__(self, executor: Any,
                 critic: Any | None = None,
                 store: PlanCheckpointStore | None = None,
                 lessons: LessonStore | None = None,
                 policy: RetryPolicy | None = None) -> None:
        self.executor = executor
        if critic is None and _llm_wanted():
            critic = ReflexionCritic()
        self.critic = critic or _heuristic_critic_factory(None)
        self.store = store or getattr(executor, "store", None) or \
            PlanCheckpointStore()
        self.lessons = lessons or LessonStore()
        self.policy = policy or RetryPolicy()
        self._sleep = time.sleep

    def run(self, plan: Plan, *, goal: str = "") -> ReflexionResult:
        """Execute the plan, reflect on failure, adjust, retry — within
        policy. The plan object is mutated attempt-by-attempt and every
        attempt is checkpointed."""
        goal = goal or plan.goal
        result = ReflexionResult(goal=goal, success=False, attempts_used=0,
                                 final_plan=plan)
        budget = BudgetState()
        prior_signatures: set[str] = set()
        lesson_texts: list[str] = []
        try:
            return self._run(plan, goal, result, budget, prior_signatures,
                             lesson_texts)
        except Exception as exc:                     # never break the caller
            log.exception("reflexion loop failed")
            result.budget_exhausted = result.budget_exhausted or "error"
            result.lessons.append(_clip(f"reflexion error: {exc}", 300))
            return result

    # ── internals ────────────────────────────────────────────────────────
    def _run(self, plan: Plan, goal: str, result: ReflexionResult,
             budget: BudgetState, prior_signatures: set[str],
             lesson_texts: list[str]) -> ReflexionResult:
        while True:
            # ── budget gate BEFORE every attempt ────────────────────────
            if result.attempts_used >= self.policy.attempts_allowed():
                result.budget_exhausted = "attempts"
                break
            out_of = budget.exhausted(self.policy)
            if out_of:
                result.budget_exhausted = out_of
                break

            attempt_no = budget.attempt()
            result.attempts_used = attempt_no

            # ── ACT (the real executor; approvals still gate) ───────────
            run = self.executor.execute(plan)
            result.tokens_used += rough_tokens(json.dumps(
                run.data.get("plan", {}), default=str)[:8000])

            # ── OBSERVE ─────────────────────────────────────────────────
            obs = observe(plan, attempt_no)
            result.observations.append(obs.to_dict())
            self._checkpoint(plan, obs)

            if obs.ok:
                result.success = True
                self.lessons.add(
                    f"succeeded on attempt {attempt_no}: plan structure "
                    f"worked", goal=goal, source="reflexion", score=1.0)
                break

            # stall detection: same failure signature twice → stop, but
            # report WHY the loop stopped: a stall is a budget verdict too.
            if obs.progress_signature in prior_signatures:
                result.stalled = True
                result.budget_exhausted = result.budget_exhausted or "stall"
                self.lessons.add(
                    f"stalled: attempt {attempt_no} repeated the same "
                    f"failures ({obs.progress_signature}) — manual "
                    f"intervention needed", goal=goal, source="reflexion")
                break
            prior_signatures.add(obs.progress_signature)

            # ── REFLECT (why it failed → lesson) ────────────────────────
            prior = self.lessons.recall(goal, limit=5)
            reflection = self.critic.reflect(obs, goal, prior)
            lesson = sanitize_lesson(reflection.get("lesson") or "")[0]
            if lesson and lesson not in lesson_texts and \
                    lesson not in prior:
                lesson_texts.append(lesson)
                result.lessons.append(lesson)
                self.lessons.add(lesson, goal=goal,
                                 source=reflection.get("source", "reflexion"))
            result.tokens_used += rough_tokens(str(reflection))

            # last attempt? reflect + store, but do not adjust/retry
            if attempt_no >= self.policy.attempts_allowed():
                result.budget_exhausted = "attempts"
                break

            # ── ADJUST ──────────────────────────────────────────────────
            try:
                adjust_plan(plan, obs, reflection)
            except PlanValidationError as exc:
                log.warning("adjustment produced an invalid plan: %s", exc)
                result.budget_exhausted = result.budget_exhausted or ""
                result.lessons.append(_clip(f"adjustment rejected: {exc}",
                                            300))
                break

            # ── backoff within budget, then RETRY ───────────────────────
            delay = backoff_delay(self.policy, attempt_no)
            if delay > 0 and \
                    (self.policy.clock() - budget.started_at + delay) < \
                    self.policy.max_wall_seconds:
                self._sleep(delay)

        result.wall_seconds = self.policy.clock() - budget.started_at
        return result

    def _checkpoint(self, plan: Plan, obs: Observation) -> None:
        """Persist the attempt trail into the plan's checkpoint (best-effort,
        masked — the plan dict is already secret-free by construction)."""
        try:
            data = self.store.load(plan.plan_id) or \
                plan.to_dict(include_runtime=True)
            attempts = list(data.get("reflexion_attempts") or [])
            attempts.append({**obs.to_dict(), "at": _now()})
            data["reflexion_attempts"] = attempts[-MAX_ATTEMPTS_KEPT:]
            # reflexion_lessons are preserved from earlier passes untouched;
            # fresh lessons are added by the loop via the LessonStore.
            data.setdefault("reflexion_lessons", [])
            self.store.save(data)
        except Exception as exc:
            log.warning("reflexion checkpoint failed: %s", exc)
