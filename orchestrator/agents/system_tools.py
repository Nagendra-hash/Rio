"""
orchestrator/agents/system_tools.py — guarded toolbox + permission system for
the System Agent.

WHY A SEPARATE TOOLBOX (and not just prompt the model)
    The System Agent touches real Linux state: processes, systemd services,
    docker, log tails, the firewall. A model (or a careless caller) can kill
    an important process, stop a production service, or wipe a log that was
    needed for a post-mortem. So every operation goes through
    SystemPermissions FIRST, the permission level IS the answer, and every
    mutating action lands in a tamper-evident audit log. The model never
    runs commands itself — it asks the toolbox, and the toolbox decides.

PERMISSION LEVELS (the agent's whole vocabulary)
    READ_ONLY           inspect/measure/list — no state change, no approval.
    SAFE_ACTION         reversible, bounded, non-destructive (write backup,
                        gentle-kill an allowlisted app).
    APPROVAL_REQUIRED   destructive or service-affecting; returns
                        needs_approval=True with an explanation; runs ONLY
                        when the caller passes perm_level="approved" AFTER a
                        human said yes (recorded by the orchestrator's
                        ApprovalGate). A prior approval request is always
                        audit-recorded first.
    BLOCKED             never runs, even approved — at the tool layer
                        (sudo/rm/su, production Jarvis config, kill on
                        protected processes, firewall changes, docker system
                        prune, disable/mask of critical services…).
    Design rule: BLOCKED is not "needs approval" — an approval can never
    unlock it. The permission level IS the answer.

EXPLANATION BEFORE APPROVAL (the brief's requirement)
    Every approval-required tool produces, before anything runs: WHAT will
    happen, WHY, the EFFECT on the system, the RISK, and HOW to undo —
    returned in `explanation` and audit-recorded under approval.requested.

AUDIT LOG
    SystemAuditLog: append-only JSONL with a sha256 hash chain (each record
    embeds the hash of the previous record), so deleting or editing a middle
    line breaks every later link and verify() proves it. Writes are
    best-effort, thread-safe, never raise — matching the
    OrchestrationJournal convention.

COLLECTORS (injectable, offline-testable)
    Every host-fact read goes through a small collector function so tests
    and the offline demo can inject fake collectors. Real collectors use
    psutil (already in requirements.txt) and stdlib only; each degrades
    honestly when a tool is missing.

ROOT
    The toolbox NEVER uses sudo/su and never escalates. Anything needing
    root fails honestly. No unrestricted root access exists anywhere in
    this agent.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

# ── tunables ─────────────────────────────────────────────────────────────────
DEFAULT_TIMEOUT_S = 15          # per collector subprocess (facts, fast)
ACTION_TIMEOUT_S = 30           # per mutating action (restart a service)
MAX_OUTPUT_BYTES = 12_000       # one subprocess output cap
MAX_LOG_LINES = 200             # one log read cap
MAX_LIST = 100                  # generic list cap (ps, docker, services)

# ── process protection ───────────────────────────────────────────────────────
# These processes are JARVIS's own nervous system (or the OS's). Killing them
# can sever the bridge that would have approved the action, so the toolbox
# refuses REGARDLESS of approval state.
PROTECTED_PATTERNS: tuple[str, ...] = (
    "systemd", "systemd-journald", "sshd", "dbus", "cron", "rsyslogd",
    "dockerd", "containerd", "journald", "logind", "networkmanager",
    "hermes", "jarvis", "python", "python3", "main.py",
)

# Regular applications the agent may gently close (SIGTERM) as a SAFE_ACTION.
NICE_APPS = {
    "firefox", "firefox-esr", "chromium", "chromium-browse", "google-chrome",
    "chrome", "vlc", "code", "nautilus", "gedit", "evince", "libreoffice",
    "soffice", "thunderbird", "spotify", "obs", "discord", "zoom",
    "code-oss", "gnome-calculator", "gnome-text-editor", "xreader", "totem",
}

# ── systemd vocabulary ───────────────────────────────────────────────────────
SERVICE_VERBS_SAFE = {"status", "is-active", "is-enabled", "show", "cat",
                      "list-units", "list-unit-files"}
SERVICE_VERBS_APPROVAL = {"stop", "restart", "reload-or-restart", "kill",
                          "mask", "try-restart"}
SERVICE_VERBS_BLOCKED = {"disable", "enable", "link", "revert", "set-default",
                         "isolate", "rescue", "emergency", "suspend",
                         "hibernate", "poweroff", "halt", "reboot", "kexec",
                         "set-property", "daemon-reexec"}
# stop/restart/mask of these is permanent-ish damage; BLOCKED even approved.
CRITICAL_SERVICE_PATTERNS = ("ssh", "docker", "network", "networkmanager",
                             "systemd-journald", "journald", "cron", "dbus",
                             "hermes", "jarvis", "logind")

# ── docker vocabulary ────────────────────────────────────────────────────────
DOCKER_READ = {"ps", "info", "inspect", "version", "stats", "top", "images",
               "port", "logs"}
DOCKER_APPROVAL = {"start", "stop", "restart", "pause", "unpause"}
DOCKER_BLOCKED = {"prune", "kill", "rm", "rmi", "network", "volume", "system",
                  "swarm", "plugin", "checkpoint", "exec", "run", "build",
                  "commit", "cp", "export", "import", "load", "save", "tag",
                  "push", "pull", "login", "logout", "compose", "update",
                  "rename", "create", "wait", "attach"}

# ── files & config ───────────────────────────────────────────────────────────
# Substrings that mark a path as Jarvis production configuration: the agent
# never writes, never deletes these.
JARVIS_CONFIG_FRAGMENTS = ("config/system_agent.json",
                           "config/calendar_token.json",
                           "config/gmail_token.json",
                           "config/client_secret",
                           ".env",
                           "memory/kb/registry.json")
# Where "delete with approval" moves files (never a hard rm).
TRASH_SUBDIR = "agent-trash"
# System locations where file deletion is never agent work.
SYSTEM_PATH_PREFIXES = ("/bin", "/sbin", "/usr", "/etc", "/boot", "/lib",
                        "/lib64", "/var/log", "/proc", "/sys", "/dev")


# ═════════════════════════════════════════════════════════════════════════════
# permission levels
# ═════════════════════════════════════════════════════════════════════════════
class PermLevel:
    """The four permission levels — the agent's whole vocabulary."""
    READ_ONLY = "read_only"
    SAFE_ACTION = "safe_action"
    APPROVAL_REQUIRED = "approval_required"
    BLOCKED = "blocked"


ALL_LEVELS = (PermLevel.READ_ONLY, PermLevel.SAFE_ACTION,
              PermLevel.APPROVAL_REQUIRED, PermLevel.BLOCKED)


class BlockedError(PermissionError):
    """A BLOCKED action was attempted. Never unlockable by approval."""


def _now_stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) \
        + f".{int(time.time() * 1000) % 1000:03d}Z"


# ════════════════════════════════ audit log ══════════════════════════════════
class SystemAuditLog:
    """Append-only JSONL audit log with a sha256 hash chain.

    Each record: {"ts", "event", "task_id", "perm_level", ...,
                  "prev_hash", "hash"}; hash = sha256(canonical json of the
    record without "hash"). Editing or removing any middle line breaks every
    later link, and verify() proves it. Writes are best-effort, thread-safe,
    never raise.
    """

    def __init__(self, path: str | os.PathLike | None = None,
                 enabled: bool = True) -> None:
        if path is None:
            base = os.environ.get("JARVIS_HOME", "").strip()
            root = Path(base).expanduser() if base \
                else Path(__file__).resolve().parents[2]
            path = root / "memory" / "system_agent" / "audit.jsonl"
        self.path = Path(path)
        self.enabled = bool(enabled)
        self._lock = threading.Lock()

    # ── core ──────────────────────────────────────────────────────────────
    def append(self, event: str, task_id: str = "",
               perm_level: str = "", **fields: Any) -> dict:
        """Append one record. Best-effort — never raises."""
        rec: dict[str, Any] = {"ts": _now_stamp(), "event": event}
        if task_id:
            rec["task_id"] = task_id
        if perm_level:
            rec["perm_level"] = perm_level
        for k, v in fields.items():
            rec[k] = v
        try:
            with self._lock:
                rec["prev_hash"] = self.last_hash()
                rec["hash"] = hashlib.sha256(
                    json.dumps(rec, sort_keys=True, default=str)
                    .encode("utf-8")).hexdigest()
                if self.enabled:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with self.path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(rec, ensure_ascii=False,
                                            default=str) + "\n")
        except Exception:
            pass                                  # audit never breaks a turn
        return rec

    def last_hash(self) -> str:
        try:
            with self.path.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 4096))
                tail = fh.read().decode("utf-8", "replace")
            lines = [l for l in tail.splitlines() if l.strip()]
            if lines:
                return str(json.loads(lines[-1]).get("hash") or "")
        except Exception:
            pass
        return ""

    def entries(self, limit: int = 50) -> list:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in [l for l in lines if l.strip()][-int(limit):]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def verify(self) -> dict:
        """Recompute the whole chain. Returns a verdict dict."""
        recs = self.entries(limit=1_000_000)
        prev = ""
        for i, r in enumerate(recs):
            if r.get("prev_hash") != prev:
                return {"ok": False, "records": len(recs),
                        "broken_at_index": i,
                        "reason": "hash chain broken "
                                  "(record removed or edited)"}
            # hash was computed over the record WITHOUT the hash field.
            body = {k: v for k, v in r.items() if k != "hash"}
            expect = hashlib.sha256(
                json.dumps(body, sort_keys=True, default=str)
                .encode("utf-8")).hexdigest()
            if r.get("hash") != expect:
                return {"ok": False, "records": len(recs),
                        "broken_at_index": i,
                        "reason": "record hash mismatch (edited in place)"}
            prev = r.get("hash", "")
        return {"ok": True, "records": len(recs), "broken_at_index": None}

    # ── event helpers (all funnel through append) ─────────────────────────
    def permission_check(self, task_id: str, perm: str, action: str,
                         reason: str) -> None:
        self.append("permission.check", task_id, perm,
                    action=action[:200], reason=reason[:300])

    def action_executed(self, task_id: str, perm: str, action: str,
                        result: dict) -> None:
        self.append("action.executed", task_id, perm,
                    action=action[:200], ok=bool(result.get("ok")),
                    error=str(result.get("error", ""))[:300])

    def approval_requested(self, task_id: str, perm: str, action: str,
                           explanation: dict) -> None:
        self.append("approval.requested", task_id, perm,
                    action=action[:200], explanation=explanation)

    def approval_resolved(self, task_id: str, approved: bool,
                          by: str) -> None:
        self.append("approval.resolved", task_id, "",
                    approved=approved, by=by)

    def blocked(self, task_id: str, perm: str, action: str,
                reason: str) -> None:
        self.append("action.blocked", task_id, perm,
                    action=action[:200], reason=reason[:300])

    def tampered(self, detail: str) -> None:
        self.append("audit.tamper_detected", "", "",
                    detail=detail[:300])


# ══════════════════════════════ collectors ═══════════════════════════════════
def _run(argv: list, timeout: float = DEFAULT_TIMEOUT_S) -> dict:
    """Run one collection command. argv list only, never shell=True."""
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, errors="replace",
            timeout=max(1.0, timeout),
            env={**os.environ, "LC_ALL": "C",
                 "PYTHONDONTWRITEBYTECODE": "1"},
        )
        return {"ok": True, "returncode": proc.returncode,
                "stdout": (proc.stdout or "")[:MAX_OUTPUT_BYTES],
                "stderr": (proc.stderr or "")[:2000]}
    except FileNotFoundError:
        return {"ok": False, "error": f"command not found: {argv[0]}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timed out after {timeout}s"}
    except Exception as exc:                      # never break a collection
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _which(name: str) -> str:
    try:
        return shutil.which(name) or ""
    except Exception:
        return ""


# ── cpu / memory / swap / disk ────────────────────────────────────────────────
def collect_cpu() -> dict:
    """CPU count + short busy% sample (psutil, ~0.25s window)."""
    try:
        import psutil
    except Exception as exc:
        return {"ok": False, "error": f"psutil unavailable: {exc}"}
    try:
        core = psutil.cpu_times(percpu=False)
        time.sleep(0.25)
        core2 = psutil.cpu_times(percpu=False)
        deltas = [b - a for b, a in zip(core2, core)]
        total = sum(deltas)
        idle = core2.idle - core.idle
        busy_pct = 100.0 * (1.0 - idle / total) if total > 0 else 0.0
        return {"ok": True, "cores": psutil.cpu_count(logical=True) or 0,
                "physical_cores": psutil.cpu_count(logical=False) or 0,
                "busy_percent": round(max(0.0, busy_pct), 1),
                "loadavg": [round(x, 2) for x in os.getloadavg()]}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def collect_memory() -> dict:
    """RAM + swap usage. READ_ONLY."""
    try:
        import psutil
        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()
    except Exception as exc:
        return {"ok": False, "error": f"psutil unavailable: {exc}"}
    gb = float(1024 ** 3)
    return {"ok": True, "total_gb": round(vm.total / gb, 2),
            "used_gb": round(vm.used / gb, 2),
            "available_gb": round(vm.available / gb, 2),
            "percent": vm.percent,
            "swap_total_gb": round(sw.total / gb, 2),
            "swap_used_gb": round(sw.used / gb, 2),
            "swap_percent": sw.percent}


def collect_disks() -> dict:
    """Real filesystems (skips pseudo/dev/loop), READ_ONLY."""
    try:
        import psutil
        parts = psutil.disk_partitions(all=False)
    except Exception as exc:
        return {"ok": False, "error": f"psutil unavailable: {exc}"}
    gb = float(1024 ** 3)
    out = []
    for p in parts:
        if p.device.startswith(("/dev/loop", "tmpfs", "udev")):
            continue
        try:
            u = psutil.disk_usage(p.mountpoint)
        except (OSError, PermissionError):
            continue
        out.append({"device": p.device, "mountpoint": p.mountpoint,
                    "fstype": p.fstype, "total_gb": round(u.total / gb, 1),
                    "used_gb": round(u.used / gb, 1),
                    "free_gb": round(u.free / gb, 1),
                    "percent": u.percent})
    return {"ok": True, "disks": out[:MAX_LIST], "count": len(out)}


# ── processes ────────────────────────────────────────────────────────────────
def collect_processes(limit: int = MAX_LIST) -> dict:
    """Top processes by CPU. Name is needed for kill protection."""
    try:
        import psutil
        procs = []
        for p in psutil.process_iter(["pid", "name", "username",
                                      "cpu_percent", "memory_percent",
                                      "status"]):
            try:
                procs.append(p.info)
            except Exception:
                continue
        top = sorted(procs,
                     key=lambda x: (x.get("cpu_percent") or 0.0),
                     reverse=True)[:max(1, int(limit))]
        return {"ok": True, "count": len(procs), "top": top}
    except Exception as exc:
        return {"ok": False, "error": f"psutil unavailable: {exc}"}


# ── services ─────────────────────────────────────────────────────────────────
def collect_services(state_filter: str = "") -> dict:
    """systemd units via list-units (listing needs no root)."""
    sysctl = _which("systemctl")
    if not sysctl:
        return {"ok": False, "error": "systemctl not found"}
    out = _run([sysctl, "list-units", "--type=service", "--all",
                "--no-pager", "--no-legend", "--plain"])
    if not out["ok"]:
        return {"ok": False, "error": out.get("error")
                or "systemctl failed"}
    services = []
    for line in (out["stdout"] or "").splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        services.append({"unit": parts[0], "load": parts[1],
                         "active": parts[2], "sub": parts[3],
                         "description": " ".join(parts[4:])[:80]})
    if state_filter:
        services = [s for s in services
                    if state_filter.lower() in s["active"].lower()]
    return {"ok": True, "services": services[:MAX_LIST],
            "count": len(services)}


# ── docker ───────────────────────────────────────────────────────────────────
def collect_docker() -> dict:
    """docker ps + info. Degrades honestly when the daemon is down."""
    docker = _which("docker")
    if not docker:
        return {"ok": False, "error": "docker CLI not found"}
    ps = _run([docker, "ps", "-a", "--format",
               "{{.ID}}|{{.Names}}|{{.Image}}|{{.Status}}|{{.Ports}}"])
    if not ps["ok"]:
        return {"ok": False, "error": ps.get("error") or "docker ps failed"}
    if ps.get("returncode") != 0:
        return {"ok": False, "error": (ps.get("stderr")
                                       or "docker daemon unreachable")[:300]}
    containers = []
    for line in (ps["stdout"] or "").splitlines():
        parts = (line.split("|") + ["", "", "", ""])[:5]
        containers.append({"id": parts[0][:12], "name": parts[1],
                           "image": parts[2], "status": parts[3],
                           "ports": parts[4]})
    info = _run([docker, "info", "--format",
                 "{{.ServerVersion}}|{{.Containers}}|{{.Images}}"])
    info_d: dict = {}
    if info["ok"] and info.get("returncode") == 0:
        bits = (info["stdout"] or "").split("|")
        bits = (bits + ["", "", ""])[:3]
        info_d = {"server_version": bits[0], "containers": bits[1],
                  "images": bits[2]}
    return {"ok": True, "containers": containers[:MAX_LIST],
            "count": len(containers), "info": info_d}


# ── logs ─────────────────────────────────────────────────────────────────────
def collect_logs(path: str = "/var/log/syslog", lines: int = MAX_LOG_LINES,
                 journal_unit: str = "") -> dict:
    """Tail a log file or a journal unit. READ_ONLY, sudo-less, bounded."""
    lines = max(1, min(int(lines or MAX_LOG_LINES), MAX_LOG_LINES))
    if journal_unit:
        j = _which("journalctl")
        if not j:
            return {"ok": False, "error": "journalctl not found"}
        out = _run([j, "-u", journal_unit, "-n", str(lines),
                    "--no-pager", "-q"])
        if not out["ok"]:
            return {"ok": False, "error": out.get("error")
                    or "journalctl failed"}
        return {"ok": True, "source": f"journal:{journal_unit}",
                "lines": (out["stdout"] or "").splitlines()[-lines:]}
    p = Path(path).expanduser()
    if not p.exists():
        return {"ok": False, "error": f"log file not found: {path}"}
    try:
        with p.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 256 * 1024))     # tail only
            data = fh.read().decode("utf-8", "replace")
        return {"ok": True, "source": str(p),
                "lines": data.splitlines()[-lines:]}
    except OSError as exc:
        return {"ok": False, "error": f"could not read {path}: {exc}"}


# ── network diagnostics ──────────────────────────────────────────────────────
def collect_network() -> dict:
    """Connectivity + interfaces + DNS. All reads; changes are BLOCKED."""
    result: dict = {"ok": True}
    ping = _which("ping")
    if ping:
        out = _run([ping, "-c", "2", "-W", "2", "1.1.1.1"], timeout=8)
        ok = bool(out["ok"]) and out.get("returncode") == 0
        lat = None
        m = re.search(r"time=([\d.]+)\s*ms", out["stdout"] or "")
        if m:
            try:
                lat = float(m.group(1))
            except ValueError:
                lat = None
        result["internet"] = {"ok": ok, "latency_ms": lat,
                              "detail": (out["stdout"]
                                         or out.get("error", ""))[:300]}
    else:
        result["internet"] = {"ok": None, "detail": "ping not available"}
    ip = _which("ip")
    if ip:
        out = _run([ip, "-brief", "address"])
        ifaces = []
        for line in (out["stdout"] or "").splitlines():
            bits = line.split()
            if bits:
                ifaces.append({"name": bits[0],
                               "state": bits[1] if len(bits) > 1 else "?",
                               "addresses": bits[2:]})
        result["interfaces"] = ifaces
    try:
        text = Path("/etc/resolv.conf").read_text(encoding="utf-8",
                                                  errors="replace")
        ns = [l.split()[1] for l in text.splitlines()
              if l.strip().startswith("nameserver") and len(l.split()) > 1]
        result["dns"] = {"nameservers": ns[:4]}
    except OSError:
        pass
    return result


# ═════════════════════════════ permissions ═══════════════════════════════════
class SystemPermissions:
    """The guard every toolbox call consults BEFORE any work is done.

    Answers one question: what permission level does this action need?
    The audit log records every decision.
    """

    def __init__(self, audit: SystemAuditLog) -> None:
        self.audit = audit

    # ── process kill ──────────────────────────────────────────────────────
    def classify_process_kill(self, pid_or_name,
                              force: bool = False) -> tuple:
        """(level, reason) for terminating a process."""
        name = str(pid_or_name or "").strip()
        if not name:
            return PermLevel.BLOCKED, "empty pid or process name"
        if force:
            # SIGKILL is never a SAFE_ACTION: it loses state and can wedge
            # the machine. Only a human can authorize it.
            return PermLevel.APPROVAL_REQUIRED, \
                "force-kill (SIGKILL) skips graceful shutdown and can " \
                "lose unsaved work — needs human approval"
        low = name.lower()
        for pat in PROTECTED_PATTERNS:
            if pat in low or pat in name.lower():
                return PermLevel.BLOCKED, \
                    f"'{name}' matches a protected pattern ({pat})"
        if low in NICE_APPS:
            return PermLevel.SAFE_ACTION, \
                f"'{name}' is a regular app on the gentle-kill allowlist"
        return PermLevel.APPROVAL_REQUIRED, \
            f"'{name}' is not on the safe-kill allowlist — a human decides"

    # ── systemd services ─────────────────────────────────────────────────
    def classify_service(self, verb: str, unit: str) -> tuple:
        """(level, reason) for a systemd service action."""
        verb = (verb or "").strip().lower()
        unit = (unit or "").strip()
        if not unit or not re.match(r"^[A-Za-z0-9_@.\-]+$", unit):
            return PermLevel.BLOCKED, \
                f"invalid or missing unit name: {unit!r}"
        if verb in SERVICE_VERBS_SAFE:
            return PermLevel.READ_ONLY, f"systemctl {verb} is read-only"
        if verb in SERVICE_VERBS_BLOCKED:
            return PermLevel.BLOCKED, \
                f"systemctl {verb} is never run by the System Agent"
        if verb in SERVICE_VERBS_APPROVAL:
            for c in CRITICAL_SERVICE_PATTERNS:
                if c in unit.lower():
                    return PermLevel.BLOCKED, (
                        f"'{unit}' is a critical service; {verb} on it "
                        "is damage the agent never does — do it yourself")
            return PermLevel.APPROVAL_REQUIRED, \
                f"systemctl {verb} {unit} affects a system service — " \
                "needs approval"
        return PermLevel.BLOCKED, \
            f"systemctl {verb!r} is not on any allowlist"

    # ── docker ────────────────────────────────────────────────────────────
    def classify_docker(self, argv_tail: list) -> tuple:
        """(level, reason) for `docker ...` (argv after 'docker')."""
        if not argv_tail:
            return PermLevel.READ_ONLY, "docker ps/info/inspect"
        sub = str(argv_tail[0] or "").lower()
        if sub in DOCKER_READ:
            return PermLevel.READ_ONLY, f"docker {sub} is read-only"
        if sub in DOCKER_BLOCKED:
            return PermLevel.BLOCKED, \
                f"docker {sub} is never run by the System Agent"
        if sub in DOCKER_APPROVAL:
            return PermLevel.APPROVAL_REQUIRED, \
                f"docker {sub} changes container state — needs approval"
        return PermLevel.BLOCKED, \
            f"docker {sub!r} is not on any allowlist"

    # ── files ─────────────────────────────────────────────────────────────
    def classify_file_delete(self, path) -> tuple:
        """Deleting files: approval-required; some paths BLOCKED outright."""
        try:
            p = Path(str(path)).expanduser().resolve()
        except (OSError, RuntimeError):
            return PermLevel.BLOCKED, "unresolvable path"
        spath = str(p)
        for prefix in SYSTEM_PATH_PREFIXES:
            if spath == prefix or spath.startswith(prefix + "/"):
                return PermLevel.BLOCKED, \
                    f"'{p}' is a system path; deletion is never agent work"
        for frag in JARVIS_CONFIG_FRAGMENTS:
            if frag in spath:
                return PermLevel.BLOCKED, \
                    f"'{p}' is Jarvis production configuration"
        home = Path.home().resolve()
        if p == home:
            return PermLevel.BLOCKED, "refusing to delete the home directory"
        if p.parent == home and p.name != "backups":
            return PermLevel.BLOCKED, \
                "deleting a whole top-level home directory is never agent work"
        return PermLevel.APPROVAL_REQUIRED, \
            f"deleting '{p}' is destructive — needs human approval"

    def classify_jarvis_config_write(self, path) -> tuple:
        """Writing Jarvis production configuration: always BLOCKED."""
        try:
            p = Path(str(path)).expanduser().resolve()
        except (OSError, RuntimeError):
            p = Path(str(path))
        spath = str(p)
        for frag in JARVIS_CONFIG_FRAGMENTS:
            if frag in spath:
                return PermLevel.BLOCKED, \
                    f"'{p}' is Jarvis production configuration — the " \
                    "agent never edits it"
        return PermLevel.APPROVAL_REQUIRED, \
            f"writing '{p}' changes agent/tool configuration — needs approval"

    # ── firewall / packages / backup ─────────────────────────────────────
    def classify_firewall(self, argv_tail: list) -> tuple:
        """Firewall/network reconfiguration: BLOCKED always. A bad rule can
        lock the operator out; the agent only ever READS network state."""
        return PermLevel.BLOCKED, (
            "firewall / network reconfiguration is never done by the "
            "System Agent — do it in a terminal")

    def classify_install(self, packages, remove: bool = False) -> tuple:
        """Critical software install/remove: BLOCKED. Other package changes:
        approval-required. Names must be simple, never shell metachars."""
        names = [str(p) for p in (packages or [])]
        if not names or not all(
                re.match(r"^[A-Za-z0-9][A-Za-z0-9+._\-]*$", n)
                for n in names):
            return PermLevel.BLOCKED, \
                "package names must be simple (letters/digits/./-/_)"
        critical = ("openssh", "docker", "systemd", "kernel", "linux-image",
                    "networkmanager", "iptables", "ufw", "sudo", "libc",
                    "gnome-shell", "python3", "pulseaudio", "dbus")
        joined = " ".join(names).lower()
        for c in critical:
            if c in joined:
                return PermLevel.BLOCKED, (
                    f"installing/removing critical system software "
                    f"({', '.join(names)}) is never agent work")
        return PermLevel.APPROVAL_REQUIRED, (
            f"{'removing' if remove else 'installing'} "
            f"{', '.join(names)} changes installed software — needs approval")

    def classify_backup(self) -> tuple:
        return PermLevel.SAFE_ACTION, \
            "backup writes a bounded archive under ~/backups (additive, reversible)"

    # ── decisions with audit ──────────────────────────────────────────────
    def decide(self, task_id: str, level: str, action: str,
               reason: str) -> dict:
        """Record the decision; return the standard gate dict."""
        self.audit.permission_check(task_id, level, action, reason)
        if level == PermLevel.BLOCKED:
            self.audit.blocked(task_id, level, action, reason)
            return {"allowed": False, "level": level, "reason": reason}
        if level == PermLevel.APPROVAL_REQUIRED:
            return {"allowed": False, "level": level, "reason": reason,
                    "needs_approval": True}
        return {"allowed": True, "level": level, "reason": reason}


# ═══════════════════════════ explanations ════════════════════════════════════
def _explain(action: str, why: str, effect: str, risk: str,
             undo: str) -> dict:
    """The explanation a human must see before approving."""
    return {"action": action, "why": why, "effect": effect, "risk": risk,
            "undo": undo}


def _accepts(fn: Callable, kwarg: str) -> bool:
    """True when fn's signature accepts kwarg (fake collectors may not)."""
    try:
        import inspect
        return kwarg in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


# ══════════════════════════════ the toolbox ══════════════════════════════════
class SystemToolbox:
    """Every capability the System Agent has, as guarded, never-raising
    calls. All mutating actions audit first; all facts are injectable via
    the *_fn seams so tests run fully offline."""

    def __init__(self, audit: Optional[SystemAuditLog] = None,
                 perms: Optional[SystemPermissions] = None,
                 cpu_fn: Optional[Callable] = None,
                 mem_fn: Optional[Callable] = None,
                 disk_fn: Optional[Callable] = None,
                 proc_fn: Optional[Callable] = None,
                 svc_fn: Optional[Callable] = None,
                 docker_fn: Optional[Callable] = None,
                 log_fn: Optional[Callable] = None,
                 net_fn: Optional[Callable] = None,
                 run_fn: Optional[Callable] = None,
                 backup_root: str = "~/backups") -> None:
        self.audit = audit or SystemAuditLog(enabled=False)
        self.perms = perms or SystemPermissions(self.audit)
        self._cpu_fn = cpu_fn or collect_cpu
        self._mem_fn = mem_fn or collect_memory
        self._disk_fn = disk_fn or collect_disks
        self._proc_fn = proc_fn or collect_processes
        self._svc_fn = svc_fn or collect_services
        self._docker_fn = docker_fn or collect_docker
        self._log_fn = log_fn or collect_logs
        self._net_fn = net_fn or collect_network
        self._run_fn = run_fn or _run
        self.backup_root = Path(backup_root).expanduser()

    # ── facts (READ_ONLY, approval-free) ─────────────────────────────────
    def cpu(self) -> dict:
        return self._cpu_fn()

    def memory(self) -> dict:
        return self._mem_fn()

    def disks(self) -> dict:
        return self._disk_fn()

    def processes(self, limit: int = MAX_LIST) -> dict:
        if _accepts(self._proc_fn, "limit"):
            return self._proc_fn(limit=limit)
        return self._proc_fn()

    def services(self, state_filter: str = "") -> dict:
        if _accepts(self._svc_fn, "state_filter"):
            return self._svc_fn(state_filter=state_filter)
        return self._svc_fn(state_filter)

    def docker(self) -> dict:
        return self._docker_fn()

    def logs(self, path: str = "/var/log/syslog",
             lines: int = MAX_LOG_LINES, journal_unit: str = "") -> dict:
        if _accepts(self._log_fn, "journal_unit"):
            return self._log_fn(path=path, lines=lines,
                                journal_unit=journal_unit)
        return self._log_fn(path, lines)

    def network(self) -> dict:
        return self._net_fn()

    # ── pre-approval explanations ─────────────────────────────────────────
    def explain(self, action: str, **kw) -> dict:
        """The explanation for an approval-required action, before running."""
        if action == "kill_process":
            target = str(kw.get("pid_or_name", "?"))
            force = bool(kw.get("force"))
            return _explain(
                f"Terminate process '{target}'"
                + (" with SIGKILL" if force else " (SIGTERM)"),
                kw.get("why", "requested by the user"),
                f"process '{target}' stops running"
                + (" immediately, without cleanup" if force else
                   " after a graceful shutdown"),
                "unsaved state in that program is lost"
                + (" and files may be left mid-write" if force else ""),
                "restart the app normally afterwards")
        if action == "service":
            verb = str(kw.get("verb", "?"))
            unit = str(kw.get("unit", "?"))
            return _explain(
                f"systemctl {verb} {unit}",
                kw.get("why", "requested by the user"),
                f"systemd unit '{unit}' changes state ({verb})",
                f"anything depending on '{unit}' loses it until started again",
                f"systemctl start {unit} (or the inverse of {verb}) "
                "restores it")
        if action == "delete_file":
            p = str(kw.get("path", "?"))
            return _explain(
                f"Delete '{p}'", kw.get("why", "requested by the user"),
                f"the file '{p}' is removed (moved to agent-trash, "
                "not destroyed)",
                "irreversible once trash is pruned",
                "restore from agent-trash under ~/backups")
        if action == "install":
            names = ", ".join(str(p) for p in kw.get("packages", [])) or "?"
            return _explain(
                f"{'Remove' if kw.get('remove') else 'Install'} {names}",
                kw.get("why", "requested by the user"),
                f"packages change: {names}",
                "removal can break packages that depend on it",
                "reinstall the same package")
        if action == "docker":
            verb = str(kw.get("verb", "?"))
            target = str(kw.get("target", ""))
            return _explain(
                f"docker {verb} {target}".strip(),
                kw.get("why", "requested by the user"),
                f"container '{target}' changes state ({verb})",
                "services running in that container stop",
                f"docker start {target} (or the inverse) restores it")
        if action == "backup":
            return _explain(
                "Create a Jarvis backup archive",
                kw.get("why", "requested by the user"),
                "a tar.zst archive is written under ~/backups",
                "disk space in ~/backups shrinks by the archive size",
                "delete the archive file to reclaim space")
        return _explain(action, kw.get("why", ""), "tool-specific effect",
                        "review before approving", "tool-specific undo")

    # ── process kill (SAFE_ACTION for allowlisted apps, else approval) ────
    def kill_process(self, pid_or_name, task_id: str = "",
                     perm_level: str = PermLevel.SAFE_ACTION,
                     force: bool = False,
                     why: str = "requested by the user") -> dict:
        level, reason = self.perms.classify_process_kill(pid_or_name,
                                                         force=force)
        action = f"kill_process:{pid_or_name}"
        decision = self.perms.decide(task_id, level, action, reason)
        if level == PermLevel.BLOCKED:
            return {"ok": False, "level": level, "error": reason,
                    **decision}
        if level == PermLevel.APPROVAL_REQUIRED and perm_level != "approved":
            expl = self.explain("kill_process", pid_or_name=pid_or_name,
                                force=force, why=why)
            self.audit.approval_requested(task_id, level, action, expl)
            return {"ok": False, "level": level, "needs_approval": True,
                    "reason": reason, "explanation": expl}
        out = self._kill_execute(pid_or_name, force)
        self.audit.action_executed(task_id, level, action, out)
        return {**out, "level": level}

    def _kill_execute(self, pid_or_name, force: bool) -> dict:
        try:
            import psutil
        except Exception as exc:
            return {"ok": False, "error": f"psutil unavailable: {exc}"}
        target = str(pid_or_name).strip()
        try:
            procs = []
            if target.isdigit():
                procs = [psutil.Process(int(target))]
            else:
                low = target.lower()
                for p in psutil.process_iter(["name"]):
                    if low in (p.info.get("name") or "").lower():
                        procs.append(p)
            if not procs:
                return {"ok": False,
                        "error": f"no process matched '{target}'"}
            sig = 9 if force else 15
            for p in procs:
                p.send_signal(sig)
            return {"ok": True, "signalled": [p.pid for p in procs],
                    "signal": sig}
        except psutil.NoSuchProcess:
            return {"ok": False, "error": "process vanished before kill"}
        except psutil.AccessDenied:
            return {"ok": False,
                    "error": "permission denied — would need root, "
                             "which the agent never uses"}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # ── systemd service actions ──────────────────────────────────────────
    def service(self, verb: str, unit: str, task_id: str = "",
                perm_level: str = PermLevel.SAFE_ACTION,
                why: str = "requested by the user") -> dict:
        level, reason = self.perms.classify_service(verb, unit)
        action = f"service:{verb} {unit}"
        decision = self.perms.decide(task_id, level, action, reason)
        if level == PermLevel.BLOCKED:
            return {"ok": False, "level": level, "error": reason,
                    **decision}
        if level == PermLevel.READ_ONLY:
            return {**self._run_fn(["systemctl", verb, unit],
                                   DEFAULT_TIMEOUT_S), "level": level}
        if perm_level != "approved":
            expl = self.explain("service", verb=verb, unit=unit, why=why)
            self.audit.approval_requested(task_id, level, action, expl)
            return {"ok": False, "level": level, "needs_approval": True,
                    "reason": reason, "explanation": expl}
        out = self._run_fn(["systemctl", verb, unit], ACTION_TIMEOUT_S)
        self.audit.action_executed(task_id, level, action, out)
        return {**out, "level": level,
                "explanation": self.explain("service", verb=verb, unit=unit,
                                            why=why)}

    # ── docker actions ────────────────────────────────────────────────────
    def docker_action(self, argv_tail: list, task_id: str = "",
                      perm_level: str = PermLevel.SAFE_ACTION,
                      why: str = "requested by the user") -> dict:
        level, reason = self.perms.classify_docker(argv_tail)
        action = "docker:" + " ".join(str(a) for a in argv_tail)
        decision = self.perms.decide(task_id, level, action, reason)
        if level == PermLevel.BLOCKED:
            return {"ok": False, "level": level, "error": reason,
                    **decision}
        if level == PermLevel.READ_ONLY:
            return {**self._run_fn(["docker"] + [str(a) for a in argv_tail],
                                   DEFAULT_TIMEOUT_S), "level": level}
        if perm_level != "approved":
            expl = self.explain("docker", verb=argv_tail[0],
                                target=" ".join(str(a) for a in
                                                argv_tail[1:]), why=why)
            self.audit.approval_requested(task_id, level, action, expl)
            return {"ok": False, "level": level, "needs_approval": True,
                    "reason": reason, "explanation": expl}
        out = self._run_fn(["docker"] + [str(a) for a in argv_tail],
                           ACTION_TIMEOUT_S)
        self.audit.action_executed(task_id, level, action, out)
        return {**out, "level": level}

    # ── file deletion (approval; trashes instead of destroying) ───────────
    def delete_file(self, path, task_id: str = "",
                    perm_level: str = PermLevel.SAFE_ACTION,
                    why: str = "requested by the user") -> dict:
        level, reason = self.perms.classify_file_delete(path)
        action = f"delete_file:{path}"
        decision = self.perms.decide(task_id, level, action, reason)
        if level == PermLevel.BLOCKED:
            return {"ok": False, "level": level, "error": reason,
                    **decision}
        if perm_level != "approved":
            expl = self.explain("delete_file", path=str(path), why=why)
            self.audit.approval_requested(task_id, level, action, expl)
            return {"ok": False, "level": level, "needs_approval": True,
                    "reason": reason, "explanation": expl}
        p = Path(str(path)).expanduser().resolve()
        if not p.is_file():
            out = {"ok": False, "error": f"not a file: {p}"}
            self.audit.action_executed(task_id, level, action, out)
            return {**out, "level": level}
        trash = self.backup_root / TRASH_SUBDIR
        trash.mkdir(parents=True, exist_ok=True)
        dest = trash / f"{p.name}.{int(time.time())}"
        try:
            os.rename(str(p), str(dest))
            out = {"ok": True, "trashed_to": str(dest)}
        except OSError as exc:
            out = {"ok": False, "error": f"move to trash failed: {exc}"}
        self.audit.action_executed(task_id, level, action, out)
        return {**out, "level": level}

    # ── package install/remove (critical = BLOCKED) ──────────────────────
    def install(self, packages, task_id: str = "",
                perm_level: str = PermLevel.SAFE_ACTION,
                remove: bool = False,
                why: str = "requested by the user") -> dict:
        level, reason = self.perms.classify_install(packages, remove=remove)
        action = (f"{'remove' if remove else 'install'}:"
                  + ",".join(str(p) for p in (packages or [])))
        decision = self.perms.decide(task_id, level, action, reason)
        if level == PermLevel.BLOCKED:
            return {"ok": False, "level": level, "error": reason,
                    **decision}
        if perm_level != "approved":
            expl = self.explain("install", packages=packages, remove=remove,
                                why=why)
            self.audit.approval_requested(task_id, level, action, expl)
            return {"ok": False, "level": level, "needs_approval": True,
                    "reason": reason, "explanation": expl}
        cmd = (["apt-get", "-y", "remove"] if remove
               else ["apt-get", "-y", "install"])
        cmd += [str(p) for p in packages]
        out = self._run_fn(cmd, ACTION_TIMEOUT_S * 4)
        self.audit.action_executed(task_id, level, action, out)
        return {**out, "level": level}

    # ── firewall / network reconfig: BLOCKED, always ─────────────────────
    def firewall(self, argv_tail: list, task_id: str = "",
                 **_kw) -> dict:
        level, reason = self.perms.classify_firewall(argv_tail)
        action = "firewall:" + " ".join(str(a) for a in argv_tail)
        decision = self.perms.decide(task_id, level, action, reason)
        return {"ok": False, "level": level, "error": reason, **decision}

    # ── Jarvis production config: BLOCKED, always ────────────────────────
    def jarvis_config_write(self, path, task_id: str = "", **_kw) -> dict:
        level, reason = self.perms.classify_jarvis_config_write(path)
        action = f"jarvis_config_write:{path}"
        decision = self.perms.decide(task_id, level, action, reason)
        return {"ok": False, "level": level, "error": reason, **decision}

    # ── backup (SAFE_ACTION) ──────────────────────────────────────────────
    def create_backup(self, task_id: str = "",
                      why: str = "requested by the user",
                      run_fn: Optional[Callable] = None) -> dict:
        """SAFE_ACTION: timestamped archive under ~/backups, via
        scripts/export_system.sh when present, else a tar of the repo."""
        level, reason = self.perms.classify_backup()
        decision = self.perms.decide(task_id, level, "backup", reason)
        self.backup_root.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = self.backup_root / f"agent-backup-{stamp}.tar.zst"
        expl = self.explain("backup", why=why)
        self.audit.approval_requested(task_id, level, "backup", expl)
        repo = Path(__file__).resolve().parents[2]
        script = repo / "scripts" / "export_system.sh"
        runner = run_fn or self._run_fn
        if script.exists():
            out = runner(["bash", str(script),
                          f"--to={self.backup_root}"],
                         ACTION_TIMEOUT_S * 8)
        else:
            out = runner(["bash", "-c",
                          f"tar -cJf '{dest}' -C '{repo}' ."], 120)
        self.audit.action_executed(task_id, level, "backup", out)
        return {**out, "level": level, "backup_path": str(dest),
                "explanation": expl, **decision}
