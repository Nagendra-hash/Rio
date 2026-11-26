"""
orchestrator/agents/dailylife/toolbox.py — the ONE shared tool interface.

All three daily-life specialists read the outside world exclusively through
this class. It WRAPS the existing infrastructure instead of duplicating it:

    web search / page fetch / local KB   → research_tools.SourceToolbox
                                           (URL policy, secret masking,
                                           permitted-source rules)
    calendar + gmail reads               → productivity.google_tools
                                           (same injectable service, same
                                           tokens, no new scopes)
    email urgency                        → productivity.email_logic
    market quotes                        → trading.data.MarketData
                                           (provenance travels: LIVE /
                                           DELAYED / SIMULATED / UNAVAILABLE)

Design rules:
  * every backend is injectable — tests run fully offline with fakes;
  * every method returns a plain dict with an "ok" flag and never raises
    outward; a failed source is REPORTED, never faked over;
  * every result carries its source label ("source"/"provenance") so no
    renderer can present unqueried data as live.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

log = logging.getLogger("jarvis.dailylife")

# Read caps shared by all three agents.
MAX_SEARCH_RESULTS = 5
MAX_NEWS_TOPICS = 4
MAX_UNREAD = 12
MAX_SYMBOLS = 6
EVENTS_WINDOW_CAP_DAYS = 14


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class DailyLifeToolbox:
    """The shared tool boundary for briefing / meal / travel."""

    def __init__(self,
                 search_fn: Callable[[str, int], list[dict]] | None = None,
                 fetch_fn: Callable[[str], str] | None = None,
                 kb_search: Callable[..., dict] | None = None,
                 calendar_factory: Callable[[], Any] | None = None,
                 gmail_factory: Callable[[], Any] | None = None,
                 market: Any = None,
                 allow_web: bool = True,
                 allow_local_kb: bool = True) -> None:
        from orchestrator.agents.research_tools import SourceToolbox
        self.sources = SourceToolbox(
            search_fn=search_fn, fetch_fn=fetch_fn, kb_search=kb_search,
            permissions=_Perms(allow_web, allow_local_kb))
        self._calendar_factory = calendar_factory
        self._gmail_factory = gmail_factory
        self._market = market

    # ── web search / fetch / local KB (permitted sources) ─────────────────
    def web_search(self, query: str,
                   max_results: int = MAX_SEARCH_RESULTS) -> dict:
        """Permitted-source web search. ok=False when the source failed or
        was disabled — the caller renders that honestly."""
        try:
            results = self.sources.web_search(query, max_results=max_results)
            return {"ok": True, "results": results, "queried_at": _now_iso(),
                    "error": ""}
        except Exception as exc:
            return {"ok": False, "results": [], "queried_at": "",
                    "error": f"web search unavailable: {exc}"[:200]}

    def fetch(self, url: str) -> dict:
        return self.sources.fetch(url)

    def local_kb(self, query: str, top_k: int = 3,
                 collection: str | None = None) -> dict:
        return self.sources.local_kb(query, top_k=top_k,
                                     collection=collection)

    # ── calendar (reads only; events are never created here) ──────────────
    def calendar_events(self, start: datetime, days: int = 1) -> dict:
        """Normalized events for [start, start+days). ok=False (with a
        short reason) when no calendar source is configured/reachable."""
        days = max(1, min(int(days), EVENTS_WINDOW_CAP_DAYS))
        if self._calendar_factory is None:
            return {"ok": False, "events": [], "error":
                    "no calendar source configured"}
        try:
            from orchestrator.agents.productivity import google_tools as gt
            svc = self._calendar_factory()
            raw = gt.list_events(svc, start,
                                 start + timedelta(days=days))
            return {"ok": True, "events": gt.event_window(raw), "error": ""}
        except Exception as exc:
            return {"ok": False, "events": [], "error":
                    f"calendar unavailable: {type(exc).__name__}"[:200]}

    # ── email (reads only; nothing is sent, moved or trashed here) ────────
    def unread_email(self, max_results: int = MAX_UNREAD) -> dict:
        """Unread inbox messages (metadata view). ok=False when no source."""
        if self._gmail_factory is None:
            return {"ok": False, "messages": [],
                    "error": "no email source configured"}
        try:
            from orchestrator.agents.productivity import google_tools as gt
            svc = self._gmail_factory()
            ids = gt.list_message_ids(svc, label="unread",
                                      max_results=max_results)
            messages = gt.fetch_messages(svc, ids, full=False)
            return {"ok": True, "messages": messages, "error": ""}
        except Exception as exc:
            return {"ok": False, "messages": [], "error":
                    f"email unavailable: {type(exc).__name__}"[:200]}

    # ── market (provenance-labeled quotes) ────────────────────────────────
    def market_snapshot(self, symbols: list[str]) -> dict:
        """One quote per symbol with its Provenance label. UNAVAILABLE
        results travel as-is — the briefing renders them as 'unavailable',
        never as a stale or guessed price."""
        clean = [str(s or "").strip() for s in (symbols or [])[:MAX_SYMBOLS]]
        clean = [s for s in clean if s]
        if not clean:
            return {"ok": False, "quotes": [], "error": "no symbols given"}
        if self._market is None:
            return {"ok": False, "quotes": [],
                    "error": "no market source configured"}
        quotes = []
        for sym in clean:
            try:
                res = self._market.quote(sym)
                quotes.append(res.to_dict())
            except Exception as exc:
                quotes.append({"ok": False, "provenance": "unavailable",
                               "source": "unknown", "error": str(exc)[:120],
                               "as_of": _now_iso(), "data": {"symbol": sym}})
        return {"ok": any(q.get("ok") for q in quotes), "quotes": quotes,
                "error": ""}


class _Perms:
    """Tiny adapter: SourceToolbox expects a SourcePermissions-shaped object."""

    def __init__(self, allow_web: bool, allow_local_kb: bool) -> None:
        from orchestrator.agents.research_tools import SourcePermissions
        self._inner = SourcePermissions(allow_web=allow_web,
                                        allow_local_kb=allow_local_kb)
        self.allow_web = allow_web
        self.allow_local_kb = allow_local_kb

    def __getattr__(self, name: str) -> Any:      # delegate the tunables
        return getattr(self._inner, name)
