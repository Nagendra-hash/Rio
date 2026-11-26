# System Agent (Mark III / JARVIS)

Linux operations agent for the host machine: monitoring, diagnostics, guarded
maintenance. Registered under the canonical name `system_agent` in the
orchestrator routing table; replaces the Phase-1 stub when
`JARVIS_SYSTEM_AGENT=on` (shadow-mode default, same convention as every other
phase).

Files:

    orchestrator/agents/system_tools.py   # permission system + guarded toolbox
    orchestrator/agents/system_agent.py   # the BaseAgent (intents, approval, checkpoints)
    tests/test_system_agent.py            # 80 offline tests
    scripts/system_agent_demo.py          # end-to-end demo (offline)
    memory/system_agent/audit.jsonl       # hash-chained audit log (runtime)

## Capabilities

| Area | Tool | Level |
|---|---|---|
| CPU / load | `cpu` | READ_ONLY |
| RAM / swap | `memory` | READ_ONLY |
| Disk usage | `disks` | READ_ONLY |
| Processes | `processes` | READ_ONLY |
| Services (systemd) | `services` / `service` | READ_ONLY / gated |
| Docker | `docker` / `docker_action` | READ_ONLY / gated |
| Logs (files + journald) | `logs` | READ_ONLY |
| Network diagnostics | `network` | READ_ONLY (changes BLOCKED) |
| Files | `delete_file` | gated (trashes, never rm) |
| Backups | `create_backup` | SAFE_ACTION |
| Application status | processes + docker + services | READ_ONLY |
| Jarvis health | `health` | READ_ONLY |
| Audit verification | `audit.verify` | READ_ONLY |

## Permission levels

The toolbox answers every action with exactly one level; the level IS the
answer — there is no "try and see":

| Level | Meaning | Examples |
|---|---|---|
| `READ_ONLY` | inspect/measure/list; no state change | cpu, memory, disks, processes, services (list/status), docker ps, logs, network read, health, audit verify |
| `SAFE_ACTION` | reversible, bounded, non-destructive | kill an allowlisted app with SIGTERM (vlc, firefox…), create a backup |
| `APPROVAL_REQUIRED` | destructive or service-affecting; runs only after a human says yes | restart/stop a non-critical service, delete a file, install/remove a non-critical package, docker start/stop, kill a non-allowlisted process, SIGKILL anything |
| `BLOCKED` | never runs, even approved | kill systemd/sshd/dockerd/hermes/python/jarvis, stop/restart ssh-docker-network-cron-dbus-journald services, systemctl enable/disable/mask, docker system prune / rm / exec / run, firewall or network reconfiguration, write/delete Jarvis production config (tokens, .env, KB registry), install/remove critical software (openssh, docker, kernel, sudo…), anything needing sudo/su |

Design rules enforced in code (`SystemPermissions`):

* BLOCKED is not "needs approval" — `perm_level="approved"` cannot unlock it.
* SIGKILL is never a SAFE_ACTION (approval even for allowlisted apps).
* The toolbox never spawns `sudo`/`su`; root-needing operations fail honestly.
* Delete = move to `~/backups/agent-trash/`, never a hard rm.

## Approval flow (explain → approve → execute → audit)

1. The agent classifies the action `APPROVAL_REQUIRED` and returns
   `TaskStatus.NEEDS_APPROVAL` with an **explanation** the human sees first:

       This action needs your approval before I run it.
         What:   systemctl restart cups
         Why:    requested by the user
         Effect: systemd unit 'cups' changes state (restart)
         Risk:   anything depending on 'cups' loses it until started again
         Undo:   systemctl start cups (or the inverse of restart) restores it

2. The orchestrator parks the task in its `ApprovalGate` (30-minute TTL; a
   stale "yes" can never fire a forgotten task).
3. On approval the orchestrator re-dispatches with
   `metadata={"approved": True, "pending_action": {"tool", "args"}}`; the
   agent runs the parked action with `perm_level="approved"` — and only the
   parked action, never a fresh parse of the text.
4. Every step (permission.check, approval.requested, approval.resolved,
   action.executed / action.blocked) is audit-logged.

## Audit log

`memory/system_agent/audit.jsonl`, append-only JSONL with a sha256 hash
chain: each record embeds `prev_hash` and its own `hash`, so removing or
editing any middle line breaks every later link.

    .venv/bin/python -c "from orchestrator.agents.system_tools import SystemAuditLog as A; print(A().verify())"

Writes are best-effort: an audit failure never breaks a user turn (same
convention as `OrchestrationJournal`).

## Checkpoints

Every task leaves `memory/system_agent/<task_id>.json`:
request text, mapped tool + args, permission level, status
(`received` / `ok` / `error` / `needs_approval` / `executed_after_approval`),
and a short result summary. Checkpoints are how any task can be reconstructed
after the fact.

## Usage

    # conversation (through the orchestrator)
    JARVIS_SYSTEM_AGENT=on ... "how much ram is free"
    JARVIS_SYSTEM_AGENT=on ... "restart cups service"        # → approval
    JARVIS_SYSTEM_AGENT=on ... "kill systemd"                # → denied

    # programmatic
    from orchestrator.agents.system_agent import SystemAgent
    agent = SystemAgent()
    result = agent.handle(request)

    # offline walkthrough
    .venv/bin/python scripts/system_agent_demo.py

## Tests

    .venv/bin/python -m pytest tests/test_system_agent.py -q   # 80 tests, offline

Coverage: permission classification for every action class, BLOCKED-even-
approved, explanation-before-approval, approved execution + audit, hash-chain
tamper detection (edit + delete), agent contract (never raises, NEEDS_INPUT on
unknown), checkpoints, registry flag, orchestrator integration (approval park /
resolve / deny), and live-host smoke of the real collectors.

## Enabling

    JARVIS_SYSTEM_AGENT=on     # real agent replaces the stub, same name

Off by default (shadow-mode). The stub keeps answering SYSTEM-lane asks with
NEEDS_APPROVAL exactly as in Phase 1.
