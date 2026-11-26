"""
orchestrator/agents/dailylife/briefing_logic.py — the daily briefing brain.

Deterministic assembly + rendering. The agent hands in a DailyLifeToolbox
and the store; every section that needs the outside world comes back with
an honesty label:

    ok=True   → the section text is built ONLY from what the source served,
                and the label names the source ("live web search 08:00",
                provenance for market quotes).
    ok=False  → the section says it is unavailable ("no live source
                queried") — nothing is invented to fill the gap.

render_briefing(..., audio=True) produces a speakable, flat-text variant
(no emoji, no markdown) ready for text-to-speech.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

MAX_CAL_EVENTS = 8
MAX_TOP_EMAILS = 4
MAX_TASKS_SHOWN = 6
MAX_WEATHER_LINES = 2
MAX_NEWS_TOPICS = 4
MAX_NEWS_PER_TOPIC = 2


def resolve_zone(name: str):
    """Reuse the productivity calendar's zone resolver (name → tzinfo)."""
    from orchestrator.agents.productivity import calendar_logic
    return calendar_logic.resolve_zone(name)


# ── assembly ─────────────────────────────────────────────────────────────────

def assemble(toolbox, store, *, city: str = "", topics: list[str] | None = None,
             symbols: list[str] | None = None, market_enabled: bool = False,
             zone=None, now: Optional[datetime] = None) -> dict:
    """Gather every section. Never raises; every section carries ok/error."""
    now = now or (datetime.now(zone) if zone
                  else datetime.now().astimezone())
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    data: dict[str, Any] = {"date": day_start.date().isoformat(),
                            "built_at": now.isoformat(timespec="seconds"),
                            "city": city, "zone": getattr(zone, "key", "")}

    # 1. Calendar — today's events.
    cal = toolbox.calendar_events(day_start, days=1)
    data["calendar"] = cal

    # 2. Important email — unread, urgency-ranked via the email agent's own
    #    classifier (reuse, not duplication).
    mail = toolbox.unread_email()
    ranked = []
    if mail.get("ok"):
        from orchestrator.agents.productivity import email_logic
        for m in mail.get("messages") or []:
            try:
                cls = email_logic.classify_message(m)
            except Exception:
                cls = {"urgency": 0, "urgency_label": "routine",
                       "category": "unknown"}
            ranked.append({**m, "urgency": cls.get("urgency", 0),
                           "urgency_label": cls.get("urgency_label", ""),
                           "category": cls.get("category", "")})
        ranked.sort(key=lambda m: -m["urgency"])
    data["email"] = {"ok": mail.get("ok", False), "error": mail.get("error", ""),
                     "messages": ranked}

    # 3. Tasks — the user's own list from the store (open first).
    tasks = store.tasks()
    open_tasks = [t for t in tasks if not t.get("done")]
    data["tasks"] = {"open": open_tasks, "done_today":
                     len(tasks) - len(open_tasks)}

    # 4. Weather — live web search for the user's city.
    if city:
        wx = toolbox.web_search(f"weather in {city} today",
                                max_results=MAX_WEATHER_LINES)
    else:
        wx = {"ok": False, "results": [], "error": "no city configured"}
    data["weather"] = wx

    # 5. User-selected news — one permitted search per topic (capped).
    news = []
    for topic in (topics or [])[:MAX_NEWS_TOPICS]:
        res = toolbox.web_search(f"{topic} latest news", max_results=3)
        news.append({"topic": topic, **res})
    data["news"] = news

    # 6. Market summary — only when the user enabled it.
    if market_enabled and symbols:
        data["market"] = toolbox.market_snapshot(symbols)
    else:
        data["market"] = {"ok": None, "quotes": [], "error": ""}
    return data


# ── rendering ────────────────────────────────────────────────────────────────

def _fmt_when(ev: dict, zone) -> str:
    s, e = ev["start"], ev["end"]
    if zone is not None:
        s, e = s.astimezone(zone), e.astimezone(zone)
    if ev.get("all_day"):
        return "all day"
    return f"{s.strftime('%H:%M')}–{e.strftime('%H:%M')}"


def _calendar_lines(cal: dict, zone) -> list[str]:
    if not cal.get("ok"):
        return [f"Calendar: unavailable ({cal.get('error') or 'no source'})."]
    events = (cal.get("events") or [])[:MAX_CAL_EVENTS]
    if not events:
        return ["Calendar: nothing scheduled today."]
    return ["Calendar:"] + [f"  • {_fmt_when(e, zone)}  {e['summary']}"
                            for e in events]


def _email_lines(mail: dict) -> list[str]:
    if not mail.get("ok"):
        return [f"Email: unavailable ({mail.get('error') or 'no source'})."]
    msgs = (mail.get("messages") or [])[:MAX_TOP_EMAILS]
    if not msgs:
        return ["Email: inbox is clear — nothing unread."]
    return ["Important email:"] + [
        f"  • {m.get('from', '?')[:40]} — {m.get('subject', '(no subject)')[:70]}"
        + (f" [{m['urgency_label']}]" if m.get("urgency_label") == "urgent"
           else "")
        for m in msgs]


def _task_lines(tasks: dict) -> list[str]:
    open_rows = (tasks.get("open") or [])[:MAX_TASKS_SHOWN]
    if not open_rows:
        return ["Tasks: nothing open."]
    lines = [f"Tasks ({len(tasks.get('open') or [])} open):"]
    lines += [f"  • {t.get('text', '')[:90]}"
              + (f" (due {t['due']})" if t.get("due") else "")
              for t in open_rows]
    return lines


def _honest_label(res: dict) -> str:
    if res.get("ok"):
        at = res.get("queried_at") or ""
        return f"live web search {at[11:16]}" if len(at) >= 16 \
            else "live web search"
    return "unavailable — no live source queried"


def _weather_lines(wx: dict, city: str) -> list[str]:
    if not wx.get("ok"):
        why = wx.get("error") or "no live source queried"
        return [f"Weather ({city or 'city unset'}): unavailable ({why})."]
    out = [f"Weather ({city}) — {_honest_label(wx)}:"]
    for r in (wx.get("results") or [])[:MAX_WEATHER_LINES]:
        title = (r.get("title") or "").strip()
        snippet = (r.get("snippet") or "").strip()[:140]
        out.append(f"  • {title}: {snippet}")
    return out


def _news_lines(news: list[dict]) -> list[str]:
    if not news:
        return ["News: no topics configured (add some with prefs_set)."]
    out = ["News:"]
    for block in news:
        topic = block.get("topic", "?")
        if not block.get("ok"):
            why = block.get("error") or "no live source queried"
            out.append(f"  {topic}: unavailable ({why}).")
            continue
        out.append(f"  {topic} — {_honest_label(block)}:")
        for r in (block.get("results") or [])[:MAX_NEWS_PER_TOPIC]:
            title = (r.get("title") or "").strip()[:100]
            out.append(f"    • {title}" + (f"  [{r['url']}]" if r.get("url")
                                            else ""))
    return out


def _market_lines(market: dict) -> list[str]:
    if market.get("ok") is None:
        return ["Market summary: disabled (enable it in your briefing prefs)."]
    if not market.get("quotes"):
        return [f"Market summary: unavailable "
                f"({market.get('error') or 'no source'})."]
    out = ["Market summary (provenance labeled):"]
    for q in market.get("quotes") or []:
        d = q.get("data") or {}
        sym = d.get("symbol") or "?"
        if not q.get("ok"):
            why = q.get("error") or "no price served"
            out.append(f"  • {sym}: unavailable ({why})")
            continue
        last = d.get("last")
        prov = q.get("provenance", "?")
        prev = d.get("previous_close")
        move = ""
        if last is not None and prev:
            try:
                pct = (float(last) - float(prev)) / float(prev) * 100.0
                move = f" ({pct:+.2f}% vs prev close)"
            except (TypeError, ValueError, ZeroDivisionError):
                move = ""
        out.append(f"  • {sym}: {last} [{prov} from {q.get('source', '?')}]"
                   f"{move}")
    return out


def render_briefing(data: dict, *, audio: bool = False) -> str:
    """The concise briefing. audio=True strips emoji/markdown and joins the
    same content into short speakable sentences."""
    zone = None
    from zoneinfo import ZoneInfo
    if data.get("zone"):
        try:
            zone = ZoneInfo(str(data["zone"]))
        except Exception:
            zone = None
    sections = [
        _calendar_lines(data.get("calendar") or {}, zone),
        _email_lines(data.get("email") or {}),
        _task_lines(data.get("tasks") or {}),
        _weather_lines(data.get("weather") or {}, data.get("city", "")),
        _news_lines(data.get("news") or []),
        _market_lines(data.get("market") or {}),
    ]
    if audio:
        spoken = []
        for section in sections:
            text = " ".join(s.replace("  • ", " ").replace("—", "-")
                            .replace("[", "(").replace("]", ")")
                            .strip() for s in section)
            text = " ".join(text.split())
            spoken.append(text)
        return f"Good day. Here is your briefing. " + " ".join(spoken)
    header = f"Daily briefing — {data.get('date', '')}"
    lines: list[str] = [header, "─" * len(header)]
    for section in sections:
        lines.extend(section)
        lines.append("")
    lines.append("Unverified sections say so explicitly — nothing here is "
                 "invented.")
    return "\n".join(lines).strip()


def render_tasks(tasks: list[dict]) -> str:
    if not tasks:
        return "No tasks on the list. Add one with metadata tasks_add."
    lines = [f"Tasks ({sum(1 for t in tasks if not t.get('done'))} open):"]
    for t in tasks:
        mark = "x" if t.get("done") else " "
        due = f" (due {t['due']})" if t.get("due") else ""
        lines.append(f"  [{mark}] {t.get('id')}: {t.get('text', '')}{due}")
    return "\n".join(lines)
