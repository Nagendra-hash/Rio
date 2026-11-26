"""
orchestrator/agents/system_agent.py — the real System Agent.

Replaces the Phase-1 stub under the SAME registry name ("system_agent") —
the router, contracts and orchestrator are untouched (the convention the
rag_agent set in Phase 2). Enabled with JARVIS_SYSTEM_AGENT=on (shadow-mode
default, like every other phase).

WHAT IT DOES
    intent()            maps a natural-language ask to a tool call + args
    handle()            executes it through SystemToolbox, which enforces
                        the permission levels (READ_ONLY / SAFE_ACTION /
                        APPROVAL_REQUIRED / BLOCKED) and audit-logs every
                        decision. Approval-required actions return
                        TaskStatus.NEEDS_APPROVAL with a full explanation
                        (what / why / effect / risk / undo) that the human
                        sees BEFORE approving.
    checkpoint()        every task leaves memory/system_agent/<id>.json —
                        the deliverable's "checkpoint".

SAFETY (contract from the phase brief)
    * no unrestricted root            → the toolbox never sudo/su; needing
                                        root fails honestly
    * approval before destructive ops → permission layer returns
                                        needs_approval + explanation; the
                                        orchestrator's ApprovalGate holds
                                        the task until a human resolves it
    * BLOCKED ≠ approval              → protected processes, critical
                                        services, Jarvis production config,
                                        firewall changes, critical packages
                                        NEVER run, even approved
    * everything audited              → hash-chained JSONL under
                                        memory/system_agent/audit.jsonl

`handle()` NEVER raises. Every failure is an AgentResult(FAILED) — the
orchestrator can always deliver something to the user.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from orchestrator.agents.system_tools import (
    PermLevel,
    SystemAuditLog,
    SystemToolbox,
)
from orchestrator.base import BaseAgent
from orchestrator.contracts import AgentRequest, AgentResult, TaskStatus, TaskType

SYSTEM_AGENT_DIR = "system_agent"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _agent_dir(explicit: Optional[str] = None) -> Path:
    """Writable state dir, relocated by JARVIS_HOME like every other
    writable path in this repo."""
    if explicit:
        return Path(explicit)
    import os
    home = os.environ.get("JARVIS_HOME", "").strip()
    if home:
        return Path(home).expanduser() / "memory" / SYSTEM_AGENT_DIR
    return Path(__file__).resolve().parents[2] / "memory" / SYSTEM_AGENT_DIR


def _safe_name(task_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.\-]", "_", str(task_id))[:120]


def checkpoint_path(task_id: str, cdir: Optional[str] = None) -> Path:
    return _agent_dir(cdir) / f"{_safe_name(task_id)}.json"


def save_checkpoint(cp: dict, cdir: Optional[str] = None) -> Path:
    d = _agent_dir(cdir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{_safe_name(cp['task_id'])}.json"
    cp["updated_at"] = _now()
    path.write_text(json.dumps(cp, indent=1, default=str)[:512_000],
                    encoding="utf-8")
    return path


def load_checkpoint(task_id: str, cdir: Optional[str] = None) -> Optional[dict]:
    try:
        return json.loads(checkpoint_path(task_id, cdir)
                          .read_text(encoding="utf-8"))
    except Exception:
        return None


# ══════════════════════════════ intent mapping ═══════════════════════════════
# Deterministic, offline lexical intent → tool + args. Same philosophy as the
# classifier: a miss costs nothing (NEEDS_INPUT), a wrong destructive guess
# is impossible because the toolbox gates every action.

_INTENTS: tuple = (
    # ACTIONS FIRST — "restart docker service" must be a service action,
    # not the read-only services fact. Facts come after.
    (re.compile(r"\b(kill|terminate|close)\s+(?:the\s+)?(?:process\s+)?"
                r"([\w.\-]+)", re.I), "kill",
     lambda m: {"pid_or_name": m.group(2)}),
    (re.compile(r"\bforce[- ]?kill\s+(?:the\s+)?(?:process\s+)?"
                r"([\w.\-]+)", re.I), "kill",
     lambda m: {"pid_or_name": m.group(2), "force": True}),
    (re.compile(r"\b(restart|stop|start|reload|status\s+of)\s+(?:the\s+)?"
                r"([\w@.\-]+)\s*(?:service|unit|daemon)?\b", re.I),
     "service",
     lambda m: {"verb": _service_verb(m.group(1)), "unit": m.group(2)}),
    (re.compile(r"\bdelete\s+(?:the\s+)?file\s+([\w./\-~]+)", re.I),
     "delete", lambda m: {"path": m.group(1)}),
    (re.compile(r"\b(install|remove|uninstall)\s+(?:the\s+)?package\s+"
                r"([\w.\-]+)", re.I), "install",
     lambda m: {"packages": [m.group(2)],
                "remove": m.group(1).lower() in ("remove", "uninstall")}),
    (re.compile(r"\b(open|allow|block)\s+(?:port|firewall)\b", re.I),
     "firewall", lambda m: {}),
    (re.compile(r"\b(change|edit|write|update)\s+(?:the\s+)?(?:jarvis\s+)?"
                r"(config|configuration)\b", re.I), "jarvis_config",
     lambda m: {"path": "config/system_agent.json"}),
    # ── facts ──
    (re.compile(r"\b(cpu|processor)\b", re.I), "cpu", lambda m: {}),
    (re.compile(r"\b(ram|memory|swap)\b", re.I), "memory", lambda m: {}),
    (re.compile(r"\b(disk|storage|filesystems?)\b", re.I), "disks",
     lambda m: {}),
    (re.compile(r"\b(processes|top\s+processes|ps)\b", re.I), "processes",
     lambda m: {}),
    (re.compile(r"\bservices?\b", re.I), "services", lambda m: {}),
    (re.compile(r"\bdocker\b", re.I), "docker", lambda m: {}),
    (re.compile(r"\b(logs?|journal)\b", re.I), "logs", lambda m: {}),
    (re.compile(r"\b(network|internet|wifi|wi-fi|dns|ping)\b", re.I),
     "network", lambda m: {}),
    (re.compile(r"\b(backups?|backup\s+now|create\s+backup)\b", re.I),
     "backup", lambda m: {}),
    (re.compile(r"\b(jarvis\s+health|health\s+check|self\s?test)\b", re.I),
     "health", lambda m: {}),
    (re.compile(r"\b(audit)\b", re.I), "audit_verify", lambda m: {}),
)


def _service_verb(word: str) -> str:
    w = (word or "").lower()
    return {"restart": "restart", "stop": "stop", "start": "start",
            "reload": "reload-or-restart", "status": "status"}.get(w, w)


def intent_for(text: str) -> tuple:
    """(tool_name, args) for the first matching intent, or ("", {})."""
    t = (text or "").strip()
    for pat, tool, extractor in _INTENTS:
        m = pat.search(t)
        if m:
            return tool, extractor(m)
    return "", {}


# ══════════════════════════════ the agent ════════════════════════════════════
class SystemAgent(BaseAgent):
    """The real System Agent. See module docstring."""

    name = "system_agent"
    description = ("Linux monitoring, services, docker, logs, network "
                   "diagnostics, files, backups, application status, "
                   "Jarvis health, and guarded maintenance actions.")
    task_types = (TaskType.SYSTEM,)

    def __init__(self, toolbox: Optional[SystemToolbox] = None,
                 audit: Optional[SystemAuditLog] = None,
                 cdir: Optional[str] = None) -> None:
        self.audit = audit or SystemAuditLog()
        self.toolbox = toolbox or SystemToolbox(audit=self.audit)
        self.cdir = cdir          # tests inject a temp dir

    # ── BaseAgent contract ────────────────────────────────────────────────
    def handle(self, request: AgentRequest) -> AgentResult:
        try:
            return self._handle(request)
        except Exception as exc:                      # never raise
            try:
                self.audit.append("agent.error", task_id=request.task_id,
                                  error=f"{type(exc).__name__}: {exc}")
            except Exception:
                pass
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.FAILED,
                text=f"[system_agent] internal error: "
                     f"{type(exc).__name__}: {exc}",
                data={"error": str(exc)}, agent=self.name)

    # ── core ──────────────────────────────────────────────────────────────
    def _handle(self, request: AgentRequest) -> AgentResult:
        text = (request.text or "").strip()
        meta = dict(request.metadata or {})

        # Resuming an approved task: metadata carries the pending action.
        if meta.get("approved") and meta.get("pending_action"):
            return self._execute_pending(request, meta)

        tool, args = intent_for(text)
        if not tool:
            return self._help(request)

        cp = {
            "task_id": request.task_id, "created_at": _now(),
            "request_text": text[:2000], "tool": tool, "args": args,
            "perm_level": None, "status": "received", "history": [],
        }
        try:
            save_checkpoint(cp, self.cdir)
        except Exception:
            pass

        result = self._dispatch(tool, args, task_id=request.task_id)

        # Checkpoint the outcome.
        cp["perm_level"] = result.get("level")
        cp["status"] = ("needs_approval"
                        if result.get("needs_approval")
                        else ("ok" if result.get("ok") else "error"))
        cp["result_summary"] = str(result.get("error")
                                   or result.get("text")
                                   or result.get("reason") or "")[:500]
        try:
            save_checkpoint(cp, self.cdir)
        except Exception:
            pass

        return self._to_agent_result(request, tool, args, result)

    # ── dispatch ──────────────────────────────────────────────────────────
    def _dispatch(self, tool: str, args: dict,
                  task_id: str = "") -> dict:
        tb = self.toolbox
        if tool == "cpu":
            return tb.cpu()
        if tool == "memory":
            return tb.memory()
        if tool == "disks":
            return tb.disks()
        if tool == "processes":
            return tb.processes(limit=10)
        if tool == "services":
            return tb.services()
        if tool == "docker":
            return tb.docker()
        if tool == "logs":
            return tb.logs(journal_unit=args.get("unit", ""))
        if tool == "network":
            return tb.network()
        if tool == "audit_verify":
            return tb.audit.verify()
        if tool == "health":
            return self.health()
        if tool == "backup":
            return tb.create_backup(task_id=task_id)
        if tool == "kill":
            return tb.kill_process(args.get("pid_or_name"),
                                   task_id=task_id,
                                   force=bool(args.get("force")))
        if tool == "service":
            return tb.service(args.get("verb", "status"),
                              args.get("unit", ""), task_id=task_id)
        if tool == "docker_action":
            return tb.docker_action(args.get("argv_tail", []),
                                    task_id=tool and task_id)
        if tool == "delete":
            return tb.delete_file(args.get("path"), task_id=task_id)
        if tool == "install":
            return tb.install(args.get("packages"), task_id=task_id,
                              remove=bool(args.get("remove")))
        if tool == "firewall":
            return tb.firewall(args.get("argv_tail", []), task_id=task_id)
        if tool == "jarvis_config":
            return tb.jarvis_config_write(args.get("path", ""),
                                          task_id=task_id)
        return {"ok": False, "error": f"unknown tool: {tool}"}

    # ── Jarvis health ─────────────────────────────────────────────────────
    def health(self) -> dict:
        """Is Jarvis itself OK? Disk space, load, the dashboard process,
        the orchestrator registry, recent agent errors. READ_ONLY."""
        checks: dict[str, Any] = {}
        mem = self.toolbox.memory()
        disk = self.toolbox.disks()
        cpu = self.toolbox.cpu()
        checks["memory_ok"] = bool(mem.get("ok")) and \
            float(mem.get("percent", 100) or 100) < 95.0
        root_disk = next((d for d in (disk.get("disks") or [])
                          if d.get("mountpoint") == "/"), None)
        checks["disk_ok"] = bool(root_disk) and \
            float(root_disk.get("percent", 100) or 100) < 95.0
        checks["cpu_ok"] = bool(cpu.get("ok")) and \
            float(cpu.get("busy_percent", 100) or 100) < 98.0
        # Dashboard process up? (ui.py / main.py run the dashboard)
        dash = {"ok": None, "detail": "not probed"}
        try:
            import psutil
            hits = [p.info for p in psutil.process_iter(
                ["name", "cmdline"]) if any(
                    "ui.py" in (c or "") for c in
                    (p.info.get("cmdline") or []))]
            dash = {"ok": bool(hits),
                    "detail": f"{len(hits)} dashboard process(es)"}
        except Exception as exc:
            dash = {"ok": None, "detail": f"probe failed: {exc}"}
        checks["dashboard"] = dash
        # Orchestrator reachable in-process?
        try:
            from orchestrator.registry import AgentRegistry
            checks["orchestrator"] = {"ok": True,
                                      "detail": "registry importable"}
        except Exception as exc:
            checks["orchestrator"] = {"ok": False, "detail": str(exc)[:200]}
        audit_v = self.audit.verify()
        checks["audit_chain_ok"] = bool(audit_v.get("ok"))
        ok = all(v for k, v in checks.items()
                 if k in ("memory_ok", "disk_ok", "cpu_ok",
                          "audit_chain_ok"))
        return {"ok": ok, "checks": checks, "audit": audit_v,
                "when": _now()}

    # ── approved-task execution (orchestrator re-dispatch) ────────────────
    def pending_action_from(self, result_data: dict) -> Optional[dict]:
        """The dict the orchestrator should carry through the approval gate
        and hand back on re-dispatch (metadata.pending_action)."""
        if not isinstance(result_data, dict):
            return None
        if not result_data.get("needs_approval"):
            return None
        return {"tool": result_data.get("tool"),
                "args": result_data.get("args", {})}

    def _execute_pending(self, request: AgentRequest,
                         meta: dict) -> AgentResult:
        """The human approved; run the parked action with
        perm_level='approved' and record the resolution."""
        pending = meta.get("pending_action") or {}
        tool = str(pending.get("tool") or "")
        args = dict(pending.get("args") or {})
        by = str(meta.get("by") or "user")
        self.audit.approval_resolved(request.task_id, True, by)
        result = self._dispatch_approved(tool, args,
                                         task_id=request.task_id)
        cp = load_checkpoint(request.task_id, self.cdir) or \
            {"task_id": request.task_id, "created_at": _now(),
             "request_text": "(approved task)", "tool": tool, "args": args,
             "history": []}
        cp["status"] = "executed_after_approval"
        cp["approved_by"] = by
        cp["result_summary"] = str(result.get("error") or
                                   result.get("reason") or
                                   "ok")[:500]
        try:
            save_checkpoint(cp, self.cdir)
        except Exception:
            pass
        text = self._render(tool, result)
        return AgentResult(
            task_id=request.task_id,
            status=TaskStatus.COMPLETED if result.get("ok")
            else TaskStatus.FAILED,
            text=text, data={"result": result, "approved": True,
                             "tool": tool},
            agent=self.name)

    def _dispatch_approved(self, tool: str, args: dict,
                           task_id: str) -> dict:
        """Same as _dispatch but with perm_level='approved' — only reachable
        after the orchestrator's ApprovalGate resolved the task."""
        tb = self.toolbox
        if tool == "kill":
            return tb.kill_process(args.get("pid_or_name"), task_id=task_id,
                                   perm_level="approved",
                                   force=bool(args.get("force")))
        if tool == "service":
            return tb.service(args.get("verb", "restart"),
                              args.get("unit", ""), task_id=task_id,
                              perm_level="approved")
        if tool == "docker_action":
            return tb.docker_action(args.get("argv_tail", []),
                                    task_id=task_id, perm_level="approved")
        if tool == "delete":
            return tb.delete_file(args.get("path"), task_id=task_id,
                                  perm_level="approved")
        if tool == "install":
            return tb.install(args.get("packages"), task_id=task_id,
                              perm_level="approved",
                              remove=bool(args.get("remove")))
        return {"ok": False, "error": f"tool '{tool}' is not approvable"}

    # ── AgentResult rendering ─────────────────────────────────────────────
    def _to_agent_result(self, request: AgentRequest, tool: str,
                         args: dict, result: dict) -> AgentResult:
        needs_approval = bool(result.get("needs_approval"))
        if needs_approval:
            pending = {"tool": tool, "args": args}
            expl = result.get("explanation") or {}
            lines = [
                "This action needs your approval before I run it.",
                f"  What:   {expl.get('action', tool)}",
                f"  Why:    {expl.get('why', 'requested')}",
                f"  Effect: {expl.get('effect', 'system state changes')}",
                f"  Risk:   {expl.get('risk', 'see the audit log')}",
                f"  Undo:   {expl.get('undo', 'manual restore')}",
            ]
            return AgentResult(
                task_id=request.task_id,
                status=TaskStatus.NEEDS_APPROVAL,
                text="\n".join(lines),                data={"approval_summary": f"{tool}: "
                                          f"{expl.get('action', '')}",
                      "needs_approval": True, "tool": tool, "args": args,
                      "pending_action": {"tool": tool, "args": args},
                      "explanation": expl, "level": result.get("level")},
                agent=self.name)
        if result.get("level") == PermLevel.BLOCKED or \
                (not result.get("ok") and result.get("level")
                 == PermLevel.BLOCKED):
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.DENIED,
                text=f"Refused (BLOCKED): {result.get('error', '')}",
                data={"result": result, "tool": tool, "blocked": True},
                agent=self.name)
        status = TaskStatus.COMPLETED if result.get("ok") \
            else TaskStatus.FAILED
        return AgentResult(
            task_id=request.task_id, status=status,
            text=self._render(tool, result),
            data={"result": result, "tool": tool},
            agent=self.name)

    def _render(self, tool: str, r: dict) -> str:
        """Deterministic, bounded human summary of a tool result."""
        if not r.get("ok"):
            return f"{tool}: {r.get('error', 'failed')}"
        if tool == "cpu":
            return (f"CPU: {r.get('cores', '?')} logical cores, "
                    f"{r.get('busy_percent', '?')}% busy, "
                    f"load {r.get('loadavg')}")
        if tool == "memory":
            return (f"RAM: {r.get('used_gb')}G / {r.get('total_gb')}G "
                    f"({r.get('percent')}%); swap "
                    f"{r.get('swap_used_gb')}G / "
                    f"{r.get('swap_total_gb')}G")
        if tool == "disks":
            rows = [f"  {d.get('mountpoint')}: {d.get('free_gb')}G free "
                    f"of {d.get('total_gb')}G ({d.get('percent')}%)"
                    for d in (r.get("disks") or [])[:6]]
            return "Disks:\n" + "\n".join(rows)
        if tool == "processes":
            rows = [f"  {p.get('pid')} {p.get('name')} "
                    f"(cpu {p.get('cpu_percent')}%)"
                    for p in (r.get("top") or [])[:10]]
            return f"{r.get('count')} processes; top:\n" + "\n".join(rows)
        if tool == "services":
            rows = [f"  {s.get('unit')}: {s.get('active')}/{s.get('sub')}"
                    for s in (r.get("services") or [])[:15]]
            return f"{r.get('count')} services:\n" + "\n".join(rows)
        if tool == "docker":
            rows = [f"  {c.get('name')} ({c.get('image')}): "
                    f"{c.get('status')}"
                    for c in (r.get("containers") or [])[:10]]
            return (f"{r.get('count')} containers:\n" + "\n".join(rows)
                    if rows else "docker: no containers")
        if tool == "logs":
            lines = r.get("lines") or []
            return f"Last {len(lines)} log lines from {r.get('source')}:\n" \
                + "\n".join(lines[-20:])
        if tool == "network":
            net = r.get("internet") or {}
            s = (f"internet: {'ok' if net.get('ok') else 'DOWN'}"
                 + (f" ({net.get('latency_ms')}ms)" if net.get("latency_ms")
                    else ""))
            ifaces = r.get("interfaces") or []
            if ifaces:
                s += "\n" + "\n".join(
                    f"  {i.get('name')}: {i.get('state')} "
                    f"{','.join(i.get('addresses') or [])}"
                    for i in ifaces[:6])
            return s
        if tool == "health":
            checks = r.get("checks") or {}
            flags = " ".join(f"{k}={'OK' if v else 'FAIL'}"
                             for k, v in checks.items()
                             if isinstance(v, bool))
            return f"Jarvis health: {'OK' if r.get('ok') else 'DEGRADED'} " \
                f"[{flags}]"
        if tool == "backup":
            return f"Backup written: {r.get('backup_path', '?')}"
        if tool == "audit_verify":
            return (f"Audit log OK — {r.get('records')} records"
                    if r.get("ok") else
                    f"Audit log TAMPERED: {r.get('reason')}")
        if tool == "kill":
            return f"Signalled {len(r.get('signalled') or [])} process(es)"
        if tool == "service":
            return f"systemctl {r.get('level')}: rc={r.get('returncode')}"
        if tool == "delete":
            return f"Moved to trash: {r.get('trashed_to')}"
        if tool == "install":
            return f"Package change rc={r.get('returncode')}"
        return f"{tool}: done"

    # ── help / NEEDS_INPUT ────────────────────────────────────────────────
    def _help(self, request: AgentRequest) -> AgentResult:
        return AgentResult(
            task_id=request.task_id, status=TaskStatus.NEEDS_INPUT,
            text="[system_agent] I can: cpu | memory/swap | disks | "
                 "processes | services | docker | logs [unit=X] | network | "
                 "backup | jarvis health | audit verify — plus guarded "
                 "actions: kill <app>, restart <service>, delete file <path>, "
                 "install package <name> (approval required; critical ones "
                 "blocked).",
            data={"help": True}, agent=self.name)
