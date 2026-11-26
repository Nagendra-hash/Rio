"""
tests/test_news_briefing.py — NEWS & IMPACT engine + HTTP contract.

No network is used here. The live collector is exercised separately in the
test report; these tests lock in deterministic rules and the API contract.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import actions.news_briefing as nb  # noqa: E402
import dashboard.rio_api as rio_api  # noqa: E402


PAYLOAD = {
    "ok": True, "version": 2,
    "generated_at": "2026-10-22T16:00:00+00:00",
    "date": "2026-10-22",
    "session": {"date_label": "Thu, 22 Oct 2026", "phase_label": "Post-market"},
    "pulse": [{"symbol": "^NSEI", "name": "NIFTY 50", "price": 22687.65,
               "change_pct": -0.54, "change_label": "-0.54%", "ok": True,
               "source": "NSE", "as_of_label": "22 Oct 2026, 21:30 IST",
               "group": "india"}],
    "sections": {
        "B": {"key": "B", "title": "Market Outlook Today", "status": "ok",
              "data": {"direction": "Bearish", "confidence": "4 of 7",
                       "factors": []}, "items": []},
        "G": {"key": "G", "title": "FII & DII Activity", "status": "ok",
              "items": [], "data": {"rows": [
                  {"category": "DII", "buy": 10, "sell": 5, "net": 5,
                   "net_label": "₹5.00 cr", "date": "22-Oct-2026"}]}},
    },
    "sources": [{"name": "NSE", "ok": True, "count": 2}],
    "errors": [], "sections_ok": 16, "sections_total": 16,
    "freshness": {"generated_at": "2026-10-22T16:00:00+00:00",
                  "age_s": 10, "ttl_s": 300, "stale": False,
                  "stale_reason": ""},
    "meta": {"cached": False, "elapsed_ms": 1200, "provider": "news_briefing"},
    "disclaimer": "Research and information only. Not investment advice.",
    "partial": False,
}


def _app(auth: bool = True) -> TestClient:
    app = FastAPI()
    rio_api.register(app, lambda request: auth)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _no_tool_registration(monkeypatch):
    # Never create the real autonomy engine inside an HTTP contract test.
    monkeypatch.setattr(rio_api, "_ensure_news_tool",
                        lambda: {"ok": True, "detail": "test stub"})


# ── deterministic rules ─────────────────────────────────────────────────────

def test_pivot_formula_uses_classic_floor_trader_math():
    p = nb._pivots({"high": 110.0, "low": 90.0, "close": 100.0})
    assert p["p"] == pytest.approx(100.0)
    assert p["r1"] == pytest.approx(110.0)
    assert p["s1"] == pytest.approx(90.0)
    assert p["r2"] == pytest.approx(120.0)
    assert p["s2"] == pytest.approx(80.0)


def test_rsi_is_100_for_a_strictly_rising_series():
    closes = [float(i) for i in range(1, 40)]
    assert nb._rsi(closes, 14) == pytest.approx(100.0)


def test_sma_and_change_helpers():
    assert nb._sma([1, 2, 3, 4], 2) == pytest.approx(3.5)
    assert nb._chg(110, 100) == (10.0, pytest.approx(10.0))
    assert nb._chg(None, 100) == (None, None)


def test_money_and_crore_formatting_never_mislabels_units():
    assert nb._cr(1234.5) == "₹1,234.50 cr"
    assert nb._money(1234.5, "INR") == "₹1,234.50"


def test_sector_catalyst_matching_rejects_summary_only_matches():
    sec = next(s for s in nb.SECTORS if s["key"] == "psu_banks")
    summary_only = {
        "title": "Silver ETFs dip nearly 3% as rate hike fears bite",
        "summary": "PSU bank shares and SBI were mentioned in passing.",
        "sectors": ["PSU Banks"],
    }
    assert nb._article_matches_sector(summary_only, sec) is False
    headline = {
        "title": "PSU bank stocks rally as SBI beats estimates",
        "summary": "", "sectors": ["PSU Banks"],
    }
    assert nb._article_matches_sector(headline, sec) is True


def test_outlook_confidence_compares_like_directions(monkeypatch):
    quotes = {
        "^NSEI": {"ok": True, "change_pct": 1.0, "source": "t",
                  "as_of": "2026-10-22T10:00:00+00:00",
                  "as_of_label": "x"},
        "^NSEBANK": {"ok": True, "change_pct": 1.2, "source": "t",
                     "as_of": "2026-10-22T10:00:00+00:00",
                     "as_of_label": "x"},
        "^INDIAVIX": {"ok": True, "change_pct": -3.0, "source": "t",
                      "as_of": "2026-10-22T10:00:00+00:00",
                      "as_of_label": "x"},
    }
    sec = nb._build_outlook(quotes, {"data": {"indices": []}},
                            {"data": {"sectors": []}}, {}, [], [])
    assert sec["data"]["direction"] == "Bullish"
    assert sec["data"]["confidence"] == ("3 of 3 measured inputs point "
                                         "bullish")


def test_fii_dii_source_count_matches_parsed_rows(monkeypatch):
    # The generic NSE helper reports count=0 for list endpoints; the flows
    # source must expose the number of rows the section actually renders.
    monkeypatch.setattr(nb, "_nse_get", lambda path, refresh=False: (
        [{"category": "FII/FPI", "buyValue": 10, "sellValue": 12,
          "netValue": -2, "date": "22-Oct-2026"}],
        {"ok": True, "count": 0}))
    out = nb._fetch_fii_dii(True)
    assert out["status"] == "ok"
    assert out["status_detail"]["count"] == 1
    assert out["rows"][0]["net"] == -2


def test_delta_ticker_uses_microsecond_time_and_labels_24h_change():
    q = nb._parse_delta_ticker({
        "close": 80959.0, "mark_price": "80966.52", "spot_price": "80999.6",
        "ltp_change_24h": "-4.3004", "mark_change_24h": "-4.1631",
        "timestamp": 1792687964807913, "open": 84597.0,
        "high": 84837.0, "low": 80959.0, "turnover_usd": 1019044716.6,
        "oi_value_usd": "91670973.3016", "funding_rate": "0.0046380",
    }, "BTCUSD", "Bitcoin")
    assert q["ok"] is True
    assert q["source"] == "Delta Exchange India"
    assert q["price"] == 80959.0
    assert q["change_pct"] == pytest.approx(-4.3004)
    assert q["change_basis"] == "24h"
    assert q["change_label"].endswith("24h")
    assert q["as_of"].startswith("2026-10-22T16:52:44")
    assert q["quote_time_estimated"] is False


def test_delta_ticker_without_a_price_is_unavailable():
    q = nb._parse_delta_ticker({"ltp_change_24h": "-1.0"}, "BTCUSD",
                               "Bitcoin")
    assert q["ok"] is False
    assert "no last price" in q["error"]


def test_quote_item_keeps_the_change_basis():
    row = nb._quote_item(nb._parse_delta_ticker(
        {"close": 100.0, "ltp_change_24h": "2.0",
         "timestamp": 1792687964807913}, "BTCUSD", "Bitcoin"))
    assert row["change_basis"] == "24h"
    assert row["change_label"] == "+2.00% 24h"


def test_briefing_digest_is_honest_when_unavailable():
    text = nb.briefing_digest({"ok": False, "errors": ["provider down"]})
    assert "no verified briefing" in text.lower()
    assert "provider down" in text


# ── history persistence ─────────────────────────────────────────────────────

def test_history_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(nb, "HISTORY_DIR", tmp_path)
    (tmp_path / "2026-10-21.json").write_text(
        json.dumps({"ok": True, "date": "2026-10-21",
                    "generated_at": "2026-10-21T16:00:00+00:00",
                    "sections": {"B": {"data": {"direction": "Mixed"}}},
                    "sections_ok": 16, "sections_total": 16}),
        encoding="utf-8")
    rows = nb.list_history(10)
    assert len(rows) == 1
    assert rows[0]["date"] == "2026-10-21"
    assert rows[0]["direction"] == "Mixed"
    loaded = nb.load_history("2026-10-21")
    assert loaded and loaded["ok"] is True
    assert nb.load_history("2026-10-20") is None


# ── HTTP contract ───────────────────────────────────────────────────────────

def test_briefing_endpoints_require_auth():
    c = _app(auth=False)
    assert c.get("/api/rio/news/briefing").status_code == 401
    assert c.post("/api/rio/news/briefing/refresh").status_code == 401
    assert c.get("/api/rio/news/briefing/history").status_code == 401
    assert c.get("/api/rio/news/briefing/settings").status_code == 401
    assert c.post("/api/rio/news/briefing/schedule").status_code == 401


def test_briefing_get_forwards_date_and_refresh(monkeypatch):
    seen = {}

    def fake(refresh=False, date=None):
        seen.update({"refresh": refresh, "date": date})
        return dict(PAYLOAD)

    monkeypatch.setattr(nb, "build_briefing", fake)
    c = _app()
    r = c.get("/api/rio/news/briefing?date=2026-10-21&refresh=1",
              headers={"Authorization": "Bearer test"})
    assert r.status_code == 200
    assert seen == {"refresh": True, "date": "2026-10-21"}
    assert r.json()["sections_ok"] == 16


def test_briefing_refresh_post(monkeypatch):
    monkeypatch.setattr(nb, "build_briefing",
                        lambda refresh=False: dict(PAYLOAD, refresh_seen=refresh))
    c = _app()
    r = c.post("/api/rio/news/briefing/refresh",
               headers={"Authorization": "Bearer test"})
    assert r.status_code == 200
    assert r.json()["refresh_seen"] is True


def test_briefing_history_endpoint(monkeypatch):
    monkeypatch.setattr(nb, "list_history",
                        lambda limit: [{"date": "2026-10-21"}])
    c = _app()
    r = c.get("/api/rio/news/briefing/history?limit=5",
              headers={"Authorization": "Bearer test"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "items": [{"date": "2026-10-21"}],
                        "count": 1}


def test_briefing_settings_rejects_unknown_keys(monkeypatch):
    c = _app()
    r = c.post("/api/rio/news/briefing/settings",
               headers={"Authorization": "Bearer test"},
               json={"evil": True})
    assert r.status_code == 400
    assert "no allowed keys" in r.json()["error"]


def test_briefing_settings_accepts_whitelisted_patch(monkeypatch):
    monkeypatch.setattr(nb, "save_settings",
                        lambda patch: {"refresh_ttl_s": patch["refresh_ttl_s"]})
    c = _app()
    r = c.post("/api/rio/news/briefing/settings",
               headers={"Authorization": "Bearer test"},
               json={"refresh_ttl_s": 600, "evil": True})
    assert r.status_code == 200
    assert r.json()["settings"]["refresh_ttl_s"] == 600


def test_briefing_schedule_endpoint(monkeypatch):
    monkeypatch.setattr(nb, "install_schedule",
                        lambda: {"ok": True, "added": ["x"], "existing": [],
                                 "errors": []})
    c = _app()
    r = c.post("/api/rio/news/briefing/schedule",
               headers={"Authorization": "Bearer test"})
    assert r.status_code == 200
    assert r.json()["added"] == ["x"]


def test_briefing_exception_is_honest(monkeypatch):
    def boom(refresh=False, date=None):
        raise RuntimeError("builder exploded")

    monkeypatch.setattr(nb, "build_briefing", boom)
    c = _app()
    r = c.get("/api/rio/news/briefing",
              headers={"Authorization": "Bearer test"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "builder exploded" in body["errors"][0]
    assert body["sections"] == {}
