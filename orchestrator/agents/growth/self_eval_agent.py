"""
orchestrator/agents/growth/self_eval_agent.py — SELF-EVALUATION (Phase 18).

The business-capability workflow the phase brief names, as fixed stages:

    GOALS → ACTIVITIES → RESULTS → REFLECTION → IMPROVEMENT PLAN

    GOALS        read the user's goal list from the vault
    ACTIVITIES   what actually happened since the last review: activity
                 log entries grouped and counted (deterministic)
    RESULTS      goal-by-goal status: on_track / off_track / done /
                 stalled, decided from the evidence recorded — dates,
                 logged activity, never from opinion
    REFLECTION   the orchestrator Reflector (GENERATE → CRITIQUE →
                 IMPROVE, hard ceilings) runs over the evidence summary;
                 the deterministic critic is the offline floor so the
                 review works with no model at all
    IMPROVEMENT  a saved artifact: per-goal next actions (from the actual
                 failures shown in stage results), persisted to the vault
                 as the new review record

HONESTY (no invention)
    * results are computed from recorded evidence — a goal with no
      activities goes "no activity recorded", not "doing fine";
    * the reflection's draft is built from the SAME summary the
      deterministic critic sees: every claim in it is one of those
      lines (the lesson-philosophy of reflexion.py: a lesson you cannot
      point to is not a lesson);
    * the improvement plan derives strictly from the flagged gaps —
      gaps nothing flagged → plan says so.

SAFETY
    * everything here is READ_ONLY except saving the review record
      (a growth-vault write, no external effect → no approval;
      DELETION of goals is refused in code — safety.DELETION_REFUSAL);
    * `Reflector` never raises; the agent's handle() never raises.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from orchestrator.agents.growth import safety, shared
from orchestrator.agents.growth.shared import GrowthVault
from orchestrator.base import BaseAgent
from orchestrator.contracts import (AgentRequest, AgentResult, TaskStatus,
                                    TaskType)
from orchestrator.reflection import (Reflector, ReflectionPolicy, _clip as _rclip)

log = logging.getLogger("jarvis.self_eval")

REVIEW_MAX_LINES = 40

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SelfEvalAgent(BaseAgent):
    name = "self_eval_agent"
    description = (
        "Self-evaluation: tracks goals, measures activities and results "
        "from recorded evidence, runs the reflection loop (critique with "
        "hard ceilings, never raises) over the honest summary, and saves "
        "an improvement plan derived from the flagged gaps. Never deletes "
        "your data and never pads a stalled goal.")
    task_types = (TaskType.SELF_EVAL,)

    def __init__(self, store: GrowthVault | None = None,
                 reflector: Reflector | None = None) -> None:
        self.store = store or GrowthVault()
        self.reflector = reflector  # built lazily on first review

    # ── entry ────────────────────────────────────────────────────────────
    def handle(self, request: AgentRequest) -> AgentResult:
        try:
            meta = request.metadata or {}
            if safety.is_execution_request(request.text):
                return AgentResult(
                    task_id=request.task_id, status=TaskStatus.DECLINED,
                    text="That is an execution ask, not a reflection one. "
                         "Self-evaluation reads evidence and plans Deltas "
                         "— execution lives with the agents that own the "
                         "action, behind their own approvals.",
                    data={"refusal": True}, agent=self.name)
            action = str(meta.get("eval_action") or "").strip().lower() \
                or self._intent(request.text)
            handler = self._HANDLERS.get(action, SelfEvalAgent._review)
            return handler(self, request)
        except Exception as exc:                  # never raise outward
            log.warning("self-eval error: %s", type(exc).__name__)
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.FAILED,
                               error="self-eval failed", agent=self.name)

    @staticmethod
    def _intent(text: str) -> str:
        t = " ".join(str(text or "").lower().split())
        if re.search(r"\b(?:list|show|what are) (?:my |the )?(goals)\b", t) \
                or re.search(r"\bgoals?\b", t) and "set" not in t \
                and "add" not in t:
            return "goals"
        if re.search(r"\b(?:set|add|record|create)\b.{0,24}\bgoal\b", t):
            return "add_goal"
        if re.search(r"\b(?:log|record|note)\b.{0,24}\bactivity\b", t):
            return "log_activity"
        if re.search(r"\bactivities?\b", t):
            return "activities"
        if re.search(r"\b(?:plan|improvement)\b", t):
            return "plan"
        return "review"

    # ── helpers ──────────────────────────────────────────────────────────
    def _reply(self, request, text, *, action: str,
               status: TaskStatus = TaskStatus.COMPLETED,
               extra: dict | None = None) -> AgentResult:
        from orchestrator.agents.productivity import security as prod_sec
        body = prod_sec.redact(str(text or ""))
        return AgentResult(
            task_id=request.task_id, status=status, text=body,
            data={"self_eval": True, "action": action, **(extra or {})},
            agent=self.name)

    # ── goals ────────────────────────────────────────────────────────────
    def _goals(self, request) -> AgentResult:
        goals = self.store.goals()
        if not goals:
            return self._reply(request,
                               "No goals recorded yet — set one with "
                               "metadata {eval_action: 'add_goal', goal: "
                               "{name: 'ship v2', target: '2026-12-01', "
                               "metric: 'signups'}}.", action="goals",
                               status=TaskStatus.NEEDS_INPUT)
        lines = [f"Goals ({len(goals)}):"]
        for g in goals:
            lines.append(f"  • {g.get('name')}  (target "
                         f"{g.get('target') or '—'}, metric "
                         f"{g.get('metric') or '—'})")
        self.store.audit("goals_listed", n=len(goals))
        return self._reply(request, "\n".join(lines), action="goals",
                           extra={"goals": len(goals)})

    def _add_goal(self, request) -> AgentResult:
        meta = request.metadata or {}
        raw = meta.get("goal") if isinstance(meta.get("goal"), dict) else None
        if raw is None and meta.get("goal_json"):
            try:
                raw = json.loads(str(meta["goal_json"]))
            except Exception:
                raw = None
        if raw is None:
            return self._reply(request,
                               "metadata {eval_action: 'add_goal', goal: "
                               "{name: '…', target?: '2026-…', metric?: "
                               "'…'}} — a goal without a name records "
                               "nothing.", action="add_goal",
                               status=TaskStatus.NEEDS_INPUT)
        name = str(raw.get("name") or "").strip()[:120]
        if not name:
            return self._reply(request, "The goal needs a name.",
                               action="add_goal",
                               status=TaskStatus.NEEDS_INPUT)
        goals = self.store.goals()
        goals = [g for g in goals if g.get("name") != name]
        goals.append({"name": name,
                      "target": str(raw.get("target") or "")[:40],
                      "metric": str(raw.get("metric") or "")[:60],
                      "added_at": _now_iso()})
        self.store.save_goals(goals)
        self.store.audit("goal_added", name=name[:60])
        return self._reply(request, f"Goal recorded: '{name}'. Review when "
                                    "you want the honest version.",
                           action="add_goal", extra={"goals": len(goals)})

    def _log_activity(self, request) -> AgentResult:
        meta = request.metadata or {}
        what = str(meta.get("what") or request.text or "").strip()[:300]
        goal = str(meta.get("goal_name") or "").strip()[:120]
        if not what:
            return self._reply(request,
                               "metadata {eval_action: 'log_activity', "
                               "what: '…', goal_name?: '…'} records an "
                               "activity — results come from records.",
                               action="log_activity",
                               status=TaskStatus.NEEDS_INPUT)
        self.store.log_activity({"event": "activity", "what": what,
                                 "goal_name": goal, "at": _now_iso()})
        self.store.audit("activity_logged", goal=goal[:60])
        return self._reply(request, "Activity recorded — that's the raw "
                                    "material results are measured from.",
                           action="log_activity")

    # ── the workflow: ACTIVITIES stage ───────────────────────────────────
    @staticmethod
    def _activity_summary(rows: list[dict], goals: list[dict]) -> dict:
        """Deterministic evidence roll-up. Returns per-goal activity
        counts + the last activity time, plus unlinked activities."""
        names = [str(g.get("name") or "") for g in goals]
        per_goal = {n: 0 for n in names}
        last_at = {n: "" for n in names}
        unlinked: list[str] = []
        for row in rows:
            g = str(row.get("goal_name") or "").strip()
            what = str(row.get("what") or "").strip()[:120]
            when = str(row.get("at") or "")[:19]
            if g in per_goal:
                per_goal[g] += 1
                last_at[g] = max(last_at[g], when)
            elif what:
                unlinked.append(f"{when} {what}")
        return {"per_goal": per_goal, "last_at": last_at,
                "unlinked": unlinked[-10:],
                "n_activities": len(rows)}

    # ── the workflow: RESULTS stage ───────────────────────────────────────
    @staticmethod
    def _result_of(goal: dict, act: dict) -> tuple[str, str]:
        """(status, evidence line). The status is COMPUTED, never felt.
        Date evidence outranks movement: an overdue goal with zero
        activity is overdue, not merely quiet."""
        name = str(goal.get("name") or "")
        count = act["per_goal"].get(name, 0)
        target = str(goal.get("target") or "").strip()
        line = (f"{name}: {count} logged activit(ies)"
                + (f", last at {act['last_at'][name][:16]}"
                   if act["last_at"].get(name) else
                   ", none recorded"))
        try:
            tgt = datetime.fromisoformat(target)
            today = datetime.now(timezone.utc).replace(tzinfo=None)
            days_left = (tgt - today).days
            if days_left < 0:
                return "overdue", f"{line}; target {target} passed"
            if count == 0:
                return "no_activity", (line + " → no movement, say so "
                                       "plainly")
            if count < 3 and days_left < 30:
                return "stalled", (f"{line}; target {target}, only "
                                   f"{days_left}d left and thin movement")
            return "on_track", f"{line}; target {target} ({days_left}d left)"
        except Exception:
            if count == 0:
                return "no_activity", line + " → no movement, say so plainly"
            return "on_track", line

    # ── stages: build the reflection draft ───────────────────────────────
    @classmethod
    def _evidence_lines(cls, goals: list[dict], act: dict) -> list[str]:
        out: list[str] = []
        for g in goals:
            status, line = cls._result_of(g, act)
            out.append(f"[{status}] {line}")
        if act["unlinked"]:
            out.append(f"[unlinked] {len(act['unlinked'])} logged "
                       "activit(ies) name no goal — tie them or they "
                       "help nothing")
        if not out:
            out.append("[empty] no goals recorded — nothing to reflect on "
                       "yet")
        return out

    @staticmethod
    def _improvement_line(status: str, goal_name: str) -> str | None:
        """The deterministic mapping from a COMPLAINT to its next action.
        A goal nothing flagged produces no plan line."""
        if status in ("no_activity", "overdue", "stalled"):
            verb = ("restart with one concrete activity this week"
                    if status == "no_activity"
                    else "re-plan the remaining runway or re-scope "
                         "the goal" if status == "overdue"
                    else "increase cadence before the target date")
            return f"{goal_name}: {status} → {verb}"
        return None

    def _review(self, request) -> AgentResult:
        goals = self.store.goals()
        acts = self.store.activities(limit=10 ** 6)
        act = self._activity_summary(acts, goals)
        lines = self._evidence_lines(goals, act)
        draft = "\n".join(lines)

        # ── REFLECTION stage: the orchestrator Reflector ────────────────
        started = time.monotonic()
        if self.reflector is None:
            self.reflector = Reflector(
                policy=ReflectionPolicy(max_rounds=2))
        refl = self.reflector.reflect(
            "Critique this self-evaluation honestly: which goals are "
            "actually moving, which are stalled, and what must change?",
            draft=draft)
        wall = time.monotonic() - started

        # ── IMPROVEMENT PLAN stage (deterministic, from the gaps) ───────
        plan_rows: list[str] = []
        for line in lines:
            m = re.match(r"\[(\w+)\] ([^:]+):", line)
            if m:
                next_act = self._improvement_line(m.group(1),
                                                  m.group(2).strip())
                if next_act:
                    plan_rows.append("  • " + next_act)
        plan_block = ("Improvement plan (from the flagged gaps only):\n"
                      + "\n".join(plan_rows[:REVIEW_MAX_LINES])) \
            if plan_rows else ("Improvement plan: nothing flagged — every "
                               "goal shows movement against its target. "
                               "(A plan with no complaints is not "
                               "invented here.)")

        # ── persist the review record (the vault write) ─────────────────
        review = {"started_at": _now_iso(),
                  "goals": len(goals), "activities": len(acts),
                  "evidence": lines[:REVIEW_MAX_LINES],
                  "reflection": {"score": refl.score,
                                 "approved": refl.approved,
                                 "rounds": refl.rounds_used,
                                 "issues": (refl.critique.issues()[:6]
                                            if refl.critique else []),
                                 "source": refl.source},
                  "improvement_plan": [r.strip("• ").strip()
                                       for r in plan_rows],
                  "wall_seconds": round(wall, 2)}
        rows = self.store.reviews()
        rows.append({"review": review, "closed_at": _now_iso()})
        self.store.save_reviews(rows[-40:])
        self.store.audit("review_saved", goals=len(goals),
                         acts=len(acts), score=refl.score)

        body = ("Self-evaluation (evidence-computed, not vibes)\n"
                + "\n".join(f"  {ln}" for ln in lines)
                + f"\n\nReflection loop: score {refl.score}/10, "
                  f"{refl.rounds_used} round(s), critic={refl.source}"
                + (f" — issues: " + "; ".join(refl.critique.issues()[:3])
                   if refl.critique and refl.critique.issues() else "")
                + f"\n\n{plan_block}"
                + "\n\nSaved to the vault as a review record (no external "
                  "effect).")
        return self._reply(
            request, body, action="review",
            extra={"review": review,
                   "reflection_score": refl.score,
                   "rounds_used": refl.rounds_used,
                   "wall_seconds": round(wall, 2)})

    def _activities(self, request) -> AgentResult:
        acts = self.store.activities(limit=30)
        if not acts:
            return self._reply(request,
                               "No activities recorded — log them with "
                               "{eval_action: 'log_activity', what: '…'} "
                               "and results will come from them.",
                               action="activities")
        goals = [str(g.get("name") or "") for g in self.store.goals()]
        act = self._activity_summary(acts, goals).get("per_goal") if goals \
            else {}
        lines = [f"Recent activities ({len(self.store.activities(limit=10 ** 6))}"
                 " total, showing 30):"]
        for a in acts[-30:]:
            g = a.get("goal_name") or "—"
            lines.append(f"  {str(a.get('at'))[:16]}  [{g}] "
                         f"{str(a.get('what'))[:110]}")
        self.store.audit("activities_listed", n=len(acts))
        return self._reply(request, "\n".join(lines), action="activities")

    def _plan(self, request) -> AgentResult:
        rows = self.store.reviews()
        if not rows:
            return self._reply(request,
                               "No reviews yet — run a review first; the "
                               "improvement plan is derived from its "
                               "flagged gaps.", action="plan",
                               status=TaskStatus.NEEDS_INPUT)
        latest = rows[-1].get("review") or {}
        plan = latest.get("improvement_plan") or []
        if not plan:
            return self._reply(request,
                               "The last review has no complaints — "
                               "nothing flagged, so there is no plan to "
                               "pad. Re-review after new activity lands.",
                               action="plan")
        body = ("Improvement plan (from the last review "
                f"{str(latest.get('started_at'))[:19]}):\n"
                + "\n".join(f"  • {p}" for p in plan[:REVIEW_MAX_LINES]))
        return self._reply(request, body, action="plan",
                           extra={"plan": plan})

    _HANDLERS = {
        "goals": _goals,
        "add_goal": _add_goal,
        "set_goal": _add_goal,
        "log_activity": _log_activity,
        "activities": _activities,
        "review": _review,
        "evaluate": _review,
        "plan": _plan,
        "improvement_plan": _plan,
    }
