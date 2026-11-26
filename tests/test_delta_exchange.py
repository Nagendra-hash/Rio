"""
tests/test_delta_exchange.py — offline contract tests for Delta India.

No network: every Delta response is a fake payload. The tests lock in the
parts that are easy to get wrong:

  1. product pagination and exact/base resolution;
  2. backward candle pagination + normalization (no invented bars);
  3. funding events at the product's interval, decimal rates;
  4. MarketData venue routing (explicit venue and cached-product detection);
  5. paper-only execution metadata and the disabled LiveBroker;
  6. the backtest Delta catalog/run contract;
  7. Telegram recovery classification (recovered flap != ERROR);
  8. the Delta UI wiring exists on both rooms.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from orchestrator.agents import delta_exchange as dx  # noqa: E402
from orchestrator.agents.backtest import symbols as bs  # noqa: E402
from orchestrator.agents.trading import broker as bk  # noqa: E402
from orchestrator.agents.trading.data import (  # noqa: E402
    DataResult, MarketData, Provenance,
)
from orchestrator.agents.trading.delta import DeltaTradingAdapter  # noqa: E402


def _product(symbol: str, contract_type: str, underlying: str,
             quote: str, *, pid: int = 1, interval: int = 28800) -> dict:
    return {
        "symbol": symbol,
        "id": pid,
        "contract_type": contract_type,
        "underlying_asset": {"symbol": underlying},
        "quoting_asset": {"symbol": quote},
        "description": f"{underlying} {contract_type}",
        "state": "live",
        "trading_status": "operational",
        "tick_size": "0.5",
        "contract_value": "0.001",
        "default_leverage": "200",
        "product_specs": {"rate_exchange_interval": interval},
    }


PRODUCTS = [
    _product("BTCUSD", "perpetual_futures", "BTC", "USD", pid=27),
    _product("BTC_INR", "spot", "BTC", "INR", pid=28, interval=0),
    _product("ETHUSD", "perpetual_futures", "ETH", "USD", pid=29),
    _product("SOL_INR", "spot", "SOL", "INR", pid=30, interval=0),
]


class FakeDeltaClient:
    """Minimal fake with the real client's public surface used by the tests."""

    base_url = "https://api.india.delta.exchange"
    timeout = 2.0
    product_ttl_s = 60.0

    def __init__(self):
        self._products = []
        self._products_at = 0.0
        self._products_error = ""
        self.last_error = ""
        self.calls = []

    def credentials_configured(self):
        return False

    def _get(self, path, params=None, *, timeout=None):
        self.calls.append((path, dict(params or {})))
        if path == "/v2/products":
            return {"result": PRODUCTS, "meta": {"after": ""}}
        if path.startswith("/v2/tickers/"):
            return {"result": {
                "close": "83267.5", "open": "81535", "high": "83687.5",
                "low": "81280.5", "mark_price": "83265.2",
                "spot_price": "83266.3", "volume": "12719.25",
                "turnover": "1051208490.6", "funding_rate": "0.01",
                "oi": "784.83", "tick_size": "0.5",
                "timestamp": 1792578056349954,
            }}
        if path == "/v2/history/candles":
            # Newest-first, paginated backwards by the client.
            end = int((params or {}).get("end") or 0)
            if end >= 300:
                rows = [
                    {"time": 290, "open": 1, "high": 2, "low": 1,
                     "close": 2, "volume": 10},
                    {"time": 280, "open": 1, "high": 2, "low": 1,
                     "close": 3, "volume": 10},
                    {"time": 270, "open": 1, "high": 2, "low": 1,
                     "close": 4, "volume": 10},
                ]
            elif end >= 269:
                rows = [
                    {"time": 260, "open": 1, "high": 2, "low": 1,
                     "close": 5, "volume": 10},
                    {"time": 250, "open": 1, "high": 2, "low": 1,
                     "close": 6, "volume": 10},
                ]
            else:
                rows = []
            return {"result": rows}
        raise AssertionError(f"unexpected path {path}")

    # The real client's products() uses _fetch_products/_parse_product; for
    # tests we expose the same parsed products directly.
    def products(self, *, force=False, contract_types=None):
        if not self._products:
            self._products = [dx._parse_product(r) for r in PRODUCTS]
            self._products = [p for p in self._products if p]
        return list(self._products)

    def cached_product(self, symbol):
        want = str(symbol or "").upper()
        return next((p for p in self.products() if p.symbol.upper() == want),
                    None)

    def product(self, symbol, *, live_only=True):
        return self.cached_product(symbol)

    def find_products(self, query, *, contract_type=None, limit=60):
        q = str(query or "").upper()
        rows = [p for p in self.products()
                if not contract_type or p.contract_type == contract_type]
        exact = [p for p in rows if p.symbol.upper() == q]
        if exact:
            return exact[:limit]
        tokens = {t for t in q.replace("_", " ").split() if len(t) >= 2}
        out = [p for p in rows
               if p.underlying.upper() in tokens or p.symbol.upper() in tokens]
        return out[:limit]

    def resolve_product(self, query, *, contract_type=None):
        rows = self.find_products(query, contract_type=contract_type)
        if not rows:
            raise dx.DeltaNotFound(f"no live Delta product matches '{query}'")
        exact = [p for p in rows if p.symbol.upper() == str(query).upper()]
        if len(exact) == 1:
            return exact[0]
        if len(rows) == 1:
            return rows[0]
        raise dx.DeltaAmbiguous(query, rows)

    def ticker(self, symbol):
        return self._get(f"/v2/tickers/{symbol}")["result"]

    def candles(self, symbol, resolution, start, end, *, max_candles=600_000):
        return dx.DeltaClient.candles(
            SimpleNamespace(_get=self._get), symbol, resolution, start, end,
            max_candles=max_candles)

    def funding_history(self, symbol, start, end):
        return dx.DeltaClient.funding_history(
            SimpleNamespace(product=self.product, candles=self.candles),
            symbol, start, end)

    def status(self):
        return {"ok": True, "status": "CONNECTED", "products": 4,
                "perpetuals": 2, "spot": 2, "base_url": self.base_url,
                "credentials_configured": False, "ticker_ok": True,
                "ticker_detail": "BTCUSD last=83267.5", "read_only": True,
                "mode": "public data + paper only"}


# ── 1. products + resolution ────────────────────────────────────────────────

def test_products_and_ambiguity():
    client = FakeDeltaClient()
    rows = client.products()
    assert [p.symbol for p in rows] == ["BTCUSD", "BTC_INR", "ETHUSD",
                                        "SOL_INR"]
    assert client.resolve_product("BTCUSD").market_class == "delta_perp"
    assert client.resolve_product("BTC_INR").market_class == "delta_spot"
    with pytest.raises(dx.DeltaAmbiguous) as exc:
        client.resolve_product("BTC")
    assert {p.symbol for p in exc.value.candidates} == {"BTCUSD", "BTC_INR"}
    assert client.resolve_product("BTC", contract_type="spot").symbol == "BTC_INR"
    with pytest.raises(dx.DeltaNotFound):
        client.resolve_product("DOGE")


def test_symbols_resolve_delta_uses_exact_product():
    client = FakeDeltaClient()
    r = bs.resolve_delta("BTC", client=client)
    assert not r.ok and r.needs_input
    assert set(r.options) == {"BTCUSD", "BTC_INR"}
    exact = bs.resolve_delta("BTCUSD", client=client)
    assert exact.ok and exact.instrument.symbol == "BTCUSD"
    assert exact.instrument.market_class == "delta_perp"
    spot = bs.resolve_delta("BTC_INR", market="delta_spot", client=client)
    assert spot.ok and spot.instrument.market_class == "delta_spot"


# ── 2. candles + funding ────────────────────────────────────────────────────

def test_candles_paginate_backwards_and_sort_oldest_first():
    client = FakeDeltaClient()
    result = client.candles("BTCUSD", "1h", 0, 300)
    assert result.ok
    assert [r["time"] for r in result.rows] == [250, 260, 270, 280, 290]
    assert result.requests_made == 2
    assert all(r["close"] > 0 for r in result.rows)


def test_funding_uses_interval_boundaries_and_decimal_rates():
    class FundingClient(FakeDeltaClient):
        def candles(self, symbol, resolution, start, end, *, max_candles=600_000):
            assert symbol == "FUNDING:BTCUSD"
            return dx.CandleResult(True, rows=[
                {"time": 0, "close": 1.0},
                {"time": 3600, "close": 1.0},
                {"time": 28800, "close": 2.0},
                {"time": 57600, "close": 3.0},
            ])

    events, err = FundingClient().funding_history("BTCUSD", 0, 100000)
    assert not err
    assert [e["time"] for e in events] == [0, 28800, 57600]
    assert events[-1]["rate"] == pytest.approx(0.03)


# ── 3. trading adapter + MarketData routing ────────────────────────────────

def test_trading_adapter_quote_and_history():
    class DirectCandles(FakeDeltaClient):
        def candles(self, symbol, resolution, start, end, *, max_candles=600_000):
            return dx.CandleResult(True, rows=[
                {"time": 100, "open": 1, "high": 2, "low": 1, "close": 2,
                 "volume": 10},
                {"time": 200, "open": 2, "high": 3, "low": 2, "close": 3,
                 "volume": 10},
                {"time": 300, "open": 3, "high": 4, "low": 3, "close": 4,
                 "volume": 10},
                {"time": 400, "open": 4, "high": 5, "low": 4, "close": 5,
                 "volume": 10},
                {"time": 500, "open": 5, "high": 6, "low": 5, "close": 6,
                 "volume": 10},
            ])

    adapter = DeltaTradingAdapter(client=DirectCandles())
    q = adapter.quote("BTCUSD")
    assert q.ok and q.provenance is Provenance.LIVE
    assert q.data["last"] == pytest.approx(83267.5)
    assert q.data["market_class"] == "delta_perp"
    assert q.data["funding_rate"] == pytest.approx(0.0001)
    h = adapter.history("BTCUSD", period="1d", interval="1h")
    assert h.ok and len(h.data["bars"]) == 5
    assert h.data["resolution"] == "1h"
    assert adapter.options_chain("BTCUSD").ok is False
    assert adapter.news("BTCUSD").ok is False


def test_market_data_routes_delta_by_explicit_and_cached_symbol():
    class Real:
        name = "real"

        def quote(self, symbol, request_live=False):
            return DataResult(ok=True, provenance=Provenance.DELAYED,
                              source="real", data={"symbol": symbol})

        def history(self, symbol, period="6mo", interval="1d"):
            return DataResult(ok=True, provenance=Provenance.DELAYED,
                              source="real", data={"symbol": symbol})

        def options_chain(self, symbol):
            return DataResult(ok=True, provenance=Provenance.DELAYED,
                              source="real", data={})

        def news(self, symbol):
            return DataResult(ok=True, provenance=Provenance.DELAYED,
                              source="real", data={})

    delta = DeltaTradingAdapter(client=FakeDeltaClient())
    md = MarketData(real=Real(), delta=delta)
    assert md.quote("BTCUSD").source == "delta_india"       # cached exact
    assert md.quote("AAPL").source == "real"
    assert md.quote("BTCUSD", venue="delta").source == "delta_india"
    assert md.quote("BTCUSD", venue="delta", prefer_sim=True).source == "delta_india"
    assert md.quote("BTCUSD", venue="delta_perp",
                    prefer_sim=True).source == "delta_india"
    assert md.quote("BTCUSD", venue="auto").source == "real"
    assert md.history("BTCUSD", period="1d", interval="1h").source == "delta_india"
    assert md.history("BTCUSD", period="1d", interval="1h",
                      prefer_sim=True, venue="delta").source == "delta_india"
    assert md.options_chain("BTCUSD", venue="delta").ok is False


# ── 4. paper-only execution ────────────────────────────────────────────────

def test_paper_broker_keeps_venue_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    pb = bk.PaperBroker(slip_bps=0)
    res = pb.open_position(
        symbol="BTCUSD", direction="LONG", entry=100.0, stop_loss=95.0,
        targets=[110.0], units=1, strategy="trend_follow", signal_score=3,
        approved_by="test", venue="delta", product_id=27,
        contract_type="perpetual_futures", quote_currency="USD",
        contract_value=0.001, market_class="delta_perp")
    assert res["ok"] and res["key"] == "delta:BTCUSD:trend_follow"
    pos = pb.open_positions()[res["key"]]
    assert pos["venue"] == "delta"
    assert pos["product_id"] == 27
    assert pos["contract_type"] == "perpetual_futures"
    assert pos["market_class"] == "delta_perp"


def test_live_broker_is_disabled_and_delta_client_has_no_order_surface():
    with pytest.raises(bk.OrderError):
        bk.LiveBroker()
    forbidden = ("order", "place", "cancel", "account", "balance", "position")
    names = {n.lower() for n in dir(dx.DeltaClient)}
    assert not [n for n in names if any(f in n for f in forbidden)]


# ── 5. Telegram recovery state machine ─────────────────────────────────────

def test_telegram_recovered_flap_is_not_an_error(monkeypatch):
    import actions.hermes_health as hh

    class Proc:
        def __init__(self, text):
            self.stdout = text
            self.returncode = 0

    sample = (
        "Oct 21 14:47:06 host hermes[1]: [Telegram] Sticky path failed\n"
        "Oct 21 15:05:03 host hermes[1]: [Telegram] api.telegram.org path "
        "failed (ConnectError(''))\n"
        "Oct 21 15:05:13 host hermes[1]: [Telegram] Using sticky IPv4 "
        "Telegram API path 149.154.166.110\n"
    )
    monkeypatch.setattr(hh.subprocess, "run", lambda *a, **k: Proc(sample))
    assert hh._recent_gateway_telegram_errors() == (0, 2)

    unresolved = sample.replace(
        "Using sticky IPv4 Telegram API path 149.154.166.110",
        "still failed")
    monkeypatch.setattr(hh.subprocess, "run", lambda *a, **k: Proc(unresolved))
    assert hh._recent_gateway_telegram_errors() == (3, 0)


# ── 6. read-only surfaces — the RIO trading/backtest PAGES were removed on
#       2026-10-23; the Delta UI-wiring checks went with them. The Delta API
#       routes below stay mounted (old shell overlay + Settings still use them).

# ── 7. Trading API Delta routes ────────────────────────────────────────────

def test_trading_delta_routes_require_auth_and_resolve_first(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import dashboard.trading_api as ta

    monkeypatch.setattr(ta, "_delta_resolve",
                        lambda symbol, market="": {"ok": True,
                                                   "symbol": "BTCUSD"})
    monkeypatch.setattr(ta, "_collect_delta_status",
                        lambda: {"ok": True, "status": "CONNECTED",
                                 "products": 4, "read_only": True})
    monkeypatch.setattr(ta, "_collect_delta_products",
                        lambda query="", market="": {
                            "ok": True, "count": 1, "products": [
                                {"symbol": "BTCUSD"}], "query": query,
                            "market": market})
    monkeypatch.setattr(ta, "_collect_delta_quote",
                        lambda symbol: {"ok": True, "symbol": symbol,
                                        "data": {"last": 100.0},
                                        "provenance": "live"})
    monkeypatch.setattr(ta, "_collect_chart",
                        lambda symbol, period, venue="", interval="1d": {
                            "ok": True, "symbol": symbol, "venue": venue,
                            "interval": interval,
                            "bars": [{"close": 1}], "count": 1})

    app = FastAPI()
    ta.register(app, lambda request: True)
    c = TestClient(app)
    assert c.get("/api/rio/trading/delta/status").json()["status"] == "CONNECTED"
    assert c.get("/api/rio/trading/delta/products?market=perp").json()["count"] == 1
    q = c.get("/api/rio/trading/delta/quote?symbol=BTCUSD&market=perp").json()
    assert q["ok"] is True and q["symbol"] == "BTCUSD"
    ch = c.get("/api/rio/trading/delta/chart?symbol=BTCUSD&period=3mo").json()
    assert ch["ok"] is True and ch["venue"] == "delta"

    denied = FastAPI()
    ta.register(denied, lambda request: False)
    d = TestClient(denied)
    assert d.get("/api/rio/trading/delta/status").status_code == 401
    assert d.get("/api/rio/trading/delta/quote?symbol=BTCUSD").status_code == 401


def test_delta_resolve_ambiguity_is_a_real_choice(monkeypatch):
    import dashboard.trading_api as ta

    class FakeClient:
        def resolve_product(self, query, *, contract_type=None):
            raise dx.DeltaAmbiguous(query, [
                dx._parse_product(PRODUCTS[0]), dx._parse_product(PRODUCTS[1])])

    monkeypatch.setattr(dx, "get_client", lambda: FakeClient())
    out = ta._delta_resolve("BTC", "perp")
    assert out["ok"] is False and out["needs_input"] is True
    assert out["options"] == ["BTCUSD", "BTC_INR"]


# ── 8. Hermes backtest action uses the exact Delta product ─────────────────

def test_backtest_action_delta_ambiguous_never_starts(monkeypatch):
    from actions import backtest_agent as ba
    from orchestrator.agents.backtest.symbols import Resolution

    called = []
    monkeypatch.setattr(
        "orchestrator.agents.backtest.symbols.resolve",
        lambda *a, **k: Resolution(
            ok=False, needs_input=True, question="Which Delta product?",
            options=("BTCUSD", "BTC_INR"), error="ambiguous"))
    monkeypatch.setattr(
        "orchestrator.agents.backtest.jobs.start_job",
        lambda *a, **k: called.append((a, k)) or {"ok": True})
    out = ba.backtest_agent({"action": "start", "venue": "delta",
                             "symbol": "BTC"})
    assert "BTCUSD" in out and "BTC_INR" in out
    assert "Nothing was run" in out
    assert called == []


def test_backtest_action_delta_exact_product_starts_job(monkeypatch):
    from actions import backtest_agent as ba
    from orchestrator.agents.backtest.symbols import Instrument, Resolution

    inst = Instrument(
        symbol="BTCUSD", name="Bitcoin", asset_class="crypto", market="perp",
        market_class="delta_perp", exchange="Delta Exchange India",
        instrument_type="BTCUSD perpetual", quote_currency="USD",
        provider_symbols={"delta_india": "BTCUSD"})
    monkeypatch.setattr(
        "orchestrator.agents.backtest.symbols.resolve",
        lambda *a, **k: Resolution(ok=True, instrument=inst))
    calls = []
    monkeypatch.setattr(
        "orchestrator.agents.backtest.jobs.start_job",
        lambda *a, **k: calls.append((a, k)) or {
            "ok": True, "job_id": "bt-delta-test", "deduplicated": False})
    out = ba.backtest_agent({"action": "start", "venue": "delta",
                             "symbol": "BTCUSD", "market": "delta_perp"})
    assert "bt-delta-test" in out and "BTCUSD" in out
    assert calls
    args, kwargs = calls[0]
    assert args == ("BTCUSD", "delta_perp")
    assert kwargs["options"]["venue"] == "delta"

    calls.clear()
    out = ba.backtest_agent({"action": "start", "venue": "delta_perp",
                             "symbol": "BTCUSD"})
    assert "bt-delta-test" in out
    assert calls[0][0] == ("BTCUSD", "delta_perp")


def test_trading_agent_passes_delta_venue_to_data_stage(tmp_path, monkeypatch):
    from orchestrator.agents.trading.data import SimulatedAdapter
    import orchestrator.agents.trading.data as data_mod
    from orchestrator.contracts import AgentRequest, TaskType

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    sim = SimulatedAdapter(base_price=100.0, drift=0.001, vol=0.01, bars=300)
    bars = sim.history("TEST", period="6mo", interval="1d").data["bars"]
    calls = []

    class FakeMD:
        def __init__(self, *a, **k):
            pass

        def history(self, symbol, period="6mo", interval="1d", *,
                    prefer_sim=False, venue=None):
            calls.append(("history", symbol, venue))
            return DataResult(ok=True, provenance=Provenance.LIVE,
                              source="delta_india",
                              data={"bars": bars, "venue": "delta"})

        def quote(self, symbol, *, prefer_sim=False, request_live=False,
                  venue=None):
            calls.append(("quote", symbol, venue))
            return DataResult(ok=True, provenance=Provenance.LIVE,
                              source="delta_india",
                              data={"symbol": symbol, "last": 100.0,
                                    "venue": "delta", "market": "perp",
                                    "market_class": "delta_perp",
                                    "product_id": 27,
                                    "contract_type": "perpetual_futures",
                                    "quote_currency": "USD",
                                    "contract_value": 0.001})

        def options_chain(self, symbol, *, prefer_sim=False, venue=None):
            return DataResult(ok=False, provenance=Provenance.UNAVAILABLE,
                              source="delta_india", error="no chain")

        def news(self, symbol, *, prefer_sim=False, venue=None):
            return DataResult(ok=False, provenance=Provenance.UNAVAILABLE,
                              source="delta_india", error="no news")

    monkeypatch.setattr(data_mod, "MarketData", FakeMD)
    from orchestrator.agents.trading.trading_agent import TradingAgent
    agent = TradingAgent()
    agent.handle(AgentRequest(
        task_id="task-delta-venue", task_type=TaskType.TRADING,
        text="analyze BTCUSD",
        metadata={"symbol": "BTCUSD", "strategy": "trend_follow",
                  "venue": "delta", "capital": 10_000}))
    assert ("history", "BTCUSD", "delta") in calls
    assert ("quote", "BTCUSD", "delta") in calls


def test_product_distinguishes_venue_outage_from_missing_product():
    c = dx.DeltaClient(base_url="https://example.invalid", timeout=0.01)

    def fail(path, params=None, **kwargs):
        raise dx.DeltaError("Delta HTTP 503: upstream down")

    c._get = fail
    assert c.product("BTCUSD") is None
    assert "503" in c.last_error

    c.products = lambda **kwargs: []
    c._get = lambda *a, **k: (_ for _ in ()).throw(
        dx.DeltaError("Delta HTTP 404: no such product"))
    assert c.product("NOPE") is None
    assert c.last_error == ""
