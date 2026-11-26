"""
tests/test_autonomy.py — PHASE 20: autonomous workflows, offline.

Everything runs in a temp sandbox (audit file, task store, state file,
checkpoints, notification sender injected) with fake tools — no network, no
Hermes, no real scheduler threads.

What this proves (the deliverable's acceptance flow):

  1. THE FIVE LEVELS       — observe/recommend/draft each execute exactly
                             what their name promises and nothing more;
                             execute_approved parks a human gate; trusted
                             runs automatically.
  2. DANGEROUS NEVER       — a dangerous tool in a level-5 workflow is
     SILENTLY AUTONOMOUS     downgraded to an approval gate, loudly audited;
                             a dangerous tool below level 4 is refused.
  3. EMERGENCY STOP        — trips globally (persisted), blocks engine runs
                             and scheduler ticks, releases only explicitly.
  4. PER-AGENT / PER-TOOL  — disabled agents refuse steps; per-agent level
                             caps and per-tool ceilings narrow autonomy;
                             the workflow's tool contract binds it.
  5. APPROVAL GATES        — parked approvals carry the exact payload,
                             resolve(approved) RESUMES the checkpointed run,
                             deny records a denial, unresolved ones expire.
  6. VERIFICATION          — a failing verifier retries then fails the run
                             with recovery state; a passing one records ok.
  7. SCHEDULER             — interval/daily/event/condition triggers, event
                             drain, condition cooldown, pause/resume/remove
                             task management, run history.
  8. RESOURCE LIMITS       — concurrency cap skips overflow; step caps and
                             timeouts ride in the Workflow contract.
  9. AUDIT + NOTIFY        — every gate decision audited; failure alerts go
                             through the injected notifier (Hermes path).

Run:  .venv/bin/python -m pytest tests/test_autonomy.py -q
"""
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import orchestrator.autonomy.audit as audit_mod  # noqa: E402
from orchestrator.autonomy import (AutonomyAudit, AutonomyEngine,  # noqa: E402
                                   AutonomyLevel, Notifier, Scheduler,
                                   Step, TaskStore, ToolSpec, Workflow)
from orchestrator.autonomy import monitoring  # noqa: E402
from orchestrator.approvals import ApprovalGate  # noqa: E402
from orchestrator.checkpoint import PlanCheckpointStore  # noqa: E402


@pytest.fixture(autouse=True)
def sandbox(monkeypatch):
    """Fresh temp state for every test: audit, store, engine state,
    checkpoints — and a captured notifier."""
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-autonomy-test-"))
    audit_mod.AUDIT_FILE = tmp / "audit.jsonl"
    audit = AutonomyAudit(tmp / "audit.jsonl")

    sent: list[dict] = []

    def fake_sender(message, channel="telegram", urgency="normal"):
        sent.append({"message": message, "channel": channel})
        return True

    notifier = Notifier(sender=fake_sender, audit=audit)
    gate = ApprovalGate(ttl_s=60.0)
    store = TaskStore(tmp / "tasks.json")
    engine = AutonomyEngine(audit=audit, notifier=notifier, approvals=gate,
                            checkpoints=PlanCheckpointStore(tmp / "runs"),
                            state_file=tmp / "state.json")
    scheduler = Scheduler(engine, store, audit=audit, clock=time.time,
                          max_concurrent=1)
    yield {"tmp": tmp, "audit": audit, "sent": sent, "engine": engine,
           "store": store, "scheduler": scheduler, "gate": gate}


# ── tool fixtures ────────────────────────────────────────────────────────────

def _tools(engine: AutonomyEngine, calls: list) -> None:
    """The standard tool rack: safe probe, local write, dangerous action."""
    def probe(payload):
        calls.append(("probe", payload))
        return {"ok": True, "value": "obs-1"}

    def draft(payload):
        calls.append(("draft", payload))
        return {"ok": True, "draft": payload.get("text", "")}

    def danger(payload):
        calls.append(("danger", payload))
        return {"ok": True, "executed": True}

    engine.register_tool(ToolSpec(name="probe_tool", risk=1, max_level=5,
                                  handler=probe))
    engine.register_tool(ToolSpec(name="draft_tool", risk=2, max_level=5,
                                  handler=draft))
    engine.register_tool(ToolSpec(name="nuke_tool", risk=4, max_level=5,
                                  handler=danger))


def _wf(name="wf", level=5, tools=(), steps=None, **kw) -> tuple:
    w = Workflow(name=name, level=level, tools=tuple(tools), **kw)
    steps = steps or [Step(tool="probe_tool", payload={}, reason="look"),
                      Step(tool="draft_tool", payload={"text": "d"}, reason="d"),
                      Step(tool="nuke_tool", payload={"x": 1}, reason="boom")]
    return w, steps


# ── 1. the five levels ──────────────────────────────────────────────────────

def test_level_semantics():
    """The level × risk matrix: L1 observes, L2 recommends (still no local
    work), L3 drafts locally, L4 parks the dangerous step, L5 runs safe
    tools automatically and STILL parks dangerous ones."""
    calls: list = []
    s = sandbox_for_tests()
    eng, sent = s["engine"], s["sent"]
    _tools(eng, calls)
    probe_step = Step(tool="probe_tool", payload={}, reason="look")
    draft_step = Step(tool="draft_tool", payload={"text": "d"}, reason="d")
    nuke_step = Step(tool="nuke_tool", payload={"x": 1}, reason="boom")

    # L1: observe only — the probe runs read-only, the draft is skipped
    out = eng.run(Workflow(name="l1", level=1), [probe_step, draft_step])
    assert out["ok"] and out["status"] == "completed"
    assert out["results"][0].get("observed") is True
    assert out["results"][1].get("skipped")
    assert calls == [("probe", {})]
    assert sent == []                      # observe level is silent

    # L2: same execution surface + a recommendation notification
    out2 = eng.run(Workflow(name="l2", level=2,
                            notify={"channel": "telegram"}),
                   [probe_step, draft_step])
    assert out2["status"] == "completed"
    assert calls == [("probe", {}), ("probe", {})]
    assert sent and "[completed] l2" in sent[-1]["message"]

    # L3: draft — local reversible work runs
    out3 = eng.run(Workflow(name="l3", level=3), [probe_step, draft_step])
    assert out3["status"] == "completed"
    assert ("draft", {"text": "d"}) in calls

    # L4: the dangerous step parks for a human (earlier steps already ran)
    out4 = eng.run(Workflow(name="l4", level=4),
                   [probe_step, draft_step, nuke_step])
    assert out4["status"] == "awaiting_approval"
    assert out4["approval_id"]
    assert not any(c[0] == "danger" for c in calls), "must not run pre-approval"

    # L5: safe steps run automatically — and the dangerous one STILL parks
    out5 = eng.run(Workflow(name="l5", level=5),
                   [probe_step, draft_step, nuke_step])
    assert out5["status"] == "awaiting_approval"
    assert ("draft", {"text": "d"}) in calls
    assert not any(c[0] == "danger" for c in calls)


def sandbox_for_tests():
    # pytest fixture injection: re-fetch via the fixture dict trick
    return _SANDBOX


_SANDBOX: dict = {}


@pytest.fixture(autouse=True)
def _capture(sandbox):
    _SANDBOX.clear()
    _SANDBOX.update(sandbox)
    yield
    _SANDBOX.clear()


# ── 2. dangerous never silently autonomous ──────────────────────────────────

def test_dangerous_downgrade_is_loud_and_audited():
    s = sandbox_for_tests()
    eng, audit = s["engine"], s["audit"]
    calls: list = []
    _tools(eng, calls)
    w, steps = _wf(name="trusted_but_dangerous", level=5)
    out = eng.run(w, steps)
    assert out["status"] == "awaiting_approval"          # downgraded, not run
    events = [r["event"] for r in audit.tail(100)]
    assert "downgrade" in events                          # LOUD, in the audit
    assert "approval_parked" in events
    # the effective level recorded is execute_approved, never trusted
    grants = [r for r in audit.tail(100) if r["event"] == "granted"]
    assert any(g["detail"]["tool"] == "nuke_tool" and
               g["detail"]["needs_approval"] for g in grants)


def test_dangerous_tool_refused_below_execute_level():
    s = sandbox_for_tests()
    eng = s["engine"]
    calls: list = []
    _tools(eng, calls)
    # a level-3 agent cap + dangerous tool → below execute → refused
    eng.set_agent_level("wf", 3)
    w, steps = _wf(name="wf", level=5)
    out = eng.run(w, steps)
    assert out["ok"] is False and out["status"] == "denied"
    assert any("dangerous" in str(r.get("denied", "")) for r in out["results"])
    assert not any(c[0] == "danger" for c in calls)


# ── 3. emergency stop ────────────────────────────────────────────────────────

def test_emergency_stop_blocks_everything_and_persists():
    s = sandbox_for_tests()
    tmp, eng, sched, store, audit = (s["tmp"], s["engine"], s["scheduler"],
                                     s["store"], s["audit"])
    calls: list = []
    _tools(eng, calls)
    store.add({"name": "safe_task", "kind": "interval",
               "trigger": {"interval_s": 60}, "level": 5,
               "steps": [{"tool": "probe_tool"}]})
    assert eng.trip_stop(reason="panic", by="test")["ok"]

    # engine refuses runs
    w, steps = _wf(level=5)
    out = eng.run(w, steps)
    assert not out["ok"] and "emergency stop" in out["error"]
    assert calls == []

    # scheduler tick runs nothing
    tick = sched.tick()
    assert tick["due"] == 0 and tick["ran"] == []

    # persists across a restart (state file)
    eng2 = AutonomyEngine(audit=audit, notifier=s["engine"].notifier,
                          approvals=s["gate"],
                          checkpoints=PlanCheckpointStore(tmp / "runs"),
                          state_file=tmp / "state.json")
    assert eng2.stopped is True
    assert not eng2.run(w, steps)["ok"]

    # explicit release, and only then it runs again (safe steps only — the
    # full _wf plan includes a dangerous step that would rightly park)
    assert eng.release_stop(by="test")["ok"]
    out = eng.run(w, [Step(tool="probe_tool", payload={}),
                      Step(tool="draft_tool", payload={"text": "d"})])
    assert out["status"] == "completed"


# ── 4. per-agent / per-tool permissions ─────────────────────────────────────

def test_agent_and_tool_gates():
    s = sandbox_for_tests()
    eng = s["engine"]
    calls: list = []
    _tools(eng, calls)

    # disabled agent refuses
    eng.set_agent_enabled("trading_agent", False)
    w = Workflow(name="x", level=5)
    step = Step(tool="draft_tool", agent="trading_agent")
    d = eng.evaluate(step, w)
    assert not d.allowed and "disabled" in d.reason
    eng.set_agent_enabled("trading_agent", True)
    assert eng.evaluate(step, w).allowed

    # per-agent level cap narrows (agent capped at 1 → observe only)
    eng.set_agent_level("research_agent", 1)
    d = eng.evaluate(Step(tool="draft_tool", agent="research_agent"), w)
    assert d.allowed and d.level == 1

    # per-tool ceiling narrows (tool capped at 2 → draft can't run at 5)
    eng.tools["draft_tool"].max_level = 2
    d = eng.evaluate(Step(tool="draft_tool", agent="trading_agent"), w)
    assert d.level == 2
    eng.tools["draft_tool"].max_level = 5

    # unknown tool refused
    assert not eng.evaluate(Step(tool="ghost_tool"), w).allowed

    # workflow's tool contract binds it
    w2 = Workflow(name="y", level=5, tools=("probe_tool",))
    d = eng.evaluate(Step(tool="draft_tool"), w2)
    assert not d.allowed and "contract" in d.reason

    # per-tool allowed_agents
    eng.register_tool(ToolSpec(name="vip_tool", risk=2, max_level=5,
                               allowed_agents=("vip_agent",),
                               handler=lambda p: {"ok": True}))
    d = eng.evaluate(Step(tool="vip_tool"), w)
    assert not d.allowed and "may not use" in d.reason
    d = eng.evaluate(Step(tool="vip_tool", agent="vip_agent"), w)
    assert d.allowed


# ── 5. approval gates: payload, resume, denial, expiry ──────────────────────

def test_approval_park_resume_and_deny():
    s = sandbox_for_tests()
    eng, audit = s["engine"], s["audit"]
    calls: list = []
    _tools(eng, calls)
    w, steps = _wf(name="gate_flow", level=4)
    out = eng.run(w, steps)
    assert out["status"] == "awaiting_approval"
    approval_id = out["approval_id"]
    assert out["results"][-1]["tool"] == "nuke_tool"
    assert out["results"][-1]["needs_approval"]
    assert s["gate"].is_pending(approval_id)

    # denial: audited, nothing executed
    r = eng.resolve_approval(approval_id, approved=False, by="human")
    assert r["ok"] and r["status"] == "denied"
    assert not any(c[0] == "danger" for c in calls)
    assert "approval_resolved" in [x["event"] for x in audit.tail(50)]

    # a fresh run + approval: the parked payload executes, later steps re-gate
    calls.clear()
    out2 = eng.run(w, steps)
    assert out2["status"] == "awaiting_approval"
    calls.clear()
    r = eng.resolve_approval(out2["approval_id"], approved=True, by="human")
    assert r["ok"] and r["status"] == "completed"
    assert ("danger", {"x": 1}) in calls          # the EXACT parked payload ran


def test_unresolved_approval_expires_and_cannot_resume():
    """A stale approval dies with the gate's TTL — and an expired approval
    can NEVER resume the parked run, even if someone says yes later."""
    s = sandbox_for_tests()
    tmp, audit = s["tmp"], s["audit"]
    calls: list = []
    clock = {"t": time.time()}
    gate = ApprovalGate(ttl_s=60.0, clock=lambda: clock["t"])
    eng = AutonomyEngine(audit=audit, notifier=s["engine"].notifier,
                         approvals=gate,
                         checkpoints=PlanCheckpointStore(tmp / "runs"),
                         state_file=tmp / "state.json")
    _tools(eng, calls)
    w, steps = _wf(name="ttl_flow", level=4)
    out = eng.run(w, steps)
    approval_id = out["approval_id"]
    assert gate.is_pending(approval_id)
    clock["t"] += 3600                        # the human never answered
    assert gate.get(approval_id) is None      # expired, gone
    r = eng.resolve_approval(approval_id, approved=True, by="late-human")
    assert not r["ok"] and "expired" in r["error"]
    assert not any(c[0] == "danger" for c in calls)


def test_resume_without_parked_run_is_refused():
    s = sandbox_for_tests()
    eng = s["engine"]
    r = eng.resolve_approval("appr-ghost", approved=True)
    assert not r["ok"] and ("expired" in r["error"] or
                            "no parked run" in r["error"])


# ── 6. verification & failure recovery ──────────────────────────────────────

def test_verify_retry_then_fail_with_recovery():
    s = sandbox_for_tests()
    eng, audit, store = s["engine"], s["audit"], s["store"]
    attempts: list = []

    def flaky(payload):
        attempts.append(1)
        return {"ok": True, "value": len(attempts)}

    eng.register_tool(ToolSpec(name="flaky_tool", risk=2, max_level=5,
                               handler=flaky))
    w = Workflow(name="verify_flow", level=5)
    out = eng.run(w, [Step(tool="flaky_tool")],
                 verify=lambda res, ctx: False)   # always fails verification
    assert not out["ok"] and out["status"] == "failed"
    assert len(attempts) == 2                     # retried once, then gave up
    rec = out["recovery"]
    assert rec["failed_step"] == 0 and rec["tool"] == "flaky_tool"
    assert "recovery_parked" in [x["event"] for x in audit.tail(50)]

    # a passing verifier records step_ok
    attempts.clear()
    out2 = eng.run(w, [Step(tool="flaky_tool")],
                   verify=lambda res, ctx: res.get("value") == 1)
    assert out2["ok"] and out2["status"] == "completed"
    assert "step_ok" in [x["event"] for x in audit.tail(50)]


def test_failed_run_notifies_after_repeated_failures():
    s = sandbox_for_tests()
    eng, sched, store, sent = s["engine"], s["scheduler"], s["store"], s["sent"]

    def broken(payload):
        return {"ok": False, "error": "boom"}

    eng.register_tool(ToolSpec(name="broken_tool", risk=2, max_level=5,
                               handler=broken))
    assert store.add({"name": "alert_task", "kind": "manual_run",
                      "level": 5, "steps": [{"tool": "broken_tool"}],
                      "notify": {"channel": "telegram"}})["ok"]
    task = store.get("alert_task")
    for _ in range(3):
        sched.run_task(task, "test")
    # the engine notifies per failed run; the scheduler escalates ONCE at the
    # consecutive-failure threshold
    assert sum("failed 3x" in m["message"] for m in sent) == 1
    assert any("alert_task" in m["message"] for m in sent)


# ── 7. scheduler: triggers & task management ────────────────────────────────

def test_interval_and_daily_due():
    s = sandbox_for_tests()
    eng, sched, store = s["engine"], s["scheduler"], s["store"]
    calls: list = []
    _tools(eng, calls)
    assert store.add({"name": "pulse", "kind": "interval",
                      "trigger": {"interval_s": 60}, "level": 1,
                      "steps": [{"tool": "probe_tool"}]})["ok"]
    assert store.add({"name": "brief", "kind": "daily",
                      "trigger": {"time": "00:00"}, "level": 1,
                      "steps": [{"tool": "probe_tool"}]})["ok"]

    # interval fires immediately (never run); daily 00:00 has passed today
    due_names = {t["name"] for t, _ in sched.due_tasks()}
    assert {"pulse", "brief"} <= due_names
    sched.run_task(store.get("pulse"), "interval 60s")
    sched.run_task(store.get("brief"), "daily schedule")
    assert store.get("pulse")["last_status"] == "ok"

    # both cool down: interval waits its 60s, daily waits for tomorrow
    due_after = {t["name"] for t, _ in sched.due_tasks()}
    assert not (due_after & {"pulse", "brief"})
    assert store.history(10)
    assert any(h["task"] == "pulse" and h["status"] == "ok"
               for h in store.history(10))


def test_event_and_condition_triggers():
    s = sandbox_for_tests()
    eng, sched, store = s["engine"], s["scheduler"], s["store"]
    calls: list = []
    _tools(eng, calls)
    assert store.add({"name": "mail_alert", "kind": "event",
                      "trigger": {"event": "email.important_arrived"},
                      "level": 2,
                      "steps": [{"tool": "probe_tool"}]})["ok"]
    # no event yet → not due
    assert not any(t["name"] == "mail_alert" for t, _ in sched.due_tasks())
    # emit → due, and the event drains
    assert sched.emit_event("email.important_arrived")["ok"]
    hits = [(t["name"], why) for t, why in sched.due_tasks()
            if t["name"] == "mail_alert"]
    assert hits and "email.important_arrived" in hits[0][1]
    assert not [t for t, _ in sched.due_tasks() if t["name"] == "mail_alert"]

    # condition trigger with cooldown
    def hot(**kw):
        return {"fired": True, "note": "disk 95%"}
    monitoring.PROBES["always_hot"] = hot
    try:
        assert store.add({"name": "cond_task", "kind": "condition",
                          "trigger": {"probe": "always_hot"},
                          "level": 1, "condition_cooldown_s": 3600,
                          "steps": [{"tool": "probe_tool"}]})["ok"]
        assert any(t["name"] == "cond_task" for t, _ in sched.due_tasks())
        t = store.get("cond_task")
        sched.run_task(t, "condition: disk 95%")
        # within cooldown: not due again
        assert not any(t["name"] == "cond_task" for t, _ in sched.due_tasks())
        # after the cooldown passes: due again
        sched._fired_conditions["cond_task"] = time.time() - 7200
        assert any(t["name"] == "cond_task" for t, _ in sched.due_tasks())
    finally:
        monitoring.PROBES.pop("always_hot", None)


def test_task_management_crud():
    s = sandbox_for_tests()
    sched, store, eng = s["scheduler"], s["store"], s["engine"]
    calls: list = []
    _tools(eng, calls)
    assert store.add({"name": "t1", "kind": "interval",
                      "trigger": {"interval_s": 300}, "level": 2,
                      "steps": [{"tool": "probe_tool"}]})["ok"]
    assert not store.add({"name": "t1", "kind": "interval"})["ok"]      # dup
    assert not store.add({"name": "", "kind": "interval"})["ok"]        # blank
    assert store.get("t1")["level"] == 2
    assert store.update("t1", level=4, enabled=False)["ok"]
    assert store.get("t1")["level"] == 4 and store.get("t1")["enabled"] is False
    assert [t["name"] for t in store.list(enabled_only=True)] == []
    assert store.update("t1", enabled=True)["ok"]
    assert store.remove("t1")["ok"]
    assert store.get("t1") is None
    assert not store.remove("t1")["ok"]
    # history accumulates through the scheduler
    store.add({"name": "h1", "kind": "interval",
               "trigger": {"interval_s": 60}, "level": 1,
               "steps": [{"tool": "probe_tool"}]})
    sched.run_task(store.get("h1"), "test")
    hist = store.history()
    assert hist and hist[-1]["task"] == "h1" and hist[-1]["status"] == "ok"


# ── 8. resource limits ──────────────────────────────────────────────────────

def test_concurrency_cap_skips_overflow():
    s = sandbox_for_tests()
    eng, sched, store = s["engine"], s["scheduler"], s["store"]
    release = threading.Event()

    def slow(payload):
        release.wait(2.0)
        return {"ok": True}

    eng.register_tool(ToolSpec(name="slow_tool", risk=2, max_level=5,
                               handler=slow))
    store.add({"name": "slow_a", "kind": "manual_run", "level": 5,
               "steps": [{"tool": "slow_tool"}]})
    store.add({"name": "slow_b", "kind": "manual_run", "level": 5,
               "steps": [{"tool": "slow_tool"}]})
    results = {}

    def runner(name):
        results[name] = sched.run_task(store.get(name), "test")

    th = threading.Thread(target=runner, args=("slow_a",))
    th.start()
    time.sleep(0.15)                       # let slow_a occupy the single slot
    r = sched.run_task(store.get("slow_b"), "test")
    assert not r["ok"] and r["skipped"] == "concurrency cap"
    release.set()
    th.join(5)
    assert results["slow_a"]["ok"]


def test_resource_limits_step_budget_and_timeout():
    """Resource limits are refused loudly: a plan over the step budget fails
    before anything runs; a run that outlives its timeout budget fails."""
    s = sandbox_for_tests()
    eng, sched, store, audit = (s["engine"], s["scheduler"], s["store"],
                                s["audit"])
    calls: list = []
    _tools(eng, calls)
    assert store.add({"name": "capped", "kind": "manual_run", "level": 5,
                      "max_steps": 1,
                      "steps": [{"tool": "probe_tool"},
                                {"tool": "draft_tool"}]})["ok"]
    out = sched.run_task(store.get("capped"), "test")
    assert not out["ok"] and "step budget" in out["error"]
    assert calls == []                                   # nothing ran
    assert "denied" in [r["event"] for r in audit.tail(50)]

    # timeout budget: a slow tool inside a 0.2s workflow fails on the clock
    def slow(payload):
        time.sleep(0.4)
        return {"ok": True}
    eng.register_tool(ToolSpec(name="slow_tool", risk=2, max_level=5,
                               handler=slow))
    w = Workflow(name="slow_wf", level=5, timeout_s=0.2)
    out2 = eng.run(w, [Step(tool="slow_tool")])
    assert not out2["ok"] and "budget" in out2["error"]


# ── 9. audit + notifier ─────────────────────────────────────────────────────

def test_audit_covers_the_lifecycle():
    s = sandbox_for_tests()
    eng, audit = s["engine"], s["audit"]
    calls: list = []
    _tools(eng, calls)
    w, steps = _wf(name="audited_flow", level=4)
    out = eng.run(w, steps)
    eng.resolve_approval(out["approval_id"], approved=True, by="human")
    eng.trip_stop("done testing", by="human")
    events = [r["event"] for r in audit.tail(200)]
    for expected in ("run_start", "granted", "approval_parked",
                     "approval_used", "run_complete", "approval_resolved",
                     "emergency_stop"):
        assert expected in events, expected
    rows = audit.tail(3)
    assert rows and all("ts" in r and "event" in r for r in rows)


def test_hermes_notifier_contract():
    """The default sender talks to the Hermes CLI; failures are honest."""
    calls: list = []

    def fake_cli(message, channel="telegram", urgency="normal"):
        calls.append((channel, message))
        return channel == "telegram"

    n = Notifier(sender=fake_cli)
    r = n.notify("hello", channel="telegram", urgency="high")
    assert r["ok"] is True and calls == [("telegram", "hello")]
    r2 = n.notify("hello", channel="sms")
    assert r2["ok"] is False and r2["detail"]          # honest failure
    # the CLI path: no hermes binary → reports not-ok, never raises
    real = Notifier(sender=Notifier._hermes_cli)
    out = real.notify("test", channel="telegram")
    assert out["ok"] in (True, False)


# ── 10. agent selection ─────────────────────────────────────────────────────

def test_agent_selection_dispatches_to_the_right_agent():
    """A workflow step can select and dispatch to a registered orchestrator
    agent — first can_handle wins — through the standard tool gate."""
    from orchestrator.base import BaseAgent
    from orchestrator.contracts import AgentRequest, TaskStatus, TaskType
    from orchestrator.registry import AgentRegistry
    from orchestrator.autonomy.engine import make_agent_tool

    served: list[str] = []

    class Researcher(BaseAgent):
        name = "research_agent"
        task_types = (TaskType.RESEARCH,)

        def handle(self, request: AgentRequest):
            served.append(request.text)
            from orchestrator.contracts import AgentResult
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.COMPLETED, text="found it")

    class Trader(BaseAgent):
        name = "trading_agent"
        task_types = (TaskType.TRADING, TaskType.RESEARCH)   # also covers research

        def handle(self, request: AgentRequest):
            served.append(f"trader:{request.text}")
            from orchestrator.contracts import AgentResult
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.COMPLETED, text="traded")

    reg = AgentRegistry()
    reg.register(Researcher())          # registered first → wins the research ask
    reg.register(Trader())

    s = sandbox_for_tests()
    eng = s["engine"]
    eng.register_tool(make_agent_tool(reg))
    w = Workflow(name="dispatch", level=5)
    out = eng.run(w, [Step(tool="run_agent",
                           payload={"task_type": "research",
                                    "text": "study the harbor manifest"})])
    assert out["ok"] and out["status"] == "completed"
    step = out["results"][0]
    assert step["agent"] == "research_agent"          # SELECTION happened
    assert served == ["study the harbor manifest"]
    assert step["text"] == "found it"

    # an unknown task type or empty text fails honestly, never silently
    out2 = eng.run(w, [Step(tool="run_agent",
                            payload={"task_type": "rag", "text": "x"})])
    assert out2["status"] == "failed"
    out3 = eng.run(w, [Step(tool="run_agent", payload={"task_type": "research"})])
    assert out3["status"] == "failed"

    # at level 4 the agent tool parks for approval like any outward step
    out4 = eng.run(Workflow(name="d4", level=4),
                   [Step(tool="run_agent",
                         payload={"task_type": "research", "text": "y"})])
    assert out4["status"] == "awaiting_approval"
    assert served == ["study the harbor manifest"]     # nothing ran pre-approval
