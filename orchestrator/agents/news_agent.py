"""
orchestrator/agents/news_agent.py — NEWS CURATOR (Phase 19, live edition).

Three real lanes, all read-only:

  briefing  — the full NEWS & IMPACT daily market briefing (sections A–P)
              from actions/news_briefing, with measured data + timestamps.
  headline  — live articles scraped from public publisher RSS/Atom feeds and
              Google News search (DuckDuckGo News as fallback), plus the
              existing actions/web_search pipeline as a last resort.
  calendar  — Forex Factory economic-calendar events. The collector first
              attempts the public calendar HTML (a real scrape) and, because
              Forex Factory guards it with Cloudflare, falls back to Forex
              Factory's own public weekly JSON feed. The result says which
              path was used; no event is ever invented.

Topic extraction is deterministic (no LLM, no invented queries):

    "news" / "headlines" / "top news today"     → top headlines (no query)
    "news about space" / "news on cricket"      → topic "space" / "cricket"
    "cricket news today"                        → topic "cricket"
    "latest on quantum computing"               → topic "quantum computing"
    "forex factory" / "economic calendar"       → Forex Factory calendar
    "high impact economic events"               → calendar, High only
    anything else with substance                → used as the query verbatim

metadata overrides: topic (explicit, wins over text), max_results (1..15,
default 8), source="calendar"|"forex", impact="high|medium|low",
currency="USD".

Failure honesty: a pipeline that raises (no network, every backend down)
returns FAILED with the REAL error — headlines are never invented. An empty
but successful fetch is COMPLETED with an explicit nothing-found note.

Structured items ride in result.data["items"] (and result.data["calendar"]
for the calendar lane) so the dashboard can render them as cards; the text
digest remains the chat answer.
"""
from __future__ import annotations

import re
from typing import Any

from orchestrator.base import BaseAgent
from orchestrator.contracts import AgentRequest, AgentResult, TaskStatus, \
    TaskType

_MAX_RESULTS_CAP = 15

# Bare "give me news" asks — no topic inside.
_GENERIC_RE = re.compile(
    r"^(top\s+|latest\s+|today'?s?\s+|the\s+|breaking\s+|whats?\s+|what's\s+)*"
    r"(world\s+|top\s+)?(news|headlines|top\s+stories|today'?s\s+news)"
    r"(\s+(today|now|please))?\s*[!?.]*$", re.I)
# "news about X" / "news on X" / "latest news for X"
_TOPIC_AFTER_RE = re.compile(
    r"^(?:top\s+|latest\s+|breaking\s+)?news"
    r"(?:\s+(?:about|on|for|regarding|from))?\s+(.+?)\s*[!?.]*$", re.I)
# "space news" / "space news today"
_TOPIC_BEFORE_RE = re.compile(
    r"^(.+?)\s+news(\s+(?:today|now))?\s*[!?.]*$", re.I)
# "latest on X" / "(tell|give) me the latest on X"
_LATEST_ON_RE = re.compile(
    r"^(?:tell\s+me\s+|give\s+me\s+|show\s+me\s+)?(?:the\s+)?"
    r"(?:latest|recent|top)\s+(?:on|about|in)\s+(.+?)\s*[!?.]*$", re.I)

# Explicit calendar asks. Kept in sync with actions/news_feed.is_calendar_query.
_CALENDAR_RE = re.compile(
    r"\b(forex\s*factory|forex\s*calendar|economic\s+calendar|"
    r"economic\s+events?|market\s+calendar|calendar\s+events?)\b", re.I)
_IMPACT_RE = re.compile(r"\b(high|medium|low)[\s-]*impact\b", re.I)
# Full NEWS & IMPACT briefing asks (not every headline request).
_BRIEFING_RE = re.compile(
    r"\b(news\s*(&|and)\s*impact|market\s+briefing|daily\s+briefing|"
    r"news\s+briefing|morning\s+briefing|market\s+wrap|"
    r"briefing)\b", re.I)


def wants_briefing(text: str, metadata: dict | None = None) -> bool:
    """True when the ask is for the full sourced NEWS & IMPACT briefing."""
    md = metadata or {}
    if md.get("briefing") is True or str(md.get("kind") or "") == "briefing":
        return True
    return bool(_BRIEFING_RE.search(str(text or "")))


def extract_topic(text: str) -> str:
    """Deterministic topic extraction from a news ask ("" = top headlines)."""
    t = re.sub(r"\s+", " ", (text or "").strip())
    t = t.rstrip("?!. ")
    if not t or _GENERIC_RE.match(t):
        return ""
    for pattern in (_TOPIC_AFTER_RE, _LATEST_ON_RE, _TOPIC_BEFORE_RE):
        m = pattern.match(t)
        if m:
            topic = m.group(1).strip()
            # a bare generic leftover ("news about it") is not a topic
            if topic and len(topic) >= 2 and not _GENERIC_RE.match(topic):
                return topic
            return ""
    if len(t) >= 4:
        return t                    # substantive ask → verbatim query
    return ""


def wants_calendar(text: str, metadata: dict | None = None) -> bool:
    """True for a Forex Factory / economic-calendar ask."""
    md = metadata or {}
    src = str(md.get("source") or md.get("kind") or "").strip().lower()
    if md.get("calendar") is True or src in {
            "calendar", "forex", "forexfactory", "forex_factory"}:
        return True
    return bool(_CALENDAR_RE.search(str(text or "")))


class NewsCuratorAgent(BaseAgent):
    """Live news + Forex Factory calendar. Read-only."""

    name = "news_agent"
    description = (
        "News Curator: fetches live headlines by scraping publisher RSS/Atom "
        "feeds and Google News search, and Forex Factory economic-calendar "
        "events (calendar HTML scrape, with the official weekly feed as "
        "fallback). Returns structured card items plus a sourced digest, and "
        "reports honestly when a source is unavailable. Read-only — never "
        "sends or posts.")

    # NEWS only: the curator must not claim the RESEARCH lane, or — since the
    # router ranks an agent declaring more task types first — it would shadow
    # the dedicated research_agent for every research ask.
    task_types = (TaskType.NEWS,)

    def handle(self, request: AgentRequest) -> AgentResult:
        md: dict[str, Any] = request.metadata or {}
        try:
            max_results = int(md.get("max_results", 8))
        except (TypeError, ValueError):
            max_results = 8
        max_results = max(1, min(_MAX_RESULTS_CAP, max_results))
        topic = str(md.get("topic") or extract_topic(request.text)).strip()
        impact = str(md.get("impact") or "").strip().lower()
        if not impact:
            m = _IMPACT_RE.search(request.text or "")
            impact = m.group(1).lower() if m else ""
        currency = str(md.get("currency") or "").strip().upper()

        if wants_briefing(request.text, md):
            return self._briefing(request)
        if wants_calendar(request.text, md):
            return self._calendar(request, max_results, impact, currency)
        return self._news(request, topic, max_results)

    # ── NEWS & IMPACT briefing lane ──────────────────────────────────────
    def _briefing(self, request: AgentRequest) -> AgentResult:
        try:
            from actions.news_briefing import (build_briefing,
                                               briefing_digest)
            payload = build_briefing(refresh=bool(
                (request.metadata or {}).get("refresh")))
        except Exception as exc:
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.FAILED, text="",
                data={"briefing": True},
                error=f"briefing unavailable: {type(exc).__name__}: "
                      f"{str(exc)[:180]}", agent=self.name)
        if not payload.get("ok"):
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.FAILED, text="",
                data={"briefing": True, "sections": payload.get("sections") or {},
                      "errors": payload.get("errors") or []},
                error=("briefing unavailable: "
                       + "; ".join(payload.get("errors") or ["unknown error"]))[:300],
                agent=self.name)
        return AgentResult(
            task_id=request.task_id, status=TaskStatus.COMPLETED,
            text=briefing_digest(payload),
            data={
                "briefing": True,
                "version": payload.get("version"),
                "date": payload.get("date"),
                "session": payload.get("session") or {},
                "sections": payload.get("sections") or {},
                "sources": payload.get("sources") or [],
                "errors": payload.get("errors") or [],
                "freshness": payload.get("freshness") or {},
                "meta": payload.get("meta") or {},
                "disclaimer": payload.get("disclaimer") or "",
                "source_pipeline": "actions/news_briefing.build_briefing",
            },
            agent=self.name)

    # ── Forex Factory calendar lane ─────────────────────────────────────
    def _calendar(self, request: AgentRequest, max_results: int,
                  impact: str, currency: str) -> AgentResult:
        try:
            from actions.news_feed import fetch_forex_factory, calendar_digest
            res = fetch_forex_factory(impact=impact, currency=currency,
                                      limit=max_results)
        except Exception as exc:
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.FAILED, text="",
                data={"source": "forex_factory", "impact": impact,
                      "currency": currency},
                error=f"Forex Factory unavailable: {exc}"[:200],
                agent=self.name)

        items = res.get("items") or []
        data = {
            "source": "forex_factory",
            "source_mode": res.get("source_mode") or "",
            "sources": res.get("sources") or [],
            "impact": impact,
            "currency": currency,
            "items": items,
            "calendar": items,
            "max_results": max_results,
        }
        if not items:
            sources = data.get("sources") or []
            all_failed = bool(sources) and not any(s.get("ok") for s in sources)
            if all_failed:
                detail = "; ".join(
                    f"{s.get('name', 'source')}: {s.get('detail', 'failed')}"
                    for s in sources)[:300]
                return AgentResult(
                    task_id=request.task_id, status=TaskStatus.FAILED,
                    text="", data={**data, "empty": True},
                    error=f"Forex Factory unavailable: {detail}", agent=self.name)
            why = "matching the impact/currency filter" if (impact or currency) \
                else "for this week"
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.COMPLETED,
                text=("The Forex Factory calendar answered, but returned no "
                      f"events {why}. Nothing was invented to fill the gap."),
                data={**data, "empty": True}, agent=self.name)

        return AgentResult(
            task_id=request.task_id, status=TaskStatus.COMPLETED,
            text=calendar_digest(items, max_items=max_results),
            data=data, agent=self.name)

    # ── live headline lane ──────────────────────────────────────────────
    def _news(self, request: AgentRequest, topic: str,
              max_results: int) -> AgentResult:
        collected = None
        try:
            from actions.news_feed import collect_news, headlines_digest
            collected = collect_news(query=topic, limit=max_results,
                                     kind="headline")
            items = [i for i in (collected.get("items") or [])
                     if i.get("kind") == "headline"]
            if items:
                return AgentResult(
                    task_id=request.task_id, status=TaskStatus.COMPLETED,
                    text=headlines_digest(items, topic, max_results),
                    data={
                        "topic": topic, "max_results": max_results,
                        "items": items,
                        "counts": collected.get("counts") or {},
                        "sources": collected.get("sources") or [],
                        "source_pipeline": "actions/news_feed.collect_news",
                    },
                    agent=self.name)
        except Exception as exc:
            # keep the real reason for data while the legacy pipeline gets a
            # chance to answer — never swallow the error entirely.
            collected = {"errors": [f"{type(exc).__name__}: {exc}"[:200]]}

        # Legacy CurrentsAPI → Gemini → DDG text pipeline. It also covers the
        # case where every feed answered but with zero items.
        try:
            from actions.web_search import _news
            digest = _news(topic)
        except Exception as exc:
            reason = (collected or {}).get("errors") or []
            detail = f"{type(exc).__name__}: {exc}"[:200]
            if reason:
                detail = f"{detail}; feed collector: {reason[0]}"
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.FAILED, text="",
                data={"topic": topic, "max_results": max_results},
                error=f"news pipeline unavailable: {detail}"[:300],
                agent=self.name)

        if not digest or digest.startswith("No news found"):
            why = f"no news returned for topic '{topic}'" if topic \
                else "no headlines returned"
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.COMPLETED,
                text=f"The news pipeline answered, but found nothing: {why}.",
                data={"topic": topic, "max_results": max_results,
                      "empty": True, "items": []},
                agent=self.name)

        return AgentResult(
            task_id=request.task_id, status=TaskStatus.COMPLETED,
            text=digest,
            data={"topic": topic, "max_results": max_results,
                  "items": [],
                  "source_pipeline": "actions/web_search._news"},
            agent=self.name)
