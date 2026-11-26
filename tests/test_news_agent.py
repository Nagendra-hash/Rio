"""
tests/test_news_agent.py — offline acceptance tests for the Phase 19
News Curator agent (live edition).

NO network, NO news keys, NO LLM: both collectors are monkeypatched
(actions.news_feed.collect_news / fetch_forex_factory and the legacy
actions/web_search._news). These tests lock the agent's own logic.

What this locks in:

  TOPIC EXTRACTION (deterministic, no invented queries)
    1. bare asks ("news", "headlines", "top news today") → no topic
    2. "news about X" / "news on X" / "X news today" /
       "latest on X" → topic X
    3. substantive asks pass through verbatim
  HANDLE — HEADLINES
    4. structured live items become the digest and ride in data["items"]
    5. legacy pipeline digest passes through when the feed collector is empty
    6. FAILED with the REAL error when both pipelines fail
    7. honest empty — "No news found" is reported as such, never padded
    8. max_results metadata clamps to 1..15
  HANDLE — FOREX FACTORY
    9. calendar intent (text or metadata) uses the calendar lane
   10. "high impact" is parsed into the real impact filter
   11. empty calendar is COMPLETED and explicitly empty
   12. calendar failure is FAILED with the real error
  WIRING
   13. registry: real NewsCuratorAgent behind JARVIS_NEWS_AGENT=on,
       stub when unset — same convention as every other agent

Run:  .venv/bin/python -m pytest tests/test_news_agent.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import pytest  # noqa: E402

from orchestrator.agents import build_default_registry  # noqa: E402
from orchestrator.agents.news_agent import (  # noqa: E402
    NewsCuratorAgent, extract_topic, wants_calendar)
from orchestrator.base import StubAgent  # noqa: E402
from orchestrator.contracts import (AgentRequest, TaskStatus,  # noqa: E402
                                    TaskType)


def _req(text: str, **meta) -> AgentRequest:
    return AgentRequest(task_id="task-test-news-0001", task_type=TaskType.NEWS,
                        text=text, metadata=meta)


@pytest.fixture(autouse=True)
def _offline_collector(monkeypatch):
    """Default: the live feed collector answers with nothing.

    Tests that exercise the structured path override collect_news themselves;
    everything else falls through to the mocked legacy pipeline, exactly like
    the original suite. No HTTP is ever attempted.
    """
    import actions.news_feed as nf
    monkeypatch.setattr(nf, "collect_news",
                        lambda **kw: {"items": [], "counts": {},
                                      "sources": []})


def _headline_item(**kw):
    item = {
        "id": "headline-1", "kind": "headline",
        "title": "Oil rises on supply cuts",
        "summary": "Brent crude climbed after producers agreed cuts.",
        "url": "https://example.com/oil", "source": "BBC News",
        "section": "World", "published": "2026-10-20T10:00:00+00:00",
        "domain": "example.com",
    }
    item.update(kw)
    return item


def _calendar_item(**kw):
    item = {
        "id": "calendar-1", "kind": "calendar", "title": "CPI m/m",
        "summary": "USD · High impact · Forecast 0.3% · Previous 0.2%",
        "url": "https://www.forexfactory.com/calendar",
        "source": "Forex Factory", "section": "Economic calendar",
        "published": "2026-10-20T08:30:00-04:00", "currency": "USD",
        "impact": "High", "forecast": "0.3%", "previous": "0.2%",
        "actual": "", "date_label": "Tue 20 Oct", "time_label": "08:30",
        "tz_label": "GMT-04:00",
    }
    item.update(kw)
    return item


# ── 1-3. topic extraction ─────────────────────────────────────────────────────

@pytest.mark.parametrize("ask,topic", [
    ("news", ""),
    ("headlines", ""),
    ("top news today", ""),
    ("what's the latest news?", ""),
    ("news about space", "space"),
    ("news on cricket", "cricket"),
    ("latest news for india", "india"),
    ("cricket news today", "cricket"),
    ("ai news", "ai"),
    ("latest on quantum computing", "quantum computing"),
    ("breaking news please", ""),
    ("Tell me the latest on Mars missions", "Mars missions"),
])
def test_extract_topic(ask, topic):
    assert extract_topic(ask) == topic


def test_extract_topic_verbatim_for_substantive_ask():
    assert extract_topic("implications of the new EU AI act") == \
        "implications of the new EU AI act"


def test_calendar_intent_detection():
    for ask in ("forex factory", "economic calendar", "forex calendar",
                "high impact economic events", "market calendar"):
        assert wants_calendar(ask), ask
    assert not wants_calendar("news about oil")
    assert wants_calendar("anything", {"source": "calendar"})
    assert wants_calendar("anything", {"source": "forex"})


# ── 4-8. handle() — headlines ────────────────────────────────────────────────

def _agent() -> NewsCuratorAgent:
    return NewsCuratorAgent()


def test_handle_structured_items_become_cards_and_digest(monkeypatch):
    import actions.news_feed as nf
    seen = {}

    def fake_collect(**kw):
        seen.update(kw)
        return {
            "items": [_headline_item()],
            "counts": {"headline": 1, "calendar": 0, "total": 1},
            "sources": [{"name": "BBC News", "ok": True, "count": 1}],
        }

    monkeypatch.setattr(nf, "collect_news", fake_collect)
    res = _agent().handle(_req("news about oil"))

    assert res.status is TaskStatus.COMPLETED
    assert "Latest news: oil" in res.text
    assert "Oil rises on supply cuts" in res.text
    assert "BBC News" in res.text
    assert res.data["items"][0]["title"] == "Oil rises on supply cuts"
    assert res.data["counts"]["headline"] == 1
    assert res.data["source_pipeline"] == "actions/news_feed.collect_news"
    assert seen == {"query": "oil", "limit": 8, "kind": "headline"}


def test_handle_completed_passes_digest_through(monkeypatch):
    import actions.web_search as ws
    monkeypatch.setattr(ws, "_news", lambda q: f"Latest news: {q}\n1. Headline")
    res = _agent().handle(_req("news about space"))
    assert res.status is TaskStatus.COMPLETED
    assert res.ok
    assert "Headline" in res.text
    assert res.data["topic"] == "space"
    assert res.data["source_pipeline"] == "actions/web_search._news"


def test_handle_generic_ask_has_empty_topic(monkeypatch):
    import actions.web_search as ws
    seen = {}

    def fake_news(q):
        seen["q"] = q
        return "Latest news: 1. Headline"
    monkeypatch.setattr(ws, "_news", fake_news)
    res = _agent().handle(_req("headlines"))
    assert res.status is TaskStatus.COMPLETED
    assert seen["q"] == ""                       # top-headlines mode


def test_handle_failed_reports_real_error(monkeypatch):
    import actions.web_search as ws

    def boom(q):
        raise RuntimeError("every backend down")
    monkeypatch.setattr(ws, "_news", boom)
    res = _agent().handle(_req("news about space"))
    assert res.status is TaskStatus.FAILED
    assert "every backend down" in res.error
    assert res.text == ""                        # never invents headlines


def test_handle_empty_pipeline_result_is_honest(monkeypatch):
    import actions.web_search as ws
    monkeypatch.setattr(ws, "_news", lambda q: "No news found for: obscurity")
    res = _agent().handle(_req("news about obscurity"))
    assert res.status is TaskStatus.COMPLETED
    assert "found nothing" in res.text
    assert res.data["empty"] is True


def test_handle_max_results_clamped(monkeypatch):
    import actions.web_search as ws
    monkeypatch.setattr(ws, "_news", lambda q: "Latest news: 1. H")
    res = _agent().handle(_req("news", max_results=999))
    assert res.data["max_results"] == 15
    res = _agent().handle(_req("news", max_results="not-a-number"))
    assert res.data["max_results"] == 8


# ── 9-12. handle() — Forex Factory ──────────────────────────────────────────

def test_handle_calendar_intent_returns_calendar_items(monkeypatch):
    import actions.news_feed as nf
    seen = {}

    def fake_calendar(**kw):
        seen.update(kw)
        return {
            "ok": True, "items": [_calendar_item()],
            "source_mode": "official_json_feed",
            "sources": [{"name": "Forex Factory (feed)", "ok": True,
                         "count": 1}],
        }

    monkeypatch.setattr(nf, "fetch_forex_factory", fake_calendar)
    res = _agent().handle(_req("forex factory calendar"))

    assert res.status is TaskStatus.COMPLETED
    assert "Forex Factory calendar" in res.text
    assert "CPI m/m" in res.text
    assert "Forecast 0.3%" in res.text
    assert res.data["calendar"][0]["title"] == "CPI m/m"
    assert res.data["items"] == res.data["calendar"]
    assert res.data["source"] == "forex_factory"
    assert res.data["source_mode"] == "official_json_feed"
    assert seen == {"impact": "", "currency": "", "limit": 8}


def test_handle_calendar_metadata_source(monkeypatch):
    import actions.news_feed as nf
    monkeypatch.setattr(nf, "fetch_forex_factory", lambda **kw: {
        "ok": True, "items": [_calendar_item()], "source_mode": "html_scrape",
        "sources": []})
    res = _agent().handle(_req("what's on the docket", source="forex"))
    assert res.status is TaskStatus.COMPLETED
    assert res.data["source"] == "forex_factory"


def test_handle_high_impact_filter_is_parsed(monkeypatch):
    import actions.news_feed as nf
    seen = {}

    def fake_calendar(**kw):
        seen.update(kw)
        return {"ok": False, "items": [], "source_mode": "official_json_feed",
                "sources": [{"name": "Forex Factory (feed)", "ok": True,
                             "count": 86, "detail": "official weekly feed"}]}

    monkeypatch.setattr(nf, "fetch_forex_factory", fake_calendar)
    res = _agent().handle(_req("show high impact economic events"))
    assert res.status is TaskStatus.COMPLETED
    assert seen["impact"] == "high"
    assert "no events" in res.text.lower()
    assert res.data["empty"] is True


def test_handle_calendar_all_sources_failed_is_failed(monkeypatch):
    import actions.news_feed as nf
    monkeypatch.setattr(nf, "fetch_forex_factory", lambda **kw: {
        "ok": False, "items": [], "source_mode": "",
        "sources": [
            {"name": "Forex Factory (HTML)", "ok": False, "count": 0,
             "detail": "HTTP 403; Cloudflare challenge"},
            {"name": "Forex Factory (feed)", "ok": False, "count": 0,
             "detail": "HTTP 429"},
        ]})
    res = _agent().handle(_req("economic calendar"))
    assert res.status is TaskStatus.FAILED
    assert "HTTP 403" in res.error and "HTTP 429" in res.error
    assert res.text == ""                        # never a fake empty success


def test_handle_calendar_failure_reports_real_error(monkeypatch):
    import actions.news_feed as nf

    def boom(**kw):
        raise RuntimeError("Forex Factory HTTP 429")
    monkeypatch.setattr(nf, "fetch_forex_factory", boom)
    res = _agent().handle(_req("economic calendar"))
    assert res.status is TaskStatus.FAILED
    assert "HTTP 429" in res.error
    assert res.text == ""


# ── 13. registry wiring ──────────────────────────────────────────────────────

def test_registry_real_when_gate_on(monkeypatch):
    monkeypatch.setenv("JARVIS_NEWS_AGENT", "on")
    reg = build_default_registry()
    agent = reg.get("news_agent")
    assert agent is not None and type(agent).__name__ == "NewsCuratorAgent"


def test_registry_stub_when_gate_off(monkeypatch):
    monkeypatch.delenv("JARVIS_NEWS_AGENT", raising=False)
    reg = build_default_registry()
    agent = reg.get("news_agent")
    assert agent is not None and isinstance(agent, StubAgent)
