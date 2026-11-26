"""
dashboard/system_api.py — Phase 8 System Control Center backend.

Four REST surfaces, every value measured at request time (N/A when a source
is unreadable — never invented):

    GET /api/rio/system/metrics        CPU · RAM · GPU · temperature · disk ·
                                       network · top processes
    GET /api/rio/system/services       RIO · Hermes · Ollama · RAG · Database ·
                                       API · Telegram · WhatsApp as
                                       ONLINE / WARNING / OFFLINE / ERROR
    GET /api/rio/system/activity       unified timeline (AI, Agents, Hermes,
                                       Files, Trading, System, Errors) with a
                                       category filter
    GET /api/rio/system/notifications  derived notification centre
                                       (Success/Warning/Error/Information ×
                                       Trading/Agent/File/System)

Service-status vocabulary (documented so a status is never ambiguous):
    ONLINE   probe confirms healthy
    OFFLINE  probe confirms not running / not listening
    WARNING  running but degraded (recent errors, partial reachability)
    ERROR    the probe itself failed or the service is in an error state
             (unreadable store, HTTP 5xx, a port that accepts but never
             answers — i.e. the wedged :8000 API)

Real-time: a single daemon watcher tails the SAME journals the REST surface
reads and pushes {type: "system_event"} over the EXISTING /ws broadcast —
the activity timeline, notifications and a 15 s metrics sample update live
with ZERO client-side polling. The client only fetches on page load and on
explicit Refresh.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse

from dashboard.rio_api import BASE_DIR, _age_s, _tail_jsonl, _tcp_up

STATUSES = ("ONLINE", "WARNING", "OFFLINE", "ERROR")
CATEGORIES = ("all", "ai", "agents", "hermes", "files", "trading", "system",
              "errors")
SEVERITIES = ("Success", "Warning", "Error", "Information")
DOMAINS = ("Trading", "Agent", "File", "System")

MEM = BASE_DIR / "memory"
# The KB moved to knowledge/ in Phase 6; memory/kb is the pre-move copy.
# Follow whichever audit trail actually exists (knowledge/ is live).
KB_AUDIT = (BASE_DIR / "knowledge" / "audit.log"
            if (BASE_DIR / "knowledge" / "audit.log").exists()
            else MEM / "kb" / "audit.log")

_SVC_TTL_S = 10.0
_svc_cache: dict = {"t": 0.0, "data": None}


# ═══════════════════════════════════════════════════════════════ metrics
def _top_processes(limit: int = 8) -> list[dict]:
    """Top processes by CPU — two real samples (prime, 250 ms, read)."""
    import psutil
    seen: dict[int, object] = {}
    for p in psutil.process_iter():
        try:
            p.cpu_percent(None)                 # prime this Process object
            seen[p.pid] = p
        except Exception:
            continue
    time.sleep(0.25)
    rows: list[dict] = []
    for pid, p in seen.items():
        try:
            rows.append({"pid": pid, "name": p.name(),
                         "cpu": round(float(p.cpu_percent(None)), 1),
                         "mem": round(float(p.memory_percent()), 1),
                         "status": p.status()})
        except Exception:
            continue
    rows.sort(key=lambda r: (-(r["cpu"] or 0.0), -(r["mem"] or 0.0)))
    return rows[:limit]


def _collect_metrics() -> dict:
    """CPU · RAM · GPU · temp · disk · network · processes — all measured."""
    out: dict = {"ok": True,
                 "ts": datetime.now().isoformat(timespec="seconds")}
    try:
        from actions.system_monitor import get_system_status
        st = get_system_status()               # cpu, ram, gpu, temp, uptime
        out["cpu"] = {"percent": st.get("cpu_percent")}
        out["ram"] = {"percent": st.get("ram_percent"),
                      "used_gb": st.get("ram_used_gb"),
                      "total_gb": st.get("ram_total_gb")}
        out["gpu"] = {"percent": st.get("gpu_percent")}     # None → N/A
        out["temp"] = {"c": st.get("cpu_temp_c")}           # None → N/A
        out["uptime"] = st.get("uptime")
        out["process_count"] = st.get("process_count")
    except Exception as exc:
        out["cpu"] = out["ram"] = out["gpu"] = out["temp"] = None
        out["error"] = f"host metrics unavailable: {type(exc).__name__}"

    try:
        import shutil
        du = shutil.disk_usage(str(BASE_DIR))
        out["disk"] = {"total_gb": round(du.total / 1024 ** 3, 1),
                       "used_gb": round(du.used / 1024 ** 3, 1),
                       "free_gb": round(du.free / 1024 ** 3, 1),
                       "percent": round(100.0 * du.used / du.total, 1)}
    except Exception:
        out["disk"] = None

    try:
        import psutil
        a = psutil.net_io_counters()
        time.sleep(0.3)                        # bounded sample (worker thread)
        b = psutil.net_io_counters()
        out["net"] = {
            "rx_kbps": round((b.bytes_recv - a.bytes_recv) * 8 / 1000 / 0.3, 1),
            "tx_kbps": round((b.bytes_sent - a.bytes_sent) * 8 / 1000 / 0.3, 1),
            "rx_gb": round(b.bytes_recv / 1024 ** 3, 2),
            "tx_gb": round(b.bytes_sent / 1024 ** 3, 2),
        }
    except Exception:
        out["net"] = None

    try:
        out["procs"] = _top_processes()
    except Exception:
        out["procs"] = None
    return out


# ══════════════════════════════════════════════════════════════ services
def _svc_rio() -> tuple[str, str]:
    """RIO = the main.py process (pidfile first, process scan as fallback)."""
    import psutil
    pid = None
    try:
        pid = int(Path("/tmp/jarvis.pid").read_text().strip())
    except Exception:
        pid = None
    if pid:
        if psutil.pid_exists(pid):
            try:
                p = psutil.Process(pid)
                cmd = " ".join(p.cmdline())
                if "main.py" in cmd:
                    up = int(time.time() - p.create_time())
                    return "ONLINE", (f"pid {pid} running {up // 3600}h"
                                      f"{up % 3600 // 60}m · dashboard serving")
                return "WARNING", (f"pid {pid} alive but not main.py "
                                   f"({cmd[:60]})")
            except psutil.NoSuchProcess:
                return "OFFLINE", f"pidfile pid {pid} no longer exists"
        return "OFFLINE", f"pidfile pid {pid} not running"
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            if "main.py" in " ".join(p.info.get("cmdline") or []):
                return "ONLINE", f"pid {p.info['pid']} (no pidfile)"
        except Exception:
            continue
    return "OFFLINE", "no RIO process (main.py) found"


def _svc_hermes(gw: tuple[bool, str]) -> tuple[str, str]:
    from actions.hermes_health import _probe_bridge
    gw_ok, gw_d = gw
    cli_ok, cli_d = _probe_bridge()
    if gw_ok and cli_ok:
        return "ONLINE", f"gateway: {gw_d} · CLI: ok"
    if gw_ok:
        return "WARNING", f"gateway up but CLI unhealthy: {cli_d}"
    return "OFFLINE", gw_d


def _svc_ollama() -> tuple[str, str]:
    if not _tcp_up(11434):
        return "OFFLINE", "not listening on :11434"
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags",
                                    timeout=1.5) as r:
            n = len((json.loads(r.read()) or {}).get("models", []))
        return "ONLINE", f"listening on :11434 · {n} models installed"
    except Exception as exc:
        return "WARNING", f"port open but API failed: {type(exc).__name__}"


def _svc_rag() -> tuple[str, str]:
    try:
        from core import rag_service as rs
        resp = rs.stats(rs.CALLER_OWNER)
        s = resp.get("stats") if isinstance(resp, dict) else None
        if not isinstance(s, dict):
            return "ERROR", "rag stats returned an unexpected shape"
        total = s.get("documents")
        if total is None and isinstance(s.get("collections"), dict):
            total = sum(s["collections"].values())
        cols = len(s.get("collections") or [])
        return "ONLINE", f"in-process service · {total} docs in {cols} collections"
    except Exception as exc:
        return "ERROR", f"rag service error: {type(exc).__name__}: {exc}"


def _svc_database() -> tuple[str, str]:
    """SQLite layer: the Hermes state DB (read-only) + the SQL-agent registry."""
    import sqlite3
    state_db = Path.home() / ".hermes" / "state.db"
    try:
        con = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=3)
        con.execute("SELECT count(*) FROM sqlite_master")
        con.close()
        db_ok, db_d = True, "state.db OK"
    except Exception as exc:
        db_ok, db_d = False, f"state.db unreadable: {type(exc).__name__}"
    reg_n = reach = 0
    reg_note = ""
    try:
        reg = json.loads((MEM / "sql" / "databases.json").read_text(
            encoding="utf-8"))
        reg_n = len(reg) if isinstance(reg, list) else 0
        reach = sum(1 for d in reg
                    if isinstance(d, dict)
                    and Path(str(d.get("source_path") or "")).exists())
        reg_note = f" · {reach}/{reg_n} registered SQL DBs reachable"
    except Exception:
        reg_note = " · SQL registry unreadable"
    if not db_ok:
        return "ERROR", f"{db_d}{reg_note}"
    if reg_n and reach == 0:
        return "WARNING", f"{db_d}{reg_note}"
    return "ONLINE", f"{db_d}{reg_note}"


def _probe_api_port(port: int, https: bool = False,
                    timeout: float = 2.0) -> tuple[str, str]:
    """One API port: HTTP answer = ONLINE, silence = ERROR (wedged), refusal
    = OFFLINE, any other probe failure = WARNING (uncertain — never guessed)."""
    url = f"{'https' if https else 'http'}://127.0.0.1:{port}/"
    ctx = None
    if https:
        import ssl
        ctx = ssl._create_unverified_context()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "rio-probe"})
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=ctx) as r:
            return "ONLINE", f":{port} HTTP {r.status}"
    except urllib.error.HTTPError as exc:
        if exc.code < 500:
            return "ONLINE", f":{port} HTTP {exc.code}"
        return "WARNING", f":{port} HTTP {exc.code}"
    except Exception as exc:
        reason = getattr(exc, "reason", exc)
        msg = str(exc).lower()
        if isinstance(reason, (TimeoutError, socket.timeout)) \
                or "timed out" in msg:
            return "ERROR", f":{port} no response in {timeout:.0f}s (wedged)"
        if isinstance(reason, ConnectionRefusedError) or "refused" in msg:
            return "OFFLINE", f":{port} connection refused"
        return "WARNING", f":{port} probe failed: {type(exc).__name__}"


def _svc_api() -> tuple[str, str]:
    """The RIO REST API on its canonical ports (8000 + TLS alias 8001)."""
    parts: list[str] = []
    results: list[str] = []
    st, d = _probe_api_port(8000)
    results.append(st)
    parts.append(d)
    if (BASE_DIR / "config" / "certs" / "jarvis.key").exists():
        st, d = _probe_api_port(8001, https=True)
        results.append(st)
        parts.append(d)
    if "ONLINE" in results:
        status = "ONLINE"
    elif "ERROR" in results:
        status = "ERROR"
    elif "WARNING" in results:
        status = "WARNING"
    else:
        status = "OFFLINE"
    return status, " · ".join(parts)


def _svc_telegram(gw: tuple[bool, str]) -> tuple[str, str]:
    gw_ok, _ = gw
    if not gw_ok:
        return "OFFLINE", "gateway not running — nothing polls Telegram"
    from actions.hermes_health import _probe_telegram
    tg_ok, tg_d = _probe_telegram()
    if tg_ok:
        return "ONLINE", tg_d
    if "but gateway logged" in tg_d:
        return "WARNING", tg_d
    if "rejected" in tg_d:
        return "ERROR", tg_d
    return "OFFLINE", tg_d


def _svc_whatsapp(gw: tuple[bool, str]) -> tuple[str, str]:
    from dashboard.hermes_api import (GATEWAY_LOG, _env_value, _log_tail,
                                      _whatsapp_state, CONNECTED, ERROR,
                                      DISCONNECTED)
    gw_ok, _ = gw
    if _env_value("WHATSAPP_ENABLED").lower() not in {"1", "true", "yes", "on"}:
        return "OFFLINE", "WHATSAPP_ENABLED not set in ~/.hermes/.env"
    if not gw_ok:
        return "OFFLINE", "gateway not running — bridge not up"
    wa_status, wa_detail = _whatsapp_state(_log_tail(GATEWAY_LOG))
    if wa_status == CONNECTED:
        return "ONLINE", wa_detail
    if wa_status == ERROR:
        return "ERROR", wa_detail
    return "OFFLINE", wa_detail          # DISCONNECTED (incl. explicit state)


def _collect_services() -> dict:
    """All eight probes in parallel, 10 s TTL so Refresh stays honest but
    does not hammer the Bot API. Returns age/cached so callers can tell."""
    now = time.time()
    cached = _svc_cache["data"]
    if cached is not None and now - _svc_cache["t"] < _SVC_TTL_S:
        data = dict(cached)
        data["cached"] = True
        data["age_s"] = round(now - _svc_cache["t"], 1)
        return data

    from actions.hermes_health import _probe_gateway
    gw = _probe_gateway()                    # shared by Hermes/Telegram/WhatsApp

    checks = {
        "RIO": _svc_rio,
        "Hermes": lambda: _svc_hermes(gw),
        "Ollama": _svc_ollama,
        "RAG": _svc_rag,
        "Database": _svc_database,
        "API": _svc_api,
        "Telegram": lambda: _svc_telegram(gw),
        "WhatsApp": lambda: _svc_whatsapp(gw),
    }
    services: list[dict] = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {name: pool.submit(fn) for name, fn in checks.items()}
        for name, fut in futures.items():
            try:
                status, detail = fut.result(timeout=25)
            except Exception as exc:                # probe must never raise
                status, detail = "ERROR", f"probe failed: {type(exc).__name__}"
            if status not in STATUSES:              # defensive: honest only
                status, detail = "ERROR", f"unexpected probe result {status!r}"
            services.append({"name": name, "status": status,
                             "detail": str(detail)[:300]})
    data = {"ok": True,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "services": services, "cached": False, "age_s": 0.0}
    _svc_cache["t"] = time.time()
    _svc_cache["data"] = data
    return data


# ═════════════════════════════════════════════════════════════ activity
# One unified timeline from the journals the system already keeps. Each
# source contributes (ts, category, text, error); nothing is synthesised.
_SOURCES: tuple[dict, ...] = (
    {"key": "llm", "cat": "ai", "path": MEM / "llm_usage.jsonl",
     "kind": "jsonl", "n": 60},
    {"key": "orch", "cat": "agents", "path": MEM / "orchestration" / "journal.jsonl",
     "kind": "jsonl", "n": 60},
    {"key": "auto", "cat": "agents", "path": MEM / "autonomy" / "audit.jsonl",
     "kind": "jsonl", "n": 30},
    {"key": "hermes", "cat": "hermes", "path": MEM / "hermes_events.jsonl",
     "kind": "jsonl", "n": 40},
    {"key": "kb", "cat": "files", "path": KB_AUDIT, "kind": "tsv", "n": 30},
    {"key": "perf", "cat": "trading", "path": MEM / "trading" / "performance.jsonl",
     "kind": "jsonl", "n": 40},
    {"key": "live", "cat": "trading", "path": MEM / "trading" / "live_trades.jsonl",
     "kind": "jsonl", "n": 10},
    {"key": "mistakes", "cat": "trading", "path": MEM / "trading" / "paper_mistakes.jsonl",
     "kind": "jsonl", "n": 10},
    {"key": "incidents", "cat": "system", "path": MEM / "incidents.jsonl",
     "kind": "jsonl", "n": 30},
)

_ERR_EVENTS = {"task.error", "plan.plan_failed", "plan.step_failed"}


def _tail_tsv(path: Path, n: int = 30, max_bytes: int = 65536) -> list[dict]:
    """Last n `ts\taction\tdetail` records from the KB audit log (bounded)."""
    try:
        if not path.exists():
            return []
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            blob = fh.read().decode("utf-8", errors="replace")
        lines = blob.splitlines()
        if size > max_bytes and lines:
            lines = lines[1:]
        out: list[dict] = []
        for line in lines[-n:]:
            parts = line.split("\t", 2)
            if len(parts) == 3:
                out.append({"ts": parts[0], "action": parts[1],
                            "detail": parts[2]})
        return out
    except Exception:
        return []


def _parse_record(key: str, rec: dict) -> tuple | None:
    """(ts, category, text, error) for one journal record — None to skip."""
    try:
        if key == "llm":
            ok = bool(rec.get("ok", True))
            return (rec.get("ts"), "ai",
                    f"model call {'OK' if ok else 'FAILED'} · "
                    f"{rec.get('provider')}/{rec.get('model')} · "
                    f"{rec.get('latency_s')}s", not ok)
        if key == "orch":
            ev = str(rec.get("event") or "")
            text = ev
            label = rec.get("task_type") or rec.get("agent")
            if label:
                text += f" · {label}"
            reason = rec.get("reason") or rec.get("detail") or rec.get("error")
            if reason:
                text += f" — {str(reason)[:90]}"
            return (rec.get("ts"), "agents", text, ev in _ERR_EVENTS)
        if key == "auto":
            ev = str(rec.get("event") or "")
            text = ev + (f" · {rec.get('workflow')}" if rec.get("workflow") else "")
            err = any(t in ev for t in ("fail", "error", "denied", "blocked",
                                        "tamper"))
            return (rec.get("ts"), "agents", text, err)
        if key == "hermes":
            stage = str(rec.get("stage") or "")
            text = stage.replace("_", " ")
            if rec.get("channel"):
                text += f" · {rec.get('channel')}"
            if rec.get("file"):
                text += f" · 📎 {rec.get('file')}"
            elif rec.get("text"):
                text += f" · “{str(rec.get('text'))[:80]}”"
            if rec.get("agent"):
                text += f" · agent {rec.get('agent')}"
            return (rec.get("ts"), "hermes", text, rec.get("ok") is False)
        if key == "kb":
            action = str(rec.get("action") or "")
            err = ("fail" in action) or ("error" in action)
            return (rec.get("ts"), "files",
                    f"{action} · {str(rec.get('detail') or '')[:100]}", err)
        if key == "perf":
            hit = "hit ✓" if rec.get("correct") else "miss ✗"
            return (rec.get("ts"), "trading",
                    f"{rec.get('symbol')} {rec.get('direction')} "
                    f"{rec.get('interval')} · score {rec.get('score')} · "
                    f"{rec.get('ret_pct')}% · {hit}", False)
        if key == "live":
            failed = bool(rec.get("failed_cancels") or rec.get("failed_flattens"))
            text = str(rec.get("reason") or "trade event")
            if rec.get("kill_switch"):
                text = f"kill switch · {text}"
            if rec.get("mode_set_to"):
                text += f" → {rec.get('mode_set_to')}"
            return (rec.get("timestamp"), "trading", text, failed)
        if key == "mistakes":
            return (rec.get("time"), "trading",
                    f"{rec.get('symbol')} {rec.get('mistake_type')} — "
                    f"{str(rec.get('mistake_description') or '')[:80]}", False)
        if key == "incidents":
            detail = (rec.get("model") or rec.get("reason")
                      or rec.get("detail") or "")
            return (rec.get("at"), "system",
                    f"{rec.get('event')}" + (f" · {str(detail)[:80]}"
                                             if detail else ""), True)
    except Exception:
        return None
    return None


def _item(key: str, ts: str, cat: str, text: str, error: bool) -> dict | None:
    age = _age_s(str(ts or ""))
    if age is None:
        return None
    return {"id": hashlib.sha1(f"{key}|{ts}|{text}".encode()).hexdigest()[:16],
            "ts": ts, "age_s": round(age, 1), "category": cat,
            "source": key, "text": str(text)[:220], "error": bool(error)}


def _collect_activity(category: str = "all", limit: int = 60) -> dict:
    items: list[dict] = []
    for src in _SOURCES:
        if src["kind"] == "tsv":
            recs = _tail_tsv(src["path"], n=src["n"])
        else:
            recs = _tail_jsonl(src["path"], n=src["n"])
        for rec in recs:
            parsed = _parse_record(src["key"], rec)
            if not parsed:
                continue
            it = _item(src["key"], *parsed)
            if it is not None:
                items.append(it)
    items.sort(key=lambda x: x["age_s"])
    counts = {c: 0 for c in CATEGORIES}
    counts["all"] = len(items)
    for it in items:
        counts[it["category"]] += 1
        if it["error"]:
            counts["errors"] += 1
    if category == "errors":
        shown = [i for i in items if i["error"]]
    elif category != "all":
        shown = [i for i in items if i["category"] == category]
    else:
        shown = items
    return {"ok": True,
            "ts": datetime.now().isoformat(timespec="seconds"),
            "counts": counts, "categories": list(CATEGORIES),
            "category": category, "items": shown[:limit]}


# ════════════════════════════════════════════════════════ notifications
# The notification centre is DERIVED from the same journals: every card
# points back at a real record (source + ts + id). Rules are explicit so a
# severity/domain is never a guess — records with no rule make no card.
_OK_STATUS = {"completed", "ok", "success", "done", "approved"}
_FAIL_STATUS = {"failed", "error", "fail", "denied", "declined"}


def _notify_record(key: str, rec: dict) -> dict | None:
    """{severity, domain, title, detail} for one record, or None (no card)."""
    try:
        if key == "incidents":
            detail = (rec.get("model") or rec.get("reason")
                      or rec.get("detail") or "")
            return {"severity": "Error", "domain": "System",
                    "title": str(rec.get("event") or "incident"),
                    "detail": str(detail)[:160]}
        if key == "llm":
            if not bool(rec.get("ok", True)):
                return {"severity": "Warning", "domain": "Agent",
                        "title": "Model call failed",
                        "detail": f"{rec.get('provider')}/{rec.get('model')}"
                                  f" after {rec.get('latency_s')}s"}
            return None
        if key == "orch":
            ev = str(rec.get("event") or "")
            if ev == "task.agent_result":
                st = str(rec.get("status") or "").lower()
                detail = f"{rec.get('agent')}: {str(rec.get('detail') or '')[:120]}"
                if st in _FAIL_STATUS:
                    return {"severity": "Error", "domain": "Agent",
                            "title": "Agent run failed", "detail": detail}
                if st in _OK_STATUS:
                    return {"severity": "Success", "domain": "Agent",
                            "title": "Agent run completed", "detail": detail}
                return None
            if ev == "task.error":
                return {"severity": "Error", "domain": "Agent",
                        "title": "Task error",
                        "detail": str(rec.get("error") or "")[:160]}
            if ev == "task.fallback":
                return {"severity": "Warning", "domain": "Agent",
                        "title": "Fallback used",
                        "detail": str(rec.get("reason") or "")[:160]}
            if ev == "plan.approved":
                return {"severity": "Success", "domain": "Agent",
                        "title": "Plan approved",
                        "detail": str(rec.get("detail") or rec.get("task_id")
                                      or "")[:160]}
            if ev == "plan.plan_failed":
                return {"severity": "Error", "domain": "Agent",
                        "title": "Plan failed",
                        "detail": str(rec.get("detail") or "")[:160]}
            if ev == "plan.awaiting_approval":
                return {"severity": "Information", "domain": "Agent",
                        "title": "Plan awaiting approval",
                        "detail": str(rec.get("detail") or "")[:160]}
            return None
        if key == "auto":
            ev = str(rec.get("event") or "")
            if any(t in ev for t in ("fail", "error", "denied", "blocked",
                                     "tamper")):
                return {"severity": "Error", "domain": "Agent",
                        "title": f"Autonomy: {ev}",
                        "detail": str(rec.get("workflow") or "")[:160]}
            if "approv" in ev and "reject" not in ev:
                return {"severity": "Success", "domain": "Agent",
                        "title": f"Autonomy: {ev}",
                        "detail": str(rec.get("workflow") or "")[:160]}
            return None
        if key == "kb":
            action = str(rec.get("action") or "")
            detail = str(rec.get("detail") or "")[:160]
            if "fail" in action or "error" in action:
                return {"severity": "Error", "domain": "File",
                        "title": f"File {action}", "detail": detail}
            if action == "ingest":
                return {"severity": "Success", "domain": "File",
                        "title": "Document indexed", "detail": detail}
            if action.startswith("ingest_"):
                return {"severity": "Information", "domain": "File",
                        "title": f"File {action}", "detail": detail}
            return None
        if key == "live":
            failed = bool(rec.get("failed_cancels") or rec.get("failed_flattens"))
            detail = f"{rec.get('reason') or ''}"
            if rec.get("mode_set_to"):
                detail += f" · mode {rec.get('mode_set_to')}"
            if failed:
                return {"severity": "Error", "domain": "Trading",
                        "title": "Trade action failed", "detail": detail[:160]}
            if rec.get("kill_switch"):
                return {"severity": "Warning", "domain": "Trading",
                        "title": "Kill switch tripped", "detail": detail[:160]}
            return {"severity": "Information", "domain": "Trading",
                    "title": "Trade event", "detail": detail[:160]}
        if key == "mistakes":
            return {"severity": "Warning", "domain": "Trading",
                    "title": f"Mistake: {rec.get('mistake_type')}",
                    "detail": f"{rec.get('symbol')} — "
                              f"{str(rec.get('mistake_description') or '')[:120]}"}
        if key == "hermes":
            if rec.get("ok") is False:
                return {"severity": "Error", "domain": "System",
                        "title": f"Pipeline {str(rec.get('stage') or '').replace('_', ' ')} failed",
                        "detail": str(rec.get("channel") or "")[:160]}
            if rec.get("file") and rec.get("stage") == "message_received":
                return {"severity": "Success", "domain": "File",
                        "title": f"Document received via {rec.get('platform') or 'bridge'}",
                        "detail": str(rec.get("file") or "")[:160]}
            return None
    except Exception:
        return None
    return None


def _notify_item(key: str, rec: dict) -> dict | None:
    rule = _notify_record(key, rec)
    if not rule:
        return None
    ts = (rec.get("at") or rec.get("ts") or rec.get("time")
          or rec.get("timestamp"))
    age = _age_s(str(ts or ""))
    if age is None:
        return None
    uid = hashlib.sha1(f"{key}|{ts}|{rule['title']}|{rule['detail']}".encode()
                       ).hexdigest()[:16]
    return {"id": uid, "ts": ts, "age_s": round(age, 1),
            "severity": rule["severity"], "domain": rule["domain"],
            "title": rule["title"], "detail": rule["detail"],
            "source": key}


def _collect_notifications(limit: int = 40) -> dict:
    items: list[dict] = []
    seen: set[str] = set()
    for src in _SOURCES:
        if src["key"] == "perf":          # backtest bars: activity only
            continue
        if src["kind"] == "tsv":
            recs = _tail_tsv(src["path"], n=src["n"])
        else:
            recs = _tail_jsonl(src["path"], n=src["n"])
        for rec in recs:
            nit = _notify_item(src["key"], rec)
            if nit and nit["id"] not in seen:
                seen.add(nit["id"])
                items.append(nit)
    items.sort(key=lambda x: x["age_s"])
    counts = {s: 0 for s in SEVERITIES}
    by_domain = {d: 0 for d in DOMAINS}
    for it in items:
        counts[it["severity"]] += 1
        by_domain[it["domain"]] += 1
    return {"ok": True,
            "ts": datetime.now().isoformat(timespec="seconds"),
            "severities": list(SEVERITIES), "domains": list(DOMAINS),
            "counts": counts, "by_domain": by_domain,
            "items": items[:limit]}


# ═══════════════════════════════════════════════════════ real-time watcher
# ONE daemon thread tails the journals the REST surface reads and pushes
# over the EXISTING /ws broadcast ({type: "system_event"}). The client
# polls nothing: timeline, notifications and a 15 s metrics sample arrive
# live; services/metrics are re-read only on page load or Refresh.
_watch_lock = threading.Lock()
_watch_state: dict = {"started": False, "offsets": {},
                      "last_metrics": 0.0}


def _broadcast(server, msg: dict) -> None:
    """Best-effort push on the server's loop; silent when no client loop."""
    loop = getattr(server, "_loop", None)
    if loop is None or loop.is_closed():
        return
    try:
        asyncio.run_coroutine_threadsafe(server.broadcast(msg), loop)
    except Exception:
        pass


def _read_new(path: Path, offset: int) -> tuple[bytes, int]:
    """Bytes from offset up to the last complete newline (never a partial)."""
    with path.open("rb") as fh:
        fh.seek(offset)
        data = fh.read()
    nl = data.rfind(b"\n")
    if nl < 0:
        return b"", offset
    return data[:nl + 1], offset + nl + 1


def _watch_loop(server) -> None:
    by_path = {src["path"]: src for src in _SOURCES}
    # start at EOF: REST serves history, the watcher serves what is NEW
    offsets: dict[Path, int] = {}
    for p in by_path:
        try:
            offsets[p] = p.stat().st_size
        except OSError:
            offsets[p] = 0
    _watch_state["offsets"] = offsets
    while True:
        time.sleep(2.0)
        try:
            for p, src in by_path.items():
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                if size < offsets[p]:            # truncated → reread from 0
                    offsets[p] = 0
                if size <= offsets[p]:
                    continue
                blob, new_off = _read_new(p, offsets[p])
                if not blob:
                    continue
                offsets[p] = new_off
                for raw in blob.decode("utf-8", errors="replace").splitlines():
                    rec = None
                    if src["kind"] == "tsv":
                        parts = raw.split("\t", 2)
                        if len(parts) == 3:
                            rec = {"ts": parts[0], "action": parts[1],
                                   "detail": parts[2]}
                    else:
                        try:
                            obj = json.loads(raw)
                            if isinstance(obj, dict):
                                rec = obj
                        except Exception:
                            rec = None
                    if rec is None:
                        continue
                    parsed = _parse_record(src["key"], rec)
                    if parsed:
                        it = _item(src["key"], *parsed)
                        if it is not None:
                            _broadcast(server, {"type": "system_event",
                                                "kind": "activity",
                                                "item": it})
                    nit = _notify_item(src["key"], rec)
                    if nit:
                        _broadcast(server, {"type": "system_event",
                                            "kind": "notification",
                                            "item": nit})
            # live metrics sample — only while somebody is connected
            if (server._clients
                    and time.time() - _watch_state["last_metrics"] >= 15.0):
                _watch_state["last_metrics"] = time.time()
                _broadcast(server, {"type": "system_event",
                                    "kind": "metrics",
                                    "metrics": _collect_metrics()})
        except Exception:
            pass                                # the watcher must never die


def start_watcher(server) -> None:
    """Idempotent: one watcher thread per process."""
    with _watch_lock:
        if _watch_state["started"]:
            return
        _watch_state["started"] = True
    threading.Thread(target=_watch_loop, args=(server,),
                     daemon=True, name="rio-system-watch").start()


# ═════════════════════════════════════════════════════════════════ REST
def _int_arg(req: Request, name: str, default: int, lo: int,
             hi: int) -> int | JSONResponse:
    raw = req.query_params.get(name, "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        return JSONResponse({"detail": f"{name} must be an integer"},
                            status_code=400)
    if val < lo or val > hi:
        return JSONResponse({"detail": f"{name} must be {lo}..{hi}"},
                            status_code=400)
    return val


def register(app, _auth) -> None:
    """Attach /api/rio/system/* to the FastAPI app (shared bearer auth)."""
    import asyncio

    @app.get("/api/rio/system/metrics", include_in_schema=False)
    async def sys_metrics(req: Request):
        if not _auth(req):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return JSONResponse(await asyncio.get_event_loop().run_in_executor(
            None, _collect_metrics))

    @app.get("/api/rio/system/services", include_in_schema=False)
    async def sys_services(req: Request):
        if not _auth(req):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return JSONResponse(await asyncio.get_event_loop().run_in_executor(
            None, _collect_services))

    @app.get("/api/rio/system/activity", include_in_schema=False)
    async def sys_activity(req: Request):
        if not _auth(req):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        category = req.query_params.get("category", "all").strip().lower()
        if category not in CATEGORIES:
            return JSONResponse(
                {"detail": f"unknown category {category!r} — "
                           f"use one of {', '.join(CATEGORIES)}"},
                status_code=400)
        limit = _int_arg(req, "limit", 60, 1, 200)
        if isinstance(limit, JSONResponse):
            return limit
        return JSONResponse(await asyncio.get_event_loop().run_in_executor(
            None, lambda: _collect_activity(category, limit)))

    @app.get("/api/rio/system/notifications", include_in_schema=False)
    async def sys_notifications(req: Request):
        if not _auth(req):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        limit = _int_arg(req, "limit", 40, 1, 100)
        if isinstance(limit, JSONResponse):
            return limit
        return JSONResponse(await asyncio.get_event_loop().run_in_executor(
            None, lambda: _collect_notifications(limit)))
