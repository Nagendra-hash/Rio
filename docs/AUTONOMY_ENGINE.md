# Autonomy Engine & Scheduled Workflows — final architecture (Phase 20)

> Code: `orchestrator/autonomy/` (`engine.py`, `scheduler.py`, `monitoring.py`, `audit.py`)
> REST: `dashboard/autonomy_api.py` (registered in `dashboard/server.py`)
> Tests: `tests/test_autonomy.py` (18, fully offline) · State: `memory/autonomy/`
> Reuses: `orchestrator.approvals.ApprovalGate` · `orchestrator.checkpoint.PlanCheckpointStore` · Hermes CLI

This is the final layer of the stack: everything before Phase 20 was a
capability Jarvis could be ASKED to use; Phase 20 is how Jarvis acts
WITHOUT being asked — on a schedule, on an event, on a condition — under
strict, auditable autonomy limits.

## 1. The five autonomy levels (strict, monotonic)

| Level | Name             | What may happen                                                       |
|-------|------------------|-----------------------------------------------------------------------|
| 1     | `observe`        | Read-only probes run; results recorded; nothing said, nothing done    |
| 2     | `recommend`      | Observations reach the user (Hermes) as advice; nothing executed      |
| 3     | `draft`          | Local, reversible work (drafts, RAG notes, research); nothing leaves  |
| 4     | `execute_approved`| Real actions run only after an explicit human "yes" (parked + checkpointed) |
| 5     | `trusted`        | Runs automatically — for non-dangerous tools only (see §2)            |

## 2. The level × risk matrix — the whole phase in one table

Every tool is registered with a risk class. The engine's single security
decision point (`AutonomyEngine.evaluate`) maps effective level × risk:

| eff level \ tool risk | 1 read-only | 2 local/reversible | 3 outward-facing | 4 dangerous |
|-----------------------|-------------|--------------------|------------------|-------------|
| 1–2                   | observe     | skip               | DENY             | DENY        |
| 3 (draft)             | execute     | execute            | DENY             | DENY        |
| 4 (approved)          | execute     | execute            | PARK approval    | PARK approval |
| 5 (trusted)           | execute     | execute            | execute (audited)| **PARK — downgraded, loudly** |

* **Effective level** = `min(workflow level, per-agent level cap, tool cap,
  dangerous cap)`. Nothing can raise it; config can only lower it.
* **Dangerous actions must never silently become autonomous**: risk-4 tools
  (trading execution, spend, deletion, raw shell) are hard-capped at level
  4 in code (`DANGEROUS_CAP`) — a level-5 workflow using one is DOWNGRADED
  to an approval gate with an audited `downgrade` + `approval_parked`
  trail. Config cannot lift this; only the code floor can.
* Risk-3 tools (send telegram, calendar write) run automatically only at
  level 5; at level 4 they park an approval; below, they deny.

## 3. The safety rails (all deliverables, where they live)

| Control | Where | Behaviour |
|---|---|---|
| Global emergency stop | `AutonomyEngine.trip_stop/release_stop` | File-backed flag (`state.json`) → survives restart; when tripped, engine refuses every run and the scheduler tick returns immediately (`stopped: True`); release is explicit + audited |
| Per-agent enable/disable | `engine.set_agent_enabled` | Disabled agent's steps are denied at the gate; toggle survives restart |
| Per-tool permission | `ToolSpec(risk, max_level, needs_approval, allowed_agents)` + `engine.set_agent_level` | Unknown tool, tool outside the workflow's contract, or agent not allowed on tool → denied; per-agent level cap narrows |
| Human approval gates | `orchestrator/approvals.py` (reused) | Level-4 (and dangerous) steps park with the EXACT payload checkpointed; `resolve_approval(approved=True)` **resumes** the run executing precisely that payload (audited `approval_used`); denial marks the checkpoint `denied`; unresolved approvals EXPIRE (gate TTL) and an expired one can never resume — "a stale yes can never fire a forgotten task" |
| Audit logs | `orchestrator/autonomy/audit.py` → `memory/autonomy/audit.jsonl` | Append-only JSONL: every grant/skip/downgrade/deny/stop/approval/recovery/notify event, with run_id, workflow, actor, detail; written BEFORE the action it witnesses |
| Failure recovery | `engine._recover` + scheduler failure streak | Per-step retry with verification; failed runs checkpoint recovery state; the scheduler counts consecutive failures and escalates through Hermes ONCE at the threshold (3); a success clears the streak |
| Resource limits | `Workflow.max_steps/timeout_s` + `Scheduler.max_concurrent` | Step budget refused loudly (never silently truncated); wall-clock budget checked before and after each step; concurrency cap skips overflow runs |
| Scheduled-task management | `TaskStore` (`memory/autonomy/scheduled_tasks.json`) | add/get/update/remove/list + pause/resume (`enabled`), run history (last 1000), per-task `last_run/next_run/failures/last_status` |

## 4. Triggers (the scheduler)

`Scheduler.tick()` finds due tasks and runs each through the engine — the
scheduler NEVER executes anything by itself:

* **interval** — every `interval_s` (hard floor 60 s; no API-tier raids)
* **daily** — at `HH:MM` local time, once per day (fires only when it has
  not run since today's target time)
* **event** — fires when `scheduler.emit_event(name)` delivers a matching
  signal (e.g. `email.important_arrived`, `price.level_hit`); the event
  drains when consumed
* **condition** — fires when a **probe** (see §5) says true, subject to a
  per-task `condition_cooldown_s` (default 6 h) so an always-true condition
  cannot re-fire every tick

## 5. Monitoring probes (`monitoring.py`)

Plain functions returning `{"fired": bool, "note": str, ...}` — read-only,
defensive (missing deps degrade to `fired=False` with a note, never a
crashed tick), registered in `PROBES` by name; workflows add their own via
`register_probe`. Built-ins cover the phase's example workflows:

| Probe | Covers |
|---|---|
| `disk_pressure` | system health alert (disk %, free GB; memory % via psutil) |
| `backup_freshness` | backup checks (newest file age in a watched location) |
| `important_email` | important email alert (VIP senders / subject keywords, fresh window) |
| `market_prep_due` | trading market preparation window |
| `process_alive` | a critical service is up |

## 6. Notifications through Hermes

`Notifier` wraps the delivery path; the default `_hermes_cli` shells out to
the Hermes CLI's `send -t telegram` (same env discipline as
`actions/hermes_task.py`), best-effort with a 30 s cap. A failed delivery
is reported honestly (`ok: False`) — it never claims success. The engine
notifies on: run completion (level ≥ 2 with a `notify` channel), denied
runs, parked approvals ("needs approval"), and failed steps; the scheduler
adds the repeated-failure escalation. Level-1 workflows are silent — that
is the point of observe-only.

## 7. The example workflows, as configured

```python
store.add({"name": "morning_briefing", "kind": "daily", "level": 2,
           "trigger": {"time": "07:00"},
           "steps": [{"tool": "market_snapshot", "reason": "pre-open data"},
                     {"tool": "email_check", "reason": "overnight mail"}],
           "notify": {"channel": "telegram"}})
store.add({"name": "backup_watchdog", "kind": "condition", "level": 2,
           "trigger": {"probe": "backup_freshness", "params": {"max_age_h": 26}},
           "steps": [{"tool": "backup_check"}], "notify": {"channel": "telegram"}})
store.add({"name": "trade_executor", "kind": "event", "level": 5,
           "trigger": {"event": "price.level_hit"},
           "steps": [{"tool": "place_trade"}],           # → parks approval
           "notify": {"channel": "telegram"}})
```

## 8. REST surface (same bearer auth as all `/api` routes)

| Endpoint | Purpose |
|---|---|
| `GET /api/autonomy/status` | engine + scheduler snapshot (stop state, disabled agents, levels, tools, task count) |
| `POST /api/autonomy/stop` / `resume` | trip / release the global emergency stop |
| `POST /api/autonomy/agent` | per-agent enable/disable or level cap |
| `GET /api/autonomy/approvals` | pending human gates |
| `POST /api/autonomy/approve` | `{approval_id, approved, by?}` → resolve + resume |
| `GET/POST /api/autonomy/tasks` | scheduled-task management (add/update/pause/resume/remove) |
| `GET /api/autonomy/runs` | task run history |
| `GET /api/autonomy/audit` | audit tail (`?limit=&event=`) |

## 9. Test map (`tests/test_autonomy.py` — 18, offline: temp state, injected notifier/clock, fake tools)

1. level semantics: L1 observes (silent), L2 recommends (notifies, no local
   work), L3 drafts, L4 parks, L5 auto-runs safe + STILL parks dangerous
2. dangerous-in-trusted downgraded + audited; dangerous below execute
   refused outright
3. emergency stop: blocks engine + scheduler, survives restart, explicit
   release only
4. per-agent disable, per-agent level cap, per-tool ceiling, workflow tool
   contract, per-tool allowed agents
5. approval: park → deny (nothing runs) → re-park → approve → the exact
   parked payload executes; unknown approvals refused; expired approvals
   can never resume
6. verification: failing verifier retries then fails with recovery state;
   passing verifier records `step_ok`
7. scheduler: interval + daily due/cool-down, event drain, condition probe
   + cooldown; task CRUD, pause/resume, history
8. resource limits: step budget refused before anything runs; timeout
   budget; concurrency cap skips overflow
9. audit lifecycle lines + notifier contract (Hermes path honest on failure)
10. agent selection: a step dispatches to a registered orchestrator agent
    (first can_handle wins) through the standard tool gate; unknown task
    types and empty asks fail honestly; level 4 parks the dispatch
