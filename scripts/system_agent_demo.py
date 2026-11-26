#!/usr/bin/env python3
"""
scripts/system_agent_demo.py — System Agent end-to-end walkthrough, offline.

Shows the full permission lifecycle on scripted/fake collectors (NO docker,
NO systemd writes, NO root, NO network needed):

    .venv/bin/python scripts/system_agent_demo.py

Stages:
    1. READ_ONLY facts        (cpu / memory / disks — fake collectors)
    2. SAFE_ACTION            (kill an allowlisted app — SIGTERM, no target
                               needed: the point is the permission decision)
    3. APPROVAL_REQUIRED      (restart a service → explanation → approve →
                               execute → audit)
    4. BLOCKED                (kill systemd / firewall change / Jarvis
                               config write — refused even when "approved")
    5. Audit log verification (hash chain)
    6. Jarvis health check
    7. Checkpoints            (what a task leaves behind)

Every stage prints what the agent would say to the user.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from orchestrator.agents.system_agent import SystemAgent, load_checkpoint  # noqa: E402
from orchestrator.agents.system_tools import (PermLevel, SystemAuditLog,  # noqa: E402
                                              SystemToolbox)
from orchestrator.contracts import AgentRequest, TaskType, new_task_id  # noqa: E402

GREEN, YELLOW, RED, DIM, BOLD, END = ("\033[32m", "\033[33m", "\033[31m",
                                      "\033[2m", "\033[1m", "\033[0m")


def fake_toolbox(audit, tmp: Path) -> SystemToolbox:
    return SystemToolbox(
        audit=audit,
        cpu_fn=lambda: {"ok": True, "cores": 4, "busy_percent": 23.4,
                        "loadavg": [0.4, 0.5, 0.6]},
        mem_fn=lambda: {"ok": True, "total_gb": 8, "used_gb": 4.2,
                        "percent": 52.5, "swap_total_gb": 2,
                        "swap_used_gb": 0.4, "swap_percent": 20.0},
        disk_fn=lambda: {"ok": True, "count": 1, "disks": [
            {"device": "/dev/sda1", "mountpoint": "/", "fstype": "ext4",
             "total_gb": 100, "used_gb": 41, "free_gb": 59, "percent": 41}]},
        proc_fn=lambda limit=10: {"ok": True, "count": 128, "top": [
            {"pid": 912, "name": "vlc", "cpu_percent": 8.1},
            {"pid": 1, "name": "systemd", "cpu_percent": 0.2}][:limit]},
        svc_fn=lambda state_filter="": {"ok": True, "count": 2,
                                        "services": [
            {"unit": "cron.service", "active": "active", "sub": "running",
             "description": "scheduler"},
            {"unit": "cups.service", "active": "active", "sub": "running",
             "description": "printing"}]},
        docker_fn=lambda: {"ok": False, "error": "docker CLI not found"},
        log_fn=lambda path="/var/log/syslog", lines=200, journal_unit="": {
            "ok": True, "source": path, "lines": ["demo line 1",
                                                  "demo line 2"]},
        net_fn=lambda: {"ok": True, "internet": {"ok": True,
                                                 "latency_ms": 12.5},
                        "interfaces": [{"name": "wlan0", "state": "UP",
                                        "addresses": ["192.168.1.42"]}]},
        run_fn=lambda argv, timeout=15: {"ok": True, "returncode": 0,
                                         "stdout": "active", "stderr": ""},
        backup_root=str(tmp / "backups"),
    )


COLORS = {PermLevel.READ_ONLY: GREEN, PermLevel.SAFE_ACTION: GREEN,
          PermLevel.APPROVAL_REQUIRED: YELLOW, PermLevel.BLOCKED: RED}


def show(agent, text: str) -> None:
    task_id = new_task_id(prefix="demo")
    r = agent.handle(AgentRequest(task_id=task_id, task_type=TaskType.SYSTEM,
                                  text=text))
    level = (r.data or {}).get("result", {}).get("level", "")
    color = COLORS.get(level, DIM) if isinstance(level, str) else DIM
    tag = f"{color}[{level or r.status.value}]{END}" if level \
        else f"{DIM}[{r.status.value}]{END}"
    print(f"\n{BOLD}$ {text}{END}")
    print(f"  {tag} {r.text.replace(chr(10), chr(10) + '    ')}")
    return r


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="system-agent-demo-"))
    audit = SystemAuditLog(path=tmp / "audit.jsonl")
    agent = SystemAgent(toolbox=fake_toolbox(audit, tmp), audit=audit,
                        cdir=str(tmp / "memory" / "system_agent"))

    print(f"{BOLD}=== System Agent demo (all collectors scripted; the "
          f"permission layer is REAL) === {END}")
    print(f"audit log : {audit.path}")
    print(f"checkpoints: {agent.cdir}")

    print(f"\n{BOLD}1. READ_ONLY — facts, no approval ever{END}")
    show(agent, "what is my cpu usage")
    show(agent, "how much ram is free")
    show(agent, "network diagnostics")

    print(f"\n{BOLD}2. SAFE_ACTION — allowlisted app, gentle kill{END}")
    r = show(agent, "kill vlc")
    if not (r.data or {}).get("result", {}).get("ok", False):
        print(f"    {DIM}(no vlc running here — the permission decision is "
              f"the point: allowlisted app → SAFE_ACTION, no approval){END}")

    print(f"\n{BOLD}3. APPROVAL_REQUIRED — explanation first, then run{END}")
    r = show(agent, "restart cups service")
    pend = (r.data or {}).get("pending_action")
    if pend:
        print(f"  {YELLOW}→ human approves; re-dispatching with the parked "
              f"action{END}")
        r2 = agent.handle(AgentRequest(
            task_id=r.task_id, task_type=TaskType.SYSTEM,
            text="(approved task)",
            metadata={"approved": True, "by": "user",
                      "pending_action": pend}))
        print(f"    {GREEN}[executed]{END} "
              f"{r2.text.replace(chr(10), chr(10) + '    ')}")

    print(f"\n{BOLD}4. BLOCKED — refused even if a human approved{END}")
    show(agent, "kill systemd")
    show(agent, "open port 8080 on the firewall")
    show(agent, "edit the jarvis config")

    print(f"\n{BOLD}5. Audit log — hash-chain verification{END}")
    v = audit.verify()
    print(f"  records={v['records']} ok={v['ok']}")

    print(f"\n{BOLD}6. Jarvis health check{END}")
    h = agent.health()
    print(f"  ok={h['ok']}  checks: "
          + ", ".join(f"{k}={'OK' if v else 'FAIL'}" if isinstance(v, bool)
                      else f"{k}={v.get('ok')}"
                      for k, v in h["checks"].items()))

    print(f"\n{BOLD}7. Checkpoints written by this demo{END}")
    for p in sorted(Path(agent.cdir).glob("*.json"))[:6]:
        cp = json.loads(p.read_text())
        print(f"  {p.name}: tool={cp.get('tool')} "
              f"status={cp.get('status')}")

    print(f"\n{DIM}(tmp state in {tmp} — nothing on this machine was "
          f"touched){END}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
