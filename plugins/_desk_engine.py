"""
plugins/_desk_engine.py — runtime for the Market Intelligence Desk.

Implements the Grok-guide's operational core against JARVIS primitives:

  Handoffs   — the structured specialist→Lead→Chief contract (guide §9), stored
               as JSONL under memory/market_desk/ with stable event / run /
               delivery identities and the duplicate & delivery controls (§10).
  Skills     — one reusable research procedure per specialist (§7), executed as
               a bounded LLM call. The specialist's profile (from
               _desk_roster.profile_prompt) is the system prompt; the skill
               defines inputs, window rules, limits and the required JSON.
  Schedule   — the guide's §12 Asia/Kolkata production table, every routine
               born PAUSED (§11: activate only after acceptance tests pass)
               and driven by idempotent due-checks with persisted cursors.

LLM calls go through google.genai Models.generate_content, which the installed
llm_router patches for key/model failover. Every client is created with an
http_options timeout — an unbounded generate() would pin a default-executor
worker and starve reconnect DNS (the 2026-09-21 outage).

State layout (memory/market_desk/):
  handoffs.jsonl        production handoffs + lead reviews + chief reports
  handoffs_test.jsonl   acceptance-test records (never enter production)
  state.json            routine active/paused flags + last-run cursors + budget
  universe.json         approved company universe (owner-edited, see roster)
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from memory.config_manager import BASE_DIR
from plugins._desk_roster import (
    CHIEF,
    COVERAGE_COMPLETED_CLEAR,
    COVERAGE_COMPLETED_MATERIAL,
    COVERAGE_INCOMPLETE,
    COVERAGE_RETRYABLE,
    DEPARTMENTS,
    ROSTER,
    all_leads,
    lead_of,
    load_universe,
    profile_prompt,
    resolve_company,
    specialists_of,
)

IST = ZoneInfo("Asia/Kolkata")
DESK_DIR = BASE_DIR / "memory" / "market_desk"
STATE_FILE = DESK_DIR / "state.json"

_TEST_SUFFIX = "_test"

# ── §12 production schedule — every routine starts PAUSED ────────────────────
# weekday uses Python's convention (0=Mon .. 6=Sun); "0-4" = weekdays. first_monday adds the
# fund-holdings gate (first Monday of the month; exit without research
# otherwise, per the guide's §12 note).
ROUTINES: list[dict] = [
    {"id": "pre_earnings_calendar", "role": "Pre-Earnings Intelligence Analyst",
     "weekday": "0-4", "time": "08:00", "purpose": "Pre-Earnings defence calendar"},
    {"id": "industry_morning", "role": "Industry Research Analyst",
     "weekday": "0-4", "time": "08:15", "purpose": "Industry structure morning"},
    {"id": "geopolitical_morning", "role": "Geopolitical Risk Analyst",
     "weekday": "0-4", "time": "08:30", "purpose": "Geopolitical transmission morning"},
    {"id": "chief_premarket", "role": CHIEF,
     "weekday": "0-4", "time": "09:00", "purpose": "Pre-market Lead synthesis"},
    {"id": "news_primary", "role": "Research Analyst",
     "weekday": "0-4", "time": "12:00", "purpose": "Primary-source news"},
    {"id": "sentiment_midday", "role": "Sentiment Analyst",
     "weekday": "0-4", "time": "12:30", "purpose": "Midday published-narrative check"},
    {"id": "sentiment_close", "role": "Sentiment Analyst",
     "weekday": "0-4", "time": "15:35", "purpose": "Closing published-narrative check"},
    {"id": "volume_triage", "role": "Market Volume Intelligence Analyst",
     "weekday": "0-4", "time": "18:00", "purpose": "End-of-day volume triage"},
    {"id": "insider_review", "role": "Insider Activity Analyst",
     "weekday": "0-4", "time": "18:10", "purpose": "Insider-disclosure review"},
    {"id": "fundamental_pair", "role": "Fundamental Research Analyst",
     "weekday": "4", "time": "18:30", "purpose": "Fundamental pair review"},
    {"id": "fund_holdings", "role": "Fund Holdings Research Analyst",
     "weekday": "0", "time": "18:30", "first_monday": True,
     "purpose": "Fund-holdings review"},
    {"id": "forensics_pair", "role": "Financial Forensics Analyst",
     "weekday": "4", "time": "18:50", "purpose": "Financial-forensics pair review"},
    {"id": "chief_evening", "role": CHIEF,
     "weekday": "0-4", "time": "19:15", "purpose": "Evening Lead synthesis"},
]

# Event-driven specialists (guide §12): their Leads invoke them when a
# qualifying event exists — no permanent daily routines.
EVENT_DRIVEN = {
    "News Credibility Analyst", "Market Impact Analyst", "Stock Movement Analyst",
    "Bull Case Analyst", "Bear Case Analyst", "Research Validation Analyst",
    "Short-Seller Research Analyst",
}

_BUDGET_DEFAULT = 12          # max LLM calls per run_schedule sweep


# ══════════════════════════════════════════════════════════════════════════════
# §10 duplicate & delivery controls
# ══════════════════════════════════════════════════════════════════════════════

def _now_ist() -> datetime:
    return datetime.now(IST)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _event_id(*parts) -> str:
    """Stable identity for a research event (§10) — same inputs must collide."""
    raw = "|".join(str(p) for p in parts).lower()
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"routines": {}, "cursors": {}, "budget": _BUDGET_DEFAULT}


def _save_state(state: dict) -> None:
    DESK_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False),
                          encoding="utf-8")


def routine_active(routine_id: str, state: Optional[dict] = None) -> bool:
    """§11: routines are born paused; the Grok-analogue authority for active
    state is state.json, not a chat acknowledgement."""
    st = state or _load_state()
    return bool(st.get("routines", {}).get(routine_id, {}).get("active", False))


def set_routine_active(routine_id: str, active: bool) -> str:
    known = {r["id"] for r in ROUTINES}
    if routine_id not in known:
        return f"Unknown routine '{routine_id}'. Known: {', '.join(sorted(known))}"
    st = _load_state()
    st.setdefault("routines", {}).setdefault(routine_id, {})["active"] = active
    _save_state(st)
    return f"Routine '{routine_id}' is now {'ACTIVE' if active else 'paused'}."


# ══════════════════════════════════════════════════════════════════════════════
# Handoff store (§9 contract + §10 controls)
# ══════════════════════════════════════════════════════════════════════════════

class HandoffStore:
    """Append-only JSONL. Production and test records never mix (guide §10)."""

    def __init__(self, test: bool = False):
        self.path = DESK_DIR / f"handoffs{_TEST_SUFFIX if test else ''}.jsonl"

    def append(self, record: dict) -> None:
        DESK_DIR.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def read_all(self, limit: int = 400) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out

    def find_delivered(self, event_id: str, reporting_period: str) -> Optional[dict]:
        """A delivered record for this (event, period) pair suppresses reruns."""
        for rec in self.read_all():
            if (rec.get("event_id") == event_id
                    and rec.get("reporting_period") == reporting_period
                    and rec.get("delivery_status") == "delivered"):
                return rec
        return None

    def latest_run_wins(self, event_id: str, run_id: str) -> bool:
        """§10: an old run must never change a newer run's record."""
        for rec in reversed(self.read_all()):
            if rec.get("event_id") == event_id:
                return rec.get("run_id", "") <= run_id
        return True


def _reporting_period(window: str) -> str:
    """Period identity — the desk's reporting buckets (all IST dates)."""
    today = _now_ist().date()
    if window == "day":
        return today.isoformat()
    if window == "week":
        return f"{today.isocalendar().year}-W{today.isocalendar().week:02d}"
    if window == "month":
        return today.strftime("%Y-%m")
    return today.isoformat()


# ══════════════════════════════════════════════════════════════════════════════
# The bounded LLM call — profile is the system prompt (guide §4 step 4)
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════
# Hermes bridge — bidirectional integration with the Hermes agent
# (~/.hermes, gateway at ~/.hermes/gateway.sock, CLI at ~/.local/bin/hermes).
#
# OUTBOUND (desk → Hermes): a specialist whose Gemini call fails can delegate
# its research turn to Hermes (`hermes -z`, which runs Hermes' full agent loop
# with its own skills/keys — a genuinely independent capacity, not another
# Gemini key). The reply must still satisfy the same STRICT JSON contract and
# is provenance-tagged so the Lead sees the research came via Hermes.
#
# INBOUND (Hermes → desk): the 'market-desk' Hermes skill (installed under
# ~/.hermes/skills/market-desk/SKILL.md) tells Hermes to query this desk via
#   python3 plugins/_desk_cli.py ask "<question>" [--companies X,Y]
# so Telegram/WhatsApp users talk to the desk through Hermes. Hermes never
# needs the handoff schema — the CLI returns ready-to-read text.
# ══════════════════════════════════════════════════════════════════════════════

import os as _os
import shutil as _shutil

_HERMES_BIN = _shutil.which("hermes") or str(Path.home() / ".local" / "bin" / "hermes")
_HERMES_ENV = {
    "PATH": _os.pathsep.join([
        str(Path.home() / ".local" / "bin"),
        str(Path.home() / ".hermes" / "node" / "bin"),
        "/usr/local/bin", "/usr/bin", "/bin",
    ]),
    "HOME": str(Path.home()),
}
_HERMES_TIMEOUT_S = 180          # one full agent turn, bounded
_HERMES_ENABLED_KEY = "desk_hermes_fallback"   # plugin_config: market_desk


def _hermes_available() -> bool:
    return Path(_HERMES_BIN).exists()


def _hermes_research(role: str, task: str) -> Optional[str]:
    """Delegate one specialist's research turn to the Hermes agent. Returns
    the agent's final text, or None on absence/failure/timeout."""
    if not _hermes_available():
        return None
    import subprocess
    try:
        proc = subprocess.run(
            [_HERMES_BIN, "-z", task],
            capture_output=True, text=True,
            timeout=_HERMES_TIMEOUT_S,
            env=_HERMES_ENV,
        )
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        print(f"[MarketDesk] ⏱️ Hermes agent turn timed out for {role}")
        return None
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return None
    out = proc.stdout.strip()
    print(f"[MarketDesk] 🤖 research via Hermes for {role} ({len(out)} chars)")
    return out


def hermes_bridge_status() -> dict:
    """Bridged-provider posture for 'status' — measurements, not vibes."""
    return {
        "hermes_cli": _hermes_available(),
        "hermes_gateway": (Path.home() / ".hermes" / "gateway.sock").exists(),
        "fallback_enabled": bool(_setting_val(_HERMES_ENABLED_KEY, True)),
    }


def _setting_val(key: str, default):
    """Read market_desk plugin config without importing the plugin module."""
    try:
        from memory.config_manager import get_plugin_setting
        return get_plugin_setting("market_desk", key, default)
    except Exception:
        return default


def _ask_llm(role: str, task: str, timeout_ms: int = 90_000,
             allow_hermes: bool = True) -> Optional[str]:
    """One bounded generate_content call. Returns None on any failure — the
    caller records a retryable coverage state instead of inventing content.

    Free-tier aware: a 429 RESOURCE_EXHAUSTED carries an explicit retryDelay
    (e.g. 40s). The desk sleeps that long once and retries — pacing itself to
    the quota is strictly better than burning the attempt and writing a
    retryable failure record.

    Hermes fallback (when desk_hermes_fallback is on and the CLI exists): if
    Gemini is quota-exhausted after the paced retry, the research turn goes to
    the Hermes agent — independent skills and keys. Its reply is wrapped so it
    stays inside the same STRICT JSON contract; _extract_json + provenance
    tagging downstream are unchanged."""
    import time as _time
    text = _gemini_call(role, task, timeout_ms, _time)
    if text is not None:
        return text
    if allow_hermes and _setting_val(_HERMES_ENABLED_KEY, True):
        wrapped = ("You are completing a research turn for a JARVIS market-desk "
                   f"role: {role}. Complete the task, then end your reply with "
                   "a line starting with DESK_JSON: followed by a single-line "
                   "JSON object matching the requested schema.\n\n" + task)
        htext = _hermes_research(role, wrapped)
        if htext:
            return f"(research channel: hermes)\n{htext}"
    return None


def _gemini_call(role: str, task: str, timeout_ms: int, _time) -> Optional[str]:
    try:
        from google import genai
        from memory.config_manager import load_api_keys
        key = load_api_keys().get("gemini_api_key")
        if not key:
            return None
        client = genai.Client(api_key=key, http_options={"timeout": timeout_ms})

        def _call():
            return client.models.generate_content(
                model="gemini-flash-latest",
                contents=[
                    {"role": "user", "parts": [{"text": profile_prompt(role)}]},
                    {"role": "user", "parts": [{"text": task}]},
                ],
            )

        try:
            resp = _call()
        except Exception as e:
            if "RESOURCE_EXHAUSTED" not in str(e):
                raise
            delay = 45.0                      # RetryInfo says 30–45s on free tier
            m = re.search(r"retry in ([\d.]+)s", str(e))
            if m:
                delay = min(90.0, float(m.group(1)) + 2.0)
            print(f"[MarketDesk] ⏳ quota wait {delay:.0f}s for {role}…")
            _time.sleep(delay)                # worker thread — loop never blocks
            resp = _call()                    # exactly one paced retry
        return (resp.text or "").strip() or None
    except Exception as e:
        print(f"[MarketDesk] ⚠️ LLM call failed for {role}: {e}")
        return None


def _extract_json(text: str) -> Optional[dict]:
    """Best-effort JSON object extraction from a model reply."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    raw = m.group(0)
    try:
        return json.loads(raw)
    except Exception:
        # common fixups: trailing commas, single quotes are NOT attempted —
        # keep it strict, a parse miss just means retryable coverage.
        raw2 = re.sub(r",\s*([}\]])", r"\1", raw)
        try:
            return json.loads(raw2)
        except Exception:
            return None


# ══════════════════════════════════════════════════════════════════════════════
# Skills (§7) — one reusable procedure per specialist
# ══════════════════════════════════════════════════════════════════════════════

_SKILL_JSON_SPEC = """
Return STRICT JSON only (no prose, no markdown fences) with exactly these keys:
{
  "sources": [{"title": str, "url": str, "publication_time": str|null,
               "retrieval_time": str, "data_as_of": str|null}],
  "verified_facts": [str],
  "interpretation": str,
  "contradictions": [str],
  "unknowns": [str],
  "coverage_status": "completed_with_material_findings |
                      completed_no_material_findings | incomplete_coverage |
                      failed_but_retryable",
  "confidence": "high | medium | low",
  "recommended_lead_action": str
}
Rules: every fact must trace to a listed source; missing data goes in
"unknowns" — never fill it; "no information found" is NOT "nothing happened"
(use incomplete_coverage unless sources were exhausted).
"""


def _specialist_task(role: str, company: dict, window: str,
                     focus: str = "") -> str:
    uni = load_universe()
    return (
        f"TASK: {ROSTER[role]['owns']}\n"
        f"COMPANY: {company['name']} ({company['symbol']}, {company['market']})\n"
        f"APPROVED UNIVERSE ONLY: {', '.join(c['symbol'] for c in uni['companies'])}\n"
        f"TIME WINDOW: the {window} ending {_iso(_now_ist())} (Asia/Kolkata).\n"
        f"Distinguish event time vs publication time vs retrieval time vs "
        f"data-as-of time for every item.\n"
        f"{('FOCUS: ' + focus + chr(10)) if focus else ''}"
        f"Use web search for discovery, treat snippets as leads, prefer primary "
        f"sources (exchange filings, company IR, regulators, government). "
        f"Limit yourself to at most 5 source retrievals; then report.\n"
        f"{_SKILL_JSON_SPEC}"
    )


def run_specialist(role: str, company: Optional[dict], window: str = "day",
                   focus: str = "", store: Optional[HandoffStore] = None,
                   run_id: str = "", request_id: str = "adhoc",
                   test: bool = False) -> dict:
    """Execute one specialist skill → one structured handoff record.

    §10 controls applied here: stable event id, duplicate suppression against
    delivered records, old-run-cannot-overwrite-newer, and a single retry on
    unparseable output (retrieval/retry limits are explicit, not infinite).
    """
    store = store or HandoffStore(test=test)
    period = _reporting_period(window)
    sym = company["symbol"] if company else "desk"
    event_id = _event_id(request_id, role, sym, period)
    run_id = run_id or _event_id("run", event_id, _iso(_now_ist()))

    dup = store.find_delivered(event_id, period)
    if dup:
        return {"duplicate": True, "handoff_id": dup.get("handoff_id"),
                "role": role, "company": sym, "period": period,
                "note": "already delivered for this event+period — suppressed"}

    base = {
        "handoff_id": _event_id("handoff", event_id, run_id),
        "run_id": run_id,
        "event_id": event_id,
        "request_id": request_id,
        "reporting_period": period,
        "record_type": "handoff",
        "bot": role,
        "department": ROSTER[role]["department"],
        "company": company,
        "window": window,
        "created_at": _iso(_now_ist()),
        "delivery_status": "pending_delivery",
        "lead_review": {"status": "none", "reviewer": "", "note": ""},
    }

    text = _ask_llm(role, _specialist_task(role, company or {}, window, focus))
    # Provenance: the Lead must see which research channel produced this
    # handoff — Gemini primary or the Hermes bridge (independent agent).
    research_channel = "hermes" if (text or "").startswith(
        "(research channel: hermes)") else "gemini"
    data = _extract_json(text or "")
    if data is None:                                    # one bounded retry
        text = _ask_llm(role, _specialist_task(role, company or {}, window, focus))
        data = _extract_json(text or "")
    if data is None:
        base.update({
            "coverage_status": COVERAGE_RETRYABLE,
            "confidence": "low",
            "unknowns": ["skill run failed to produce a parseable report"],
            "sources": [], "verified_facts": [], "interpretation": "",
            "contradictions": [], "recommended_lead_action":
                "Mark incomplete; the specialist run is retryable.",
            # The failure NOTICE is itself delivered — coverage and delivery
            # are tracked separately (guide §10). The Lead must see that this
            # run failed rather than hearing nothing.
            "delivery_status": "delivered",
        })
        store.append(base)
        return base

    cov = data.get("coverage_status", COVERAGE_INCOMPLETE)
    if cov not in {COVERAGE_COMPLETED_MATERIAL, COVERAGE_COMPLETED_CLEAR,
                   COVERAGE_INCOMPLETE, COVERAGE_RETRYABLE}:
        cov = COVERAGE_INCOMPLETE
    base.update({
        "sources": data.get("sources", [])[:8],
        "verified_facts": data.get("verified_facts", [])[:12],
        "interpretation": str(data.get("interpretation", ""))[:1200],
        "contradictions": data.get("contradictions", [])[:6],
        "unknowns": data.get("unknowns", [])[:8],
        "coverage_status": cov,
        "confidence": data.get("confidence", "low"),
        "recommended_lead_action": str(data.get("recommended_lead_action", ""))[:400],
        "delivery_status": "delivered",
        "research_channel": research_channel,
    })
    store.append(base)
    return base


# ══════════════════════════════════════════════════════════════════════════════
# Lead synthesis and Chief synthesis
# ══════════════════════════════════════════════════════════════════════════════

def run_lead(lead_role: str, handoffs: list[dict],
             store: Optional[HandoffStore] = None, run_id: str = "",
             request_id: str = "adhoc", test: bool = False) -> dict:
    """The Lead reviews its specialists' handoffs (§6): dedupes, reconciles,
    marks acceptance — and produces ONE consolidated report for the Chief."""
    store = store or HandoffStore(test=test)
    period = _reporting_period("day")
    event_id = _event_id(request_id, "lead", lead_role, period)
    run_id = run_id or _event_id("run", event_id, _iso(_now_ist()))

    def _brief(h: dict) -> str:
        return json.dumps({
            "bot": h.get("bot"), "company": (h.get("company") or {}).get("symbol"),
            "facts": h.get("verified_facts", [])[:4],
            "interpretation": h.get("interpretation", "")[:300],
            "unknowns": h.get("unknowns", [])[:3],
            "coverage": h.get("coverage_status"),
            "confidence": h.get("confidence"),
        }, ensure_ascii=False)

    payload = "\n".join(_brief(h) for h in handoffs) or "(no handoffs received)"
    task = (
        f"REPORTING PERIOD: {period} (Asia/Kolkata). You are the Lead. Below are "
        f"structured handoffs from your authorised specialists:\n{payload}\n\n"
        "Produce STRICT JSON only:\n"
        '{"accepted": [bot names], "needs_correction": [{"bot": str, "why": str}],\n'
        ' "incomplete": [bot names], "dedup_note": str, "conflicts": [str],\n'
        ' "consolidated_report": str, "coverage_gaps": [str],\n'
        ' "escalate_to_independent_review": true|false}\n'
        "Rules: message delivery is not acceptance — verify each handoff's "
        "evidence before accepting; keep coverage gaps visible; do not redo the "
        "specialists' analysis; escalate genuinely disputed investment "
        "conclusions to Independent Research Review."
    )
    record = {
        "handoff_id": _event_id("leadreport", event_id, run_id),
        "run_id": run_id, "event_id": event_id, "request_id": request_id,
        "reporting_period": period, "record_type": "lead_report",
        "bot": lead_role, "department": ROSTER[lead_role]["department"],
        "created_at": _iso(_now_ist()),
        "delivery_status": "pending_delivery",
        "inputs": [h.get("handoff_id") for h in handoffs],
    }
    data = _extract_json(_ask_llm(lead_role, task) or "")
    if data is None:
        record.update({
            "coverage_status": COVERAGE_RETRYABLE,
            "consolidated_report": "(lead synthesis failed — retryable)",
            "delivery_status": "failed",
        })
    else:
        record.update({
            "accepted": data.get("accepted", []),
            "needs_correction": data.get("needs_correction", []),
            "incomplete": data.get("incomplete", []),
            "conflicts": data.get("conflicts", []),
            "coverage_gaps": data.get("coverage_gaps", []),
            "escalate": bool(data.get("escalate_to_independent_review")),
            "consolidated_report": str(data.get("consolidated_report", ""))[:2500],
            "delivery_status": "delivered",
        })
        # §10: a Lead acceptance is also recorded on each specialist handoff.
        accepted = set(record.get("accepted", []))
        for h in handoffs:
            if h.get("bot") in accepted and h.get("record_type") == "handoff":
                h["lead_review"] = {"status": "accepted",
                                    "reviewer": lead_role,
                                    "note": record["handoff_id"]}
                store.append({**h, "record_type": "handoff_review"})
    store.append(record)
    return record


def run_chief(question: str, lead_reports: list[dict],
              store: Optional[HandoffStore] = None, run_id: str = "",
              request_id: str = "adhoc", test: bool = False) -> dict:
    """The Chief consolidates Lead reports (§6) — never raw searches."""
    store = store or HandoffStore(test=test)
    period = _reporting_period("day")
    event_id = _event_id(request_id, "chief", period)
    run_id = run_id or _event_id("run", event_id, _iso(_now_ist()))

    def _brief(r: dict) -> str:
        return json.dumps({
            "lead": r.get("bot"), "report": r.get("consolidated_report", "")[:600],
            "gaps": r.get("coverage_gaps", []), "conflicts": r.get("conflicts", []),
            "escalate": r.get("escalate", False),
        }, ensure_ascii=False)

    task = (
        f"USER QUESTION: {question}\nPERIOD: {period} (Asia/Kolkata).\n"
        "LEAD REPORTS:\n" + ("\n".join(_brief(r) for r in lead_reports)
                             or "(none — say so plainly)") + "\n\n"
        "Produce STRICT JSON only:\n"
        '{"answer": str, "departments_consulted": [str], "disagreements": [str],\n'
        ' "as_of_notes": [str], "coverage_gaps": [str],\n'
        ' "escalated_to_review": true|false}\n'
        "Rules: combine without hiding disagreements; keep every as-of date and "
        "gap visible; label assessments as assessments; no trade recommendations, "
        "no guarantees; if reports are empty or incomplete, the answer must say "
        "exactly what is missing."
    )
    record = {
        "handoff_id": _event_id("chiefreport", event_id, run_id),
        "run_id": run_id, "event_id": event_id, "request_id": request_id,
        "reporting_period": period, "record_type": "chief_report",
        "bot": CHIEF, "department": "chief", "created_at": _iso(_now_ist()),
        "delivery_status": "pending_delivery",
        "inputs": [r.get("handoff_id") for r in lead_reports],
    }
    data = _extract_json(_ask_llm(CHIEF, task) or "")
    if data is None:
        record.update({"answer": "(chief synthesis failed — retryable)",
                       "coverage_status": COVERAGE_RETRYABLE,
                       "delivery_status": "failed"})
    else:
        record.update({
            "answer": str(data.get("answer", ""))[:4000],
            "departments_consulted": data.get("departments_consulted", []),
            "disagreements": data.get("disagreements", []),
            "as_of_notes": data.get("as_of_notes", []),
            "coverage_gaps": data.get("coverage_gaps", []),
            "escalated_to_review": bool(data.get("escalated_to_review")),
            "coverage_status": COVERAGE_COMPLETED_MATERIAL,
            "delivery_status": "delivered",
        })
    store.append(record)
    return record


# ══════════════════════════════════════════════════════════════════════════════
# Orchestration
# ══════════════════════════════════════════════════════════════════════════════

def _window_for(routine_id: str) -> str:
    return "week" if routine_id in {"fundamental_pair", "forensics_pair",
                                    "fund_holdings"} else "day"


def _is_first_monday(d: datetime) -> bool:
    return d.weekday() == 0 and d.day <= 7


def due_routines(now: Optional[datetime] = None,
                 state: Optional[dict] = None) -> list[dict]:
    """Routines whose scheduled time has arrived and not yet run today.
    Paused routines never fire (§11); the fund-holdings gate is explicit."""
    now = now or _now_ist()
    state = state or _load_state()
    cursors = state.get("cursors", {})
    wd = now.weekday()
    due = []
    for r in ROUTINES:
        if not routine_active(r["id"], state):
            continue
        spec = r["weekday"]
        if "-" in spec:
            lo, hi = (int(x) for x in spec.split("-"))
            ok = (lo <= wd <= hi) if lo <= hi else (wd >= lo or wd <= hi)
        else:
            ok = wd == int(spec)
        if not ok:
            continue
        if r.get("first_monday") and not _is_first_monday(now):
            continue
        hh, mm = map(int, r["time"].split(":"))
        if (now.hour, now.minute) < (hh, mm):
            continue
        if cursors.get(r["id"]) == now.date().isoformat():
            continue
        due.append(r)
    return due


# One sweep at a time. The scheduler ticks every 5 minutes while a
# quota-paced sweep can run for many minutes; a second concurrent entry would
# double-spend the budget and duplicate Lead reports before cursors advance.
# §10: a run must never race itself.
_SWEEP_LOCK = threading.Lock()


def run_schedule(max_calls: Optional[int] = None, test: bool = False) -> str:
    """Execute all due, ACTIVE routines: specialist → Lead consolidation.
    Budget-capped; the cursor advances only after a run completes (§10).
    Serialized: overlapping ticks are skipped, never queued."""
    if not _SWEEP_LOCK.acquire(blocking=False):
        return "Desk: a sweep is already running — this tick was skipped."
    try:
        return _run_schedule_locked(max_calls, test)
    finally:
        _SWEEP_LOCK.release()


def _run_schedule_locked(max_calls: Optional[int], test: bool) -> str:
    state = _load_state()
    budget = max_calls or int(state.get("budget", _BUDGET_DEFAULT))
    due = due_routines(state=state)
    if not due:
        return "Desk: no routines are due."
    store = HandoffStore(test=test)
    spent = 0
    done, skipped = [], []
    for r in due:
        if spent >= budget:
            skipped.append(f"{r['id']} (budget)")
            continue
        run_id = _event_id("sched", r["id"], _iso(_now_ist()))
        dept_key = ROSTER[r["role"]]["department"]
        if r["role"] == CHIEF:
            reports = [rec for rec in store.read_all(limit=80)
                       if rec.get("record_type") == "lead_report"
                       and rec.get("reporting_period") == _reporting_period("day")]
            rec = run_chief(f"{r['purpose']} — scheduled synthesis", reports,
                            store=store, run_id=run_id,
                            request_id=r["id"], test=test)
            spent += 1
        else:
            uni = load_universe()["companies"]
            handoffs = []
            for c in uni:
                if spent >= budget:
                    skipped.append(f"{r['id']}:{c['symbol']} (budget)")
                    continue
                h = run_specialist(r["role"], c, window=_window_for(r["id"]),
                                   store=store, run_id=run_id,
                                   request_id=r["id"], test=test)
                spent += 1
                if not h.get("duplicate"):
                    handoffs.append(h)
            lead_role = DEPARTMENTS[dept_key]["lead"]
            rec = run_lead(lead_role, handoffs, store=store, run_id=run_id,
                           request_id=r["id"], test=test)
            spent += 1
        cursors = state.setdefault("cursors", {})
        cursors[r["id"]] = _now_ist().date().isoformat()
        _save_state(state)
        done.append(f"{r['id']} → {rec.get('delivery_status', '?')}")
    msg = "Desk sweep: " + "; ".join(done) if done else "Desk sweep: nothing ran."
    if skipped:
        msg += f" | skipped: {', '.join(skipped)}"
    return msg


def ask_desk(question: str, companies: Optional[list[str]] = None,
             max_depts: int = 2, test: bool = False) -> str:
    """Owner entry point (§16): route a question through Chief → Leads →
    specialists for the needed departments only, then return the synthesis."""
    store = HandoffStore(test=test)
    run_id = _event_id("ask", question, _iso(_now_ist()))
    uni = load_universe()
    targets = []
    for q in (companies or []):
        c = resolve_company(q, uni)
        if c:
            targets.append(c)
    if not targets:
        targets = uni["companies"][:3]          # bounded default: 3 universe names

    dept_order = ["market_news", "equity_research", "market_activity",
                  "industry_geopolitical"]
    lead_reports, spent, notes = [], 0, []
    for dk in dept_order[:max_depts]:
        lead_role = DEPARTMENTS[dk]["lead"]
        handoffs = []
        for spec_role in specialists_of(lead_role)[:2]:     # 2 specialists/dept
            for c in targets[:2]:                            # 2 companies/dept
                if spent >= 8:
                    break
                h = run_specialist(spec_role, c, window="day",
                                   focus=question, store=store,
                                   run_id=run_id, request_id=run_id, test=test)
                spent += 1
                if not h.get("duplicate"):
                    handoffs.append(h)
        lr = run_lead(lead_role, handoffs, store=store, run_id=run_id,
                      request_id=run_id, test=test)
        lead_reports.append(lr)
    chief = run_chief(question, lead_reports, store=store, run_id=run_id,
                      request_id=run_id, test=test)
    gaps = "; ".join(chief.get("coverage_gaps", [])[:3])
    out = chief.get("answer") or "The desk produced no synthesis."
    if gaps:
        out += f"\nCoverage gaps: {gaps}"
    if chief.get("disagreements"):
        out += "\nDisagreements: " + "; ".join(chief["disagreements"][:3])
    return out
