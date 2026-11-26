"""
tests/test_rio_news.py — HTTP contract for the Newsroom page + API.

The collector is monkeypatched, so no HTTP request leaves the process.

What this locks in:
  1. GET /api/rio/news requires the same bearer auth as every other API.
  2. Query parameters (q, kind, impact, currency, limit, refresh) are
     forwarded to the collector unchanged.
  3. The payload (items, counts, sources, honest errors) is returned as-is.
  4. A collector exception becomes an honest 200 {ok:false,error} — never a
     fabricated feed.
  5. /news serves the shell with news.js, and the asset is whitelisted.
  6. /news is in the RIO route table (and therefore the server's dest guard).
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dashboard.rio_api as rio_api  # noqa: E402


nf_url = "https://www.forexfactory.com/calendar"

PAYLOAD = {
    "ok": True,
    "query": "",
    "kind": "all",
    "impact": "high,medium",
    "currency": "",
    "items": [
        {"id": "headline-1", "kind": "headline", "title": "Oil rises",
         "summary": "Supply cuts.", "url": "https://example.com/oil",
         "source": "BBC News", "published": "2026-10-20T10:00:00+00:00",
         "section": "World", "domain": "example.com"},
        {"id": "calendar-1", "kind": "calendar", "title": "CPI m/m",
         "summary": "USD · High impact", "url": nf_url,
         "source": "Forex Factory", "currency": "USD", "impact": "High",
         "forecast": "0.3%", "previous": "0.2%", "actual": "",
         "published": "2026-10-20T08:30:00-04:00",
         "date_label": "Tue 20 Oct", "time_label": "08:30",
         "tz_label": "GMT-04:00"},
    ],
    "counts": {"headline": 1, "calendar": 1, "total": 2},
    "sources": [
        {"name": "BBC News", "ok": True, "count": 1, "ms": 120,
         "detail": "RSS/Atom feed", "mode": "rss"},
        {"name": "Forex Factory (HTML)", "ok": False, "count": 0, "ms": 200,
         "detail": "HTTP 403; Cloudflare challenge", "mode": "html_scrape"},
        {"name": "Forex Factory (feed)", "ok": True, "count": 1, "ms": 90,
         "detail": "official weekly calendar feed",
         "mode": "official_json_feed"},
    ],
    "errors": ["Forex Factory (HTML): HTTP 403; Cloudflare challenge"],
    "partial": True,
    "fetched_at": "2026-10-20T12:00:00+00:00",
    "elapsed_ms": 640,
    "cached": False,
}


def _app(auth: bool = True) -> TestClient:
    app = FastAPI()
    rio_api.register(app, lambda request: auth)
    return TestClient(app)


# ── 1-4. API contract ────────────────────────────────────────────────────────

def test_news_endpoint_requires_auth(monkeypatch):
    monkeypatch.setattr(rio_api, "_collect_news", lambda **k: dict(PAYLOAD))
    c = _app(auth=False)
    assert c.get("/api/rio/news").status_code == 401
    assert c.get("/api/rio/news",
                 headers={"Authorization": "Bearer nope"}).status_code == 401


def test_news_endpoint_forwards_filters(monkeypatch):
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return dict(PAYLOAD)

    monkeypatch.setattr(rio_api, "_collect_news", fake)
    c = _app()
    r = c.get("/api/rio/news?q=oil&kind=calendar&impact=high"
              "&currency=USD&limit=7&refresh=1",
              headers={"Authorization": "Bearer test"})
    assert r.status_code == 200
    assert seen == {"query": "oil", "limit": 7, "kind": "calendar",
                    "impact": "high", "currency": "USD", "refresh": True}
    body = r.json()
    assert body["counts"]["total"] == 2
    assert body["items"][1]["kind"] == "calendar"
    assert body["partial"] is True


def test_news_endpoint_returns_source_honesty(monkeypatch):
    monkeypatch.setattr(rio_api, "_collect_news", lambda **k: dict(PAYLOAD))
    c = _app()
    body = c.get("/api/rio/news",
                 headers={"Authorization": "Bearer test"}).json()
    names = {s["name"]: s for s in body["sources"]}
    assert names["Forex Factory (HTML)"]["ok"] is False
    assert names["Forex Factory (feed)"]["ok"] is True
    assert names["Forex Factory (feed)"]["mode"] == "official_json_feed"
    assert body["errors"] == [PAYLOAD["errors"][0]]


def test_news_endpoint_exception_is_honest(monkeypatch):
    def boom(**kw):
        raise RuntimeError("collector exploded")
    monkeypatch.setattr(rio_api, "_collect_news", boom)
    c = _app()
    r = c.get("/api/rio/news", headers={"Authorization": "Bearer test"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["items"] == []
    assert "collector exploded" in body["error"]


# ── 5-6. shell + route table ─────────────────────────────────────────────────

def test_news_shell_route_and_asset():
    c = _app()
    r = c.get("/news")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    assert "news.js" in r.text
    a = c.get("/rio/assets/news.js")
    assert a.status_code == 200
    assert "application/javascript" in a.headers.get("content-type", "")
    assert "RIONews" in a.text


def test_news_in_route_table():
    assert "/news" in rio_api.ROUTES
