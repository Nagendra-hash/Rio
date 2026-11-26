"""
tests/test_news_feed.py — offline tests for the live news collector.

NO network: every parser is fed a fixture, and the collectors are replaced
with fakes. What this locks in:

  1. RSS and Atom parsing → real title/link/source/timestamp, HTML stripped,
     publisher suffix removed, items without a link dropped.
  2. Forex Factory JSON feed parsing → currency, impact, forecast/previous,
     date labels + timezone, no invented "actual" value.
  3. Forex Factory HTML scraping → day breaker + event rows, impact class,
     time, forecast/previous/actual; Cloudflare challenge returns [].
  4. Calendar dedupe is by event id, NOT by the shared calendar URL.
  5. collect_news combines both lanes, reports per-source status, caches the
     result, and never hides a failing source.
  6. An explicit calendar ask bypasses headlines; a topic query bypasses the
     calendar.
  7. Persistence: a failed/empty collector returns ok=False (honest empty).

Run:  python3 -m pytest tests/test_news_feed.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actions import news_feed as nf  # noqa: E402


RSS_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Example</title>
  <item>
    <title>Oil jumps on supply news - Example Wire</title>
    <link>https://example.com/oil</link>
    <description><![CDATA[<p>Brent rose <b>2%</b>.</p>]]></description>
    <pubDate>Tue, 20 Oct 2026 10:00:00 GMT</pubDate>
    <source>Example Wire</source>
  </item>
  <item>
    <title>No link item</title>
    <description>should be skipped</description>
  </item>
</channel></rss>"""

ATOM_FIXTURE = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Example</title>
  <entry>
    <title>Central bank holds rates</title>
    <link rel="alternate" href="https://example.org/rates"/>
    <summary type="html">&lt;p&gt;Rates unchanged at 4.5%.&lt;/p&gt;</summary>
    <updated>2026-10-20T11:30:00Z</updated>
  </entry>
</feed>"""

FF_HTML_FIXTURE = """
<html><body><table>
  <tr class="calendar__row calendar__row--day-breaker">
    <td class="calendar__date">Monday, October 20, 2026</td>
  </tr>
  <tr class="calendar__row">
    <td class="calendar__date"></td>
    <td class="calendar__time">8:30am</td>
    <td class="calendar__currency">USD</td>
    <td class="calendar__impact">
      <span class="icon--ff-impact-red" title="High Impact Expected"></span>
    </td>
    <td class="calendar__event">
      <span class="calendar__event-title">CPI m/m</span>
    </td>
    <td class="calendar__forecast">0.3%</td>
    <td class="calendar__previous">0.2%</td>
    <td class="calendar__actual">0.4%</td>
  </tr>
  <tr class="calendar__row">
    <td class="calendar__date"></td>
    <td class="calendar__time">10:00am</td>
    <td class="calendar__currency">EUR</td>
    <td class="calendar__impact">
      <span class="icon--ff-impact-orange" title="Medium Impact Expected"></span>
    </td>
    <td class="calendar__event">
      <span class="calendar__event-title">Industrial Production m/m</span>
    </td>
    <td class="calendar__forecast">0.1%</td>
    <td class="calendar__previous">-0.2%</td>
  </tr>
</table></body></html>"""

FF_FEED_FIXTURE = [
    {"title": "CPI m/m", "country": "USD",
     "date": "2026-10-20T08:30:00-04:00", "impact": "High",
     "forecast": "0.3%", "previous": "0.2%"},
    {"title": "GDP q/q", "country": "GBP",
     "date": "2026-10-21T04:00:00-04:00", "impact": "Medium",
     "forecast": "0.2%", "previous": "0.1%"},
]


@pytest.fixture(autouse=True)
def _clean_caches():
    nf._cache.clear()
    nf._ff_raw_cache.update({"t": 0.0, "data": None, "ttl": 0.0})
    yield
    nf._cache.clear()
    nf._ff_raw_cache.update({"t": 0.0, "data": None, "ttl": 0.0})


def _headline(title="Headline", url="https://example.com/a", **kw):
    item = {
        "id": nf._item_id("headline", url, title),
        "kind": "headline", "title": title, "summary": "summary",
        "url": url, "source": "Example", "section": "World",
        "published": "2026-10-20T10:00:00+00:00",
        "domain": "example.com", "currency": "", "impact": "",
        "forecast": "", "previous": "", "actual": "",
        "date_label": "", "time_label": "", "tz_label": "",
    }
    item.update(kw)
    return item


def _calendar(title="CPI m/m", **kw):
    item = {
        "id": nf._item_id("calendar", "USD", title,
                          "2026-10-20T08:30:00-04:00"),
        "kind": "calendar", "title": title, "summary": "USD · High impact",
        "url": nf.FF_HOME, "source": "Forex Factory",
        "section": "Economic calendar",
        "published": "2026-10-20T08:30:00-04:00",
        "domain": "forexfactory.com", "currency": "USD", "impact": "High",
        "forecast": "0.3%", "previous": "0.2%", "actual": "",
        "date_label": "Tue 20 Oct", "time_label": "08:30",
        "tz_label": "GMT-04:00",
    }
    item.update(kw)
    return item


# ── 1. RSS / Atom ────────────────────────────────────────────────────────────

def test_parse_rss_strips_html_and_publisher_suffix():
    items = nf.parse_feed(RSS_FIXTURE,
                          {"name": "Example Wire", "section": "Markets"})
    assert len(items) == 1                       # item without link is dropped
    it = items[0]
    assert it["title"] == "Oil jumps on supply news"
    assert it["url"] == "https://example.com/oil"
    assert it["summary"] == "Brent rose 2%."
    assert it["source"] == "Example Wire"
    assert it["section"] == "Markets"
    assert it["published"] == "2026-10-20T10:00:00+00:00"
    assert it["kind"] == "headline" and it["id"]


def test_parse_atom():
    items = nf.parse_feed(ATOM_FIXTURE,
                          {"name": "Atom Example", "section": "World"})
    assert len(items) == 1
    it = items[0]
    assert it["title"] == "Central bank holds rates"
    assert it["url"] == "https://example.org/rates"
    assert it["summary"] == "Rates unchanged at 4.5%."
    assert it["published"] == "2026-10-20T11:30:00+00:00"


def test_parse_feed_rejects_malformed_xml():
    with pytest.raises(Exception):
        nf.parse_feed("<rss><channel><item>", {"name": "bad"})


# ── 2. Forex Factory JSON feed ──────────────────────────────────────────────

def test_parse_forex_feed_real_fields_only():
    items = nf.parse_forex_feed(FF_FEED_FIXTURE)
    assert len(items) == 2
    cpi = items[0]
    assert cpi["kind"] == "calendar"
    assert cpi["currency"] == "USD"
    assert cpi["impact"] == "High"
    assert cpi["forecast"] == "0.3%" and cpi["previous"] == "0.2%"
    assert cpi["actual"] == ""                  # the feed has no actual
    assert cpi["date_label"] == "Tue 20 Oct"
    assert cpi["time_label"] == "08:30"
    assert cpi["tz_label"] == "GMT-04:00"
    assert cpi["url"] == nf.FF_HOME
    assert cpi["state"] in ("past", "upcoming")
    assert all(i["impact"] for i in items)


def test_parse_forex_feed_accepts_bytes_and_json_string():
    import json
    raw = json.dumps(FF_FEED_FIXTURE).encode()
    assert len(nf.parse_forex_feed(raw)) == 2
    assert nf.parse_forex_feed({"not": "a list"}) == []


# ── 3. Forex Factory HTML scrape ────────────────────────────────────────────

def test_parse_forex_html_day_breaker_and_rows():
    items = nf.parse_forex_html(FF_HTML_FIXTURE)
    assert len(items) == 2
    first, second = items
    assert first["title"] == "CPI m/m"
    assert first["currency"] == "USD"
    assert first["impact"] == "High"
    assert first["forecast"] == "0.3%"
    assert first["previous"] == "0.2%"
    assert first["actual"] == "0.4%"
    assert first["date_label"] == "Tue 20 Oct"
    assert first["time_label"] == "08:30"
    assert first["published"].startswith("2026-10-20T08:30")
    assert second["impact"] == "Medium"
    assert second["actual"] == ""               # absent cell → honest empty


def test_parse_forex_html_cloudflare_challenge_returns_empty():
    page = ("<html><head><title>Just a moment...</title></head>"
            "<body>cf-challenge</body></html>")
    assert nf.parse_forex_html(page) == []
    assert nf.parse_forex_html("<html><body>no calendar</body></html>") == []


def test_parse_feed_suppresses_google_news_summary():
    blob = """<rss version="2.0"><channel><item>
      <title>Oil rises - Reuters</title>
      <link>https://news.google.com/rss/articles/abc</link>
      <description>Oil rises Reuters Related headline Another story</description>
      <pubDate>Tue, 20 Oct 2026 10:00:00 GMT</pubDate>
      <source>Reuters</source>
    </item></channel></rss>"""
    items = nf.parse_feed(blob, {"key": "google", "name": "Google News",
                                 "section": "Search"})
    assert items[0]["title"] == "Oil rises"
    assert items[0]["summary"] == ""             # no duplicated title/wire list


def test_parse_feed_strips_title_that_is_only_repeated_with_source():
    blob = """<rss version="2.0"><channel><item>
      <title>Rates held</title>
      <link>https://example.com/rates</link>
      <description>Rates held - Example Wire</description>
      <source>Example Wire</source>
    </item></channel></rss>"""
    items = nf.parse_feed(blob, {"name": "Example Wire"})
    assert items[0]["summary"] == ""


# ── 4. dedupe ───────────────────────────────────────────────────────────────

def test_calendar_dedupe_uses_event_id_not_shared_url():
    a = _calendar(title="CPI m/m")
    b = _calendar(title="GDP q/q", id=nf._item_id("calendar", "GBP", "GDP q/q",
                                                  "2026-10-21T04:00:00-04:00"))
    out = nf._dedupe([a, b, a])
    assert len(out) == 2
    assert [x["title"] for x in out] == ["CPI m/m", "GDP q/q"]


def test_headline_dedupe_uses_url():
    a = _headline(title="One", url="https://example.com/x")
    b = _headline(title="One duplicate", url="https://example.com/x")
    assert len(nf._dedupe([a, b])) == 1


# ── 5-7. collector ──────────────────────────────────────────────────────────

def test_collect_combines_lanes_and_caches(monkeypatch):
    calls = {"headlines": 0, "calendar": 0}

    def fake_headlines(q, limit):
        calls["headlines"] += 1
        return [_headline()], [{"name": "Example", "ok": True, "count": 1,
                                "ms": 1, "detail": "RSS/Atom feed",
                                "mode": "rss"}]

    def fake_calendar(impact, currency, limit):
        calls["calendar"] += 1
        return {"ok": True, "items": [_calendar()],
                "source_mode": "official_json_feed",
                "sources": [{"name": "Forex Factory (feed)", "ok": True,
                             "count": 1, "ms": 1, "detail": "feed",
                             "mode": "official_json_feed"}]}

    monkeypatch.setattr(nf, "_search_headlines", fake_headlines)
    monkeypatch.setattr(nf, "fetch_forex_factory", fake_calendar)

    out = nf.collect_news(limit=5)
    assert out["ok"] is True
    assert out["counts"] == {"headline": 1, "calendar": 1, "total": 2}
    assert [i["kind"] for i in out["items"]] == ["headline", "calendar"]
    assert {s["name"] for s in out["sources"]} == {"Example",
                                                   "Forex Factory (feed)"}
    assert out["cached"] is False

    cached = nf.collect_news(limit=5)
    assert cached["cached"] is True
    assert calls == {"headlines": 1, "calendar": 1}


def test_calendar_query_skips_headlines(monkeypatch):
    seen = {}

    def fake_calendar(impact, currency, limit):
        seen["impact"] = impact
        return {"ok": True, "items": [_calendar(impact="High")],
                "source_mode": "official_json_feed", "sources": []}

    monkeypatch.setattr(nf, "fetch_forex_factory", fake_calendar)
    monkeypatch.setattr(nf, "_search_headlines",
                        lambda *a, **k: pytest.fail("headlines must not run"))

    out = nf.collect_news("forex factory high impact", limit=5)
    assert out["kind"] == "calendar"
    assert out["counts"]["calendar"] == 1
    assert out["counts"]["headline"] == 0


def test_topic_query_skips_calendar(monkeypatch):
    monkeypatch.setattr(nf, "_search_headlines",
                        lambda q, limit: ([_headline()], []))
    monkeypatch.setattr(nf, "fetch_forex_factory",
                        lambda *a, **k: pytest.fail("calendar must not run"))
    out = nf.collect_news("oil prices", limit=5)
    assert out["kind"] == "headline"
    assert out["counts"] == {"headline": 1, "calendar": 0, "total": 1}


def test_collect_reports_failures_without_inventing(monkeypatch):
    monkeypatch.setattr(nf, "_search_headlines",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("RSS down")))
    monkeypatch.setattr(nf, "fetch_forex_factory",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("FF 429")))
    out = nf.collect_news(limit=5)
    assert out["ok"] is False
    assert out["items"] == []
    assert any("RSS down" in e for e in out["errors"])
    assert any("FF 429" in e for e in out["errors"])


def test_collect_forwards_impact_and_currency_filter(monkeypatch):
    seen = {}

    def fake_calendar(impact, currency, limit):
        seen.update(impact=impact, currency=currency, limit=limit)
        return {"ok": False, "items": [], "source_mode": "",
                "sources": [{"name": "Forex Factory (feed)", "ok": False,
                             "count": 0, "ms": 1, "detail": "HTTP 429",
                             "mode": "official_json_feed"}]}

    monkeypatch.setattr(nf, "fetch_forex_factory", fake_calendar)
    out = nf.collect_news(kind="calendar", impact="high,medium",
                          currency="usd", limit=9)
    assert seen == {"impact": "high,medium", "currency": "usd", "limit": 9}
    assert out["ok"] is False
    assert out["errors"] == ["Forex Factory (feed): HTTP 429"]


def test_calendar_sorts_upcoming_first_then_released(monkeypatch):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    past = (now - timedelta(hours=3)).isoformat()
    future = (now + timedelta(hours=3)).isoformat()
    raw = {"items": [
        _calendar(title="Past", published=past,
                  id=nf._item_id("calendar", "USD", "Past", past)),
        _calendar(title="Future", published=future,
                  id=nf._item_id("calendar", "USD", "Future", future)),
    ], "mode": "official_json_feed", "sources": []}
    monkeypatch.setattr(nf, "_forex_raw", lambda: raw)
    out = nf.fetch_forex_factory(limit=10)
    assert [i["title"] for i in out["items"]] == ["Future", "Past"]


def test_partial_result_caches_for_30s(monkeypatch):
    def failing_calendar(impact, currency, limit):
        return {"ok": False, "items": [], "source_mode": "",
                "sources": [{"name": "Forex Factory (feed)", "ok": False,
                             "count": 0, "ms": 1, "detail": "HTTP 429",
                             "mode": "official_json_feed"}]}

    monkeypatch.setattr(nf, "fetch_forex_factory", failing_calendar)
    out = nf.collect_news(kind="calendar", limit=4)
    assert out["ok"] is False
    assert nf._cache and all(e["ttl"] == 30.0 for e in nf._cache.values())


def test_fetch_forex_factory_filters_cached_raw(monkeypatch):
    raw = {"items": [_calendar(title="CPI", impact="High"),
                     _calendar(title="GDP", impact="Low",
                               id=nf._item_id("calendar", "GBP", "GDP",
                                              "2026-10-21T04:00:00-04:00"))],
           "mode": "official_json_feed",
           "sources": [{"name": "Forex Factory (feed)", "ok": True,
                        "count": 2, "ms": 1, "detail": "feed",
                        "mode": "official_json_feed"}]}
    monkeypatch.setattr(nf, "_forex_raw", lambda: raw)
    out = nf.fetch_forex_factory(impact="high", limit=10)
    assert out["ok"] is True
    assert [i["title"] for i in out["items"]] == ["CPI"]
    assert out["source_mode"] == "official_json_feed"


# ── digest helpers ──────────────────────────────────────────────────────────

def test_digests_contain_real_values_only():
    text = nf.headlines_digest([_headline()], "oil")
    assert "Latest news: oil" in text
    assert "Headline" in text and "https://example.com/a" in text
    cal = nf.calendar_digest([_calendar()], max_items=3)
    assert "Forex Factory calendar" in cal
    assert "USD" in cal and "High impact" in cal
    assert "Forecast 0.3%" in cal and "Previous 0.2%" in cal
    assert "Actual" not in cal                   # empty values are omitted


def test_calendar_intent_detection():
    for ask in ("forex factory", "economic calendar", "forex calendar",
                "high impact economic events", "market calendar"):
        assert nf.is_calendar_query(ask), ask
    for ask in ("news about oil", "india news", "cricket"):
        assert not nf.is_calendar_query(ask), ask
