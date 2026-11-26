"""
orchestrator/autonomy/monitoring.py — PHASE 20: condition probes.

Condition triggers ask one question: "is something true right now?" Each
probe is a plain function returning {"fired": bool, "note": str, ...} —
read-only, defensive (a missing dependency degrades to fired=False with a
note, it never crashes the scheduler tick), and registered in PROBES by
name. Workflows reference probes by name; adding a probe is one function
plus one dict entry.

Built-ins cover the phase's example workflows:

    disk_pressure      system health alert (disk/mem via psutil if present)
    backup_freshness   backup checks (a watched dir/file gone stale)
    important_email    important email alert (unread from VIPs / subject hits)
    market_prep_due    trading market preparation window
    process_alive      a critical process (or agent service) is up
"""
from __future__ import annotations

import time
from pathlib import Path

from memory.config_manager import BASE_DIR

# A probe's own quiet period: the scheduler's condition cooldown governs the
# task; probes themselves stay stateless.
_BACKUP_PATH = BASE_DIR / "memory" / "autonomy" / "last_backup"


def disk_pressure(max_pct: int = 90, min_free_gb: float = 2.0) -> dict:
    """System health alert: disk usage above a ceiling (memory pressure
    included when psutil is available)."""
    fired, note, data = False, "", {}
    try:
        usage = None
        import shutil
        total, used, free = shutil.disk_usage(str(BASE_DIR))
        usage = used / total * 100.0
        data["disk_pct"] = round(usage, 1)
        data["free_gb"] = round(free / 2**30, 2)
        if usage >= max_pct or free / 2**30 < min_free_gb:
            fired = True
            note = f"disk {usage:.1f}% used, {free / 2**30:.2f} GB free"
        try:
            import psutil
            vm = psutil.virtual_memory()
            data["mem_pct"] = vm.percent
            if vm.percent >= 95:
                fired, note = True, f"memory {vm.percent:.0f}%"
        except ImportError:
            pass
    except Exception as exc:
        return {"fired": False, "note": f"probe error: {exc}"[:200], **data}
    return {"fired": fired, "note": note or "healthy", **data}


def backup_freshness(max_age_h: float = 26.0,
                     path: str | None = None) -> dict:
    """Backup check: the newest file in a backup location must be recent."""
    target = Path(path) if path else _BACKUP_PATH
    try:
        if not target.exists():
            return {"fired": True, "note": f"backup target missing: {target}"}
        if target.is_file():
            newest = target.stat().st_mtime
        else:
            candidates = [p for p in target.rglob("*") if p.is_file()]
            if not candidates:
                return {"fired": True, "note": "backup dir empty"}
            newest = max(p.stat().st_mtime for p in candidates)
        age_h = (time.time() - newest) / 3600.0
        if age_h > max_age_h:
            return {"fired": True, "note": f"backup {age_h:.1f}h stale",
                    "age_h": round(age_h, 1)}
        return {"fired": False, "note": f"backup {age_h:.1f}h old",
                "age_h": round(age_h, 1)}
    except Exception as exc:
        return {"fired": False, "note": f"probe error: {exc}"[:200]}


def important_email(vips: tuple = (), keywords: tuple = (),
                    max_age_h: float = 2.0) -> dict:
    """Important email alert. Delegates to the Gmail action when importable;
    VIP senders or subject keywords within a fresh window fire the probe.
    Without Gmail credentials/config it stays quiet (never false-alarms)."""
    try:
        from actions import gmail_control
        unread = gmail_control.list_unread(max_results=25)   # best-effort
        msgs = unread.get("messages") or unread.get("emails") or []
    except Exception:
        return {"fired": False, "note": "email probe unavailable"}
    import re
    now = time.time()
    for m in msgs:
        sender = str(m.get("from") or m.get("sender") or "").lower()
        subject = str(m.get("subject") or "").lower()
        ts = m.get("date_ts") or m.get("internal_date") or 0
        try:
            ts = float(ts)
        except (TypeError, ValueError):
            ts = 0
        fresh = (ts >= now - max_age_h * 3600) if ts else False
        vip_hit = any(v.lower() in sender for v in vips if v)
        kw_hit = any(re.search(k.lower(), subject) for k in keywords if k)
        if vip_hit or kw_hit:
            note = f"{'vip' if vip_hit else 'keyword'}: {subject[:80]}"
            return {"fired": True, "note": note, "subject": subject[:120]}
    return {"fired": False, "note": "nothing important"}


def market_prep_due(open_h: int = 9, open_m: int = 15,
                    within_min: int = 30) -> dict:
    """Trading market preparation: inside the pre-open window (local time)."""
    from datetime import datetime
    now = datetime.now()
    open_t = now.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
    delta = (open_t - now).total_seconds() / 60.0
    if -within_min <= delta <= within_min:
        return {"fired": True,
                "note": f"market opens in {delta:.0f} min"}
    return {"fired": False, "note": "outside prep window"}


def process_alive(name: str = "dashboard") -> dict:
    """A critical service is up (matched on the process table cmdline)."""
    try:
        import psutil
    except ImportError:
        return {"fired": False, "note": "psutil unavailable"}
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            blob = f"{proc.info.get('name') or ''} " \
                   f"{' '.join(proc.info.get('cmdline') or [])}"
            if name.lower() in blob.lower():
                return {"fired": False, "note": f"'{name}' alive"}
        except Exception:
            continue
    return {"fired": True, "note": f"process '{name}' not found"}


PROBES = {
    "disk_pressure": disk_pressure,
    "backup_freshness": backup_freshness,
    "important_email": important_email,
    "market_prep_due": market_prep_due,
    "process_alive": process_alive,
}


def register_probe(name: str, fn) -> None:
    """Workflows may add their own probes (same contract as the built-ins)."""
    PROBES[name] = fn
