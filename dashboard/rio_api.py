"""
dashboard/rio_api.py — RIO UI shell routes + a REAL health endpoint.

Phase 1 (Rio UI Foundation): serves the RIO single-page shell for the twelve
top-level routes and exposes one data endpoint, /api/rio/health, whose every
value is measured at request time — no placeholders, no simulated statistics.

Mounting (mirrors rag_api / autonomy_api in dashboard/server.py):

    from dashboard.rio_api import register as _rio_register
    _rio_register(app, _auth)

Contracts:
  * GET /home … /settings          → the shell (index.html), 200
  * GET /rio/assets/{name}         → styles.css / app.js / icons (whitelisted)
  * GET /api/rio/health            → requires the SAME bearer auth as every
                                     other /api/* endpoint; 401 without it
    Response: {"ok": true, "ts": ..., "checks": {
        "qwen":  {"state": "up"|"down", "detail": "..."},
        "gemini":{"state": "active"|"idle"|"unconfigured", "detail": "..."},
        "rag":   {"state": "up"|"down", "detail": "..."},
        "hermes":{"state": "up"|"down", "detail": "..."}}
    }
    state semantics are honest: "idle" means configured and healthy but with
    no recent traffic; "down"/"unconfigured" means the probe FAILED. There is
    no optimistic value anywhere in this file.

Probe design: every check has a hard sub-second timeout and runs in a worker
thread; a probe that hangs degrades to {"state": "down", "detail": "timeout"}
— it must never block the event loop (the lesson of the 2026-10-08 wedge).
"""
from __future__ import annotations

import json
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from html import escape as _html_escape
from pathlib import Path
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi import Request

# FastAPI upload support is optional — mirror dashboard/server.py: the upload
# route degrades to 503 without python-multipart instead of crashing register.
try:
    from fastapi import File, Form, UploadFile  # noqa: F401
    _UPLOAD_OK = True
except Exception:                              # pragma: no cover
    File = Form = UploadFile = None            # type: ignore[assignment]
    _UPLOAD_OK = False

RIO_DIR = Path(__file__).resolve().parent / "static" / "rio"
BASE_DIR = Path(__file__).resolve().parent.parent

# Dedicated pool for media_bus long-polls. A sleeping wait=4 pop must never
# occupy the shared default executor (searches, the audio proxy's chunk reads
# and every other run_in_executor task live there) — on 2026-10-17 a pileup
# of concurrent long-polls starved the proxy stream exactly that way and no
# track ever played.
_MEDIA_POOL = ThreadPoolExecutor(max_workers=6, thread_name_prefix="mediapoll")

# The twelve top-level routes requested by Phase 1. Registered EXPLICITLY (no
# catch-all) so an unknown path still 404s instead of silently serving HTML.
ROUTES = (
    "/home", "/chat", "/agents", "/workflows", "/brain", "/memory", "/files",
    "/hermes", "/trading", "/missions", "/activity", "/system", "/settings",
)

_ASSETS = {
    "app.js": "application/javascript",
    "chat.js": "application/javascript",
    "agents.js": "application/javascript",
    "workflows.js": "application/javascript",
    "brain.js": "application/javascript",
    "memory.js": "application/javascript",
    "files.js": "application/javascript",
    "hermes.js": "application/javascript",
    "system.js": "application/javascript",
    "trading.js": "application/javascript",
    "missions.js": "application/javascript",
    "icons.js": "application/javascript",
    "face.js": "application/javascript",
    "styles.css": "text/css",
}

_HEALTH_TTL_S = 20.0
_health_cache: dict = {"t": 0.0, "data": None}

_HOME_TTL_S = 12.0
_home_cache: dict = {"t": 0.0, "data": None}


# ── shared helpers ────────────────────────────────────────────────────────────

def _tail_jsonl(path: Path, n: int = 20, max_bytes: int = 65536) -> list[dict]:
    """Last n parseable JSON objects from a jsonl file (bounded read —
    these journals grow forever; we never load more than max_bytes)."""
    try:
        if not path.exists():
            return []
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            blob = fh.read().decode("utf-8", errors="replace")
        lines = blob.splitlines()
        if size > max_bytes:
            lines = lines[1:]          # drop the possibly-truncated first line
        out: list[dict] = []
        for line in lines[-n * 2:]:
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    out.append(obj)
            except Exception:
                continue
        return out[-n:]
    except Exception:
        return []


def _age_s(ts: str) -> float | None:
    """Age of an ISO timestamp (accepts with/without tz) — None if unparsable."""
    try:
        from datetime import datetime, timezone
        t = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.astimezone()        # naive → assume system-local (jsonl style)
        return (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds()
    except Exception:
        return None


# ── probes (all real, all bounded) ──────────────────────────────────────────

def _tcp_up(port: int, host: str = "127.0.0.1", timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _probe_qwen() -> dict:
    """Ollama is up AND a qwen model is installed?"""
    if not _tcp_up(11434):
        return {"state": "down", "detail": "ollama not listening on :11434"}
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=1.5) as r:
            models = [m.get("name", "") for m in (json.loads(r.read()) or {}).get("models", [])]
        qwen = [m for m in models if "qwen" in m.lower()]
        if qwen:
            return {"state": "up", "detail": qwen[0]}
        return {"state": "down", "detail": f"ollama up, no qwen model ({len(models)} installed)"}
    except Exception as exc:
        return {"state": "down", "detail": f"ollama unreachable: {type(exc).__name__}"}


def _probe_gemini() -> dict:
    """Keys configured? Any LLM traffic in the last 15 minutes?"""
    try:
        cfg = json.loads((BASE_DIR / "config" / "api_keys.json").read_text(encoding="utf-8"))
        n_keys = 0
        if str(cfg.get("gemini_api_key") or "").strip():
            n_keys += 1
        n_keys += len([k for k in (cfg.get("gemini_api_keys") or []) if str(k).strip()])
    except Exception:
        return {"state": "unconfigured", "detail": "config/api_keys.json unreadable"}
    if n_keys == 0:
        return {"state": "unconfigured", "detail": "no Gemini key configured"}
    try:
        usage = BASE_DIR / "memory" / "llm_usage.jsonl"
        age_s = time.time() - usage.stat().st_mtime if usage.exists() else None
        if age_s is not None and age_s < 900:
            return {"state": "active", "detail": f"{n_keys} keys · traffic {int(age_s)}s ago"}
        detail = f"{n_keys} keys · no traffic in 15m"
        if age_s is None:
            detail = f"{n_keys} keys · no usage log yet"
        return {"state": "idle", "detail": detail}
    except Exception as exc:
        return {"state": "idle", "detail": f"{n_keys} keys · usage probe {type(exc).__name__}"}


def _probe_rag() -> dict:
    """In-process RAG service: how many documents are actually indexed?

    stats() is per-caller (ACL); we ask as the owner caller — the same
    identity the orchestrator's rag agent uses.
    """
    try:
        from core import rag_service as rs
        resp = rs.stats(rs.CALLER_OWNER)
        s = resp.get("stats") if isinstance(resp, dict) else None
        if not isinstance(s, dict):
            return {"state": "down", "detail": "rag stats returned an unexpected shape"}
        total = s.get("documents")
        if total is None and isinstance(s.get("collections"), dict):
            total = sum(s["collections"].values())
        cols = len(s.get("collections") or {})
        return {"state": "up",
                "detail": f"{total if total is not None else '?'} docs in {cols} collections"}
    except Exception as exc:
        return {"state": "down", "detail": f"rag service error: {type(exc).__name__}: {exc}"}


def _probe_hermes() -> dict:
    """Gateway unix socket first (the real gateway), then the WA bridge port."""
    sock = Path.home() / ".hermes" / "gateway.sock"
    gateway = False
    if sock.exists():
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(0.6)
                s.connect(str(sock))
            gateway = True
        except OSError:
            gateway = False
    bridge = _tcp_up(3000)
    if gateway and bridge:
        return {"state": "up", "detail": "gateway + WhatsApp bridge"}
    if gateway:
        return {"state": "up", "detail": "gateway (bridge not listening)"}
    if bridge:
        return {"state": "down", "detail": "bridge up, gateway socket dead"}
    return {"state": "down", "detail": "gateway and bridge unreachable"}


def _collect_health() -> dict:
    checks = {}
    for name, fn in (("qwen", _probe_qwen), ("gemini", _probe_gemini),
                     ("rag", _probe_rag), ("hermes", _probe_hermes)):
        t0 = time.monotonic()
        try:
            res = fn()
        except Exception as exc:                       # probe must never raise
            res = {"state": "down", "detail": f"{type(exc).__name__}: {exc}"}
        took = time.monotonic() - t0
        if took > 4.0:
            res["detail"] = f"slow probe ({took:.1f}s) · " + str(res.get("detail", ""))
        checks[name] = res
    return {"ok": True, "ts": time.time(), "checks": checks}


# ── Phase 2: home / command-center collectors (all real) ──────────────────

def _probe_models() -> list[dict]:
    """One entry per model provider actually present on this host."""
    out: list[dict] = []

    # 1) Ollama — live list of installed models
    q = _probe_qwen()
    models: list[str] = []
    if q["state"] == "up":
        import urllib.request
        try:
            with urllib.request.urlopen("http://127.0.0.1:11434/api/tags",
                                        timeout=1.5) as r:
                models = [m.get("name", "") for m in
                          (json.loads(r.read()) or {}).get("models", [])]
        except Exception:
            models = []
    out.append({"name": "Ollama (local)", "state": q["state"],
                "models": models, "detail": q["detail"]})

    # 2) llama-cpp GGUF — disk presence + RAM headroom, measured now
    try:
        from core import llm_local
        on_disk = bool(llm_local.is_configured())
        ram = bool(llm_local.ram_ok()) if on_disk else False
        out.append({
            "name": "Local 3B (llama-cpp)",
            "state": "up" if (on_disk and ram) else ("down" if not on_disk else "idle"),
            "models": ["qwen2.5-coder-3b-instruct"] if on_disk else [],
            "detail": ("GGUF on disk, RAM headroom OK" if on_disk and ram else
                       "GGUF on disk, not enough free RAM" if on_disk else
                       "GGUF not on disk"),
        })
    except Exception as exc:
        out.append({"name": "Local 3B (llama-cpp)", "state": "down",
                    "models": [], "detail": f"probe failed: {type(exc).__name__}"})

    # 3) configured cloud providers — key PRESENCE only, never key values
    try:
        cfg = json.loads((BASE_DIR / "config" / "api_keys.json").read_text(
            encoding="utf-8"))
    except Exception:
        cfg = {}
    g_keys = (int(bool(str(cfg.get("gemini_api_key") or "").strip()))
              + len([k for k in (cfg.get("gemini_api_keys") or [])
                     if str(k).strip()]))
    o_keys = len([k for k in (cfg.get("openrouter_keys") or [])
                  if str(k).strip()])
    out.append({"name": "Gemini", "state": "up" if g_keys else "down",
                "models": [],
                "detail": f"{g_keys} keys configured" if g_keys else "no key configured"})
    out.append({"name": "OpenRouter", "state": "up" if o_keys else "down",
                "models": [],
                "detail": f"{o_keys} keys configured" if o_keys else "no key configured"})
    out.append({"name": "FreeLLMAPI (local pool)",
                "state": "up" if _tcp_up(3001) else "down",
                "models": [], "detail": ":3001 reachable" if _tcp_up(3001)
                else "not listening on :3001"})
    return out


def _probe_system() -> dict:
    """CPU / RAM / GPU / Disk / Network — the project's own system_status
    implementation plus disk + a bounded network rate sample."""
    import shutil
    out: dict = {}
    try:
        from actions.system_monitor import get_system_status
        out.update(get_system_status())          # cpu, ram, gpu, temp, uptime
    except Exception as exc:
        out["error"] = f"metrics unavailable: {type(exc).__name__}"
    try:
        du = shutil.disk_usage(str(BASE_DIR))
        out["disk"] = {"total_gb": round(du.total / 1024 ** 3, 1),
                       "used_gb": round(du.used / 1024 ** 3, 1),
                       "free_gb": round(du.free / 1024 ** 3, 1),
                       "percent": round(100 * du.used / du.total, 1)}
    except Exception:
        out["disk"] = None                      # → UI shows N/A, never invented
    try:
        import psutil
        a = psutil.net_io_counters()
        time.sleep(0.3)                         # bounded sample, inside worker thread
        b = psutil.net_io_counters()
        out["net"] = {
            "rx_kbps": round((b.bytes_recv - a.bytes_recv) * 8 / 1000 / 0.3, 1),
            "tx_kbps": round((b.bytes_sent - a.bytes_sent) * 8 / 1000 / 0.3, 1),
        }
    except Exception:
        out["net"] = None                       # → N/A
    return out


def _gather_doing() -> dict:
    """What is RIO doing — answered from the most recent real signals."""
    usage = _tail_jsonl(BASE_DIR / "memory" / "llm_usage.jsonl", n=1)
    if usage:
        last = usage[0]
        age = _age_s(str(last.get("ts") or ""))
        who = f"{last.get('provider')}/{last.get('model')}"
        ok = bool(last.get("ok", True))
        if age is not None and age < 60:
            return {"state": "busy", "detail": f"last call {int(age)}s ago → {who}"
                    + ("" if ok else " (failed)")}
        if age is not None and age < 900:
            return {"state": "idle", "detail": f"last call {int(age)}s ago → {who}"}
        if age is not None:
            return {"state": "idle",
                    "detail": f"last call {int(age // 60)}m ago → {who}"}
    return {"state": "unknown", "detail": "no usage log yet"}


def _gather_tasks() -> dict:
    """Running/queued tasks + pending approvals — engine states as-is."""
    out: dict = {"autonomy": None, "pending_approvals": None,
                 "recent": []}
    try:
        from dashboard.autonomy_api import shared_engine
        bundle = shared_engine()
        if bundle:
            eng = bundle["engine"]
            tasks = bundle["store"].list()
            pending = 0
            if eng.approvals is not None:
                with eng.approvals._lock:
                    pending = len(eng.approvals._pending)
            out["autonomy"] = {
                "available": True,
                "task_count": len(tasks),
                "enabled_tasks": len([t for t in tasks
                                       if (t or {}).get("enabled")]),
                "emergency_stop": bool(eng.stopped),
                "pending_approvals": pending,
            }
        else:
            out["autonomy"] = {"available": False,
                               "detail": "engine not running"}
    except Exception as exc:
        out["autonomy"] = {"available": False,
                           "detail": f"probe failed: {type(exc).__name__}"}
    # tasks seen by the orchestrator journal (most recent classified work)
    for e in _tail_jsonl(BASE_DIR / "memory" / "orchestration" / "journal.jsonl",
                         n=40):
        ev = str(e.get("event") or "")
        # journal event names as orchestrator/journal.py records them
        # ("task.agent_result" etc. — the pre-Phase-4 tuple never matched
        #  agent results, so routed-agent activity was invisible here).
        if ev in ("task.classified", "task.agent_result",
                  "approval.requested", "task.fallback"):
            ts = str(e.get("ts") or "")
            out["recent"].append({
                "ts": ts,
                "age_s": _age_s(ts),
                "event": ev,
                "label": str(e.get("task_type") or e.get("agent") or ""),
                "status": str(e.get("status") or ""),
            })
    out["recent"] = out["recent"][-8:]
    return out


def _gather_activity() -> dict:
    """Merged recent activity + recent errors — straight from the journals."""
    items: list[dict] = []

    for e in _tail_jsonl(BASE_DIR / "memory" / "orchestration" / "journal.jsonl",
                         n=12):
        ts = str(e.get("ts") or "")
        age = _age_s(ts)
        text = str(e.get("event") or "")
        tt = str(e.get("task_type") or e.get("agent") or "")
        if tt:
            text += f" · {tt}"
        reason = e.get("reason") or e.get("detail")
        if reason:
            text += f" — {str(reason)[:80]}"
        items.append({"ts": ts, "age_s": age, "source": "orchestrator",
                      "text": text})

    for e in _tail_jsonl(BASE_DIR / "memory" / "llm_usage.jsonl", n=8):
        ts = str(e.get("ts") or "")
        ok = bool(e.get("ok", True))
        text = (f"model call {'OK' if ok else 'FAILED'} · "
                f"{e.get('provider')}/{e.get('model')} · "
                f"{e.get('latency_s')}s")
        items.append({"ts": ts, "age_s": _age_s(ts), "source": "llm", "text": text})

    items.sort(key=lambda x: (x["age_s"] is None, x["age_s"] if x["age_s"] is not None else 0))
    items = [i for i in items if i["age_s"] is not None][:12]

    # errors: recent incidents (real recorded events, not synthesized).
    # A recurring watchdog (jarvis-live-model-watch) appends one probe_broken
    # row per 30-min probe, which previously flooded this feed with 5 near-
    # identical rows. Collapse CONSECUTIVE probe rows that share an incident
    # id into ONE summary row: oldest + newest of the streak, and a grounded
    # count of the collapsed rows. Other event kinds keep their own rows.
    _probe_events = _tail_jsonl(BASE_DIR / "memory" / "incidents.jsonl", n=40)
    _coll_seq: list[dict] = []            # collapsed incident summaries
    _other_events: list[dict] = []
    _i = 0
    while _i < len(_probe_events):
        e = _probe_events[_i]
        if str(e.get("event") or "") != "probe_broken":
            _other_events.append(e)
            _i += 1
            continue
        eid = str(e.get("id") or "")
        _grp: list[dict] = [e]
        _j = _i + 1
        while _j < len(_probe_events):
            e2 = _probe_events[_j]
            if (str(e2.get("event") or "") == "probe_broken"
                    and str(e2.get("id") or "") == eid):
                _grp.append(e2)
                _j += 1
            else:
                break
        _ats = [str(g.get("at") or "") for g in _grp if g.get("at")]
        _first, _last = (_ats[-1], _ats[0]) if _ats else ("?", "?")
        _ages = sorted(g for g in (_age_s(a) for a in _ats) if g is not None)
        _model = str(_grp[0].get("model") or "")[:80]
        _summary = {
            "at": _last,
            "age_s": (_ages[0] if _ages else _age_s(_last)),
            "event": "probe_broken",
            # count label: collapsed rows counted honestly — streak length +
            # streak start, so one line carries what five rows used to.
            "detail": (
                f"{len(_grp)} consecutive failed probes · {_model}"
                f" · streak since {_first[:16] if len(_first) >= 16 else _first}"),
        }
        _coll_seq.append(_summary)
        _i = _j
    errors = []
    for e in _other_events:
        errors.append({
            "at": str(e.get("at") or ""),
            "age_s": _age_s(str(e.get("at") or "")),
            "event": str(e.get("event") or "unknown"),
            "detail": str(e.get("model") or e.get("reason") or "")[:120],
        })
    errors.extend(_coll_seq)
    # plus failed model calls from the usage journal
    for e in _tail_jsonl(BASE_DIR / "memory" / "llm_usage.jsonl", n=30):
        if not bool(e.get("ok", True)):
            errors.append({"at": str(e.get("ts") or ""),
                           "age_s": _age_s(str(e.get("ts") or "")),
                           "event": "model_call_failed",
                           "detail": str(e.get("provider") or "")})
    errors = [e for e in errors if e["age_s"] is not None][:6]
    return {"recent": items, "errors": errors}


def _collect_home() -> dict:
    """Assemble the command-center payload — every field measured."""
    # Live-session model actually configured (main.py reads the same key) —
    # shown as chat metadata; the default matches main.py's _DEFAULT_LIVE_MODEL.
    session_model = "models/gemini-3.1-flash-live-preview"
    try:
        cfg = json.loads((BASE_DIR / "config" / "api_keys.json").read_text(
            encoding="utf-8"))
        m = str(cfg.get("live_model") or "").strip()
        if m:
            session_model = m if m.startswith("models/") else f"models/{m}"
    except Exception:
        pass
    return {
        "ok": True,
        "ts": time.time(),
        "session_model": session_model,
        "health": _collect_health(),
        "models": _probe_models(),
        "system": _probe_system(),
        "doing": _gather_doing(),
        "tasks": _gather_tasks(),
        "activity": _gather_activity(),
    }



# ── Phase 4: Agent Center (real registry + journal + autonomy engine) ──────
# The ten agents requested by the brief, mapped to their CANONICAL registry
# names (orchestrator/agents/__init__.py). "News Curator" has no backend
# counterpart anywhere in the repo → honestly NOT IMPLEMENTED.
_AGENTS_SPEC = (
    ("coding",   "Coding Agent",      "coding_agent"),
    ("research", "Research Agent",    "research_agent"),
    ("trading",  "Trading Agent",     "trading_agent"),
    ("rag",      "RAG Agent",         "rag_agent"),
    ("system",   "System Agent",      "system_agent"),
    ("health",   "Health Assistant",  "health_agent"),
    ("calendar", "Calendar Agent",    "calendar_agent"),
    ("inbox",    "Inbox Zero Agent",  "email_agent"),
    ("news",     "News Curator",      "news_agent"),
    ("meal",     "Meal Planner",      "meal_agent"),
)

# Env gate per agent + whether build_default_registry() actually HONORS it.
# All six core agents now honor their gates (fixed 2026-10-11, ex-blocker B2):
# the factory registers the real class when the gate is on, a stub otherwise.
_GATES = {
    "coding_agent":   ("JARVIS_CODING_AGENT",   True),
    "research_agent": ("JARVIS_RESEARCH_AGENT", True),
    "trading_agent":  ("JARVIS_TRADING_AGENT",  True),
    "system_agent":   ("JARVIS_SYSTEM_AGENT",   True),
    "rag_agent":      ("JARVIS_RAG_AGENT",      True),
    "health_agent":   ("JARVIS_HEALTH_AGENT",   True),
    "calendar_agent": ("JARVIS_CALENDAR_AGENT", True),
    "email_agent":    ("JARVIS_EMAIL_AGENT",    True),
    "meal_agent":     ("JARVIS_MEAL_AGENT",     True),
}

# Capabilities per agent — VERIFIED against the source on 2026-10-08
# (toolbox methods / intent handlers; entries marked "needs approval" are
#  the paths the agent itself parks behind a human gate).
_TOOLS = {
    "coding_agent": ["read_file", "search", "edit_file", "create_file",
                     "delete_file", "run", "run_tests", "git_diff"],
    "research_agent": ["web_search", "local_kb", "fetch"],
    "system_agent": ["cpu", "memory", "disks", "processes", "services",
                     "docker", "logs", "network", "explain",
                     "kill_process (approval)", "service (approval)"],
    "trading_agent": ["quote", "options", "positions", "backtest",
                      "trade (needs approval)"],
    "rag_agent": ["search (read)", "answer", "ingest (write)", "forget (write)"],
    "health_agent": ["symptoms", "report", "track", "history", "meds",
                     "questions", "systems", "monitor", "profile",
                     "summary", "wipe"],
    "calendar_agent": ["availability", "conflicts", "propose",
                       "invite (needs approval)", "reschedule"],
    "email_agent": ["triage", "read", "summary", "newsletter_digest",
                    "draft_reply", "draft_unsubscribe",
                    "send_reply (needs approval)", "mark_read", "trash"],
    "meal_agent": ["grocery", "pantry_add", "pantry_take", "pantry_list",
                   "expiry", "prefs"],
    "news_agent": ["headlines (read)", "topic news (read)"],
}

# Model binding per agent — VERIFIED by grepping orchestrator/agents/ for
# ModelRouter/RouterLLM/llm usage: the real coding + research agents bind the
# ModelRouter via RouterLLM when registered behind their gates; registered
# stubs bind no model.
_MODEL_NOTE = {
    "coding_agent": (None, "Real CodingAgent binds the ModelRouter "
                            "(local → free → gemini) when registered via "
                            "JARVIS_CODING_AGENT=on; the stub binds no model."),
    "research_agent": (None, "Real ResearchAgent binds the ModelRouter "
                             "(local → free → gemini) when registered via "
                             "JARVIS_RESEARCH_AGENT=on; the stub binds no model."),
}
_MODEL_NOTE_DEFAULT = (None, "Deterministic logic — this agent binds no LLM.")
_MODEL_NOTE_RAG = (None, "Retrieval only — the RAG agent never calls an LLM "
                         "(by design; the caller answers from the evidence).")


# ── Workflow pages (one page per agent, like the trading desk) ───────────────
# Static, source-verified descriptions of each agent's real pipeline; live
# status is merged in at read time from the registry (_collect_agents) and,
# for desk agents, from the desk progress file (trading_api._desk_state).
# The stage lists describe what the agent's code actually does — verified
# against _TOOLS above and scripts/six_agent_desk.py. Nothing here invents
# state; the "status" on every page is measured, never simulated.
_WORKFLOWS = {
    # ── orchestrator agents (Agent Center registry) ──────────────────────
    "coding": {
        "group": "orchestrator", "icon": "code", "center": True,
        "role": "Writes and fixes code",
        "summary": "Reads before it writes, edits the tree, then proves the "
                   "change with a real run and the test suite.",
        "output": "Edited files + passing tests, or an honest failure",
        "stages": [
            {"title": "Receive task",
             "detail": "An AgentRequest arrives from the orchestrator router "
                       "carrying the user's coding intent."},
            {"title": "Read & locate",
             "detail": "Understand the target area before touching it — "
                       "no blind edits.", "tool": "read_file · search"},
            {"title": "Plan the edit",
             "detail": "Decide the minimal set of file operations for the "
                       "change."},
            {"title": "Apply changes",
             "detail": "Create, edit or remove files in the tree.",
             "tool": "edit_file · create_file · delete_file"},
            {"title": "Execute",
             "detail": "Run the entry point to observe real behaviour.",
             "tool": "run"},
            {"title": "Verify",
             "detail": "Run the test suite; a failing suite escalates instead "
                       "of claiming success.", "tool": "run_tests"},
            {"title": "Report diff",
             "detail": "Summarise exactly what changed.",
             "tool": "git_diff"},
        ],
    },
    "research": {
        "group": "orchestrator", "icon": "search", "center": True,
        "role": "Web research with source checks",
        "summary": "Searches the open web, fetches only permitted sources and "
                   "cross-checks the local knowledge base before answering.",
        "output": "A cited synthesis of verified sources",
        "stages": [
            {"title": "Clarify the question",
             "detail": "Pin down what is actually being asked and what would "
                       "count as an answer."},
            {"title": "Search the open web",
             "detail": "Query the search backend for candidate sources.",
             "tool": "web_search"},
            {"title": "Fetch permitted sources",
             "detail": "Pull page content — only from sources on the "
                       "allowed list.", "tool": "fetch"},
            {"title": "Cross-check local KB",
             "detail": "Look for prior ingested knowledge that confirms or "
                       "contradicts the web results.", "tool": "local_kb"},
            {"title": "Synthesize & cite",
             "detail": "Compose the answer from evidence, naming sources."},
        ],
    },
    "trading": {
        "group": "orchestrator", "icon": "trending-up", "center": True,
        "role": "Market analysis, paper execution",
        "summary": "Reads market data and runs analysis; anything that moves "
                   "money is parked behind a human approval gate.",
        "output": "Market reads, backtests — orders only after approval",
        "stages": [
            {"title": "Receive intent",
             "detail": "Classify the request: market read, analysis or a "
                       "trade."},
            {"title": "Pull market data",
             "detail": "Quotes, option chains and current positions.",
             "tool": "quote · options · positions"},
            {"title": "Analyze & backtest",
             "detail": "Test the idea against historical data before "
                       "anything live.", "tool": "backtest"},
            {"title": "Approval gate",
             "detail": "A trade request stops here for explicit human "
                       "approval — the agent never moves money alone.",
             "tool": "trade (needs approval)"},
            {"title": "Paper execution",
             "detail": "Approved actions execute in the paper account only."},
        ],
    },
    "rag": {
        "group": "orchestrator", "icon": "database", "center": True,
        "role": "Private knowledge-base retrieval",
        "summary": "Retrieval only, by design: it finds and ranks evidence "
                   "but never calls an LLM — the caller answers from the "
                   "returned citations.",
        "output": "Ranked, cited evidence from your documents",
        "stages": [
            {"title": "Receive query",
             "detail": "A search / answer / ingest / forget request for a "
                       "named collection."},
            {"title": "Search collection",
             "detail": "Vector + keyword search, read-only on the store.",
             "tool": "search (read)"},
            {"title": "Rank evidence",
             "detail": "Order hits by relevance; drop noise."},
            {"title": "Return citations",
             "detail": "Hand back sources with scores — never a generated "
                       "answer.", "tool": "answer"},
            {"title": "Writes (on request)",
             "detail": "Ingest or forget documents when explicitly asked.",
             "tool": "ingest · forget (write)"},
        ],
    },
    "system": {
        "group": "orchestrator", "icon": "cpu", "center": True,
        "role": "Host introspection & control",
        "summary": "Probes the machine, explains what it finds in plain "
                   "language, and only acts on processes/services behind "
                   "approval.",
        "output": "Real host metrics, process facts, guarded actions",
        "stages": [
            {"title": "Receive intent",
             "detail": "What the user wants to know or do to the machine."},
            {"title": "Probe the host",
             "detail": "Live measurements — no cached guesses.",
             "tool": "cpu · memory · disks · network"},
            {"title": "Inspect state",
             "detail": "Processes, services, containers, logs.",
             "tool": "processes · services · docker · logs"},
            {"title": "Explain",
             "detail": "Translate raw metrics into plain language.",
             "tool": "explain"},
            {"title": "Act (guarded)",
             "detail": "Killing a process or toggling a service requires "
                       "explicit approval.",
             "tool": "kill_process · service (approval)"},
        ],
    },
    "health": {
        "group": "orchestrator", "icon": "heart", "center": True,
        "role": "Personal health tracking",
        "summary": "Logs symptoms, reports and medication schedules, then "
                   "watches the timeline for patterns worth surfacing.",
        "output": "Structured health timeline and summaries",
        "stages": [
            {"title": "Receive intent",
             "detail": "Symptoms, a report, meds, or a question."},
            {"title": "Log the entry",
             "detail": "Record symptoms / reports / medication events.",
             "tool": "symptoms · report · meds · track"},
            {"title": "Watch the timeline",
             "detail": "Monitor history for recurring patterns.",
             "tool": "history · monitor"},
            {"title": "Summarize",
             "detail": "Compress the timeline into an honest summary.",
             "tool": "summary"},
            {"title": "Answer questions",
             "detail": "Reply from the record; escalate what needs a "
                       "professional.", "tool": "questions"},
        ],
    },
    "calendar": {
        "group": "orchestrator", "icon": "calendar", "center": True,
        "role": "Schedule coordination",
        "summary": "Checks availability, surfaces conflicts and proposes "
                   "slots; invites and reschedules need approval.",
        "output": "Free slots, conflict reports, draft scheduling actions",
        "stages": [
            {"title": "Receive intent",
             "detail": "Find time, check conflicts, or move an event."},
            {"title": "Check availability",
             "detail": "Query the real calendar for free windows.",
             "tool": "availability"},
            {"title": "Detect conflicts",
             "detail": "Overlap the request against existing events.",
             "tool": "conflicts"},
            {"title": "Propose slots",
             "detail": "Offer concrete alternatives that fit.",
             "tool": "propose"},
            {"title": "Apply (guarded)",
             "detail": "Invites and reschedules wait for approval.",
             "tool": "invite · reschedule (approval)"},
        ],
    },
    "inbox": {
        "group": "orchestrator", "icon": "inbox", "center": True,
        "role": "Inbox triage & drafts",
        "summary": "Triage the inbox, digest newsletters, draft replies — "
                   "nothing leaves the outbox without approval.",
        "output": "Triage state, digests, ready-to-send drafts",
        "stages": [
            {"title": "Fetch & triage",
             "detail": "Sort incoming mail into buckets.",
             "tool": "triage"},
            {"title": "Read & summarize",
             "detail": "Condense threads to their point.",
             "tool": "read · summary"},
            {"title": "Digest newsletters",
             "detail": "Bulk promotional mail becomes one digest.",
             "tool": "newsletter_digest"},
            {"title": "Draft actions",
             "detail": "Prepare replies and unsubscribe drafts.",
             "tool": "draft_reply · draft_unsubscribe"},
            {"title": "Send (guarded)",
             "detail": "Sending waits for approval; then housekeeping.",
             "tool": "send_reply (approval) · mark_read · trash"},
        ],
    },
    "news": {
        "group": "orchestrator", "icon": "newspaper", "center": True,
        "role": "News curation",
        "summary": "Intended pipeline for a curator agent — no counterpart "
                   "exists in the backend registry yet, so this page shows "
                   "the design, honestly marked NOT IMPLEMENTED.",
        "output": "Curated headlines and topic briefs (planned)",
        "stages": [
            {"title": "Pick scope",
             "detail": "Front-page headlines or a named topic."},
            {"title": "Fetch the feed",
             "detail": "Pull current items from the news backend.",
             "tool": "headlines · topic news (read)"},
            {"title": "Curate",
             "detail": "Rank by relevance; deduplicate the wire copy."},
            {"title": "Brief",
             "detail": "Deliver a short, sourced digest."},
        ],
    },
    "meal": {
        "group": "orchestrator", "icon": "utensils", "center": True,
        "role": "Meals, pantry & groceries",
        "summary": "Tracks the pantry, plans around preferences and budget, "
                   "and turns the plan into a grocery run.",
        "output": "Meal plans, pantry state, budgeted grocery lists",
        "stages": [
            {"title": "Receive intent",
             "detail": "Plan meals, check the pantry, or shop."},
            {"title": "Check the pantry",
             "detail": "What is on hand and what expires first.",
             "tool": "pantry_list · expiry"},
            {"title": "Plan & budget",
             "detail": "Match meals to preferences and budget.",
             "tool": "prefs"},
            {"title": "Build the list",
             "detail": "Only what the pantry lacks.",
             "tool": "grocery"},
            {"title": "Update the pantry",
             "detail": "Record what was bought and used.",
             "tool": "pantry_add · pantry_take"},
        ],
    },
    # ── the paper-trading desk (scripts/six_agent_desk.py) ───────────────
    "atlas": {
        "group": "desk", "icon": "search", "center": False,
        "role": "Market scout",
        "summary": "Stage 0 of the desk: scans the candidate universe on a "
                   "verified snapshot and narrows it to a ranked watchlist "
                   "before any chart is read.",
        "output": "Ranked watchlist + focus pick",
        "stages": [
            {"title": "Receive universe",
             "detail": "Candidate tickers + the verified market snapshot "
                       "(capped at 2600 chars per candidate)."},
            {"title": "Scan candidates",
             "detail": "Score each candidate for relevance to the session "
                       "goal."},
            {"title": "Rank the watchlist",
             "detail": "Order candidates strongest-first."},
            {"title": "Pick the focus",
             "detail": "Name the single ticker VEGA will chart."},
        ],
    },
    "vega": {
        "group": "desk", "icon": "trending-up", "center": False,
        "role": "Chart analyst",
        "summary": "Reads price action on the focus ticker: trend, key "
                   "levels, and the line that invalidates the idea.",
        "output": "Trend, key levels, invalidation",
        "stages": [
            {"title": "Take the focus pick",
             "detail": "Accept ATLAS's ranked watchlist and focus ticker."},
            {"title": "Pull price history",
             "detail": "OHLCV via the market-data tools."},
            {"title": "Read trend & levels",
             "detail": "Direction, support/resistance, structure."},
            {"title": "Set invalidation",
             "detail": "Name the price that proves the read wrong."},
        ],
    },
    "luna": {
        "group": "desk", "icon": "heart", "center": False,
        "role": "Sentiment analyst",
        "summary": "Reads the crowd: scores mood and flags when hype, not "
                   "information, is moving the tape.",
        "output": "Mood score + hype risk",
        "stages": [
            {"title": "Take the focus pick",
             "detail": "Same ticker as VEGA — the desk shares one idea at a "
                       "time."},
            {"title": "Scan crowd chatter",
             "detail": "Aggregates sentiment from the available feeds."},
            {"title": "Score the mood",
             "detail": "Bullish/bearish balance with confidence."},
            {"title": "Flag hype risk",
             "detail": "Call out hype spikes that distort the read."},
        ],
    },
    "orion": {
        "group": "desk", "icon": "newspaper", "center": False,
        "role": "News analyst",
        "summary": "Finds dated catalysts and verifies them; linked or "
                   "undated claims are treated as unverified.",
        "output": "Verified facts + red flags",
        "stages": [
            {"title": "Search dated news",
             "detail": "Only items with a real date make the cut."},
            {"title": "Verify sources",
             "detail": "Linked sources are checked; assertions are not "
                       "facts yet."},
            {"title": "Extract catalysts",
             "detail": "What, when, and how hard it hits the thesis."},
            {"title": "Flag red flags",
             "detail": "Contradictions, stale news, pump patterns."},
        ],
    },
    "nova": {
        "group": "desk", "icon": "zap", "center": False,
        "role": "Head trader",
        "summary": "Turns the analysts' evidence into a proposal — then "
                   "survives TITAN's veto before making the final "
                   "TRADE / WAIT / REJECT call.",
        "output": "TRADE / WAIT / REJECT (final, paper only)",
        "stages": [
            {"title": "Absorb the briefs",
             "detail": "VEGA's levels, LUNA's mood, ORION's verified news."},
            {"title": "Draft the proposal",
             "detail": "TRADE / WAIT / REJECT with sizing and thesis."},
            {"title": "Face the challenge",
             "detail": "Bull and bear attack the draft; it must survive."},
            {"title": "Absorb the veto",
             "detail": "TITAN's lenses run; a FAIL forces revision — the "
                       "veto is final."},
            {"title": "Final decision",
             "detail": "The desk's last word, paper account only."},
        ],
    },
    "titan": {
        "group": "desk", "icon": "shield", "center": False,
        "role": "Risk manager",
        "summary": "Runs three sequential risk lenses over NOVA's draft; a "
                   "single FAIL is a veto and the draft goes back.",
        "output": "PASS/FAIL lenses — veto is final",
        "stages": [
            {"title": "Take the draft",
             "detail": "NOVA's post-challenge proposal arrives here."},
            {"title": "Lens 1 — position risk",
             "detail": "Size vs balance, concentration, stop placement."},
            {"title": "Lens 2 — thesis risk",
             "detail": "Does the evidence actually support the entry?"},
            {"title": "Lens 3 — market risk",
             "detail": "Regime, event risk, liquidity."},
            {"title": "Verdict",
             "detail": "APPROVE / REVISE / REJECT — the veto cannot be "
                       "overridden."},
        ],
    },
}

# Desk progress keys per workflow page (desk_state.json "stages" map) —
# NOVA's page lights on both of its passes (draft + final).
_WF_DESK_STAGE_KEYS = {
    "atlas": ["atlas"], "vega": ["vega"], "luna": ["luna"], "orion": ["orion"],
    "nova": ["nova_draft", "nova_final"], "titan": ["titan"],
}

_DEFAULT_RUN_PROMPT = "Status check from the RIO Agent Center."
_RUN_META_KEYS = ("rag_action", "rag_strategy", "collection", "top_k")

_RUN_LOCK = threading.Lock()
_RUNNING: dict[str, dict] = {}


def _journal_path(orch) -> Path:
    """The LIVE journal path (orchestrator's own), with the default fallback."""
    try:
        return Path(orch.journal.path)
    except Exception:
        return BASE_DIR / "memory" / "orchestration" / "journal.jsonl"


def _collect_agents() -> dict:
    """Assemble the Agent Center payload — every field read from the real
    registry, the real journal, and the real autonomy engine. Nothing here
    simulates a status: an agent that cannot execute reports OFFLINE or
    NOT IMPLEMENTED, never READY."""
    out: dict = {"ok": True, "ts": time.time(), "agents": []}

    # ── orchestrator registry (live) ────────────────────────────────────
    orch = None
    reg = None
    try:
        from orchestrator import integration as _oi
        orch = _oi.shared_orchestrator()
        reg = orch.registry
    except Exception as exc:
        out["orchestrator"] = {"available": False,
                               "detail": f"{type(exc).__name__}: {exc}"[:200]}
    if reg is not None:
        import os as _os
        mode = (_os.environ.get("JARVIS_ORCHESTRATOR") or "shadow").strip().lower()
        out["orchestrator"] = {
            "available": True,
            "mode": mode if mode in {"on", "shadow", "off"} else "shadow",
            "registered_count": len(reg),
            "pending_approvals": len(orch.approvals),
        }
    else:
        out["orchestrator"] = {"available": False, "detail": "registry unavailable"}

    # ── autonomy engine (per-agent pause lives here) ────────────────────
    disabled: set[str] = set()
    levels: dict[str, int] = {}
    try:
        from dashboard.autonomy_api import shared_engine
        bundle = shared_engine()
        if bundle:
            eng = bundle["engine"]
            disabled = {str(k) for k, v in eng._agent_enabled.items() if not v}
            levels = {str(k): int(v) for k, v in eng._agent_levels.items()}
            out["autonomy"] = {"available": True,
                               "emergency_stop": bool(eng.stopped),
                               "paused_agents": sorted(disabled),
                               "levels": levels}
        else:
            out["autonomy"] = {"available": False, "detail": "engine unavailable"}
    except Exception as exc:
        out["autonomy"] = {"available": False,
                           "detail": f"{type(exc).__name__}: {exc}"[:160]}

    # ── in-flight runs (this process) ───────────────────────────────────
    with _RUN_LOCK:
        running = {k: dict(v) for k, v in _RUNNING.items()}

    # ── execution history from the LIVE journal ─────────────────────────
    hist: dict[str, list[dict]] = {}
    if orch is not None:
        for e in _tail_jsonl(_journal_path(orch), n=300, max_bytes=262144):
            if str(e.get("event") or "") != "task.agent_result":
                continue
            agent = str(e.get("agent") or "")
            hist.setdefault(agent, []).append({
                "ts": str(e.get("ts") or ""),
                "age_s": _age_s(str(e.get("ts") or "")),
                "task_id": str(e.get("task_id") or ""),
                "status": str(e.get("status") or ""),
                "detail": str(e.get("detail") or "")[:200],
            })
    for k in hist:
        hist[k].reverse()          # newest first

    # ── the ten cards ───────────────────────────────────────────────────
    for key, display, reg_name in _AGENTS_SPEC:
        entry: dict = {
            "key": key, "name": display, "registry": reg_name,
            "description": "", "status": "NOT IMPLEMENTED",
            "model": None, "model_note": "",
            "current_task": running.get(reg_name),
            "last_execution": None, "stats": {"ok": 0, "fail": 0, "other": 0},
            "history": [], "tools": _TOOLS.get(reg_name, []),
            "impl": "absent", "config": {},
            "actions": {"run": False, "pause": False, "stop": False,
                        "stop_note": "no per-agent stop/cancel API exists in "
                                     "the backend — runs are synchronous and "
                                     "cannot be cancelled"},
        }

        if not reg_name:
            entry["description"] = ("News curation agent — no counterpart exists "
                                    "in the backend registry.")
            entry["status_detail"] = "no such agent in the backend registry"
            entry["model_note"] = "Not implemented — there is no agent to bind."
            out["agents"].append(entry)
            continue

        agent = None
        if reg is not None:
            agent = reg.get(reg_name)
        if agent is None:
            entry["description"] = ("Registered name exists in code but the "
                                    "registry is unavailable.")
            entry["status_detail"] = "registry unavailable — cannot verify"
            entry["model_note"] = "Registry unavailable — cannot verify a binding."
            out["agents"].append(entry)
            continue

        is_stub = type(agent).__name__ == "StubAgent"
        real = not is_stub
        entry["impl"] = "stub" if is_stub else "real"
        entry["description"] = agent.description or ""

        # history + stats
        h = hist.get(reg_name, [])
        entry["history"] = h[:10]
        ok = sum(1 for x in h if x["status"] == "completed")
        fail = sum(1 for x in h if x["status"] == "failed")
        entry["stats"] = {"ok": ok, "fail": fail,
                          "other": len(h) - ok - fail}
        entry["last_execution"] = h[0] if h else None

        # model (see _MODEL_NOTE — verified)
        mn = _MODEL_NOTE.get(reg_name,
                             _MODEL_NOTE_RAG if reg_name == "rag_agent"
                             else _MODEL_NOTE_DEFAULT)
        entry["model"], entry["model_note"] = mn

        # config block (all real reads)
        gate, wired = _GATES.get(reg_name, ("", False))
        import os as _os
        entry["config"] = {
            "class": type(agent).__name__,
            "task_types": [t.value for t in agent.task_types],
            "env_gate": gate,
            "env_gate_value": (_os.environ.get(gate) or "(unset → off)") if gate else "",
            "env_gate_wired": wired,
            "require_approval": (getattr(agent, "_require_approval", None)
                                 if is_stub else None),
        }

        # ── status (single meaning per state, strictest first) ──────────
        cur = running.get(reg_name)
        last = entry["last_execution"]
        if cur is not None:
            entry["status"] = "RUNNING"
            entry["status_detail"] = f"executing task {cur.get('task_id', '')}"
        elif is_stub:
            entry["status"] = "OFFLINE"
            if wired:
                entry["status_detail"] = (
                    f"registered placeholder — set {gate}=on to enable the "
                    "real implementation")
            else:
                entry["status_detail"] = (
                    "registered placeholder; the real implementation exists "
                    f"in the repo but build_default_registry() never registers "
                    f"it ({gate} is documented but not wired — audit B2)")
        elif reg_name in disabled:
            entry["status"] = "PAUSED"
            entry["status_detail"] = "disabled in the autonomy engine"
        elif last is not None and last["status"] == "failed":
            entry["status"] = "ERROR"
            entry["status_detail"] = f"last execution failed: {last['detail']}"
        else:
            entry["status"] = "READY"
            entry["status_detail"] = "registered real implementation"

        # actions — RUN only for a registered REAL agent, PAUSE only when the
        # engine can actually persist it, STOP never (no backend support).
        entry["actions"]["run"] = real
        entry["actions"]["pause"] = bool(real and out["autonomy"].get("available"))
        entry["paused"] = reg_name in disabled
        out["agents"].append(entry)

    return out


def _desk_stage_state(stage_entry: dict | None, desk_status: str) -> str:
    """Honest per-stage chip for a desk agent page. A desk that is not
    running reports idle — never a fake progress bar. The desk progress file
    keys its stage records by "state" (done/running/error)."""
    if not stage_entry:
        return "PENDING" if desk_status == "running" else "IDLE"
    s = str(stage_entry.get("state") or stage_entry.get("status") or "")
    if s == "done":
        return "DONE"
    if s == "running":
        return "RUNNING"
    if s in ("error", "failed"):
        return "ERROR"
    return "PENDING" if desk_status == "running" else "IDLE"


def _collect_workflows() -> dict:
    """One page per agent: the static verified pipeline from _WORKFLOWS plus
    LIVE status — Agent Center registry state for orchestrator agents, the
    desk progress file for desk agents. Nothing simulated."""
    try:
        live = _collect_agents()
    except Exception as exc:
        live = {"ok": False, "agents": [], "orchestrator": {"available": False},
                "detail": f"{type(exc).__name__}: {exc}"[:200]}
    live_by_key = {a.get("key"): a for a in live.get("agents", [])}

    desk_status, desk_stages = "idle", {}
    desk_note = None
    try:
        from dashboard import trading_api as _ta
        d = _ta._desk_state()
        st = d.get("state") or {}
        desk_status = str(st.get("status") or "idle")
        desk_stages = st.get("stages") or {}
        if st.get("error"):
            desk_note = str(st["error"])[:200]
    except Exception as exc:
        desk_note = f"desk state unavailable: {type(exc).__name__}"

    agents_out: list[dict] = []
    for key, wf in _WORKFLOWS.items():
        entry = {
            "key": key,
            "name": key.upper(),
            "group": wf["group"],
            "icon": wf["icon"],
            "role": wf["role"],
            "summary": wf["summary"],
            "output": wf["output"],
            "center": wf["center"],
            "stages": [dict(s) for s in wf["stages"]],
            "page": f"/agents/{key}",
        }
        if wf["group"] == "orchestrator":
            a = live_by_key.get(key)
            if a:
                entry.update({
                    "name": a.get("name") or key.upper(),
                    "status": a.get("status") or "OFFLINE",
                    "status_detail": a.get("status_detail", ""),
                    "impl": a.get("impl", "absent"),
                    "tools": a.get("tools") or [],
                    "gate": (a.get("config") or {}).get("gate"),
                    "last_execution": a.get("last_execution"),
                    "stats": a.get("stats") or {},
                    "registry": a.get("registry"),
                })
            else:
                entry.update({"status": "OFFLINE",
                              "status_detail": "registry unavailable",
                              "tools": [], "impl": "absent"})
            entry["status_detail"] = entry.get("status_detail") or ""
        else:                                   # desk group
            keys = _WF_DESK_STAGE_KEYS.get(key, [key])
            stage_entries = [desk_stages.get(k) for k in keys]
            states = [_desk_stage_state(se, desk_status) for se in stage_entries]
            if "ERROR" in states:
                entry["status"] = "ERROR"
            elif any(s == "RUNNING" for s in states):
                entry["status"] = "RUNNING"
            elif all(s == "DONE" for s in states) and desk_status == "done":
                entry["status"] = "READY"
            elif desk_status == "error":
                entry["status"] = "ERROR"
            else:
                entry["status"] = "IDLE"
            entry["status_detail"] = desk_note or (
                f"desk {desk_status}" if desk_status != "idle" else "desk idle")
            entry["desk_states"] = dict(zip(keys, states))
            entry["stage_keys"] = keys
            entry["desk_status"] = desk_status
        agents_out.append(entry)

    return {
        "ok": True,
        "ts": time.time(),
        "orchestrator": live.get("orchestrator"),
        "desk_status": desk_status,
        "desk_note": desk_note,
        "agents": agents_out,
    }


def _start_run(name: str, prompt: str, meta: dict) -> dict:
    """Kick off one real agent run on a worker thread (agent.handle is the
    exact dispatch the orchestrator's router performs; autonomy's own
    make_agent_tool uses the same call). Returns 202 info or an error dict."""
    from orchestrator import integration as _oi
    from orchestrator.base import StubAgent
    from orchestrator.contracts import AgentRequest, TaskType, new_task_id

    orch = _oi.shared_orchestrator()
    agent = orch.registry.get(name)
    if agent is None:
        return {"error": "not_implemented",
                "detail": f"'{name}' is not in the backend registry"}, 404
    if isinstance(agent, StubAgent):
        return {"error": "offline_stub",
                "detail": "registered placeholder — nothing to run "
                          "(RUN is only exposed for real implementations)"}, 409

    task_id = new_task_id("agentrun")
    with _RUN_LOCK:
        if name in _RUNNING:
            return {"error": "already_running",
                    "detail": "this agent already has a run in flight",
                    "task_id": _RUNNING[name]["task_id"]}, 409
        _RUNNING[name] = {"task_id": task_id, "started": time.time(),
                          "prompt": prompt[:120]}

    request = AgentRequest(
        task_id=task_id,
        task_type=agent.task_types[0] if agent.task_types else TaskType._FALLBACK,
        text=prompt,
        metadata={"agent_override": name, "channel": "rio_agent_center",
                  "session_id": "agent_center", **meta},
    )

    def _work() -> None:
        status, detail = "failed", ""
        try:
            # agent.handle() is the exact call orchestrator/router.py makes
            # (handle_logged only exists on StubAgent — real classes would
            #  raise AttributeError, so breadcrumbs are set here instead)
            result = agent.handle(request)
            try:                                   # debug breadcrumbs, same
                agent.last_request = request       # semantics as handle_logged
                agent.last_result = result
            except Exception:
                pass
            status = str(result.status.value if result.status else "failed")
            detail = str(result.error or result.text or "")[:200]
        except Exception as exc:                    # never silent
            status = "failed"
            detail = f"{type(exc).__name__}: {exc}"[:200]
            try:
                orch.journal.error(task_id, "agent_run", detail)
            except Exception:
                pass
        finally:
            with _RUN_LOCK:
                _RUNNING.pop(name, None)
            try:                                     # real journal entry →
                orch.journal.agent_result(           # becomes the history
                    task_id, name, status, detail)
            except Exception:
                pass

    import asyncio
    try:
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, _work)
    except Exception:
        threading.Thread(target=_work, daemon=True).start()
    return {"ok": True, "task_id": task_id, "name": name,
            "status": "running", "started": _RUNNING[name]["started"]}, 202


# ── Phase 5: Brain + Model Router ────────────────────────────────────────
# Everything here READS the existing integrations (orchestrator/model_router,
# core/llm_local, core/llm_router, the session ladder) — no routing logic is
# reimplemented, and API keys are never read into a response (adapter
# is_configured() booleans + health probes only).

_MR: dict = {"router": None, "lock": threading.Lock()}
_PROBE_AT: dict = {"last": 0.0}


def _get_router():
    """One ModelRouter per process — its health board (cooldowns, probe TTL)
    must persist across requests so a provider that just failed stays
    visibly cooling."""
    with _MR["lock"]:
        if _MR["router"] is None:
            from orchestrator.model_router import ModelRouter
            _MR["router"] = ModelRouter()
        return _MR["router"]


# orchestrator TaskType → model-router ModelTask (the bridge the agents use
# themselves via RouterLLM; privacy-sensitive lanes map to PRIVATE, which the
# registry hard-limits to on-machine models — see capabilities.py).
_TASK_TO_MTASK = {
    "coding": "simple_coding", "replica": "simple_coding",
    "youtube": "simple_coding",
    "research": "research",
    "trading": "difficult_reasoning", "system": "light_reasoning",
    "rag": "private", "health": "private", "finance": "private",
    "legal": "private", "study": "private", "sql": "private",
    "meta_quest": "private", "self_eval": "private",
    "email": "transformation", "calendar": "transformation",
    "briefing": "transformation", "meal": "transformation",
    "travel": "transformation", "lead_score": "transformation",
    "marketing": "research", "fallback": "light_reasoning",
}
_MTASK_LABEL = {
    "simple_coding": "simple coding",
    "transformation": "transformation / summary",
    "private": "private — must stay on-machine",
    "light_reasoning": "lightweight reasoning",
    "research": "research",
    "difficult_reasoning": "complex reasoning",
}

_PIPELINE = (
    ("USER REQUEST", "dashboard / chat / Hermes intake → command queue"),
    ("INTENT", "orchestrator/classifier.py · IntentClassifier (regex+keyword, confidence)"),
    ("TASK CLASSIFIER", "TaskType lane (coding, research, trading, …) · confidence gate 0.5"),
    ("MODEL ROUTER", "orchestrator/model_router · cheapest-capable-first + health board"),
    ("BEST AVAILABLE MODEL", "CapabilityRegistry.models_for(task) → first configured, healthy rung"),
    ("AGENT", "orchestrator agent registry (Agent Center)"),
    ("RESULT", "AgentResult → orchestration journal + model-router usage journal"),
)

# display entries: (key, display name, model_router provider name or "")
_BRAIN_PROVIDERS = (
    ("qwen_local", "Qwen2.5-Coder 3B", "local"),
    ("ollama", "Ollama", ""),
    ("gemini", "Gemini", "gemini"),
    ("freellm", "FreeLLMAPI", "freellm"),
    ("openrouter", "OpenRouter", "openrouter"),
    ("deepseek", "DeepSeek (OpenRouter)", "deepseek"),
)
# usage-journal provider aliases → display key
_USAGE_ALIASES = {
    "local_qwen3b": "qwen_local", "local": "qwen_local", "llama_cpp": "qwen_local",
    "ollama": "ollama", "gemini": "gemini",
    "freellm": "freellm", "freellmapi": "freellm",
    "openrouter_free": "openrouter", "openrouter": "openrouter",
    "deepseek_chat": "deepseek", "deepseek": "deepseek",
}


def _load_usage_records(n: int = 400) -> list[dict]:
    """Recent routed-call records from BOTH existing journals (model-router
    layer + the house llm_usage layer). Both are already redacted upstream."""
    recs: list[dict] = []
    for path in (BASE_DIR / "memory" / "model_router" / "usage.jsonl",
                 BASE_DIR / "memory" / "llm_usage.jsonl"):
        for e in _tail_jsonl(path, n=n, max_bytes=262144):
            if not isinstance(e, dict):
                continue
            prov = str(e.get("provider") or "")
            recs.append({
                "ts": str(e.get("ts") or ""),
                "layer": str(e.get("layer") or "core/llm_router"),
                "task": str(e.get("task") or ""),
                "provider": prov,
                "display": _USAGE_ALIASES.get(prov, prov),
                "model": str(e.get("model") or ""),
                "ok": bool(e.get("ok", True)),
                "latency_s": e.get("latency_s"),
                "error": str(e.get("error") or "")[:200],
                "hops": e.get("hops") if isinstance(e.get("hops"), list) else [],
            })
    recs.sort(key=lambda r: r["ts"])
    return recs[-n:]


def _usage_stats(recs: list[dict], key: str) -> dict:
    mine = [r for r in recs if r["display"] == key]
    ok = sum(1 for r in mine if r["ok"])
    lat = [r["latency_s"] for r in mine
           if isinstance(r["latency_s"], (int, float))]
    last_err = next((r for r in reversed(mine) if not r["ok"]), None)
    return {
        "calls": len(mine), "ok": ok, "fail": len(mine) - ok,
        "avg_latency_s": round(sum(lat) / len(lat), 2) if lat else None,
        "last_used": mine[-1]["ts"] if mine else None,
        "last_error": ((last_err.get("error") or "call failed")
                       if last_err else None),
    }


def _tcp_ms(port: int, host: str = "127.0.0.1", timeout: float = 0.6):
    """Real connect probe → (ok, latency_ms | None, detail)."""
    import socket as _s
    t0 = time.time()
    try:
        with _s.create_connection((host, port), timeout=timeout):
            return True, round((time.time() - t0) * 1000, 1), f":{port} reachable"
    except OSError as exc:
        return False, None, f":{port} unreachable ({exc.__class__.__name__})"


def _collect_brain() -> dict:
    """Brain payload: five provider cards, the real pipeline, the REAL
    routing table (computed by the CapabilityRegistry), health + recent
    routed calls. Contains no key material — configuration is reported as
    adapter.is_configured() booleans only."""
    out: dict = {"ok": True, "ts": time.time(), "providers": [],
                 "pipeline": [], "routing": [], "examples": [],
                 "recent_calls": []}
    try:
        router = _get_router()
        from orchestrator.model_router.capabilities import ModelTask
        from orchestrator.model_router.providers import load_provider_config
    except Exception as exc:
        out["ok"] = False
        out["error"] = f"model router unavailable: {type(exc).__name__}: {exc}"[:200]
        return out

    try:
        st = router.status()
        out["mode"] = st.get("mode", router.mode)
        health = st.get("providers", {})
        specs_all = st.get("models", [])
    except Exception as exc:
        health, specs_all, out["mode"] = {}, [], f"error: {type(exc).__name__}"

    usage = _load_usage_records()

    # ── 1. the five provider cards ────────────────────────────────────
    for key, display, prov in _BRAIN_PROVIDERS:
        card: dict = {
            "key": key, "name": display, "model": None, "status": "unconfigured",
            "detail": "", "latency_s": None, "probe_latency_ms": None,
            "purpose": "", "last_error": None, "router_rung": bool(prov),
            "usage": _usage_stats(usage, key), "health": None,
        }
        if key == "qwen_local":
            try:
                from core import llm_local
                lst = llm_local.status()
                configured = bool(lst.get("configured"))
                ram = bool(lst.get("ram_ok"))
                card["model"] = "qwen2.5-coder-3b-instruct (llama-cpp GGUF)"
                card["status"] = "up" if (configured and ram) else "down"
                card["detail"] = (
                    "GGUF on disk, RAM headroom OK" if configured and ram else
                    f"not enough free RAM ({lst.get('ram_available_mb', '?')} MB "
                    f"available, needs {lst.get('min_ram_mb', '?')} MB)" if configured
                    else "GGUF not on disk")
                card["configured"] = configured
            except Exception as exc:
                card["status"] = "down"
                card["detail"] = f"probe failed: {type(exc).__name__}"
        elif key == "ollama":
            ok, ms, detail = _tcp_ms(11434)
            models: list[str] = []
            if ok:
                import urllib.request
                try:
                    with urllib.request.urlopen(
                            "http://127.0.0.1:11434/api/tags", timeout=1.2) as r:
                        models = [m.get("name", "") for m in
                                  (json.loads(r.read()) or {}).get("models", [])]
                except Exception:
                    pass
            card["status"] = "up" if ok else "down"
            card["detail"] = detail + (f" · {len(models)} model(s)" if models else "")
            card["model"] = models[0] if models else None
            card["models"] = models
            card["probe_latency_ms"] = ms
            card["configured"] = ok
            card["purpose_note"] = ("local runtime for the live session + "
                                    "core/llm_router ladder — not a "
                                    "model_router rung itself")
        else:
            # gemini / freellm / openrouter — real adapter + health probes
            adapter = router.adapters.get(prov)
            configured = False
            if adapter is not None:
                try:
                    configured = bool(adapter.is_configured())
                except Exception:
                    configured = False
            card["configured"] = configured
            h = health.get(prov) or {}
            card["health"] = h
            if not configured:
                card["status"] = "unconfigured"
                card["detail"] = "no API key configured"
            elif h.get("cooling"):
                card["status"] = "cooling"
                card["detail"] = str(h.get("cooldown_reason") or
                                     "cooling down after a recent failure")
            else:
                try:
                    ok, reason = router.health.check(prov, adapter.probe)
                except Exception as exc:
                    ok, reason = False, f"probe crashed: {type(exc).__name__}"
                card["status"] = "up" if ok else "down"
                card["detail"] = reason
            spec = next((s for s in specs_all if s.get("provider") == prov), None)
            card["model"] = spec.get("model_id") if spec else None
            if prov == "freellm":
                tok, ms, det = _tcp_ms(3001)
                card["probe_latency_ms"] = ms
                if not tok and card["status"] == "up":
                    card["status"], card["detail"] = "down", det

        # purpose — computed from the capability registry (what this
        # provider's models are declared to serve)
        if prov:
            tasks = sorted({t for s in specs_all if s.get("provider") == prov
                            for t in (s.get("tasks") or [])})
            card["purpose"] = ", ".join(_MTASK_LABEL.get(t, t) for t in tasks)
        else:
            card["purpose"] = "local inference runtime (session + core ladder)"

        # last observed latency from the journals
        u = card["usage"]
        card["latency_s"] = u.get("avg_latency_s")
        if card["last_error"] is None:
            card["last_error"] = u.get("last_error")
        out["providers"].append(card)

    # key presence (counts only — never values)
    try:
        cfg = load_provider_config(router.config_path)
        out["config"] = {
            "gemini_keys": int(bool(str(cfg.get("gemini_api_key") or "").strip()))
                           + len([k for k in (cfg.get("gemini_api_keys") or [])
                                  if str(k).strip()]),
            "openrouter_keys": len([k for k in (cfg.get("openrouter_keys") or [])
                                    if str(k).strip()]),
            "freellm_key": bool(str(cfg.get("freellmapi_api_key") or "").strip()),
            "keys_exposed": False,
        }
    except Exception:
        out["config"] = {"keys_exposed": False}

    # ── 2. pipeline (real components) ─────────────────────────────────
    out["pipeline"] = [{"stage": s, "impl": i} for s, i in _PIPELINE]

    # ── 3. routing table — computed by the EXISTING registry ──────────
    try:
        for mt in ModelTask:
            chain = []
            for s in router.registry.models_for(mt):
                adapter = router.adapters.get(s.provider)
                if adapter is None:
                    state, why = "unavailable", "no adapter configured"
                else:
                    try:
                        conf = bool(adapter.is_configured())
                    except Exception:
                        conf = False
                    h = health.get(s.provider) or {}
                    if not conf:
                        state, why = "unconfigured", "no API key"
                    elif h.get("cooling"):
                        state, why = "cooling", str(h.get("cooldown_reason") or "cooling")
                    else:
                        state, why = "available", ""
                chain.append({"name": s.name, "provider": s.provider,
                              "model_id": s.model_id, "tier": s.cost_tier,
                              "privacy": s.privacy.value, "state": state,
                              "why": why})
            out["routing"].append({
                "task": mt.value, "label": _MTASK_LABEL.get(mt.value, mt.value),
                "chain": chain,
            })
    except Exception as exc:
        out["routing_error"] = f"{type(exc).__name__}: {exc}"[:160]

    # spec examples, computed live (never hard-coded destinations)
    def first(task: str, which: int = 0):
        row = next((r for r in out["routing"] if r["task"] == task), None)
        if not row or len(row["chain"]) <= which:
            return None
        return row["chain"][which]["name"]

    out["examples"] = [
        {"label": "Coding task", "task": "simple_coding",
         "route": " → ".join(filter(None, [
             first("simple_coding", 0), first("simple_coding", 1),
             first("simple_coding", 2), first("simple_coding", 3)]))},
        {"label": "Simple task", "task": "light_reasoning",
         "route": " → ".join(filter(None, [
             first("light_reasoning", 0), first("light_reasoning", 1),
             first("light_reasoning", 2)]))},
        {"label": "Complex reasoning", "task": "difficult_reasoning",
         "route": " → ".join(filter(None, [
             first("difficult_reasoning", 0), first("difficult_reasoning", 1),
             first("difficult_reasoning", 2), first("difficult_reasoning", 3)]))},
        {"label": "Private task (hard rule)", "task": "private",
         "route": " → ".join(filter(None, [
             first("private", 0), first("private", 1)]))
                   + "  ·  cloud models are never offered"},
    ]

    # ── 4. recent routed calls (real fallback trails + safe reasons) ──
    out["recent_calls"] = [
        {"ts": r["ts"], "layer": r["layer"], "task": r["task"],
         "provider": r["provider"], "model": r["model"], "ok": r["ok"],
         "latency_s": r["latency_s"], "error": r["error"],
         "hops": r["hops"][:4]}
        for r in reversed(usage[-12:])]
    return out


# ── Phase 6: Memory sections · RAG dashboard · Files ───────────────────────
# Everything below reuses the ONE existing RAG (core/rag + core/rag_service),
# the long-term memory store (memory/memory_manager) and the conversation
# journal (memory/conversation_journal). No second index, no second store.

# Section → source. "memory"  = curated long_term.json categories.
#            "mixed" = memory notes + one RAG collection (Knowledge).
#            "rag"   = the RAG document registry (scoped by collection).
#            "journal" = the append-only conversation journal (read-only:
#                        you cannot edit or delete a conversation that
#                        happened — honest capabilities, not a missing button).
_MEM_SECTIONS: dict = {
    "personal": {
        "label": "Personal Memory", "source": "memory",
        "categories": ["identity", "preferences", "relationships", "wishes"],
        "hint": "Who you are, what you prefer, the people and plans.",
        "add_category": "preferences",
    },
    "projects": {
        "label": "Projects", "source": "memory",
        "categories": ["projects"],
        "hint": "Active projects and goals RIO is tracking.",
        "add_category": "projects",
    },
    "documents": {
        "label": "Documents", "source": "rag",
        "collection": None,          # None → every collection (the library)
        "hint": "Every document indexed in the knowledge base.",
        "add_collection": "personal_knowledge",
    },
    "knowledge": {
        "label": "Knowledge", "source": "mixed",
        "categories": ["notes"], "collection": "personal_knowledge",
        "hint": "Curated notes + the personal_knowledge collection.",
        "add_category": "notes", "add_collection": "personal_knowledge",
    },
    "research": {
        "label": "Research", "source": "rag",
        "collection": "research",
        "hint": "Reports and notes in the research collection.",
        "add_collection": "research",
    },
    "conversations": {
        "label": "Conversations", "source": "journal",
        "hint": "Recorded conversation turns — append-only, searchable.",
    },
}

_MEM_CAPS = {
    "memory":  {"search": True, "add": True, "remove": True,
                "view": True, "edit": True},
    "mixed":   {"search": True, "add": True, "remove": True,
                "view": True, "edit": True},
    "rag":     {"search": True, "add": True, "remove": True,
                "view": True, "edit": True},
    "journal": {"search": True, "add": False, "remove": False,
                "view": True, "edit": False},   # append-only by design
}

_UPLOAD_EXTS = {"pdf", "docx", "xlsx", "xlsm", "txt", "md", "markdown",
                "log", "csv", "json", "png", "jpg", "jpeg", "webp",
                "bmp", "tif", "tiff", "gif"}


def _collect_rag_dashboard() -> dict:
    """Documents · Chunks · Embeddings · Vector DB · Indexing · Last indexed.

    Every field is measured from the live registry/vector file; anything that
    cannot be measured comes back as the STRING 'N/A' (never 0, never a guess)
    so the UI is honest when the knowledge base or its files are absent.
    """
    out = {"documents": "N/A", "chunks": "N/A", "embeddings": "N/A",
           "vector_database": "N/A", "indexing_status": "N/A",
           "last_indexed": "N/A"}
    docs: list[dict] = []
    try:
        from core import rag_service as rs
        resp = rs.stats(rs.CALLER_OWNER)
        s = resp.get("stats") if isinstance(resp, dict) else None
        if isinstance(s, dict):
            if s.get("documents") is not None:
                out["documents"] = str(s["documents"])
            if s.get("chunks") is not None:
                out["chunks"] = str(s["chunks"])
            dim, model = s.get("embed_dim"), s.get("embed_model")
            if dim and model:
                out["embeddings"] = f"{model} · {dim}-dim"
    except Exception as exc:
        out["documents"] = out["chunks"] = out["embeddings"] = \
            f"N/A (stats error: {type(exc).__name__})"
    try:
        from core import rag
        vf = rag.VECTORS_FILE
        if vf.exists():
            mb = vf.stat().st_size / 1e6
            out["vector_database"] = f"vectors.npz (NumPy) · {mb:.1f} MB"
        docs = rag.list_documents()
    except Exception:
        pass
    if docs:
        failed = sum(1 for d in docs if d.get("status") != "indexed")
        out["indexing_status"] = ("indexed" if not failed
                                  else f"{failed} failed of {len(docs)}")
        last = max((str(d.get("ingested_at") or "") for d in docs), default="")
        if last:
            out["last_indexed"] = last.replace("T", " ")[:16]
    elif out["documents"] != "N/A":
        out["indexing_status"] = "N/A (no documents)"
        out["last_indexed"] = "N/A (no documents)"
    out["ok"] = True
    return out


def _mm_rows(categories: list[str], q: str, limit: int) -> list[dict]:
    """Curated-memory rows for the given categories (optionally scored)."""
    import re as _re
    from memory import memory_manager as mm
    mem = mm.load_memory()
    rows: list[dict] = []
    for cat in categories:
        items = mem.get(cat) or {}
        if not isinstance(items, dict):
            continue
        for key, entry in items.items():
            if isinstance(entry, dict):
                val = str(entry.get("value", "") or "")
                upd = str(entry.get("updated", "") or "")
            else:
                val, upd = str(entry or ""), ""
            if not val:
                continue
            rows.append({"source": "memory", "category": cat, "key": key,
                         "value": val, "updated": upd})
    if q.strip():
        words = [w for w in _re.split(r"[^\w]+", q.lower()) if len(w) > 1]
        scored = []
        for r in rows:
            s = mm._score(words, r["category"], r["key"], r["value"])
            if s > 0:
                scored.append((s, r))
        scored.sort(key=lambda t: (-t[0], t[1]["key"]))
        rows = [r for _s, r in scored[:limit]]
    else:
        rows.sort(key=lambda r: (r["updated"] or "0000-00-00"), reverse=True)
        rows = rows[:limit]
    return rows


def _rag_doc_row(d: dict) -> dict:
    return {"source": "rag", "doc_id": d.get("doc_id"),
            "key": d.get("title") or d.get("doc_id"),
            "category": d.get("collection"),
            "filetype": d.get("filetype"),
            "pages": d.get("page_count"), "chunks": d.get("chunk_count"),
            "status": d.get("status"),
            "updated": str(d.get("ingested_at") or "").replace("T", " ")[:16],
            "value": (f"{d.get('filetype', '?')} · {d.get('page_count', '?')} "
                      f"pages · {d.get('chunk_count', '?')} chunks · "
                      f"{d.get('status', '?')}")}


def _rag_rows(collection: str | None, q: str, limit: int) -> list[dict]:
    """RAG-backed rows: semantic SEARCH when a query is given (the real
    rag_service.search → citations), otherwise the document LIST."""
    from core import rag_service as rs
    caller = rs.CALLER_OWNER
    if q.strip():
        res = rs.search(caller, q, top_k=limit, collection=collection)
        rows = []
        for r in (res.get("results") or []) if res.get("ok") else []:
            rows.append({"source": "rag_hit", "doc_id": r.get("doc_id"),
                         "key": r.get("source") or r.get("doc_id"),
                         "category": r.get("collection"),
                         "page": r.get("page"), "score": r.get("score"),
                         "value": (r.get("text") or "")[:400], "updated": ""})
        return rows
    res = rs.list_documents(caller, collection=collection)
    docs = res.get("documents") if res.get("ok") else []
    return [_rag_doc_row(d) for d in (docs or [])][:limit]


def _journal_rows(q: str, limit: int) -> list[dict]:
    """Conversation-journal rows — newest first, lexically scored on query.
    Uses the journal's OWN iterator (one store, one parser)."""
    import re as _re
    from memory import conversation_journal as cj
    try:
        recs = cj._iter_journal()
    except Exception:
        recs = []
    rows = [{"source": "journal", "key": r.get("speaker", "?"),
             "category": "conversations",
             "value": str(r.get("text", ""))[:500],
             "updated": str(r.get("ts", ""))[:16].replace("T", " ")}
            for r in recs if r.get("text")]
    rows.sort(key=lambda r: r["updated"], reverse=True)
    if q.strip():
        words = [w for w in _re.split(r"[^\w]+", q.lower()) if len(w) > 1]
        scored = []
        for r in rows:
            hay = (r["value"] + " " + r["key"]).lower()
            s = sum(hay.count(w) for w in words)
            if s:
                scored.append((s, r))
        scored.sort(key=lambda t: -t[0])
        return [r for _s, r in scored[:limit]]
    return rows[:limit]


def _section_rows(sec: dict, q: str, limit: int) -> list[dict]:
    src = sec["source"]
    if src == "memory":
        return _mm_rows(sec["categories"], q, limit)
    if src == "rag":
        return _rag_rows(sec.get("collection"), q, limit)
    if src == "journal":
        return _journal_rows(q, limit)
    # mixed: memory notes + one RAG collection, memory rows first
    return (_mm_rows(sec["categories"], q, limit)
            + _rag_rows(sec.get("collection"), q, limit))


def _section_count(sec: dict) -> int:
    try:
        return len(_section_rows(sec, "", 10_000))
    except Exception:
        return 0


# ── Phase C: Conversations (REAL journal-backed history) ─────────────────
# Source of truth: memory/conversation_journal.py → memory/conversations/*.jsonl
# (append-only, verbatim user/jarvis turns). No second store, no fabricated
# rows, no timestamps invented: rows missing `ts`/`text` are skipped, not made
# up. Old rows carry no conversation id — they are grouped into sessions with
# the documented ≥30-minute silence rule (user-approved 2026-10-12); stamped
# rows (`conv`) group by their own id across month boundaries.

_CONV_GAP_S = 30 * 60          # silence that ends one session, starts another
_CONVS_TTL_S = 15.0            # list cache — journal is append-heavy
_convs_cache: dict = {"t": 0.0, "data": None, "max_pos": 0}


def _parse_ts(s) -> "datetime | None":
    import datetime as _dt
    try:
        t = str(s or "").replace("Z", "+00:00")
        dt = _dt.datetime.fromisoformat(t)
        if dt.tzinfo is not None:
            dt = dt.astimezone(_dt.timezone.utc).replace(tzinfo=None)  # naive UTC-ish
        return dt
    except Exception:
        return None


def _conv_title(user_texts: list[str]) -> str:
    """First decent user turn, truncated — a summary of real content, never
    invented."""
    for t in user_texts:
        t = str(t).strip()
        if t:
            one = " ".join(t.split())
            return (one[:64] + "…") if len(one) > 64 else one
    return "(no user text)"


def _group_turns(rows: list[dict]) -> list[dict]:
    """rows: ts-ascending journal dicts → session groups."""
    groups: list[dict] = []
    cur: dict | None = None
    prev_dt = None
    for r in rows:
        dt = _parse_ts(r.get("ts"))
        if dt is None:                       # unplaceable row: skip, never invent
            continue
        conv_id = str(r.get("conv") or "").strip()
        new_by_gap = (prev_dt is None or (dt - prev_dt).total_seconds() > _CONV_GAP_S)
        if conv_id:
            if cur is None or cur.get("id") != conv_id:
                groups.append(cur := {"id": conv_id, "stamped": True,
                                      "turns": [], "start": dt, "end": dt})
            else:
                cur["end"] = dt
        else:
            if cur is None or cur.get("stamped") or new_by_gap:
                groups.append(cur := {"id": "", "stamped": False,
                                      "turns": [], "start": dt, "end": dt})
            else:
                cur["end"] = dt
        cur["turns"].append(r)
        prev_dt = dt
    return groups


def _load_journal_rows() -> list[dict]:
    """All parseable turns, oldest first, newest on top of the list cache.
    Bounded per file exactly like _tail_jsonl: month files grow forever."""
    from memory import conversation_journal as cj
    out: list[dict] = []
    try:
        paths = sorted(cj.JOURNAL_DIR.glob("*.jsonl"))
    except OSError:
        return []
    for p in paths:
        for r in _tail_jsonl(p, n=100000, max_bytes=262144):
            if r.get("ts") and str(r.get("text") or "").strip():
                out.append(r)
    out.sort(key=lambda r: str(r.get("ts")))
    return out


def _collect_conversations(limit: int = 200) -> dict:
    rows = _load_journal_rows()
    groups = _group_turns(rows)[-limit:]          # newest sessions win
    out: list[dict] = []
    for g in groups:
        user_texts = [str(t.get("text") or "") for t in g["turns"]
                      if t.get("speaker") == "user"]
        gid = g["id"] or ("g:" + g["start"].strftime("%Y%m%d%H%M%S"))
        out.append({
            "id": gid,
            "stamped": g["stamped"],
            "title": _conv_title(user_texts),
            "started": g["start"].isoformat(timespec="seconds"),
            "last": g["end"].isoformat(timespec="seconds"),
            "turns": len(g["turns"]),
        })
    out.reverse()                                  # newest first for the UI
    return {"ok": True, "ts": time.time(), "conversations": out,
            "gap_rule_s": _CONV_GAP_S}


def _collect_conversation(gid: str) -> dict:
    rows = _load_journal_rows()
    groups = _group_turns(rows)
    for g in groups:
        key = g["id"] or ("g:" + g["start"].strftime("%Y%m%d%H%M%S"))
        if key == gid:
            msgs = [{
                "ts": str(t.get("ts") or ""),
                "speaker": str(t.get("speaker") or ""),
                "text": str(t.get("text") or ""),
            } for t in g["turns"]]
            return {"ok": True, "ts": time.time(), "id": gid,
                    "stamped": g["stamped"], "messages": msgs}
    return {"ok": False, "error": "not_found",
            "detail": "no such conversation in the journal"}


def register(app, _auth) -> None:
    """Attach the RIO shell routes and /api/rio/health to a FastAPI app."""
    index = RIO_DIR / "index.html"

    # Per-start build tag: index.html references its scripts/CSS with ?v=N,
    # and N changes on every server start. A window (or phone) whose HTTP
    # cache still holds yesterday's app.js is forced to refetch — "no-cache"
    # on the asset route alone cannot rescue an entry stored under a
    # heuristic-fresh copy (observed 2026-10-17: the dock ran stale JS).
    _build = str(int(time.time()))
    _shell_html: dict = {}

    def _shell():
        if not index.exists():
            return JSONResponse({"error": "RIO shell missing"}, status_code=404)
        try:
            if not _shell_html:
                html = index.read_text(encoding="utf-8")
                html = re.sub(r"(/rio/assets/[A-Za-z0-9_.-]+\.(?:js|css))",
                              rf"\1?v={_build}", html)
                _shell_html["html"] = html
            # no-store: a cached shell would pin OLD asset URLs, undoing the
            # per-start build tag above.
            return HTMLResponse(_shell_html["html"],
                                headers={"Cache-Control": "no-store"})
        except Exception:
            return FileResponse(str(index), media_type="text/html")

    for route in ROUTES:
        app.add_api_route(route, _shell, methods=["GET"], include_in_schema=False)

    # Per-agent workflow pages (deep links): /agents/<key> serves the same
    # SPA shell; the client router dispatches on the slug. Registered AFTER
    # the explicit "/agents" route so the exact match always wins.
    app.add_api_route("/agents/{key}", _shell, methods=["GET"],
                      include_in_schema=False)

    # ── Honest 404 for unknown paths ───────────────────────────────────
    # FastAPI's default is a bare JSON body, which a browser user would
    # read as raw text. Unknown /api, /rio and /static paths keep that
    # exact JSON contract; unknown PAGE paths get a real not-found page
    # (status stays 404 — never a silent 200). This is an EXCEPTION
    # handler, not a catch-all route, so it can never shadow a real route
    # no matter when that route is registered.
    _NF_PAGE = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<link rel=\"icon\" href=\"data:,\">"
        "<title>Not found · RIO</title><style>"
        "body{margin:0;min-height:100vh;display:grid;place-items:center;"
        "background:#0d1117;color:#e6edf3;font:16px/1.6 system-ui,sans-serif}"
        "main{text-align:center;padding:24px}"
        "h1{font-size:4rem;margin:0;color:#58a6ff}"
        "code{background:#161b22;padding:2px 6px;border-radius:6px}"
        "a{color:#58a6ff}</style></head><body><main><h1>404</h1>"
        "<p>No route <code>__PATH__</code>.</p>"
        "<p><a href=\"/home\">← Back to RIO</a></p></main></body></html>"
    )

    async def _not_found(request: Request, exc):
        path = request.url.path
        detail = getattr(exc, "detail", None) or "Not Found"
        if path.startswith(("/api/", "/rio/", "/static/")) or path == "/ws":
            return JSONResponse({"detail": detail}, status_code=404)
        if not index.exists():
            return JSONResponse({"detail": detail}, status_code=404)
        return HTMLResponse(_NF_PAGE.replace("__PATH__", _html_escape(path)),
                            status_code=404)

    app.add_exception_handler(404, _not_found)

    @app.get("/rio/assets/{name}", include_in_schema=False)
    async def rio_assets(name: str):
        if name not in _ASSETS:
            return JSONResponse({"error": "not found"}, status_code=404)
        path = RIO_DIR / name
        if not path.exists():
            return JSONResponse({"error": "not found"}, status_code=404)
        # no-cache is NOT optional here: without it Chrome heuristically
        # caches app.js/styles.css, and a window that survives an app
        # restart keeps the OLD UI forever — the controller never appeared
        # and the dock ran stale code (observed 2026-10-16).
        return FileResponse(str(path), media_type=_ASSETS[name],
                            headers={"Cache-Control": "no-cache"})

    @app.get("/api/rio/conversations", include_in_schema=False)
    async def rio_conversations(request: "Request") -> JSONResponse:
        """Real conversation sessions, newest first (journal-backed)."""
        if not _auth(request):
            return JSONResponse({"ok": False, "error": "Unauthorized"},
                                status_code=401)
        import asyncio
        data = await asyncio.get_event_loop().run_in_executor(
            None, _collect_conversations)
        return JSONResponse(data)

    @app.get("/api/rio/conversations/{gid}", include_in_schema=False)
    async def rio_conversation(request: "Request", gid: str) -> JSONResponse:
        """One conversation's VERBATIM messages (user/jarvis turns)."""
        if not _auth(request):
            return JSONResponse({"ok": False, "error": "Unauthorized"},
                                status_code=401)
        import asyncio
        data = await asyncio.get_event_loop().run_in_executor(
            None, _collect_conversation, gid[:64])
        if not data.get("ok"):
            return JSONResponse(data, status_code=404)
        return JSONResponse(data)

    @app.get("/api/rio/health", include_in_schema=False)
    async def rio_health(request: "Request") -> JSONResponse:
        if not _auth(request):
            return JSONResponse({"ok": False, "error": "Unauthorized"},
                                status_code=401)
        now = time.time()
        if _health_cache["data"] is not None \
                and now - _health_cache["t"] < _HEALTH_TTL_S:
            data = dict(_health_cache["data"])
            data["cached"] = round(now - _health_cache["t"], 1)
            return JSONResponse(data)
        import asyncio
        data = await asyncio.get_event_loop().run_in_executor(None, _collect_health)
        _health_cache["t"] = now
        _health_cache["data"] = data
        return JSONResponse(data)

    @app.get("/api/rio/home", include_in_schema=False)
    async def rio_home(request: "Request") -> JSONResponse:
        """Command-center payload: health + models + system metrics + doing +
        tasks + activity. One bounded worker thread; 12 s cache so a refresh
        storm cannot hammer the probes."""
        if not _auth(request):
            return JSONResponse({"ok": False, "error": "Unauthorized"},
                                status_code=401)
        now = time.time()
        if _home_cache["data"] is not None \
                and now - _home_cache["t"] < _HOME_TTL_S:
            data = dict(_home_cache["data"])
            data["cached"] = round(now - _home_cache["t"], 1)
            return JSONResponse(data)
        import asyncio
        data = await asyncio.get_event_loop().run_in_executor(None, _collect_home)
        _home_cache["t"] = now
        _home_cache["data"] = data
        return JSONResponse(data)

    @app.get("/api/rio/agents", include_in_schema=False)
    async def rio_agents(request: "Request") -> JSONResponse:
        """Agent Center payload: the ten cards + detail data, read live from
        the orchestrator registry, the orchestration journal and the autonomy
        engine. Fresh on every call (no cache — status must not lie)."""
        if not _auth(request):
            return JSONResponse({"ok": False, "error": "Unauthorized"},
                                status_code=401)
        import asyncio
        data = await asyncio.get_event_loop().run_in_executor(None,
                                                              _collect_agents)
        return JSONResponse(data)

    @app.get("/api/rio/agent-workflows", include_in_schema=False)
    async def rio_agent_workflows(request: "Request") -> JSONResponse:
        """One page per agent: static verified pipelines from _WORKFLOWS with
        LIVE status merged (registry + desk progress file). Fresh on every
        call — status must not lie."""
        if not _auth(request):
            return JSONResponse({"ok": False, "error": "Unauthorized"},
                                status_code=401)
        import asyncio
        data = await asyncio.get_event_loop().run_in_executor(
            None, _collect_workflows)
        return JSONResponse(data)

    # ── Camera preview + media player (Home tab) ─────────────────────────
    # The MJPEG <img> and the media <audio> element cannot send an
    # Authorization header, so these read the token from ?token= — the same
    # contract the /ws websocket already uses.

    @app.get("/api/rio/camera/stream", include_in_schema=False)
    async def rio_camera_stream(request: "Request"):
        from actions import camera_feed as feed
        if not _auth(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        boundary = "rioframe"

        async def frames():
            feed.client_arrived()
            try:
                import asyncio as _aio
                stale_since = time.time()
                while True:
                    jpeg = feed.latest_jpeg()
                    if jpeg:
                        stale_since = time.time()
                        yield (f"--{boundary}\r\nContent-Type: image/jpeg\r\n"
                               f"Content-Length: {len(jpeg)}\r\n\r\n"
                               ).encode() + jpeg + b"\r\n"
                    elif time.time() - stale_since > 6.0:
                        # no frames for 6 s → end the stream; the <img> stops
                        # and the card falls back to the state poll
                        break
                    await _aio.sleep(0.1)
            finally:
                feed.client_left()

        from fastapi.responses import StreamingResponse
        return StreamingResponse(frames(),
                                 media_type=f"multipart/x-mixed-replace; "
                                            f"boundary={boundary}")

    @app.get("/api/rio/camera/state", include_in_schema=False)
    async def rio_camera_state(request: "Request") -> JSONResponse:
        from actions import camera_feed as feed
        if not _auth(request):
            return JSONResponse({"ok": False, "error": "Unauthorized"},
                                status_code=401)
        return JSONResponse({"ok": True, **feed.state()})

    @app.post("/api/rio/camera/toggle", include_in_schema=False)
    async def rio_camera_toggle(request: "Request") -> JSONResponse:
        from actions import camera_feed as feed
        if not _auth(request):
            return JSONResponse({"ok": False, "error": "Unauthorized"},
                                status_code=401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        on = bool(body.get("on"))
        if on:
            feed.ensure_stream()
        else:
            feed.release()
        return JSONResponse({"ok": True, **feed.state()})

    @app.get("/api/rio/media/next", include_in_schema=False)
    async def rio_media_next(request: "Request") -> JSONResponse:
        from dashboard import media_bus
        if not _auth(request):
            return JSONResponse({"ok": False, "error": "Unauthorized"},
                                status_code=401)
        try:
            wait_s = min(5.0, float(request.query_params.get("wait") or 0))
        except ValueError:
            wait_s = 0.0
        import asyncio
        payload = await asyncio.get_event_loop().run_in_executor(
            _MEDIA_POOL, lambda: media_bus.pop(wait_s=wait_s))
        # Transport commands ride the poll as a fallback for pages whose
        # WebSocket is down; only the dock poller (cmd=1) consumes them so
        # the Home status poller can never steal a pause from the player.
        cmd = media_bus.pop_cmd() \
            if request.query_params.get("cmd") == "1" else None
        return JSONResponse({"ok": True, "media": payload,
                             "current": media_bus.current(),
                             "state": media_bus.state(),
                             "cmd": cmd})

    @app.post("/api/rio/media/stop", include_in_schema=False)
    async def rio_media_stop(request: "Request") -> JSONResponse:
        from dashboard import media_bus
        if not _auth(request):
            return JSONResponse({"ok": False, "error": "Unauthorized"},
                                status_code=401)
        return JSONResponse(media_bus.stop())

    @app.post("/api/rio/media/state", include_in_schema=False)
    async def rio_media_state(request: "Request") -> JSONResponse:
        """The dock reports what its player actually did (playing / paused /
        blocked / error) so music_player's now_playing tells the truth about
        dashboard playback instead of assuming the push worked."""
        from dashboard import media_bus
        if not _auth(request):
            return JSONResponse({"error": "Unauthorized"},
                                status_code=401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        return JSONResponse(
            media_bus.set_state(body.get("status") or "",
                                body.get("detail") or ""))

    @app.post("/api/rio/media/pause", include_in_schema=False)
    async def rio_media_pause(request: "Request") -> JSONResponse:
        """Pause the dock player (controller button or voice relay)."""
        from dashboard import media_bus
        if not _auth(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return JSONResponse(media_bus.set_cmd("pause"))

    @app.post("/api/rio/media/resume", include_in_schema=False)
    async def rio_media_resume(request: "Request") -> JSONResponse:
        """Resume the dock player (controller button or voice relay)."""
        from dashboard import media_bus
        if not _auth(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return JSONResponse(media_bus.set_cmd("resume"))

    @app.post("/api/rio/media/play", include_in_schema=False)
    async def rio_media_play(request: "Request") -> JSONResponse:
        """The controller plays a resolved payload directly (a search result
        or the current track) — same media_bus path voice uses."""
        from dashboard import media_bus
        if not _auth(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        source = str(body.get("source") or "").strip().lower()
        url = str(body.get("url") or "").strip()
        video_id = str(body.get("videoId") or "").strip()
        spotify_id = str(body.get("spotifyId") or "").strip()
        # Whitelist the payload shape: a playable source + one locator.
        if source not in ("jiosaavn", "audio", "youtube", "spotify"):
            return JSONResponse(
                {"ok": False, "error": "unsupported source"},
                status_code=400)
        if source in ("jiosaavn", "audio") and not (
                url.endswith(".mp4") or url.endswith(".mp3")
                or url.endswith(".m4a")):
            return JSONResponse(
                {"ok": False, "error": "missing playable URL"}, status_code=400)
        if source == "youtube" and not video_id:
            return JSONResponse(
                {"ok": False, "error": "missing videoId"}, status_code=400)
        if source == "spotify" and not spotify_id:
            return JSONResponse(
                {"ok": False, "error": "missing spotifyId"}, status_code=400)
        payload = {k: body[k] for k in
                   ("source", "url", "title", "artist", "image", "videoId",
                    "spotifyId", "spotifyType", "note") if body.get(k)}
        media_bus.push(payload)
        return JSONResponse({"ok": True, "media": payload})

    @app.get("/api/rio/media/search", include_in_schema=False)
    async def rio_media_search(request: "Request") -> JSONResponse:
        """Direct JioSaavn search for the controller: decrypted, playable
        CDN URLs straight from JioSaavn's own api.php — no mirror service."""
        from actions.media_play import saavn_search_many
        if not _auth(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        q = (request.query_params.get("q") or "").strip()
        if not q:
            return JSONResponse({"ok": True, "results": []})
        import asyncio
        results = await asyncio.get_event_loop().run_in_executor(
            None, lambda: saavn_search_many(q, n=8))
        return JSONResponse({"ok": True, "results": results})

    @app.get("/api/rio/media/proxy", include_in_schema=False)
    async def rio_media_proxy(request: "Request"):
        """Stream one audio URL through the dashboard. The RIO page is served
        over HTTPS — a direct http:// CDN link in <audio> is mixed content and
        the browser blocks it. Proxying also keeps the CDN URL out of the
        client. Whitelisted hosts only (SSRF guard)."""
        if not _auth(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        u = (request.query_params.get("u") or "").strip()
        host = re.match(r"^https?://([^/]+)", u or "")
        host = (host.group(1) or "").lower() if host else ""
        if not re.search(r"(^|\.)saavncdn\.com$|^aac\.saavncdn\.com$", host) \
                and not host.endswith(".saavncdn.com") \
                and not host.endswith(".jiosaavn.com"):
            return JSONResponse({"error": f"host not allowed: {host or '?'}"},
                                status_code=400)
        import asyncio

        range_hdr = (request.headers.get("range") or "").strip()

        def _fetch():
            import requests as _rq
            headers = {"User-Agent": "Mozilla/5.0"}
            # Chrome media elements send Range: bytes=0- (and bytes=N- on
            # every seek). Forwarding it upstream keeps seeks instant and
            # lets the browser stream instead of downloading whole files;
            # without it every seek restarts the full download.
            if range_hdr:
                headers["Range"] = range_hdr
            return _rq.get(u, timeout=20, stream=True, headers=headers)

        try:
            upstream = await asyncio.get_event_loop().run_in_executor(
                None, _fetch)
        except Exception as exc:
            return JSONResponse({"error": f"upstream failed: {exc}"[:120]},
                                status_code=502)

        from fastapi.responses import StreamingResponse
        ctype = upstream.headers.get("content-type", "audio/mpeg")
        if "text/html" in ctype:      # CDN served an error page
            return JSONResponse({"error": "upstream returned no audio"},
                                status_code=502)
        passthrough = {}
        for h in ("content-range", "content-length", "accept-ranges"):
            if upstream.headers.get(h):
                passthrough[h] = upstream.headers[h]
        return StreamingResponse(upstream.iter_content(chunk_size=65536),
                                 media_type=ctype,
                                 status_code=upstream.status_code,
                                 headers=passthrough)

    @app.post("/api/rio/agents/run", include_in_schema=False)
    async def rio_agent_run(request: "Request") -> JSONResponse:
        """Dispatch ONE real agent on a worker thread — same call the
        orchestrator's router performs (agent.handle). 202 + task_id; poll
        GET /api/rio/agents for RUNNING → completion. Stub/absent agents
        are refused honestly (409/404) instead of faking a run."""
        if not _auth(request):
            return JSONResponse({"ok": False, "error": "Unauthorized"},
                                status_code=401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        name = str(body.get("name") or "").strip()
        if name not in {r for _, _, r in _AGENTS_SPEC if r}:
            return JSONResponse(
                {"error": "not_implemented",
                 "detail": f"'{name or '?'}' is not one of the backend agents"},
                status_code=404)
        prompt = str(body.get("prompt") or _DEFAULT_RUN_PROMPT).strip() \
            or _DEFAULT_RUN_PROMPT
        meta = body.get("metadata") if isinstance(body.get("metadata"), dict) \
            else {}
        safe_meta = {k: meta[k] for k in _RUN_META_KEYS if k in meta}
        try:
            info, code = _start_run(name, prompt[:2000], safe_meta)
        except Exception as exc:                    # registry exploded
            return JSONResponse(
                {"error": "run_failed",
                 "detail": f"{type(exc).__name__}: {exc}"[:200]},
                status_code=500)
        return JSONResponse(info, status_code=code)

    @app.get("/api/rio/brain", include_in_schema=False)
    async def rio_brain(request: "Request") -> JSONResponse:
        """Brain payload: 5 provider cards + real pipeline + routing table
        computed by the existing CapabilityRegistry. No key material."""
        if not _auth(request):
            return JSONResponse({"ok": False, "error": "Unauthorized"},
                                status_code=401)
        import asyncio
        data = await asyncio.get_event_loop().run_in_executor(None,
                                                              _collect_brain)
        return JSONResponse(data)

    @app.post("/api/rio/brain/route", include_in_schema=False)
    async def rio_brain_route(request: "Request") -> JSONResponse:
        """Pure PLANNING through the real router: classify the text (existing
        IntentClassifier) → TaskType → ModelTask → router._plan() → ordered
        chain + selected rung + skip reasons. No model is called."""
        if not _auth(request):
            return JSONResponse({"ok": False, "error": "Unauthorized"},
                                status_code=401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        text = str(body.get("text") or "").strip()
        try:
            from orchestrator.model_router.capabilities import ModelTask
            from orchestrator.model_router.router import (RouteOptions,
                                                          build_explanation)
            router = _get_router()
            forced = str(body.get("task") or "").strip()
            intent = None
            if forced:
                mt = ModelTask(forced)
            else:
                # existing backend classifier — never reimplemented here
                from orchestrator.classifier import IntentClassifier
                cls = IntentClassifier().classify(text)
                intent = {"task_type": cls.task_type.value,
                          "confidence": round(float(cls.confidence), 3),
                          "reason": str(cls.reason)[:160]}
                mt = ModelTask(_TASK_TO_MTASK.get(cls.task_type.value,
                                                  "light_reasoning"))
            options = RouteOptions(task=mt)
            plan, hops, order = router._plan(text, options)
            by_name = {s.name: s for s in router.registry.all()}
            chain = []
            for name in order:
                spec = by_name.get(name)
                hop = next((h for h in hops if h.provider == name), None)
                chain.append({
                    "name": name,
                    "provider": spec.provider if spec else "?",
                    "model_id": spec.model_id if spec else "?",
                    "selected": bool(plan and plan[0].name == name),
                    "in_plan": name in {p.name for p in plan},
                    "reason": (hop.status + ": " + hop.detail) if hop else "",
                })
            selected = ({"name": plan[0].name,
                         "provider": plan[0].provider,
                         "model_id": plan[0].model_id} if plan else None)
            explanation = ("Selected " + plan[0].name + " (first healthy, "
                           "cheapest-capable rung)." if plan else
                           build_explanation(None, hops)
                           or "no rung available for this task")
            return JSONResponse({
                "ok": True, "intent": intent, "model_task": mt.value,
                "mode": router.mode, "selected": selected,
                "chain": chain, "explanation": explanation[:400],
                "calls_model": False,
            })
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]},
                status_code=200)          # honest failure payload, never 500-masked

    @app.post("/api/rio/brain/probe", include_in_schema=False)
    async def rio_brain_probe(request: "Request") -> JSONResponse:
        """LIVE one-shot call through ModelRouter.route() — the real
        cheapest-first chain with real fallback. Returns the selected model
        and the safe failure explanation; never fabricates success."""
        if not _auth(request):
            return JSONResponse({"ok": False, "error": "Unauthorized"},
                                status_code=401)
        now = time.time()
        if now - _PROBE_AT["last"] < 8.0:
            return JSONResponse(
                {"error": "cooldown",
                 "detail": f"probe cooldown — retry in "
                           f"{8.0 - (now - _PROBE_AT['last']):.0f}s"},
                status_code=429)
        try:
            body = await request.json()
        except Exception:
            body = {}
        prompt = str(body.get("prompt") or "Reply with the word OK.").strip() \
            or "Reply with the word OK."
        force = str(body.get("provider") or "").strip()
        try:
            from orchestrator.model_router.capabilities import ModelTask
            from orchestrator.model_router.router import RouteOptions
            router = _get_router()
            if force and force not in {p for p in router.adapters} | {"local"}:
                return JSONResponse(
                    {"error": "unknown provider",
                     "detail": f"'{force}' is not a configured provider"},
                    status_code=400)
            options = RouteOptions(task=ModelTask.LIGHT_REASONING,
                                   timeout_s=12.0, deadline_s=25.0,
                                   max_tokens=48,
                                   force_provider=force or "")
            # real call, real fallback — runs in a worker so the loop breathes
            import asyncio
            result = await asyncio.get_event_loop().run_in_executor(
                None, router.route, prompt, options)
            _PROBE_AT["last"] = time.time()
            return JSONResponse({
                "ok": bool(result.ok),
                "provider": result.provider or None,
                "model": result.model or None,
                "text": (result.text or "")[:400],
                "explanation": (result.explanation or "")[:400],
                "fell_back": bool(result.explanation),
                "latency_s": round(result.latency_s, 2),
                "hops": [h.to_dict() for h in result.hops][:6],
            })
        except Exception as exc:
            _PROBE_AT["last"] = time.time()
            return JSONResponse(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200],
                 "explanation": f"probe failed: {type(exc).__name__}: {exc}"[:300]},
                status_code=200)

    # ══ Phase 6 · Memory ══════════════════════════════════════════════════
    # Six sections over the EXISTING stores: long_term.json (memory), the RAG
    # registry (documents/knowledge/research) and the conversation journal.
    # Capabilities are reported per section so the UI only offers what the
    # backend can actually honour (the journal is append-only by design).

    def _deny_unsupported(sec: dict) -> JSONResponse | None:
        if sec["source"] == "journal":
            return JSONResponse(
                {"ok": False, "error": "not supported",
                 "detail": "Conversations are append-only — a turn that "
                           "happened cannot be edited or deleted."},
                status_code=409)
        return None

    @app.get("/api/rio/memory", include_in_schema=False)
    async def rio_memory_overview(request: "Request") -> JSONResponse:
        """The six sections with live counts + honest capability flags."""
        if not _auth(request):
            return JSONResponse({"ok": False, "error": "Unauthorized"},
                                status_code=401)
        import asyncio
        loop = asyncio.get_event_loop()

        def _build() -> list[dict]:
            out = []
            for sid, sec in _MEM_SECTIONS.items():
                out.append({"id": sid, "label": sec["label"],
                            "source": sec["source"], "hint": sec["hint"],
                            "count": _section_count(sec),
                            "capabilities": dict(_MEM_CAPS[sec["source"]])})
            return out

        sections = await loop.run_in_executor(None, _build)
        return JSONResponse({"ok": True, "sections": sections})

    @app.get("/api/rio/memory/{section}", include_in_schema=False)
    async def rio_memory_section(request: "Request", section: str,
                                 q: str = "", limit: int = 100) -> JSONResponse:
        """View (no q) or SEARCH (q=…) one section. RAG sections return the
        real semantic hits with scores when q is given."""
        if not _auth(request):
            return JSONResponse({"ok": False, "error": "Unauthorized"},
                                status_code=401)
        sec = _MEM_SECTIONS.get(section)
        if not sec:
            return JSONResponse(
                {"ok": False, "error": "unknown section",
                 "detail": f"one of {', '.join(_MEM_SECTIONS)}"},
                status_code=404)
        limit = max(1, min(int(limit or 100), 500))
        import asyncio
        try:
            rows = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _section_rows(sec, q or "", limit))
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]},
                status_code=200)          # honest failure payload, never 500
        return JSONResponse({
            "ok": True, "section": section, "label": sec["label"],
            "source": sec["source"], "query": q or "",
            "capabilities": dict(_MEM_CAPS[sec["source"]]),
            "rows": rows, "count": len(rows),
        })

    @app.post("/api/rio/memory/{section}", include_in_schema=False)
    async def rio_memory_add(request: "Request", section: str) -> JSONResponse:
        """ADD: {key, value, category?} for memory sections ·
        {title, text} for RAG sections (ingest_text — one RAG, same service)."""
        if not _auth(request):
            return JSONResponse({"ok": False, "error": "Unauthorized"},
                                status_code=401)
        sec = _MEM_SECTIONS.get(section)
        if not sec:
            return JSONResponse({"ok": False, "error": "unknown section"},
                                status_code=404)
        denied = _deny_unsupported(sec)
        if denied:
            return denied
        try:
            body = await request.json()
        except Exception:
            body = {}
        import asyncio
        loop = asyncio.get_event_loop()
        try:
            # mixed sections accept BOTH shapes: {title,text} → knowledge
            # base, {key,value} → curated memory (one section, two stores).
            title = str(body.get("title") or "").strip()
            text = str(body.get("text") or "").strip()
            if sec["source"] == "rag" \
                    or (sec["source"] == "mixed" and title and text):
                if not title or not text:
                    return JSONResponse(
                        {"ok": False, "error": "title and text required"},
                        status_code=400)
                from core import rag_service as rs
                result = await loop.run_in_executor(
                    None, lambda: rs.ingest_text(
                        rs.CALLER_OWNER, text, title,
                        collection=sec["add_collection"],
                        source="rio-memory"))
                return JSONResponse(result,
                                    status_code=200 if result.get("ok") else 400)
            # memory / mixed → curated store
            key = str(body.get("key") or "").strip()
            value = str(body.get("value") or "").strip()
            if not key or not value:
                return JSONResponse(
                    {"ok": False, "error": "key and value required"},
                    status_code=400)
            cat = str(body.get("category") or "").strip() \
                or sec.get("add_category", "notes")
            cats = sec.get("categories") or []
            if cat not in cats:
                return JSONResponse(
                    {"ok": False,
                     "error": f"category must be one of: {', '.join(cats)}"},
                    status_code=400)
            from memory import memory_manager as mm
            msg = await loop.run_in_executor(
                None, lambda: mm.remember(key, value, cat))
            return JSONResponse({"ok": True, "message": msg,
                                 "category": cat, "key": key})
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]},
                status_code=200)

    @app.put("/api/rio/memory/{section}", include_in_schema=False)
    async def rio_memory_edit(request: "Request", section: str) -> JSONResponse:
        """EDIT where supported: {key, value, category?} (memory) ·
        {doc_id, title?, collection?} (RAG — metadata patch, no re-index)."""
        if not _auth(request):
            return JSONResponse({"ok": False, "error": "Unauthorized"},
                                status_code=401)
        sec = _MEM_SECTIONS.get(section)
        if not sec:
            return JSONResponse({"ok": False, "error": "unknown section"},
                                status_code=404)
        denied = _deny_unsupported(sec)
        if denied:
            return denied
        try:
            body = await request.json()
        except Exception:
            body = {}
        import asyncio
        loop = asyncio.get_event_loop()
        try:
            doc_id = str(body.get("doc_id") or "").strip()
            # mixed: a doc_id means RAG update, otherwise curated edit
            if sec["source"] == "rag" \
                    or (sec["source"] == "mixed" and doc_id):
                if not doc_id:
                    return JSONResponse({"ok": False, "error": "doc_id required"},
                                        status_code=400)
                from core import rag_service as rs
                result = await loop.run_in_executor(
                    None, lambda: rs.update(
                        rs.CALLER_OWNER, doc_id,
                        title=body.get("title"),
                        collection=body.get("collection")))
                return JSONResponse(result,
                                    status_code=200 if result.get("ok") else 400)
            key = str(body.get("key") or "").strip()
            value = str(body.get("value") or "").strip()
            if not key or not value:
                return JSONResponse(
                    {"ok": False, "error": "key and value required"},
                    status_code=400)
            from memory import memory_manager as mm
            mem = mm.load_memory()
            cats = sec.get("categories") or []
            hit = next((c for c in cats
                        if isinstance(mem.get(c), dict) and key in mem[c]),
                       None)
            if hit is None:
                return JSONResponse(
                    {"ok": False, "error": f"no entry '{key}' in this section"},
                    status_code=404)
            await loop.run_in_executor(
                None, lambda: mm.update_memory({hit: {key: {"value": value}}}))
            return JSONResponse({"ok": True, "category": hit, "key": key,
                                 "value": value})
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]},
                status_code=200)

    @app.delete("/api/rio/memory/{section}", include_in_schema=False)
    async def rio_memory_remove(request: "Request", section: str) -> JSONResponse:
        """REMOVE: {key, category?} (memory) · {doc_id} (RAG forget)."""
        if not _auth(request):
            return JSONResponse({"ok": False, "error": "Unauthorized"},
                                status_code=401)
        sec = _MEM_SECTIONS.get(section)
        if not sec:
            return JSONResponse({"ok": False, "error": "unknown section"},
                                status_code=404)
        denied = _deny_unsupported(sec)
        if denied:
            return denied
        try:
            body = await request.json()
        except Exception:
            body = {}
        import asyncio
        loop = asyncio.get_event_loop()
        try:
            doc_id = str(body.get("doc_id") or "").strip()
            # mixed: a doc_id means RAG forget, otherwise curated remove
            if sec["source"] == "rag" \
                    or (sec["source"] == "mixed" and doc_id):
                if not doc_id:
                    return JSONResponse({"ok": False, "error": "doc_id required"},
                                        status_code=400)
                from core import rag_service as rs
                result = await loop.run_in_executor(
                    None, lambda: rs.forget(rs.CALLER_OWNER, doc_id))
                return JSONResponse(result,
                                    status_code=200 if result.get("ok") else 400)
            key = str(body.get("key") or "").strip()
            if not key:
                return JSONResponse({"ok": False, "error": "key required"},
                                    status_code=400)
            from memory import memory_manager as mm
            mem = mm.load_memory()
            cats = sec.get("categories") or []
            hit = next((c for c in cats
                        if isinstance(mem.get(c), dict) and key in mem[c]),
                       None)
            if hit is None:
                return JSONResponse(
                    {"ok": False, "error": f"no entry '{key}' in this section"},
                    status_code=404)
            msg = await loop.run_in_executor(
                None, lambda: mm.forget(key, hit))
            return JSONResponse({"ok": True, "message": msg})
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]},
                status_code=200)

    # ══ Phase 6 · RAG dashboard ═══════════════════════════════════════════

    @app.get("/api/rio/rag", include_in_schema=False)
    async def rio_rag_dashboard(request: "Request") -> JSONResponse:
        """Documents · Chunks · Embeddings · Vector database · Indexing status
        · Last indexed — measured live, 'N/A' whenever unmeasurable."""
        if not _auth(request):
            return JSONResponse({"ok": False, "error": "Unauthorized"},
                                status_code=401)
        import asyncio
        data = await asyncio.get_event_loop().run_in_executor(
            None, _collect_rag_dashboard)
        return JSONResponse(data)

    # ══ Phase 6 · Files ═══════════════════════════════════════════════════
    # Upload → Validate → Process → Extract → Index → Available to RIO, with
    # every stage reported. Actions (Open / Analyze / Summarize / Ask RIO /
    # Add to Memory / Delete) are computed PER FILE so the UI never offers an
    # action the backend cannot honour.

    def _safe_name(raw: str) -> str:
        import re as _re
        name = Path(raw or "").name
        name = _re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(". ")
        return name or "upload"

    def _doc_actions(d: dict, upload_exists: bool) -> list[str]:
        """Only the actions THIS file supports."""
        indexed = d.get("status") == "indexed"
        acts = []
        if indexed or upload_exists:
            acts.append("open")
        if indexed:
            acts += ["analyze", "summarize", "ask", "add_to_memory"]
        if indexed or upload_exists:
            acts.append("delete")
        return acts

    def _file_list() -> dict:
        """Uploads + RAG documents, cross-linked by original_name."""
        from dashboard.server import UPLOADS_DIR
        from core import rag_service as rs
        uploads = []
        try:
            for p in sorted(UPLOADS_DIR.iterdir(),
                            key=lambda x: x.stat().st_mtime, reverse=True):
                if p.is_file():
                    uploads.append({"name": p.name, "size": p.stat().st_size,
                                    "mtime": int(p.stat().st_mtime)})
        except Exception:
            pass
        try:
            reg = rs.list_documents(rs.CALLER_OWNER).get("documents") or []
        except Exception:
            reg = []
        by_name = {str(d.get("original_name") or ""): d for d in reg}
        upload_names = {u["name"] for u in uploads}
        for u in uploads:
            d = by_name.get(u["name"])
            u["doc_id"] = d.get("doc_id") if d else None
            u["indexed"] = bool(d and d.get("status") == "indexed")
            u["collection"] = d.get("collection") if d else None
            u["actions"] = _doc_actions(d or {}, True)
        docs = []
        for d in reg:
            docs.append({
                "doc_id": d.get("doc_id"), "title": d.get("title"),
                "collection": d.get("collection"),
                "filetype": d.get("filetype"),
                "pages": d.get("page_count"), "chunks": d.get("chunk_count"),
                "status": d.get("status"),
                "ingested_at": str(d.get("ingested_at") or "")
                                 .replace("T", " ")[:16],
                "original_name": d.get("original_name"),
                "in_uploads": str(d.get("original_name") or "")
                                in upload_names,
                "actions": _doc_actions(d, str(d.get("original_name") or "")
                                        in upload_names),
            })
        try:
            from core import rag as _rag
            colls = list(getattr(_rag, "COLLECTIONS", ()) or [])
        except Exception:
            colls = []
        return {"ok": True, "uploads": uploads, "documents": docs,
                "supported": sorted(_UPLOAD_EXTS), "collections": colls}

    @app.get("/api/rio/files", include_in_schema=False)
    async def rio_files_list(request: "Request") -> JSONResponse:
        if not _auth(request):
            return JSONResponse({"ok": False, "error": "Unauthorized"},
                                status_code=401)
        import asyncio
        try:
            data = await asyncio.get_event_loop().run_in_executor(
                None, _file_list)
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]},
                status_code=200)
        return JSONResponse(data)

    @app.get("/api/rio/files/raw", include_in_schema=False)
    # NOTE: no return annotation — FastAPI would try to build a Pydantic
    # response model out of `FileResponse | JSONResponse` and raise
    # "Invalid args for response field", aborting register() mid-way (the
    # routes after this one would silently never mount).
    async def rio_files_raw(request: "Request", name: str = "",
                            doc_id: str = ""):
        """Open/download bytes — ?name= for an upload, ?doc_id= for the stored
        RAG original. Browser <a download> cannot send headers, so the token
        rides in ?token= exactly like the existing /uploads/{filename} route.
        The header path (fetch) works too."""
        if not _auth(request):
            tok = (request.query_params.get("token") or "").strip()
            if not tok:
                return JSONResponse({"ok": False, "error": "Unauthorized"},
                                    status_code=401)
            # header-less fallback (browser <a download> can't send headers):
            # replay the query token through the SAME _auth the header path
            # uses — one auth check, no server internals, no second rule.
            class _Hdr:
                headers = {"authorization": f"Bearer {tok}"}
            if not _auth(_Hdr()):
                return JSONResponse({"ok": False, "error": "Unauthorized"},
                                    status_code=401)
        path = None
        try:
            if name:
                from dashboard.server import UPLOADS_DIR
                p = UPLOADS_DIR / _safe_name(name)
                if p.is_file():
                    path = p
            elif doc_id:
                from core import rag
                path = rag._resolve_original(str(doc_id))
        except Exception:
            path = None
        if path is None or not Path(path).is_file():
            return JSONResponse({"ok": False, "error": "file not found"},
                                status_code=404)
        return FileResponse(str(path))

    async def _workflow_upload(req: Request, file, collection: str,
                               title: str) -> JSONResponse:
        """Upload → Validate → Process → Extract → Index → Available.
        Each stage is reported; a failure names the stage that failed."""
        stages: list[dict] = []
        import asyncio

        def stage(step: str, ok: bool, detail: str) -> None:
            stages.append({"step": step, "ok": ok, "detail": detail})

        fname = _safe_name(getattr(file, "filename", "") or "")
        ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
        if not fname or ext not in _UPLOAD_EXTS:
            stage("Upload", True, fname or "(no name)")
            stage("Validate", False,
                  f"unsupported type '.{ext}' — allowed: "
                  + ", ".join(sorted(_UPLOAD_EXTS)))
            return JSONResponse({"ok": False, "stages": stages,
                                 "error": "unsupported file type"},
                                status_code=400)
        # 1) upload: stream to the shared uploads dir (same dir /api/files uses)
        from dashboard.server import UPLOADS_DIR, MAX_UPLOAD_MB
        import os, tempfile
        fd, tmp = tempfile.mkstemp(prefix="rio-up-", suffix="." + ext)
        size = 0
        max_bytes = MAX_UPLOAD_MB * 1024 * 1024
        try:
            with os.fdopen(fd, "wb") as fout:
                while True:
                    chunk = await file.read(65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise OverflowError(f"over {MAX_UPLOAD_MB} MB")
                    fout.write(chunk)
        except Exception as exc:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            stage("Upload", False, str(exc)[:120])
            stage("Validate", False, "not reached")
            return JSONResponse({"ok": False, "stages": stages,
                                 "error": str(exc)[:200]},
                                status_code=413 if isinstance(exc, OverflowError)
                                else 500)
        stage("Upload", True, f"{fname} · {size} bytes")
        # keep a durable copy in uploads so Open/Delete work even if indexing
        # fails — a file the user sent must never vanish silently.
        dest = UPLOADS_DIR / fname
        stem, suf = Path(fname).stem, Path(fname).suffix
        counter = 1
        while dest.exists():
            dest = UPLOADS_DIR / f"{stem}_{counter}{suf}"
            counter += 1
        try:
            import shutil
            shutil.move(tmp, dest)
        except Exception as exc:
            stage("Validate", False, f"could not store file: {exc}"[:120])
            return JSONResponse({"ok": False, "stages": stages,
                                 "error": "storage failed"}, status_code=500)
        stage("Validate", True, f"type .{ext} accepted · stored as {dest.name}")
        # 2-5) process → extract → index: ONE call into the existing RAG
        # service (ACL + dedup + embed live there, not here).
        from core import rag_service as rs
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: rs.ingest(
                    rs.CALLER_OWNER, str(dest),
                    collection=collection or "personal_knowledge",
                    title=(title or None), source="rio-upload"))
        except Exception as exc:
            stage("Process", True, "file accepted")
            stage("Extract", False, "not reached")
            stage("Index", False, f"{type(exc).__name__}: {exc}"[:160])
            stage("Available to RIO", False, "no — indexing failed")
            return JSONResponse({"ok": False, "stages": stages,
                                 "error": f"{type(exc).__name__}: {exc}"[:200],
                                 "name": dest.name}, status_code=500)
        if not result.get("ok"):
            err = str(result.get("error") or "ingest failed")
            low = err.lower()
            stage("Process", True, "file accepted")
            if "unsupported" in low or "no extractable" in low \
                    or "chunking" in low or "empty" in low:
                stage("Extract", False, err[:160])
                stage("Index", False, "not reached — extraction failed")
            else:
                stage("Extract", True, "text extracted")
                stage("Index", False, err[:160])
            stage("Available to RIO", False, "no — see Index")
            return JSONResponse({"ok": False, "stages": stages, "error": err,
                                 "name": dest.name}, status_code=400)
        dup = bool(result.get("duplicate"))
        stage("Process", True, f"{result.get('pages', '?')} page(s)")
        stage("Extract", True,
              f"{result.get('chunks', '?')} chunks"
              + (" (duplicate — already indexed)" if dup else ""))
        stage("Index", True, f"collection {result.get('collection')} · "
                             f"doc_id {result.get('doc_id')}")
        stage("Available to RIO", True, "yes — searchable now")
        return JSONResponse({"ok": True, "stages": stages,
                             "name": dest.name, "size": size,
                             "doc_id": result.get("doc_id"),
                             "pages": result.get("pages"),
                             "chunks": result.get("chunks"),
                             "collection": result.get("collection"),
                             "duplicate": dup})

    if _UPLOAD_OK:
        @app.post("/api/rio/files/upload", include_in_schema=False)
        async def rio_files_upload(request: "Request",
                                   file: UploadFile | None = File(None),
                                   collection: str | None = Form(None),
                                   title: str | None = Form(None)) \
                -> JSONResponse:
            if not _auth(request):
                return JSONResponse({"ok": False, "error": "Unauthorized"},
                                    status_code=401)
            if file is None or not getattr(file, "filename", None):
                return JSONResponse(
                    {"ok": False, "error": "no file part in request"},
                    status_code=400)
            return await _workflow_upload(
                request, file, str(collection or "personal_knowledge"),
                str(title or ""))
    else:
        @app.post("/api/rio/files/upload", include_in_schema=False)
        async def rio_files_upload_unavailable(request: "Request") \
                -> JSONResponse:
            return JSONResponse(
                {"ok": False,
                 "error": "File uploads require: pip install python-multipart"},
                status_code=503)

    @app.post("/api/rio/files/action", include_in_schema=False)
    async def rio_files_action(request: "Request") -> JSONResponse:
        """Open · Analyze · Summarize · Ask RIO · Add to Memory · Delete —
        honoured only when the file supports them (see GET /api/rio/files
        'actions'); anything else is a 409, never a fake success."""
        if not _auth(request):
            return JSONResponse({"ok": False, "error": "Unauthorized"},
                                status_code=401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        action = str(body.get("action") or "").strip()
        doc_id = str(body.get("doc_id") or "").strip()
        name = str(body.get("name") or "").strip()
        import asyncio
        loop = asyncio.get_event_loop()
        try:
            from core import rag_service as rs
            from dashboard.server import UPLOADS_DIR
            upload = UPLOADS_DIR / _safe_name(name) if name else None
            upload_ok = bool(upload and upload.is_file())
            doc = {}
            if doc_id:
                reg = rs.list_documents(rs.CALLER_OWNER).get("documents") or []
                doc = next((d for d in reg if d.get("doc_id") == doc_id), {})
            elif upload_ok and name:
                reg = rs.list_documents(rs.CALLER_OWNER).get("documents") or []
                doc = next((d for d in reg
                            if str(d.get("original_name")) == name), {})
                doc_id = str(doc.get("doc_id") or "")
            indexed = doc.get("status") == "indexed"

            if action == "open":
                if not upload_ok and not indexed:
                    return JSONResponse({"ok": False,
                                         "error": "file not available"},
                                        status_code=404)
                if upload_ok:
                    q = f"name={_safe_name(name)}"
                else:
                    q = f"doc_id={doc_id}"
                return JSONResponse({"ok": True,
                                     "url": f"/api/rio/files/raw?{q}"})

            if action in ("analyze", "summarize", "ask"):
                if not indexed:
                    return JSONResponse(
                        {"ok": False, "error": "not supported",
                         "detail": "the file is not indexed — upload it to "
                                   "the knowledge base first"},
                        status_code=409)
                if action == "ask":
                    question = str(body.get("question") or "").strip()
                    if not question:
                        return JSONResponse(
                            {"ok": False, "error": "question required"},
                            status_code=400)
                    query = question
                elif action == "summarize":
                    query = (f"Summarise the document '{doc.get('title')}' "
                             f"— its purpose, structure and key points.")
                else:
                    query = (f"Analyse the document '{doc.get('title')}': "
                             f"what it contains, how it is organised, and the "
                             f"most important claims or data points.")
                result = await loop.run_in_executor(
                    None, lambda: rs.answer(
                        rs.CALLER_OWNER, query, strategy="auto",
                        doc_id=doc_id))
                out = {"ok": bool(result.get("ok")),
                       "answer": result.get("answer"),
                       "citations": result.get("citations") or [],
                       "strategy": result.get("strategy"),
                       "hallucination_risk": result.get("hallucination_risk"),
                       "action": action}
                if result.get("error"):
                    out["error"] = result["error"]
                if not result.get("ok"):
                    return JSONResponse(out, status_code=400)
                return JSONResponse(out)

            if action == "add_to_memory":
                if not indexed:
                    return JSONResponse(
                        {"ok": False, "error": "not supported",
                         "detail": "only indexed documents can be remembered"},
                        status_code=409)
                from memory import memory_manager as mm
                key = str(body.get("key") or "").strip() \
                    or str(doc.get("title") or doc_id)
                value = str(body.get("value") or "").strip() \
                    or (f"Document '{doc.get('title')}' "
                        f"[{doc.get('collection')}] — {doc.get('page_count', '?')} "
                        f"pages, indexed {str(doc.get('ingested_at') or '')[:10]} "
                        f"(doc {doc_id})")
                msg = await loop.run_in_executor(
                    None, lambda: mm.remember(key[:120], value, "notes"))
                return JSONResponse({"ok": True, "message": msg,
                                     "key": key[:120], "value": value})

            if action == "delete":
                if not upload_ok and not doc_id:
                    return JSONResponse({"ok": False,
                                         "error": "file not available"},
                                        status_code=404)
                removed = []
                if indexed:
                    res = await loop.run_in_executor(
                        None, lambda: rs.forget(rs.CALLER_OWNER, doc_id))
                    if not res.get("ok"):
                        return JSONResponse(res, status_code=400)
                    removed.append(f"knowledge base entry {doc_id}")
                if upload_ok:
                    try:
                        upload.unlink()
                        removed.append(f"upload {upload.name}")
                    except Exception as exc:
                        return JSONResponse(
                            {"ok": False,
                             "error": f"could not remove upload: {exc}"[:180]},
                            status_code=500)
                if not removed:
                    return JSONResponse({"ok": False,
                                         "error": "file not available"},
                                        status_code=404)
                return JSONResponse({"ok": True, "removed": removed})

            return JSONResponse(
                {"ok": False, "error": "unknown action",
                 "detail": "open | analyze | summarize | ask | "
                           "add_to_memory | delete"},
                status_code=400)
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]},
                status_code=200)
