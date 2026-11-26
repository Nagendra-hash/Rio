"""
orchestrator/agents/dailylife/briefing_agent.py — the Daily Briefing Agent
(Phase 13).

Read-only over the outside world: calendar events, unread email, weather
and news searches, optional provenance-labeled market quotes. The only
state it writes is the user's OWN prefs and task list (local, 0600).

Every section that could not reach a source is labeled "unavailable — no
live source queried"; nothing is ever invented to fill a gap. The reply
always carries an audio-ready variant in data["audio"].
"""
from __future__ import annotations

import logging
import re
from typing import Any

from orchestrator.agents.dailylife import briefing_logic
from orchestrator.agents.dailylife.checkpoint import DailyLifeCheckpoint
from orchestrator.agents.dailylife.store import DailyLifeStore
from orchestrator.agents.dailylife.toolbox import DailyLifeToolbox
from orchestrator.agents.productivity import security
from orchestrator.base import BaseAgent
from orchestrator.contracts import (AgentRequest, AgentResult, TaskStatus,
                                    TaskType)

log = logging.getLogger("jarvis.briefing")


class BriefingAgent(BaseAgent):
    name = "briefing_agent"
    description = ("Daily briefing specialist: calendar, important email, "
                   "tasks, weather, user-selected news, and an optional "
                   "provenance-labeled market summary — concise text plus "
                   "an audio-ready variant. Unavailable sources are "
                   "reported honestly, never faked.")
    task_types = (TaskType.BRIEFING,)

    def __init__(self, store: DailyLifeStore | None = None,
                 checkpoint: DailyLifeCheckpoint | None = None,
                 toolbox: DailyLifeToolbox | None = None) -> None:
        self.store = store or DailyLifeStore()
        self.checkpoint = checkpoint or DailyLifeCheckpoint()
        self.toolbox = toolbox or DailyLifeToolbox()

    # ── entry point ───────────────────────────────────────────────────────
    def handle(self, request: AgentRequest) -> AgentResult:
        try:
            meta = request.metadata or {}
            action = str(meta.get("briefing_action") or "").strip().lower()
            if not action:                       # metadata-shaped intents
                if meta.get("tasks_add"):
                    action = "task_add"
                elif meta.get("task_done"):
                    action = "task_done"
                else:
                    action = self._intent(request.text)
            handler = self._HANDLERS.get(action, BriefingAgent._briefing)
            return handler(self, request)
        except Exception as exc:                     # never raise outward
            log.warning("briefing agent error: %s", type(exc).__name__)
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.FAILED,
                               error="briefing failed", agent=self.name)

    @staticmethod
    def _intent(text: str) -> str:
        t = (text or "").lower()
        if re.search(r"\b(?:add|new)\s+(?:a\s+)?task\b|\btodo\b.*\badd\b"
                     r"|^add\s+task", t):
            return "task_add"
        if re.search(r"\b(?:done|complete|finished|finish)\b.{0,20}?\btask\b"
                     r"|\btask\b.{0,20}?\b(?:done|complete)\b", t) \
                or re.match(r"^\s*(?:done|complete[d]?|finished)\b", t):
            return "task_done"
        if re.search(r"\btasks?\b|\btodos?\b", t):
            return "tasks"
        if re.search(r"\b(?:set|change|update)\b.{0,20}?"
                     r"\b(?:city|topics?|news|market|symbols?|prefs?)\b", t):
            return "prefs"
        return "briefing"

    # ── result helper ─────────────────────────────────────────────────────
    def _reply(self, request: AgentRequest, text: str, *, action: str,
               status: TaskStatus = TaskStatus.COMPLETED,
               extra: dict | None = None) -> AgentResult:
        return AgentResult(
            task_id=request.task_id, status=status,
            text=security.redact(text),
            data={"dailylife": True, "domain": "briefing",
                  "action": action, **(extra or {})},
            agent=self.name)

    # ── actions ───────────────────────────────────────────────────────────
    def _briefing(self, request: AgentRequest) -> AgentResult:
        meta = request.metadata or {}
        prefs = self.store.prefs_get("briefing")
        city = str(meta.get("city") or prefs.get("city") or "").strip()
        topics = list(meta.get("topics") or prefs.get("topics") or [])
        market_enabled = bool(meta.get("market_enabled",
                                       prefs.get("market_enabled", False)))
        symbols = list(meta.get("symbols") or prefs.get("symbols") or [])
        zone, _err = briefing_logic.resolve_zone(
            str(meta.get("timezone") or prefs.get("timezone") or ""))
        data = briefing_logic.assemble(
            self.toolbox, self.store, city=city, topics=topics,
            symbols=symbols, market_enabled=market_enabled, zone=zone)
        text = briefing_logic.render_briefing(data)
        audio = briefing_logic.render_briefing(data, audio=True)
        self.store.audit("briefing_built", market=bool(market_enabled))
        self.checkpoint.save({"kind": "briefing", "date": data["date"],
                              "state": self.store.summary()})
        return self._reply(request, text, action="briefing",
                           extra={"audio": audio, "briefing": _slim(data)})

    def _tasks(self, request: AgentRequest) -> AgentResult:
        return self._reply(request,
                           briefing_logic.render_tasks(self.store.tasks()),
                           action="tasks")

    def _task_add(self, request: AgentRequest) -> AgentResult:
        meta = request.metadata or {}
        raw = str(meta.get("tasks_add") or "").strip()
        if not raw:
            m = re.search(r"(?:add|new)\s+(?:a\s+)?(?:task|todo)\b[:\s-]*(.+)",
                          request.text or "", re.IGNORECASE)
            raw = (m.group(1) if m else "").strip()
        if not raw:
            return self._reply(request, "What task should I add? "
                               "(e.g. 'add task pay the electricity bill')",
                               action="task_add",
                               status=TaskStatus.NEEDS_INPUT)
        row = self.store.add_task(raw, due=str(meta.get("due") or ""))
        self.checkpoint.save({"kind": "tasks", "state": self.store.summary()})
        return self._reply(request,
                           f"Task added: {row['text']}\n" +
                           briefing_logic.render_tasks(self.store.tasks()),
                           action="task_add", extra={"task_id": row["id"]})

    def _task_done(self, request: AgentRequest) -> AgentResult:
        meta = request.metadata or {}
        ref = str(meta.get("task_done") or meta.get("task_id") or "").strip()
        if not ref:
            m = re.search(r"\b(?:task\s+)?(?:done|complete[d]?|finished)\b"
                          r"[:\s-]+(.+)", request.text or "", re.IGNORECASE)
            ref = (m.group(1) if m else "").strip()
        ref = re.sub(r"^task\s+", "", ref, flags=re.IGNORECASE).strip()
        if not ref:
            return self._reply(request, "Which task is done? Give its id "
                               "or the start of its text.",
                               action="task_done",
                               status=TaskStatus.NEEDS_INPUT)
        hit = self.store.complete_task(ref)
        if not hit:
            return self._reply(request,
                               f"I couldn't find an open task matching "
                               f"{ref!r}.", action="task_done")
        self.checkpoint.save({"kind": "tasks", "state": self.store.summary()})
        return self._reply(request,
                           f"Done: {hit['text']}\n" +
                           briefing_logic.render_tasks(self.store.tasks()),
                           action="task_done")

    def _prefs(self, request: AgentRequest) -> AgentResult:
        meta = request.metadata or {}
        patch: dict[str, Any] = {}
        for key in ("city", "topics", "symbols", "timezone",
                    "market_enabled"):
            if key in meta:
                patch[key] = meta[key]
        if patch:
            prefs = self.store.prefs_set("briefing", patch)
            self.checkpoint.save({"kind": "prefs",
                                  "state": self.store.summary()})
        else:
            prefs = self.store.prefs_get("briefing")
        lines = ["Briefing preferences:"] + [
            f"  • {k}: {v}" for k, v in sorted(prefs.items())]
        if not prefs:
            lines.append("  (empty — set city, topics, market_enabled, "
                         "symbols via metadata)")
        return self._reply(request, "\n".join(lines), action="prefs")

    _HANDLERS = {
        "briefing": _briefing,
        "tasks": _tasks,
        "task_add": _task_add,
        "task_done": _task_done,
        "prefs": _prefs,
    }


def _slim(data: dict) -> dict:
    """Data payload keeps counts + flags, not full blobs (the text IS the
    user-facing artifact; data stays cheap for the dashboard)."""
    mail = data.get("email") or {}
    market = data.get("market") or {}
    return {
        "date": data.get("date"),
        "city": data.get("city"),
        "calendar_ok": bool((data.get("calendar") or {}).get("ok")),
        "events": len((data.get("calendar") or {}).get("events") or []),
        "email_ok": bool(mail.get("ok")),
        "unread": len(mail.get("messages") or []),
        "open_tasks": len((data.get("tasks") or {}).get("open") or []),
        "weather_ok": bool((data.get("weather") or {}).get("ok")),
        "news_ok": [n.get("topic") for n in (data.get("news") or [])
                    if n.get("ok")],
        "market_enabled": market.get("ok") is not None,
        "market_ok": bool(market.get("ok")),
    }
