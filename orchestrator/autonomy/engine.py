"""
orchestrator/autonomy/engine.py — PHASE 20: the autonomy engine.

THE FIVE LEVELS (strict, monotonic, config-granted — never code-granted)

    1 OBSERVE           read-only probes run; results recorded; nothing said
    2 RECOMMEND         observations may reach the user (Hermes) as advice;
                        nothing is executed
    3 DRAFT             local, reversible work (draft files, RAG notes,
                        research) may run; nothing leaves the machine
    4 EXECUTE_APPROVED  real actions run only after a human "yes" parked in
                        the approval gate; the exact payload is checkpointed
    5 TRUSTED           runs automatically — and only for steps whose tool
                        is not dangerous. Dangerous tools are CAPPED at
                        level 4 by policy, so a workflow claiming 5 while
                        using one is downgraded with an audit line.

INVARIANTS (see docs/AUTONOMY_ENGINE.md)

    * effective_level = min(workflow, agent_cap, tool_cap, policy_cap) —
      nothing can raise it, config can only lower it.
    * The GLOBAL emergency stop outranks everything: tripped → nothing runs.
    * Every decision (grant / downgrade / deny / stop) is audited first.
    * Unresolved approvals expire; approval is never auto-granted anywhere.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path

from orchestrator.autonomy.audit import AutonomyAudit
from orchestrator.contracts import new_task_id

try:                                   # reuse the Phase-2 human approval gate
    from orchestrator.approvals import ApprovalGate
except Exception:                      # pragma: no cover
    ApprovalGate = None                # type: ignore[assignment,misc]

try:                                   # crash-safe plan checkpoints
    from orchestrator.checkpoint import PlanCheckpointStore
except Exception:                      # pragma: no cover
    PlanCheckpointStore = None         # type: ignore[assignment,misc]


class AutonomyLevel(IntEnum):
    OBSERVE = 1
    RECOMMEND = 2
    DRAFT = 3
    EXECUTE_APPROVED = 4
    TRUSTED = 5


LEVEL_NAMES = {1: "observe", 2: "recommend", 3: "draft", 4: "execute_approved",
               5: "trusted"}


def risk_rank(tool: "ToolSpec | str | dict") -> int:
    """1 (safe) .. 4 (dangerous). A string names a built-in class."""
    if isinstance(tool, str):
        return _TOOL_RISK.get(tool, 3)
    if isinstance(tool, dict):
        return int(tool.get("risk", 3))
    return int(getattr(tool, "risk", 3))


# ── tool policy ──────────────────────────────────────────────────────────────

@dataclass
class ToolSpec:
    """One tool the engine may run, with its permission contract."""
    name: str
    risk: int = 2                       # 1 safe .. 4 dangerous
    max_level: int = 5                  # autonomy ceiling for this tool
    needs_approval: bool = False        # always park an approval before running
    allowed_agents: tuple = ()          # empty = any agent may call it
    handler: object = None              # callable(payload: dict) -> dict


_TOOL_RISK = {
    # read-only / informational
    "rag_search": 1, "market_snapshot": 1, "health_probe": 1,
    "email_check": 1, "calendar_read": 1, "backup_check": 1,
    "news_briefing": 1,
    # local, reversible
    "draft_file": 2, "rag_ingest": 2, "research_collect": 2,
    # outward-facing or destructive
    "send_email": 4, "send_telegram": 3, "place_trade": 4,
    "delete_file": 4, "shell_command": 4, "calendar_write": 3,
}

# A dangerous tool can never run without a human "yes", whatever the config
# says. This is the phase's hard floor — code outranks config here.
DANGEROUS_CAP = AutonomyLevel.EXECUTE_APPROVED


def make_agent_tool(registry, name: str = "run_agent", risk: int = 3) -> ToolSpec:
    """Agent selection as a tool: dispatch one step to a registered
    orchestrator agent.

    Selection rule: the registry's candidates for the requested TaskType,
    in registration order, first agent whose can_handle() accepts the
    request serves it (the same routing contract the orchestrator uses).
    The tool rides the standard permission gate — by default risk 3, so it
    parks an approval at level 4 and only runs trusted at level 5; register
    with a lower risk for read-only agents (research, briefing).
    """
    def handler(payload: dict) -> dict:
        from orchestrator.contracts import AgentRequest, TaskType, TaskStatus
        text = str(payload.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "agent step needs text"}
        try:
            tt = TaskType(str(payload.get("task_type") or "fallback"))
        except ValueError:
            return {"ok": False,
                    "error": f"unknown task_type '{payload.get("task_type")}'"}
        req = AgentRequest(task_id=new_task_id("agent"), task_type=tt,
                           text=text)
        candidates = [a for a in registry.by_task_type(tt)
                      if a.can_handle(req)]
        if not candidates:
            return {"ok": False, "error": f"no agent can handle {tt.value}"}
        agent = candidates[0]                    # agent selection happens here
        try:
            result = agent.handle(req)
        except Exception as exc:
            return {"ok": False, "error": f"agent '{agent.name}' crashed: "
                                          f"{exc}"[:200]}
        return {"ok": result.status is TaskStatus.COMPLETED,
                "agent": agent.name, "status": result.status.value,
                "text": result.text[:2000], "data": dict(result.data or {}),
                "error": result.error}
    return ToolSpec(name=name, risk=risk, max_level=5, handler=handler)


@dataclass
class Step:
    """One unit of a plan: a tool call with its declared purpose."""
    tool: str
    payload: dict = field(default_factory=dict)
    reason: str = ""
    agent: str = ""                     # owning agent name ("" = engine)


@dataclass
class Workflow:
    """A registered autonomous workflow and its autonomy contract."""
    name: str
    level: int = 1                      # requested autonomy (1..5)
    agents: tuple = ()                  # allowed agents; empty = any
    tools: tuple = ()                   # tool names this workflow may use
    max_steps: int = 12                 # resource limit
    timeout_s: float = 600.0            # resource limit
    enabled: bool = True                # per-agent/workflow enable flag
    notify: dict = field(default_factory=dict)   # e.g. {"channel": "telegram"}
    description: str = ""

    def __post_init__(self) -> None:
        # None never means "nothing": tools/agents empty tuple = unconstrained
        self.tools = tuple(self.tools or ())
        self.agents = tuple(self.agents or ())
        self.notify = dict(self.notify or {})
        self.level = max(1, min(int(self.level or 1), 5))


@dataclass
class Decision:
    """The gate verdict for one step.

    mode is what the runner does with it:
      execute  run the handler (with verification)
      observe  read-only probe: handler runs, output is recorded only
      skip     level too low for this risk class — recorded, run continues
      park     park for a human approval first (level 4+ only)
    """
    allowed: bool
    level: int                          # the EFFECTIVE level after all caps
    reason: str
    mode: str = "execute"
    needs_approval: bool = False
    approval_id: str = ""


# ── notifications ────────────────────────────────────────────────────────────

class Notifier:
    """Notifications through Hermes (and anything else registered).

    deliver() is injected; the default shells out to the Hermes CLI exactly
    like actions/hermes_task.py does. A failed notification is reported
    honestly — it never claims success.
    """

    def __init__(self, sender=None, audit: AutonomyAudit | None = None) -> None:
        self.sender = sender or self._hermes_cli
        self.audit = audit

    def notify(self, message: str, channel: str = "telegram",
               urgency: str = "normal") -> dict:
        ok, detail = False, ""
        try:
            out = self.sender(message=message, channel=channel,
                              urgency=urgency)
            ok = bool(out)
            detail = str(out)[:200]
        except Exception as exc:
            detail = f"{exc}"[:200]
        if self.audit:
            self.audit.record("notify", detail={"channel": channel,
                                                "urgency": urgency,
                                                "ok": ok, "via": detail})
        return {"ok": ok, "channel": channel, "detail": detail}

    @staticmethod
    def _hermes_cli(message: str, channel: str = "telegram",
                    urgency: str = "normal") -> bool:
        """The real path: the Hermes CLI's send verb (same env discipline as
        actions/hermes_task.py). Returns False when delivery fails."""
        import os
        import subprocess
        home = os.path.expanduser("~")
        hermes = os.path.join(home, ".hermes", "hermes")
        if not Path(hermes).exists():
            return False
        env = {"PATH": os.pathsep.join([str(Path(home) / ".hermes" / "node" / "bin"),
                                        "/usr/local/bin", "/usr/bin", "/bin"]),
               "HOME": str(home)}
        try:
            proc = subprocess.run([hermes, "send", "-t", channel, message[:3500]],
                                  capture_output=True, text=True, timeout=30,
                                  env=env)
            return proc.returncode == 0 and "sent" in (proc.stdout or "").lower()
        except Exception:
            return False


# ── the engine ───────────────────────────────────────────────────────────────

class AutonomyEngine:
    """Autonomy levels + tool permissions + emergency stop + execution.

    Construction is dependency-injected so tests can run fully offline:
    notifier, approval gate, checkpoint store, clock, and tool handlers all
    come in through the constructor.
    """

    def __init__(self, *, audit: AutonomyAudit | None = None,
                 notifier: Notifier | None = None,
                 approvals: "ApprovalGate | None" = None,
                 checkpoints: "PlanCheckpointStore | None" = None,
                 tools: dict[str, ToolSpec] | None = None,
                 clock=time.time,
                 state_file: str | Path | None = None,
                 dangerous_cap: int = int(AutonomyLevel.EXECUTE_APPROVED)) -> None:
        self.audit = audit or AutonomyAudit()
        self.notifier = notifier or Notifier(audit=self.audit)
        # NB: `approvals or ...` would DISCARD an injected empty gate —
        # ApprovalGate defines __len__, so an empty gate is falsy. Identity
        # checks only.
        self.approvals = approvals if approvals is not None \
            else (ApprovalGate() if ApprovalGate else None)
        self.checkpoints = checkpoints
        self.clock = clock
        self.tools: dict[str, ToolSpec] = dict(tools or {})
        self.dangerous_cap = int(dangerous_cap)
        self._stop = False
        self._stop_reason = ""
        self._agent_enabled: dict[str, bool] = {}
        self._agent_levels: dict[str, int] = {}
        self._state_file = Path(state_file) if state_file else None
        self._lock = threading.RLock()
        if self._state_file:
            self._load_state()

    # ── state persistence (stop flag + agent toggles survive restarts) ──
    def _load_state(self) -> None:
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            self._stop = bool(data.get("emergency_stop"))
            self._stop_reason = str(data.get("stop_reason") or "")
            self._agent_enabled = {str(k): bool(v) for k, v in
                                   (data.get("agents_enabled") or {}).items()}
            self._agent_levels = {str(k): int(v) for k, v in
                                  (data.get("agents_levels") or {}).items()}
        except Exception:
            pass

    def _save_state(self) -> None:
        if not self._state_file:
            return
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "emergency_stop": self._stop,
                "stop_reason": self._stop_reason,
                "agents_enabled": self._agent_enabled,
                "agents_levels": self._agent_levels,
                "updated_at": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"),
            }, indent=1), encoding="utf-8")
            tmp.replace(self._state_file)
        except Exception:
            pass

    # ── emergency stop ───────────────────────────────────────────────────
    def trip_stop(self, reason: str = "", by: str = "user") -> dict:
        """THE button. Tripped = nothing runs, and the flag persists."""
        self._stop = True
        self._stop_reason = str(reason or "manual emergency stop")
        self._save_state()
        self.audit.record("emergency_stop", actor=by,
                          detail={"reason": self._stop_reason})
        return {"ok": True, "stopped": True, "reason": self._stop_reason}

    def release_stop(self, by: str = "user") -> dict:
        self._stop = False
        self._stop_reason = ""
        self._save_state()
        self.audit.record("emergency_stop_released", actor=by)
        return {"ok": True, "stopped": False}

    @property
    def stopped(self) -> bool:
        return self._stop

    def _stopped_guard(self, workflow: str) -> Decision | None:
        if not self._stop:
            return None
        self.audit.record("blocked_stop", workflow=workflow,
                          detail={"reason": self._stop_reason})
        return Decision(False, 0, f"emergency stop active: {self._stop_reason}")

    # ── per-agent enable / level ─────────────────────────────────────────
    def set_agent_enabled(self, agent: str, enabled: bool) -> dict:
        self._agent_enabled[str(agent)] = bool(enabled)
        self._save_state()
        self.audit.record("agent_toggle", actor="user",
                          detail={"agent": agent, "enabled": bool(enabled)})
        return {"ok": True, "agent": agent, "enabled": bool(enabled)}

    def set_agent_level(self, agent: str, level: int) -> dict:
        """An agent's own autonomy ceiling (per-agent autonomy cap)."""
        lvl = max(1, min(int(level), 5))
        self._agent_levels[str(agent)] = lvl
        self._save_state()
        self.audit.record("agent_level", actor="user",
                          detail={"agent": agent, "level": lvl})
        return {"ok": True, "agent": agent, "level": lvl}

    def register_tool(self, spec: ToolSpec) -> dict:
        self.tools[spec.name] = spec
        return {"ok": True, "tool": spec.name, "risk": spec.risk}

    # ── the gate ─────────────────────────────────────────────────────────
    def evaluate(self, step: Step, wf: Workflow) -> Decision:
        """The single security decision point. Effective level =
        min(workflow, agent cap, tool cap, dangerous policy cap); stop flag
        and per-tool approval requirements fold in here. Audits first."""
        if (d := self._stopped_guard(wf.name)) is not None:
            return d
        if not wf.enabled:
            self.audit.record("denied", workflow=wf.name,
                              detail={"why": "workflow disabled", "tool": step.tool})
            return Decision(False, 0, "workflow disabled")
        agent = step.agent or wf.name
        if self._agent_enabled.get(agent, True) is False:
            self.audit.record("denied", workflow=wf.name,
                              detail={"why": "agent disabled", "agent": agent})
            return Decision(False, 0, f"agent '{agent}' is disabled")
        spec = self.tools.get(step.tool)
        if spec is None:
            self.audit.record("denied", workflow=wf.name,
                              detail={"why": "unknown tool", "tool": step.tool})
            return Decision(False, 0, f"unknown tool '{step.tool}'")
        if wf.tools and step.tool not in wf.tools:
            self.audit.record("denied", workflow=wf.name,
                              detail={"why": "tool not in workflow contract",
                                      "tool": step.tool})
            return Decision(False, 0,
                            f"tool '{step.tool}' is not in workflow "
                            f"'{wf.name}'s contract")
        if spec.allowed_agents and agent not in spec.allowed_agents:
            self.audit.record("denied", workflow=wf.name,
                              detail={"why": "agent not allowed on tool",
                                      "agent": agent, "tool": step.tool})
            return Decision(False, 0,
                            f"agent '{agent}' may not use tool '{step.tool}'")
        caps = [int(wf.level),
                int(self._agent_levels.get(agent, 5)),
                int(spec.max_level)]
        if spec.risk >= 4:
            caps.append(self.dangerous_cap)      # hard policy floor
        eff = min(caps)
        if eff < int(wf.level):
            self.audit.record("downgrade", workflow=wf.name,
                              detail={"requested": int(wf.level),
                                      "effective": eff, "tool": step.tool,
                                      "risk": spec.risk,
                                      "why": "dangerous tool" if spec.risk >= 4
                                      else "cap"})

        # ── the level × risk matrix (the whole phase in one table) ──────
        #   eff\risk   1 read      2 local      3 outward     4 dangerous
        #   1 observe  observe     skip         DENY          DENY
        #   2 observe  observe     skip         DENY          DENY
        #   3 execute  execute     execute      DENY          DENY
        #   4 execute  execute     execute      PARK approval PARK approval
        #   5 execute  execute     execute      execute       PARK (loud)
        if spec.risk >= 4:
            if eff < int(AutonomyLevel.EXECUTE_APPROVED):
                self.audit.record("denied", workflow=wf.name,
                                  detail={"why": "dangerous tool below "
                                          "execute level", "tool": step.tool,
                                          "effective": eff})
                return Decision(False, eff,
                                f"tool '{step.tool}' is dangerous and the "
                                "effective autonomy is below execute-"
                                "approved; it cannot run like this",
                                mode="deny")
            decision = Decision(True, eff, LEVEL_NAMES.get(eff, "?"),
                                mode="park", needs_approval=True)
        elif spec.risk == 3:
            if eff >= int(AutonomyLevel.TRUSTED):
                decision = Decision(True, eff, "trusted", mode="execute")
            elif eff == int(AutonomyLevel.EXECUTE_APPROVED):
                decision = Decision(True, eff, "execute_approved", mode="park",
                                    needs_approval=True)
            else:
                self.audit.record("denied", workflow=wf.name,
                                  detail={"why": "outward tool needs trusted "
                                          "level or approval", "tool": step.tool,
                                          "effective": eff})
                decision = Decision(False, eff,
                                    f"tool '{step.tool}' is outward-facing "
                                    "and needs trusted autonomy or an "
                                    "approval", mode="deny")
        # risk <= 2: local work (reached only when no risky verdict above)
        elif spec.needs_approval:
            if eff >= int(AutonomyLevel.EXECUTE_APPROVED):
                decision = Decision(True, eff, LEVEL_NAMES.get(eff, "?"),
                                    mode="park", needs_approval=True)
            else:
                decision = Decision(False, eff,
                                    f"tool '{step.tool}' requires approval "
                                    "but the effective autonomy is lower",
                                    mode="deny")
        elif eff <= 2:
            mode = "observe" if spec.risk == 1 else "skip"
            decision = Decision(True, eff, LEVEL_NAMES.get(eff, "?"), mode=mode)
        else:
            decision = Decision(True, eff, LEVEL_NAMES.get(eff, "?"),
                                mode="execute")
        # every verdict that did not audit itself already gets its line here
        self.audit.record("granted" if decision.mode in ("execute", "park")
                          else "observe_only" if decision.mode == "observe"
                          else "skipped",
                          workflow=wf.name,
                          detail={"tool": step.tool, "effective": eff,
                                  "mode": decision.mode,
                                  "needs_approval": decision.needs_approval})
        return decision

    # ── execution: plan-and-execute with verification ────────────────────
    def run(self, wf: Workflow, steps: list[Step], *,
            verify=None, context: dict | None = None,
            approved_step: tuple[int, str] | None = None) -> dict:
        """Run a workflow's plan through the gate, step by step.

        Per step: gate → (maybe park approval) → execute → record. Failures
        record recovery state and STOP the run (a failed step invalidates
        what follows). `verify(result)` may demand a retry; a step that
        fails its final attempt parks recovery instead of being retried
        forever.

        approved_step=(i, approval_id) resumes a checkpointed run: step i
        was parked for approval and the human said yes — it executes now
        without re-parking (audited as approval_used), the rest re-gates.
        """
        run_id = new_task_id("auto")
        context = dict(context or {})
        if (d := self._stopped_guard(wf.name)) is not None:
            return {"ok": False, "run_id": run_id, "status": "stopped",
                    "error": d.reason, "results": []}
        # resource limit: a plan may not exceed the workflow's step budget —
        # refused loudly, never silently truncated
        if len(steps) > max(1, int(wf.max_steps)):
            self.audit.record("denied", workflow=wf.name,
                              detail={"why": "step budget exceeded",
                                      "steps": len(steps),
                                      "cap": int(wf.max_steps)})
            return {"ok": False, "run_id": run_id, "status": "denied",
                    "error": f"plan has {len(steps)} steps but workflow "
                            f"'{wf.name}' allows {int(wf.max_steps)} "
                            "(step budget)",
                    "results": []}
        started = self.clock()
        self.audit.record("run_start", run_id=run_id, workflow=wf.name,
                          detail={"steps": len(steps), "level": int(wf.level)})
        self._checkpoint(run_id, wf, steps, "running")
        results: list[dict] = []
        for i, step in enumerate(steps):
            if self._stop:                       # stop checked per step too
                self.audit.record("blocked_stop", run_id=run_id,
                                  workflow=wf.name)
                out = {"ok": False, "status": "stopped",
                       "error": "emergency stop active", "results": results}
                self._checkpoint(run_id, wf, steps, "stopped",
                                 completed=[r.get("step") for r in results])
                return out
            decision = self.evaluate(step, wf)
            if approved_step is not None and approved_step[0] == i:
                # the human approved THIS step's checkpointed payload
                self.audit.record("approval_used", run_id=run_id,
                                  workflow=wf.name,
                                  detail={"step": i, "tool": step.tool,
                                          "approval_id": approved_step[1]})
                decision = Decision(True, max(2, int(wf.level)),
                                    LEVEL_NAMES.get(int(wf.level), "?"),
                                    mode="execute")
            if not decision.allowed:
                results.append({"step": i, "tool": step.tool, "ran": False,
                                "denied": decision.reason})
                self._checkpoint(run_id, wf, steps, "denied",
                                 completed=[r.get("step") for r in results])
                self.audit.record("run_denied", run_id=run_id,
                                  workflow=wf.name,
                                  detail={"step": i, "tool": step.tool,
                                          "reason": decision.reason})
                self._notify_result(wf, run_id, "denied", decision.reason)
                return {"ok": False, "run_id": run_id, "status": "denied",
                        "error": decision.reason, "results": results}
            if decision.mode == "park":
                token = self._park_approval(run_id, wf, step, decision)
                results.append({"step": i, "tool": step.tool, "ran": False,
                                "needs_approval": True, "approval_id": token,
                                "reason": decision.reason})
                self._checkpoint(run_id, wf, steps, "awaiting_approval",
                                 completed=[r.get("step") for r in results],
                                 approval_id=token, pending_step=i)
                self.audit.record("approval_parked", run_id=run_id,
                                  workflow=wf.name,
                                  detail={"step": i, "tool": step.tool,
                                          "approval_id": token})
                self._notify_result(wf, run_id, "awaiting_approval",
                                    f"step {i} ({step.tool}) needs approval")
                return {"ok": True, "run_id": run_id, "status":
                        "awaiting_approval", "approval_id": token,
                        "results": results}
            if decision.mode == "observe":
                results.append({"step": i, "tool": step.tool, "ran": True,
                                "observed": True, "output":
                                self._observe(step, context)})
                continue
            if self.clock() - started > float(wf.timeout_s):
                # resource limit: the run's wall-clock budget is spent
                self._checkpoint(run_id, wf, steps, "failed",
                                 completed=[r.get("step") for r in results],
                                 failed_step=i, error="timeout budget spent")
                self.audit.record("run_failed", run_id=run_id,
                                  workflow=wf.name,
                                  detail={"why": "timeout budget spent",
                                          "step": i})
                return {"ok": False, "run_id": run_id, "status": "failed",
                        "error": f"run exceeded its {wf.timeout_s}s budget",
                        "results": results}
            if decision.mode == "skip":
                results.append({"step": i, "tool": step.tool, "ran": False,
                                "skipped": f"level {decision.level} does not "
                                           "run this yet"})
                continue
            out = self._execute_with_verify(step, wf, run_id, verify, context)
            out["step"], out["tool"] = i, step.tool
            results.append(out)
            if not out.get("ok"):
                self._checkpoint(run_id, wf, steps, "failed",
                                 completed=[r.get("step") for r in results],
                                 failed_step=i, error=out.get("error"))
                self.audit.record("run_failed", run_id=run_id,
                                  workflow=wf.name,
                                  detail={"step": i, "tool": step.tool,
                                          "error": str(out.get("error"))[:200]})
                rec = self._recover(run_id, wf, i, step, out)
                self._notify_result(wf, run_id, "failed",
                                    f"step {i} ({step.tool}) failed")
                return {"ok": False, "run_id": run_id, "status": "failed",
                        "recovery": rec, "results": results}
            if self.clock() - started > float(wf.timeout_s):
                # the budget is spent NOW — the next step must not start
                self._checkpoint(run_id, wf, steps, "failed",
                                 completed=[r.get("step") for r in results],
                                 failed_step=i + 1, error="timeout budget spent")
                self.audit.record("run_failed", run_id=run_id,
                                  workflow=wf.name,
                                  detail={"why": "timeout budget spent",
                                          "after_step": i})
                return {"ok": False, "run_id": run_id, "status": "failed",
                        "error": f"run exceeded its {wf.timeout_s}s budget",
                        "results": results}
        self._checkpoint(run_id, wf, steps, "completed",
                         completed=[r.get("step") for r in results])
        summary = self._summarize_results(results)
        self.audit.record("run_complete", run_id=run_id, workflow=wf.name,
                          detail={"steps_ok":
                                  sum(1 for r in results if r.get("ran")),
                                  "summary": summary[:200]})
        self._notify_result(wf, run_id, "completed", summary)
        return {"ok": True, "run_id": run_id, "status": "completed",
                "summary": summary, "results": results}

    @staticmethod
    def _summarize_results(results: list[dict]) -> str:
        parts: list[str] = []
        for r in results[:6]:
            if r.get("observed"):
                probe = (r.get("output") or {}).get("result") or {}
                parts.append(f"{r.get('tool')}: "
                             f"{probe.get('note') or probe.get('value') or 'observed'}")
            elif r.get("needs_approval"):
                parts.append(f"{r.get('tool')}: awaiting approval")
            elif r.get("skipped"):
                parts.append(f"{r.get('tool')}: skipped")
            elif r.get("ok"):
                parts.append(f"{r.get('tool')}: done")
            elif r.get("denied"):
                parts.append(f"{r.get('tool')}: denied")
            else:
                parts.append(f"{r.get('tool')}: failed")
        return " | ".join(parts)

    # ── internals ────────────────────────────────────────────────────────
    def _observe(self, step: Step, context: dict) -> dict:
        """Level-1 step: the probe may run (read-only) but its output is
        recorded, never delivered or acted on."""
        spec = self.tools.get(step.tool)
        try:
            out = spec.handler(step.payload) if spec and spec.handler else {}
            return {"probe": step.tool, "result": out if isinstance(out, dict)
                    else {"value": out}}
        except Exception as exc:
            return {"probe": step.tool, "error": str(exc)[:200]}

    def _execute_with_verify(self, step: Step, wf: Workflow, run_id: str,
                             verify, context: dict, attempts: int = 2) -> dict:
        spec = self.tools.get(step.tool)
        if spec is None or spec.handler is None:
            return {"ok": False, "error": f"tool '{step.tool}' has no handler"}
        last = {"ok": False, "error": "not run"}
        for attempt in range(1, max(1, attempts) + 1):
            try:
                out = spec.handler(step.payload)
                last = out if isinstance(out, dict) else {"ok": True,
                                                          "result": out}
            except Exception as exc:
                last = {"ok": False, "error": str(exc)[:300]}
            last["attempt"] = attempt
            if last.get("ok") and verify is not None:
                try:
                    if not verify(last, context):
                        last = {"ok": False, "attempt": attempt,
                                "error": "verification failed",
                                "raw": last}
                except Exception as exc:
                    last = {"ok": False, "attempt": attempt,
                            "error": f"verifier crashed: {exc}"[:300]}
            if last.get("ok"):
                self.audit.record("step_ok", run_id=run_id,
                                  workflow=wf.name,
                                  detail={"tool": step.tool,
                                          "attempt": attempt})
                return last
            self.audit.record("step_retry", run_id=run_id, workflow=wf.name,
                              detail={"tool": step.tool, "attempt": attempt,
                                      "error": str(last.get("error"))[:200]})
        return last

    def resolve_approval(self, approval_id: str, approved: bool,
                         by: str = "user") -> dict:
        """Human verdict on a parked step, then RESUME the checkpointed run
        when approved — the approved step executes its exact parked payload
        (audited approval_used), the remaining steps re-gate normally.
        A denial marks the checkpoint denied; nothing executes."""
        self.audit.record("approval_resolved", actor=by,
                          detail={"approval_id": approval_id,
                                  "approved": bool(approved)})
        # the gate decides liveness: an EXPIRED approval is refused outright —
        # a stale yes can never fire a forgotten task
        pending = None if self.approvals is None \
            else self.approvals.get(approval_id)
        if pending is None:
            self.audit.record("approval_expired", actor=by,
                              detail={"approval_id": approval_id,
                                      "why": "refusing to resume an expired "
                                             "or unknown approval"})
            return {"ok": False, "approval_id": approval_id,
                    "error": "approval expired or unknown — refusing to "
                             "resume; re-run the workflow for a fresh gate"}
        if self.approvals is not None:
            self.approvals.resolve(approval_id, approved=approved, by=by)
        if not approved:
            st = self._find_run(approval_id)
            if st is not None:
                pid, data = st
                data["status"] = "denied"
                self.checkpoints.save(data)
                self.audit.record("run_denied", run_id=pid,
                                  workflow=data.get("workflow", ""),
                                  detail={"why": "approval denied"})
            return {"ok": True, "approval_id": approval_id, "approved": False,
                    "status": "denied"}
        found = self._find_run(approval_id)
        if found is None:
            return {"ok": False, "approval_id": approval_id,
                    "error": "no parked run holds this approval"}
        pid, data = found
        wf = Workflow(name=str(data.get("workflow") or "approval"),
                      level=int(data.get("level") or 4),
                      tools=tuple(data.get("wf_tools") or ()),
                      agents=tuple(data.get("wf_agents") or ()),
                      max_steps=int(data.get("max_steps") or 12))
        steps = [Step(tool=str(s.get("tool") or ""),
                      payload=dict(s.get("payload") or {}),
                      reason=str(s.get("reason") or ""),
                      agent=str(s.get("agent") or ""))
                 for s in (data.get("steps") or [])]
        pending = int(data.get("pending_step") or 0)
        out = self.run(wf, steps, approved_step=(pending, approval_id))
        out["resumed_from"] = pid
        return out

    def _find_run(self, approval_id: str) -> "tuple[str, dict] | None":
        """The checkpoint whose parked approval is this token."""
        if self.checkpoints is None:
            return None
        try:
            for pid in self.checkpoints.list_ids():
                data = self.checkpoints.load(pid)
                if isinstance(data, dict) and \
                        data.get("approval_id") == approval_id:
                    return pid, data
        except Exception:
            pass
        return None

    def _park_approval(self, run_id: str, wf: Workflow, step: Step,
                       decision: Decision) -> str:
        token = f"appr-{run_id}-{step.tool}"
        if self.approvals is not None:
            try:
                self.approvals.request(token, wf.name,
                                       f"{step.tool}: {step.reason or step.payload}")
            except Exception:
                pass
        # the exact payload is checkpointed so an approval re-runs THE SAME
        # step, never a drifted one
        self.audit.record("approval_payload", run_id=run_id, workflow=wf.name,
                          detail={"tool": step.tool, "payload": step.payload})
        return token

    def _recover(self, run_id: str, wf: Workflow, step_i: int, step: Step,
                 out: dict) -> dict:
        """Failure recovery: park the failure with enough state to resume —
        the scheduler decides whether to retry the run or alert the human."""
        rec = {"run_id": run_id, "failed_step": step_i, "tool": step.tool,
               "error": str(out.get("error"))[:300], "resume": "retry_run"}
        self.audit.record("recovery_parked", run_id=run_id, workflow=wf.name,
                          detail=rec)
        return rec

    def _notify_result(self, wf: Workflow, run_id: str, status: str,
                       detail: str) -> None:
        """Notifications through Hermes when the workflow asks for them —
        never at observe level (silence is the whole point of level 1)."""
        if not wf.notify or int(wf.level) <= int(AutonomyLevel.OBSERVE):
            return
        try:
            self.notifier.notify(
                f"[{status}] {wf.name} — {detail}"[:500],
                channel=str(wf.notify.get("channel") or "telegram"))
        except Exception:
            pass

    def _checkpoint(self, run_id: str, wf: Workflow, steps: list[Step],
                    status: str, **extra) -> None:
        if self.checkpoints is None:
            return
        try:
            self.checkpoints.save({
                "plan_id": run_id,
                "status": status,
                "workflow": wf.name,
                "level": int(wf.level),
                "wf_tools": list(wf.tools),
                "wf_agents": list(wf.agents),
                "max_steps": int(wf.max_steps),
                "steps": [{"tool": s.tool, "payload": s.payload,
                           "reason": s.reason, "agent": s.agent}
                          for s in steps],
                **extra,
            })
        except Exception:
            pass
