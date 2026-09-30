"""
plugins/market_desk.py — Market Intelligence Desk (voice/dashboard entry).

A port of the Grok "research desk" blueprint into JARVIS: one Chief, five
Leads, seventeen specialist roles (see _desk_roster), structured handoffs with
duplicate/delivery controls (see _desk_engine), and the §12 IST production
schedule where every routine is born PAUSED.

Actions:
  ask      — route a question through Chief → Leads → specialists and return
             the synthesized answer (the guide's §16 owner entry point).
  status   — roster, routine active/paused state and last-run cursors.
  run_now  — execute all due ACTIVE routines once (acceptance testing).
  activate / pause — flip one routine's state (the readback is the reply).
  universe — show the approved company universe.

Research and decision support only. The desk never places trades, never
promises outcomes, never invents data, and never expands the approved universe
from model memory.
"""
from __future__ import annotations

import json

from plugins._desk_roster import (
    CHIEF,
    DEPARTMENTS,
    ROSTER,
    all_leads,
    all_specialists,
    load_universe,
)
from plugins._desk_engine import (
    EVENT_DRIVEN,
    ROUTINES,
    ask_desk,
    due_routines,
    run_schedule,
    set_routine_active,
    _load_state,
)

PLUGIN = {
    "name": "market_desk",
    "description": (
        "Market research desk: 23 specialist roles (news, fundamentals, "
        "forensics, geopolitics, ownership, independent review) under five "
        "Leads and a Chief, producing sourced, timestamped synthesis for the "
        "approved NSE/BSE + crypto universe. Use when the user asks the desk "
        "to research companies, run the scheduled desk routines, or manage "
        "them. Actions: ask (research a question), status (roster + routine "
        "state), run_now (execute due routines), activate/pause (routine "
        "switch), universe (approved companies). Research only — it never "
        "places trades or guarantees outcomes."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": ["ask", "status", "run_now", "activate", "pause",
                         "universe"],
                "description": "Desk operation to perform.",
            },
            "question": {
                "type": "STRING",
                "description": "For 'ask': what to research.",
            },
            "companies": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": "Optional company names/symbols/aliases to scope "
                               "the question (must be in the approved universe).",
            },
            "routine_id": {
                "type": "STRING",
                "description": "For activate/pause: the routine id, e.g. "
                               "news_primary or chief_evening.",
            },
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "market_desk",
    "title": "Market Intelligence Desk",
    "fields": [
        {"key": "max_departments", "type": "number", "label":
         "Departments consulted per ask (1-4)", "default": 2},
        {"key": "sweep_budget", "type": "number", "label":
         "Max LLM calls per scheduled sweep", "default": 12},
    ],
}


def run(parameters, **_):
    action = (parameters or {}).get("action", "status")

    if action == "ask":
        question = (parameters or {}).get("question", "").strip()
        if not question:
            return "Give the desk a question to research, sir."
        max_depts = max(1, min(4, int(_setting("max_departments", 2))))
        return ask_desk(question,
                        companies=(parameters or {}).get("companies"),
                        max_depts=max_depts)

    if action == "status":
        return _status()

    if action == "run_now":
        budget = int(_setting("sweep_budget", 12))
        return run_schedule(max_calls=budget)

    if action in ("activate", "pause"):
        rid = (parameters or {}).get("routine_id", "").strip()
        if not rid:
            return f"Which routine? Known ids: {', '.join(r['id'] for r in ROUTINES)}"
        return set_routine_active(rid, action == "activate")

    if action == "universe":
        uni = load_universe()
        rows = "\n".join(f"• {c['name']} — {c['symbol']} ({c['market']})"
                         for c in uni["companies"])
        return (f"Approved universe (v{uni.get('version', '?')}), "
                f"{len(uni['companies'])} entries:\n{rows}")

    return f"Unknown desk action '{action}'."


def _setting(key: str, default):
    from memory.config_manager import get_plugin_setting
    try:
        return get_plugin_setting("market_desk", key, default)
    except Exception:
        return default


def _status() -> str:
    state = _load_state()
    cursors = state.get("cursors", {})
    try:
        from plugins._desk_engine import hermes_bridge_status
        hb = hermes_bridge_status()
        bridge = ("Hermes bridge: "
                  f"cli={'up' if hb['hermes_cli'] else 'missing'}, "
                  f"gateway={'up' if hb['hermes_gateway'] else 'down'}, "
                  f"research-fallback="
                  f"{'on' if hb['fallback_enabled'] else 'off'}.")
    except Exception:
        bridge = "Hermes bridge: unavailable."
    lines = [
        f"Roster: 1 Chief, {len(all_leads())} Leads, "
        f"{len(all_specialists())} specialists "
        f"({len(EVENT_DRIVEN)} event-driven).",
        bridge,
        f"Due now (active only): "
        f"{', '.join(r['id'] for r in due_routines()) or 'none'}.",
        "Routines:",
    ]
    for r in ROUTINES:
        st = state.get("routines", {}).get(r["id"], {})
        flag = "ACTIVE" if st.get("active") else "paused"
        last = cursors.get(r["id"], "never run")
        if r.get("first_monday"):
            cadence = "first Monday"
        elif "-" in r["weekday"]:
            lo, hi = (int(x) for x in r["weekday"].split("-"))
            cadence = "weekdays" if (lo, hi) == (0, 4) else \
                      f"day {lo}-{hi}"
        else:
            cadence = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][
                int(r["weekday"])]
        lines.append(f"• {r['id']} — {cadence} {r['time']} IST · {flag} · "
                     f"last: {last}")
    return "\n".join(lines)
