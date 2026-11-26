"""
orchestrator/agents/dailylife/travel_agent.py — the Travel Agent (Phase 13).

Flights, trains/buses/road, hotels, tourism, route/time and price
comparison, itinerary building, and on-demand delay/price watch checks.

THE HONESTY RULE, enforced in the renderers: a live price or availability
is only ever stated when a permitted live search actually ran in THIS
call — otherwise the agent hands over deep links and says plainly that
nothing was queried. Comparisons run over supplied rows only; watches
need two live observations before they can report a delta.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

from orchestrator.agents.dailylife import travel_logic
from orchestrator.agents.dailylife.checkpoint import DailyLifeCheckpoint
from orchestrator.agents.dailylife.store import DailyLifeStore
from orchestrator.agents.dailylife.toolbox import DailyLifeToolbox
from orchestrator.agents.productivity import security
from orchestrator.base import BaseAgent
from orchestrator.contracts import (AgentRequest, AgentResult, TaskStatus,
                                    TaskType)

log = logging.getLogger("jarvis.travel")


class TravelAgent(BaseAgent):
    name = "travel_agent"
    description = ("Travel specialist: flight/train/bus/road/hotel/tourism "
                   "search via permitted sources and deep links, route & "
                   "price comparison over supplied data, itinerary "
                   "building, and price/delay watches. Never claims a live "
                   "price unless a live source was actually queried.")
    task_types = (TaskType.TRAVEL,)

    def __init__(self, store: DailyLifeStore | None = None,
                 checkpoint: DailyLifeCheckpoint | None = None,
                 toolbox: DailyLifeToolbox | None = None) -> None:
        self.store = store or DailyLifeStore()
        self.checkpoint = checkpoint or DailyLifeCheckpoint()
        self.toolbox = toolbox or DailyLifeToolbox()

    # ── entry point ───────────────────────────────────────────────────────
    def handle(self, request: AgentRequest) -> AgentResult:
        try:
            action = str((request.metadata or {}).get("travel_action")
                         or "").strip().lower() or self._intent(request.text)
            handler = self._HANDLERS.get(action, TravelAgent._search)
            return handler(self, request)
        except Exception as exc:                     # never raise outward
            log.warning("travel agent error: %s", type(exc).__name__)
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.FAILED,
                               error="travel action failed", agent=self.name)

    @staticmethod
    def _intent(text: str) -> str:
        t = (text or "").lower()
        if re.search(r"\bwatch(?:es|list)?\b|\bmonitor\b|\bprice alert\b", t):
            return "watch"
        if re.search(r"\bcompare\b|\broute options?\b|\bfastest\b|"
                     r"\bcheapest\b", t):
            return "route"
        if re.search(r"\bitinerar\w*|\bplan\b.{0,20}\btrip\b|"
                     r"\bday[- ]by[- ]day\b", t):
            return "itinerary"
        return "search"

    # ── result helper ─────────────────────────────────────────────────────
    def _reply(self, request: AgentRequest, text: str, *, action: str,
               status: TaskStatus = TaskStatus.COMPLETED,
               extra: dict | None = None) -> AgentResult:
        return AgentResult(
            task_id=request.task_id, status=status,
            text=security.redact(text),
            data={"dailylife": True, "domain": "travel",
                  "action": action, **(extra or {})},
            agent=self.name)

    def _snapshot(self, kind: str) -> None:
        try:
            self.checkpoint.save({"kind": kind,
                                  "state": self.store.summary()})
        except Exception:
            pass

    # ── parsing helpers ───────────────────────────────────────────────────
    @staticmethod
    def _trip(request: AgentRequest) -> dict:
        """origin/destination/date from metadata first, then the text."""
        meta = request.metadata or {}
        text = request.text or ""
        origin = str(meta.get("origin") or "").strip()
        destination = str(meta.get("destination") or "").strip()
        if not (origin and destination):
            m = re.search(r"\bfrom\s+([A-Za-z][\w\s.'-]{0,39}?)\s+to\s+"
                          r"([A-Za-z][\w\s.'-]{0,39}?)(?:\s+(?:on|for)\b|"
                          r"[.!?]|$)", text, re.IGNORECASE)
            if m:
                origin = origin or m.group(1).strip()
                destination = destination or m.group(2).strip()
        day = str(meta.get("date") or "").strip()
        if not day:
            m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
            day = m.group(1) if m else ""
        if not day:
            m = re.search(r"\b(?:on\s+)?(today|tomorrow)\b", text,
                          re.IGNORECASE)
            if m:
                from datetime import date, timedelta
                base = date.today()
                day = (base + timedelta(days=1)).isoformat() \
                    if m.group(1).lower() == "tomorrow" else base.isoformat()
        return {"origin": origin, "destination": destination, "date": day,
                "return_date": str(meta.get("return_date") or "").strip(),
                "passengers": int(meta.get("passengers") or 1),
                "cabin": str(meta.get("cabin") or "economy")}

    # ── actions ───────────────────────────────────────────────────────────
    def _search(self, request: AgentRequest) -> AgentResult:
        trip = self._trip(request)
        if not (trip["origin"] and trip["destination"]):
            return self._reply(request, (
                "Where from and where to? E.g. 'find flights from Delhi "
                "to Goa on 2026-10-20', or metadata {origin, destination, "
                "date}."), action="search",
                status=TaskStatus.NEEDS_INPUT)
        if not trip["date"]:
            return self._reply(request, "Which date? (YYYY-MM-DD)",
                               action="search",
                               status=TaskStatus.NEEDS_INPUT)
        links = travel_logic.deep_links(
            trip["origin"], trip["destination"], trip["date"],
            trip["return_date"], trip["passengers"], trip["cabin"])
        query = (f"flights from {trip['origin']} to {trip['destination']} "
                 f"on {trip['date']}")
        search = self.toolbox.web_search(query, max_results=3)
        text = travel_logic.render_search(trip["origin"],
                                          trip["destination"], trip["date"],
                                          trip["return_date"], links, search)
        self.store.audit("travel_search", live=bool(search.get("ok")))
        return self._reply(request, text, action="search",
                           extra={"live": bool(search.get("ok")),
                                  "links": links})

    def _route(self, request: AgentRequest) -> AgentResult:
        meta = request.metadata or {}
        options = meta.get("options") or []
        if not options:
            return self._reply(request, travel_logic.render_route({}),
                               action="route")
        rank = travel_logic.rank_options(options)
        return self._reply(request, travel_logic.render_route(rank),
                           action="route",
                           extra={"fastest": (rank.get("fastest") or {})
                                  .get("mode"),
                                  "cheapest": (rank.get("cheapest") or {})
                                  .get("mode")})

    def _itinerary(self, request: AgentRequest) -> AgentResult:
        meta = request.metadata or {}
        start = str(meta.get("start") or meta.get("date") or "").strip()
        if not start:
            m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", request.text or "")
            start = m.group(1) if m else ""
        if not start:
            return self._reply(request, "When should the itinerary start? "
                               "(YYYY-MM-DD)", action="itinerary",
                               status=TaskStatus.NEEDS_INPUT)
        days = int(meta.get("days") or 3)
        plan = travel_logic.build_itinerary(
            start, days, items_by_day=meta.get("items_by_day") or {})
        saved = self.store.save_draft("itinerary", plan,
                                      task_id=request.task_id)
        self._snapshot("itinerary")
        return self._reply(request, travel_logic.render_itinerary(plan),
                           action="itinerary",
                           extra={"draft_id": saved["id"],
                                  "days": len(plan.get("days") or [])})

    def _watch(self, request: AgentRequest) -> AgentResult:
        meta = request.metadata or {}
        sub = str(meta.get("watch_action") or "").strip().lower()
        text = request.text or ""
        if not sub:
            if re.search(r"\bremove|delete|cancel\b", text, re.IGNORECASE):
                sub = "remove"
            elif re.search(r"\bcheck|status|update\b", text, re.IGNORECASE):
                sub = "check"
            elif re.search(r"\blist|show|my watches\b", text, re.IGNORECASE):
                sub = "list"
            else:
                sub = "add"

        if sub == "list":
            return self._reply(request,
                               travel_logic.render_watches(
                                   self.store.watches()),
                               action="watch_list")
        if sub == "remove":
            wid = str(meta.get("watch_id") or "").strip()
            if not wid:
                return self._reply(request, "Which watch id?",
                                   action="watch_remove",
                                   status=TaskStatus.NEEDS_INPUT)
            ok = self.store.remove_watch(wid)
            self._snapshot("watch")
            return self._reply(request,
                               f"Watch {wid} removed." if ok else
                               f"No watch with id {wid}.", action="watch_remove")
        if sub == "check":
            return self._check_watches(request, meta)

        # add
        trip = self._trip(request)
        kind = str(meta.get("kind") or "").strip().lower()
        if kind not in ("price", "delay"):
            kind = "delay" if re.search(r"\bdelay\b", text, re.IGNORECASE) \
                else "price"
        if not (trip["origin"] and trip["destination"]):
            return self._reply(request, "A watch needs origin and "
                               "destination (metadata or 'from X to Y').",
                               action="watch_add",
                               status=TaskStatus.NEEDS_INPUT)
        row = self.store.add_watch({
            "kind": kind, "origin": trip["origin"],
            "destination": trip["destination"], "date": trip["date"],
            "threshold": meta.get("threshold"),
            "note": str(meta.get("note") or "")[:200]})
        self._snapshot("watch")
        return self._reply(request, (
            f"Watch added [{row['id']}]: {kind} for {row['origin']} → "
            f"{row['destination']} on {row['date'] or '(no date)'}.\n"
            "Delay/price monitoring runs when you ask me to check (or when "
            "a scheduler calls the check action) — I only report a delta "
            "once two LIVE observations exist."), action="watch_add",
            extra={"watch_id": row["id"]})

    def _check_watches(self, request: AgentRequest, meta: dict) -> AgentResult:
        watches = self.store.watches()
        wid = str(meta.get("watch_id") or "").strip()
        if wid:
            watches = [w for w in watches if w.get("id") == wid]
        if not watches:
            return self._reply(request, "No watches to check.",
                               action="watch_check")
        lines = ["Watch check (live):"]
        for w in watches:
            query = (f"{w.get('kind', 'price')} "
                     f"{w.get('origin', '')} to {w.get('destination', '')} "
                     f"{w.get('date', '')} current")
            res = self.toolbox.web_search(query, max_results=3)
            if res.get("ok") and res.get("results"):
                at = (res.get("queried_at") or "")[11:16]
                lines.append(f"  • [{w['id']}] {w.get('origin')} → "
                             f"{w.get('destination')}: live search"
                             + (f" at {at}" if at else "")
                             + f" returned {len(res['results'])} result(s) "
                             "— open them to read the current "
                             f"{w.get('kind')}:")
                for r in res["results"][:2]:
                    lines.append(f"      {r.get('title', '')[:80]}"
                                 + (f" — {r['url']}" if r.get("url") else ""))
            else:
                lines.append(f"  • [{w['id']}] {w.get('origin')} → "
                             f"{w.get('destination')}: no live source "
                             f"({res.get('error') or 'unavailable'}) — "
                             "cannot check now.")
            value = meta.get("value")
            if value is not None:
                delta = travel_logic.watch_delta(w.get("kind", "price"),
                                                 w.get("baseline"), value)
                if delta.get("ok"):
                    lines.append(f"      delta vs baseline: "
                                 f"{delta['delta']:+g} "
                                 f"({delta['direction']}"
                                 + (f", {delta['pct']:+g}%"
                                    if delta.get("pct") is not None else "")
                                 + ")")
                else:
                    lines.append(f"      baseline set to {value:g} — I need "
                                 "a second live value to show a delta.")
            self.store.update_watch(w["id"], {
                "last_check": datetime.now().astimezone()
                .isoformat(timespec="seconds"),
                **({"baseline": float(value)}
                   if value is not None and w.get("baseline") is None else {})})
        self._snapshot("watch")
        return self._reply(request, "\n".join(lines), action="watch_check")

    _HANDLERS = {
        "search": _search,
        "route": _route,
        "itinerary": _itinerary,
        "watch": _watch,
    }
