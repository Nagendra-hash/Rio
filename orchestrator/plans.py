"""
orchestrator/plans.py — the plan schema for the Plan-and-Execute engine.

A Plan is the inspectable artifact between "what the user asked" and "what
Jarvis does". Everything downstream (planner, executor, checkpoints, UI)
speaks exactly this format:

    Plan
    ├── goal                 verbatim user goal
    ├── steps[]              PlanStep, topologically executable
    │     ├── id             stable within the plan ("s1", "s2", ...)
    │     ├── title          one-line human summary
    │     ├── action         what to do (executor-vocabulary, see below)
    │     ├── params         action arguments (dict, JSON-safe)
    │     ├── depends_on     ids of steps that must succeed first
    │     ├── verification   how to check this step worked (optional)
    │     ├── on_fail        "abort" (default) | "skip" | "continue"
    │     ├── requires_approval  park for a human before running
    │     └── max_retries    per-step retry budget (clamped)
    ├── created_at / created_by / source ("llm" | "heuristic" | "manual")
    └── risk_notes           one line per risk, for the approval summary

Actions are the executor's vocabulary. The built-in set is deliberately tiny
and maps onto EXISTING orchestrator capabilities — the engine is core
orchestration infrastructure, not another agent:

    agent        run a registered agent (task_type + text)  → AgentResult
    llm          one Model Router / house-ladder completion → str
    shell        a whitelisted, non-destructive command     → output
    noop         a placeholder / checkpoint marker          → {}

Anything else is REJECTED at validation time (never discovered at run time),
so a plan can be fully inspected, priced, and approved before it runs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

# ── vocabulary ───────────────────────────────────────────────────────────────
ACTIONS = ("agent", "llm", "shell", "noop")
ON_FAIL = ("abort", "skip", "continue")
MAX_STEPS = 24                # a plan is a sequence, not a program
MAX_DEPENDS = 8               # per step
MAX_RETRIES = 3               # hard ceiling regardless of what a plan asks
MAX_PARAM_CHARS = 16_000      # one string param (prompt / command) cap

_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")

_PLURAL = {1: "1 step", }.get


class PlanError(ValueError):
    """A plan (or step) failed validation. Message is user-visible."""


class PlanValidationError(PlanError):
    """Raised when a plan's structure is invalid (steps, deps, actions)."""


def new_step_id(index: int) -> str:
    return f"s{index + 1}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── dataclasses ──────────────────────────────────────────────────────────────
@dataclass
class PlanStep:
    """One unit of work inside a plan. JSON-safe via to_dict/from_dict."""
    id: str
    title: str
    action: str
    params: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    verification: str = ""           # how success will be judged, for humans
    on_fail: str = "abort"           # abort | skip | continue
    requires_approval: bool = False  # park the plan before running this step
    max_retries: int = 1
    created_by: str = "planner"

    # runtime bookkeeping (executor-owned; mirrors the checkpoint)
    status: str = "pending"          # pending|running|done|failed|skipped|denied
    attempts: int = 0
    result_text: str = ""
    result_data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    # ── validation ───────────────────────────────────────────────────────
    def validate(self) -> None:
        if not _ID_RE.match(str(self.id or "")):
            raise PlanValidationError(
                f"step id {self.id!r} must match {_ID_RE.pattern}")
        if not str(self.title or "").strip():
            raise PlanValidationError(f"step {self.id}: title required")
        if self.action not in ACTIONS:
            raise PlanValidationError(
                f"step {self.id}: unknown action {self.action!r} "
                f"(allowed: {', '.join(ACTIONS)})")
        if not isinstance(self.params, dict):
            raise PlanValidationError(f"step {self.id}: params must be an object")
        if self.on_fail not in ON_FAIL:
            raise PlanValidationError(
                f"step {self.id}: on_fail {self.on_fail!r} "
                f"(allowed: {', '.join(ON_FAIL)})")
        try:
            self.max_retries = max(0, min(MAX_RETRIES, int(self.max_retries)))
        except (TypeError, ValueError):
            self.max_retries = 1
        deps = [str(d).strip() for d in (self.depends_on or []) if str(d).strip()]
        if len(deps) > MAX_DEPENDS:
            raise PlanValidationError(
                f"step {self.id}: too many dependencies ({len(deps)} > "
                f"{MAX_DEPENDS})")
        if self.id in deps:
            raise PlanValidationError(f"step {self.id}: depends on itself")
        if len(deps) != len(set(deps)):
            raise PlanValidationError(f"step {self.id}: duplicate dependencies")
        self.depends_on = deps
        for key, val in self.params.items():
            if isinstance(val, str) and len(val) > MAX_PARAM_CHARS:
                raise PlanValidationError(
                    f"step {self.id}: param '{key}' too long "
                    f"({len(val)} > {MAX_PARAM_CHARS})")

    # ── serialization ────────────────────────────────────────────────────
    def to_dict(self, *, include_runtime: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "action": self.action,
            "params": self.params,
            "depends_on": list(self.depends_on),
            "verification": self.verification,
            "on_fail": self.on_fail,
            "requires_approval": self.requires_approval,
            "max_retries": self.max_retries,
        }
        if include_runtime:
            out.update({
                "status": self.status,
                "attempts": self.attempts,
                "result_text": self.result_text[:2000],
                "result_data": self.result_data,
                "error": self.error[:1000],
                "started_at": self.started_at.isoformat() if self.started_at else None,
                "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            })
        return out

    @classmethod
    def from_dict(cls, data: Any) -> "PlanStep":
        if not isinstance(data, dict):
            raise PlanValidationError(f"step must be an object, got {type(data).__name__}")
        known = {f: data.get(f) for f in (
            "id", "title", "action", "params", "depends_on", "verification",
            "on_fail", "requires_approval", "max_retries", "created_by",
            "status", "attempts", "result_text", "result_data", "error",
            "started_at", "finished_at")}
        step = cls(
            id=str(known.get("id") or "").strip(),
            title=str(known.get("title") or "").strip(),
            action=str(known.get("action") or "").strip().lower(),
            params=dict(known.get("params") or {}) if isinstance(known.get("params"), dict) else {},
            depends_on=list(known.get("depends_on") or []) if isinstance(known.get("depends_on"), list) else [],
            verification=str(known.get("verification") or "").strip(),
            on_fail=str(known.get("on_fail") or "abort").strip().lower(),
            requires_approval=bool(known.get("requires_approval", False)),
            max_retries=_as_int(known.get("max_retries"), 1),
            created_by=str(known.get("created_by") or "planner"),
            status=str(known.get("status") or "pending"),
            attempts=_as_int(known.get("attempts"), 0),
            result_text=str(known.get("result_text") or ""),
            result_data=dict(known.get("result_data") or {}) if isinstance(known.get("result_data"), dict) else {},
            error=str(known.get("error") or ""),
        )
        for name in ("started_at", "finished_at"):
            raw = known.get(name)
            if isinstance(raw, str) and raw:
                try:
                    setattr(step, name, datetime.fromisoformat(raw))
                except ValueError:
                    pass
        step.validate()
        return step


@dataclass
class Plan:
    """An inspectable sequence of steps for one goal."""
    goal: str
    steps: list[PlanStep] = field(default_factory=list)
    plan_id: str = ""
    created_by: str = "planner"
    source: str = "heuristic"        # llm | heuristic | manual
    created_at: datetime = field(default_factory=_now)
    risk_notes: list[str] = field(default_factory=list)
    status: str = "draft"            # draft|awaiting_approval|running|completed|failed|cancelled

    # ── validation ───────────────────────────────────────────────────────
    def validate(self) -> None:
        goal = str(self.goal or "").strip()
        if not goal:
            raise PlanValidationError("plan needs a goal")
        self.goal = goal
        if not self.steps:
            raise PlanValidationError("plan needs at least one step")
        if len(self.steps) > MAX_STEPS:
            raise PlanValidationError(
                f"plan too large ({len(self.steps)} > {MAX_STEPS} steps)")
        if not self.plan_id:
            self.plan_id = new_plan_id()
        ids: set[str] = set()
        for step in self.steps:
            step.validate()
            if step.id in ids:
                raise PlanValidationError(f"duplicate step id {step.id!r}")
            ids.add(step.id)
        for step in self.steps:
            missing = [d for d in step.depends_on if d not in ids]
            if missing:
                raise PlanValidationError(
                    f"step {step.id}: unknown dependency {missing[0]!r}")
        self._detect_cycle()
        self.status = self.status or "draft"

    def _detect_cycle(self) -> None:
        by_id = {s.id: s for s in self.steps}
        state: dict[str, int] = {}          # 0=visiting, 1=done

        def visit(sid: str, path: list[str]) -> None:
            if state.get(sid) == 1:
                return
            if sid in path:
                cycle = " -> ".join(path[path.index(sid):] + [sid])
                raise PlanValidationError(f"dependency cycle: {cycle}")
            path.append(sid)
            for dep in by_id[sid].depends_on:
                visit(dep, path)
            path.pop()
            state[sid] = 1

        for s in self.steps:
            visit(s.id, [])

    # ── ordering (dependency handling) ───────────────────────────────────
    def ordered_steps(self) -> list[PlanStep]:
        """Steps in a deterministic topological order.

        The order the planner gave them is preserved where dependencies
        allow (stable Kahn's algorithm), so a reviewed plan runs in exactly
        the sequence that was shown to the human.
        """
        by_id = {s.id: s for s in self.steps}
        indegree = {s.id: 0 for s in self.steps}
        dependents: dict[str, list[str]] = {s.id: [] for s in self.steps}
        for s in self.steps:
            for dep in s.depends_on:
                indegree[s.id] += 1
                dependents[dep].append(s.id)
        # stable: planner order wins among ready steps
        ready = [s.id for s in self.steps if indegree[s.id] == 0]
        out: list[PlanStep] = []
        while ready:
            sid = ready.pop(0)
            out.append(by_id[sid])
            for nxt in dependents[sid]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    ready.append(nxt)
        return out

    def unblocked_steps(self, done_ids: set[str]) -> list[PlanStep]:
        """Steps whose dependencies are all in done_ids (execution frontier)."""
        return [s for s in self.ordered_steps()
                if s.id not in done_ids
                and all(d in done_ids for d in s.depends_on)]

    def steps_for(self, sid: str) -> PlanStep:
        for s in self.steps:
            if s.id == sid:
                return s
        raise KeyError(sid)

    def approval_steps(self) -> list[PlanStep]:
        return [s for s in self.steps if s.requires_approval]

    # ── status ───────────────────────────────────────────────────────────
    @property
    def done_ids(self) -> set[str]:
        return {s.id for s in self.steps if s.status == "done"}

    @property
    def progress(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for s in self.steps:
            counts[s.status] = counts.get(s.status, 0) + 1
        return counts

    @property
    def is_terminal(self) -> bool:
        return all(s.status in ("done", "failed", "skipped", "denied")
                   for s in self.steps)

    def summary_line(self) -> str:
        n = len(self.steps)
        appr = sum(1 for s in self.steps if s.requires_approval)
        appr_txt = f", {appr} need(s) approval" if appr else ""
        return f"{_num_steps(n)}{appr_txt}"

    # ── serialization ────────────────────────────────────────────────────
    def to_dict(self, *, include_runtime: bool = True) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "goal": self.goal,
            "status": self.status,
            "source": self.source,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat(),
            "risk_notes": list(self.risk_notes),
            "steps": [s.to_dict(include_runtime=include_runtime)
                      for s in self.steps],
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Plan":
        if not isinstance(data, dict):
            raise PlanValidationError(
                f"plan must be an object, got {type(data).__name__}")
        created_at = _now()
        raw_ts = data.get("created_at")
        if isinstance(raw_ts, str) and raw_ts:
            try:
                created_at = datetime.fromisoformat(raw_ts)
            except ValueError:
                pass
        plan = cls(
            goal=str(data.get("goal") or ""),
            steps=[PlanStep.from_dict(s) for s in (data.get("steps") or [])],
            plan_id=str(data.get("plan_id") or ""),
            created_by=str(data.get("created_by") or "planner"),
            source=str(data.get("source") or "manual"),
            created_at=created_at,
            risk_notes=[str(r) for r in (data.get("risk_notes") or [])
                        if str(r).strip()],
            status=str(data.get("status") or "draft"),
        )
        plan.validate()
        return plan

    def to_json(self, *, include_runtime: bool = True, indent: int = 1) -> str:
        import json
        return json.dumps(self.to_dict(include_runtime=include_runtime),
                          indent=indent, default=str, ensure_ascii=False)


def _num_steps(n: int) -> str:
    return f"{n} step" + ("s" if n != 1 else "")


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def new_plan_id(prefix: str = "plan") -> str:
    """Human-sortable, collision-safe plan id: plan-20261005-<hex8>."""
    import time
    import uuid
    stamp = time.strftime("%Y%m%d", time.gmtime())
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:8]}"


def make_plan(goal: str, steps: list[PlanStep], *, source: str = "manual",
              created_by: str = "planner",
              risk_notes: list[str] | None = None) -> Plan:
    """Build + validate a plan; the one constructor the planner uses."""
    plan = Plan(goal=goal, steps=list(steps), source=source,
                created_by=created_by, risk_notes=list(risk_notes or []))
    plan.validate()
    return plan
