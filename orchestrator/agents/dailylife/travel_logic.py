"""
orchestrator/agents/dailylife/travel_logic.py — travel planning brain.

Deterministic and honest:

  * deep links are built offline for flights, trains, buses, road, hotels
    and tourism — opening them is how the user reaches live prices;
  * a live web search result is reported ONLY when a permitted search
    actually ran (the toolbox returns the label);
  * route/time and price comparisons run over SUPPLIED rows (the user's
    own numbers or search snippets) — the agent never invents a price,
    duration or availability;
  * delay/price watches compare two LIVE observations and say "need a
    second live check" until both exist.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Optional
from urllib.parse import quote_plus

MAX_ITINERARY_DAYS = 14
SLOT_ORDER = ("morning", "afternoon", "evening")


def _q(text: str) -> str:
    return quote_plus(str(text or "").strip())


def deep_links(origin: str, destination: str, day: str,
               return_day: str = "", passengers: int = 1,
               cabin: str = "economy") -> dict:
    """Offline deep links for every supported mode. These are NOT price
    quotes — they are where the user checks live prices/availability."""
    o, d = _q(origin), _q(destination)
    when = _q(day)
    trip = f"Flights+from+{o}+to+{d}+on+{when}"
    if return_day:
        trip += f"+returning+{_q(return_day)}"
    cabin_code = {"economy": "1", "premium": "2", "business": "3",
                  "first": "4"}.get(str(cabin or "economy").lower(), "1")
    return {
        "flights": (f"https://www.google.com/travel/flights?q={trip}"
                    f"&curr=USD&cabin={cabin_code}&adults={max(1, int(passengers))}"),
        "trains": f"https://www.google.com/search?q=trains+from+{o}+to+{d}+on+{when}",
        "buses": f"https://www.google.com/search?q=bus+from+{o}+to+{d}+on+{when}",
        "road": f"https://www.google.com/maps/dir/?api=1&origin={o}&destination={d}",
        "hotels": f"https://www.google.com/travel/hotels?q=hotels+in+{d}+{when}",
        "tourism": f"https://www.google.com/search?q=things+to+do+in+{d}",
    }


# ── comparisons over supplied rows ───────────────────────────────────────────

def _num(value) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out >= 0 else None


def rank_options(options: list[dict]) -> dict:
    """Rank user/KB-supplied options [{mode, minutes, cost, note}].

    Rows missing a field are kept but excluded from that ranking — a
    comparison never fills a blank with an invented number.
    """
    rows = []
    for i, raw in enumerate(options or []):
        if not isinstance(raw, dict):
            continue
        rows.append({"index": i, "mode": str(raw.get("mode") or "?"),
                     "minutes": _num(raw.get("minutes")),
                     "cost": _num(raw.get("cost")),
                     "note": str(raw.get("note") or "")[:120],
                     "source": str(raw.get("source") or "user-supplied")[:80]})
    by_time = sorted([r for r in rows if r["minutes"] is not None],
                     key=lambda r: r["minutes"])
    by_cost = sorted([r for r in rows if r["cost"] is not None],
                     key=lambda r: r["cost"])
    return {
        "rows": rows,
        "fastest": by_time[0] if by_time else None,
        "cheapest": by_cost[0] if by_cost else None,
        "by_time": by_time, "by_cost": by_cost,
        "unranked_time": [r for r in rows if r["minutes"] is None],
        "unranked_cost": [r for r in rows if r["cost"] is None],
        "source_note": "comparison runs over supplied rows only — no live "
                       "source was queried",
    }


# ── itinerary ────────────────────────────────────────────────────────────────

def build_itinerary(start: str, days: int,
                    items_by_day: Optional[dict] = None) -> dict:
    """Day-by-day plan. items_by_day: {\"YYYY-MM-DD\" or \"0\"..\"n\":
    [{time?, activity, place?, note?}]}. Missing days stay free slots —
    nothing is invented to fill them."""
    try:
        start_day = datetime.strptime(str(start).strip()[:10],
                                      "%Y-%m-%d").date()
    except ValueError:
        start_day = date.today()
    days = max(1, min(int(days or 1), MAX_ITINERARY_DAYS))
    items_by_day = items_by_day or {}
    out = []
    for offset in range(days):
        day = start_day + timedelta(days=offset)
        key = day.isoformat()
        raw = items_by_day.get(key) or items_by_day.get(str(offset)) or []
        items = []
        for it in raw if isinstance(raw, list) else []:
            if not isinstance(it, dict):
                continue
            items.append({"time": str(it.get("time") or "")[:10],
                          "slot": _slot_for(str(it.get("time") or "")),
                          "activity": str(it.get("activity") or "")[:160],
                          "place": str(it.get("place") or "")[:120],
                          "note": str(it.get("note") or "")[:160]})
        items.sort(key=lambda x: (x["slot"] != "morning",
                                  x["slot"] != "afternoon", x["time"]))
        out.append({"date": key, "weekday": day.strftime("%a"),
                    "items": items})
    return {"start": start_day.isoformat(), "days": out}


def _slot_for(hhmm: str) -> str:
    try:
        hh = int(str(hhmm).split(":")[0])
    except (ValueError, IndexError):
        return "morning"
    if hh < 12:
        return "morning"
    if hh < 17:
        return "afternoon"
    return "evening"


# ── watch deltas (price / delay monitoring) ──────────────────────────────────

def watch_delta(kind: str, previous, current) -> dict:
    """Delta between two LIVE observations. kind: 'price' | 'delay'.

    Returns {ok: False, reason} until two numeric observations exist —
    a single observation cannot show a trend.
    """
    prev, curr = _num(previous), _num(current)
    if prev is None or curr is None:
        return {"ok": False, "reason": "need two live observations"}
    delta = round(curr - prev, 2)
    pct = round((delta / prev * 100.0), 2) if prev else None
    return {"ok": True, "kind": kind, "previous": prev, "current": curr,
            "delta": delta, "pct": pct,
            "direction": "up" if delta > 0 else ("down" if delta < 0 else "flat")}


# ── renderers ────────────────────────────────────────────────────────────────

def render_search(origin: str, destination: str, day: str, return_day: str,
                  links: dict, search: Optional[dict] = None) -> str:
    lines = [f"Travel options: {origin} → {destination} on {day}"
             + (f", return {return_day}" if return_day else ""), ""]
    if search and search.get("ok") and search.get("results"):
        at = (search.get("queried_at") or "")[11:16]
        lines.append(f"Live web search{' at ' + at if at else ''} — "
                     "what the sources say:")
        for r in (search.get("results") or [])[:4]:
            title = (r.get("title") or "").strip()[:90]
            snippet = (r.get("snippet") or "").strip()[:160]
            lines.append(f"  • {title}: {snippet}")
            if r.get("url"):
                lines.append(f"    {r['url']}")
        lines.append("")
    else:
        why = (search or {}).get("error") or "no live source queried"
        lines.append(f"No live search ran ({why}) — no prices or "
                     "availability are claimed below.")
        lines.append("")
    lines.append("Check live prices / availability here (I have NOT queried "
                 "them):")
    for mode, url in links.items():
        lines.append(f"  {mode:8s} {url}")
    return "\n".join(lines)


def render_route(rank: dict) -> str:
    rows = rank.get("rows") or []
    if not rows:
        return ("Give me the options to compare as metadata — e.g. "
                "options: [{mode: 'flight', minutes: 95, cost: 4200}, "
                "{mode: 'train', minutes: 420, cost: 900}] — I rank only "
                "what you supply.")
    lines = ["Route / time comparison (over supplied rows only):", ""]
    for r in rows:
        mins = f"{int(r['minutes'])} min" if r["minutes"] is not None \
            else "time not given"
        cost = f"cost {r['cost']:g}" if r["cost"] is not None \
            else "cost not given"
        lines.append(f"  {r['mode']}: {mins}, {cost}"
                     + (f" — {r['note']}" if r["note"] else ""))
    lines.append("")
    if rank.get("fastest"):
        lines.append(f"Fastest: {rank['fastest']['mode']} "
                     f"({int(rank['fastest']['minutes'])} min)")
    if rank.get("cheapest"):
        lines.append(f"Cheapest: {rank['cheapest']['mode']} "
                     f"(cost {rank['cheapest']['cost']:g})")
    skipped = len(rank.get("unranked_time") or []) \
        + len(rank.get("unranked_cost") or [])
    if skipped:
        lines.append(f"{skipped} row(s) skipped in a ranking for missing "
                     "time/cost — nothing was guessed.")
    lines.append(rank.get("source_note", ""))
    return "\n".join(lines)


def render_itinerary(plan: dict) -> str:
    lines = [f"Itinerary from {plan.get('start', '?')}:", ""]
    for day in plan.get("days") or []:
        lines.append(f"{day.get('weekday', '')} {day.get('date', '')}")
        if not day.get("items"):
            lines.append("  (free day — tell me what to plan)")
            continue
        for it in day["items"]:
            when = it.get("time") or it.get("slot") or ""
            place = f" @ {it['place']}" if it.get("place") else ""
            note = f" — {it['note']}" if it.get("note") else ""
            lines.append(f"  {when:>9s} {it.get('activity', '')}{place}{note}")
        lines.append("")
    return "\n".join(lines).strip()


def render_watches(watches: list[dict]) -> str:
    if not watches:
        return ("No watches yet. Add one with metadata kind="
                "(price|delay), origin, destination, date.")
    lines = [f"Watches ({len(watches)}):"]
    for w in watches:
        base = w.get("baseline")
        base_txt = f"{base:g}" if isinstance(base, (int, float)) \
            else "no live baseline yet"
        lines.append(f"  • [{w.get('id')}] {w.get('kind', '?')}: "
                     f"{w.get('origin', '?')} → {w.get('destination', '?')} "
                     f"on {w.get('date', '?')} — baseline: {base_txt}")
    return "\n".join(lines)
