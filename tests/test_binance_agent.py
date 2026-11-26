"""
tests/test_binance_agent.py — offline contract tests for the Binance agent.

No network: every feed is a fake. These tests lock in the wiring that was
actually broken before:

  1. the daemon's event loop really runs (it was created but never started);
  2. a healthy WS stream is NOT cancelled after a fixed timeout;
  3. WS failure falls back to REST polling, with no invented prices;
  4. alert levels / % moves fire once, then respect the cooldown;
  5. only closed candles are stored and repeated writes stay idempotent;
  6. Hermes delivery uses the resolved CLI + fixed env and never blocks;
  7. the action handler matches action_loader's `parameters=` calling contract;
  8. the router prefers BinanceAgent for crypto and TradingAgent for the rest;
  9. the agent parses candles / remove-alert / % moves without crashing.
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import core.binance_ws as bw  # noqa: E402
import actions.binance_analyzer as ba  # noqa: E402
from core.action_loader import _call_handler, _validate  # noqa: E402
from orchestrator.agents.trading.binance_agent import BinanceAgent  # noqa: E402
from orchestrator.base import BaseAgent  # noqa: E402
from orchestrator.classifier import Classification  # noqa: E402
from orchestrator.contracts import (  # noqa: E402
    AgentRequest, TaskStatus, TaskType,
)
from orchestrator.registry import AgentRegistry  # noqa: E402
from orchestrator.router import TaskRouter  # noqa: E402


# ── Fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture()
def market(tmp_path, monkeypatch):
    """Isolate the SQLite store, alerts file, and all in-memory state."""
    if bw._db_conn is not None:
        bw._db_conn.close()
    monkeypatch.setattr(bw, "_DB_PATH", tmp_path / "market.db")
    monkeypatch.setattr(bw, "_ALERTS_FILE", tmp_path / "alerts.json")
    monkeypatch.setattr(bw, "_db_conn", None)
    monkeypatch.setattr(bw, "_latest_ticker", {})
    monkeypatch.setattr(bw, "_latest_candles", {s: {} for s in bw._SYMBOLS})
    monkeypatch.setattr(bw, "_pending_alerts", {})
    monkeypatch.setattr(bw, "_connect_time", 0.0)
    monkeypatch.setattr(bw, "_ws_up", False)
    monkeypatch.setattr(bw, "_transport", "none")
    yield bw
    if bw._db_conn is not None:
        bw._db_conn.close()
        bw._db_conn = None


def _req(text: str) -> AgentRequest:
    return AgentRequest(
        task_id="test-task-0001",
        task_type=TaskType.TRADING,
        text=text,
    )


# ── Daemon lifecycle ─────────────────────────────────────────────────────────
def test_event_loop_thread_actually_runs():
    assert bw.run_coro(asyncio.sleep(0, result=42)) == 42
    assert bw._get_loop().is_running()
    names = [t.name for t in threading.enumerate()]
    assert names.count("binance-ws-loop") == 1, names


def test_start_and_stop(monkeypatch):
    started = threading.Event()

    async def fake_daemon():
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(bw, "_run_daemon", fake_daemon)
    bw.start()
    assert started.wait(2.0), "daemon coroutine never ran on the loop thread"
    bw.stop()
    assert bw._daemon_future is None
    assert bw._transport == "none"


def test_run_daemon_falls_back_to_polling_when_ws_never_connects(monkeypatch):
    polled = threading.Event()

    async def never_connects(connected):
        await asyncio.sleep(3600)

    async def fake_poll():
        polled.set()

    monkeypatch.setattr(bw, "_ws_loop", never_connects)
    monkeypatch.setattr(bw, "_poll_loop", fake_poll)
    monkeypatch.setattr(bw, "_WS_CONNECT_TIMEOUT_S", 0.05)

    asyncio.run(bw._run_daemon())
    assert polled.is_set()


def test_run_daemon_keeps_healthy_ws_past_the_connect_deadline(monkeypatch):
    """A connected stream must survive past the old hard 20 s wait_for."""
    calls = {"n": 0}

    async def healthy_stream(connected):
        calls["n"] += 1
        connected.set()
        # Return only after the test has observed that _run_daemon awaited
        # the stream instead of cancelling it.
        await asyncio.sleep(0.1)

    monkeypatch.setattr(bw, "_ws_loop", healthy_stream)
    monkeypatch.setattr(bw, "_WS_CONNECT_TIMEOUT_S", 0.05)

    asyncio.run(bw._run_daemon())
    assert calls["n"] == 1


# ── Alerts ───────────────────────────────────────────────────────────────────
def test_first_alert_is_not_suppressed_by_boot_uptime(market):
    assert market._is_cooldown("never_fired") is False


def test_price_level_alert_fires_once_then_cools_down(market):
    market.set_alert("btcusdt", below=100)
    ticker = {"symbol": "BTC", "price": 90, "change_24h": -1}
    first = market._check_alerts("btcusdt", ticker, None, None)
    assert len(first) == 1 and "below" in first[0]
    assert market._check_alerts("btcusdt", ticker, None, None) == []


def test_pct_move_alert_requires_a_closed_candle(market):
    market._save_alerts({"btcusdt": {"pct_1m": 1.0}})
    ticker = {"symbol": "BTC", "price": 102, "change_24h": 2}
    open_candle = {"open": 100, "close": 102, "closed": False}
    assert market._check_alerts("btcusdt", ticker, open_candle, None) == []

    closed = {"open": 100, "close": 102, "closed": True}
    fired = market._check_alerts("btcusdt", ticker, closed, None)
    assert len(fired) == 1 and "UP" in fired[0] and "2.00%" in fired[0]


# ── Storage ──────────────────────────────────────────────────────────────────
def test_hourly_stats_honours_the_requested_window(market):
    conn = market._open_db()
    now = int(time.time() * 1000)
    rows = [
        ("BTCUSDT", 100.0, now - 2 * 3600 * 1000),
        ("BTCUSDT", 110.0, now - 10 * 60 * 1000),
        ("BTCUSDT", 120.0, now - 60 * 1000),
    ]
    for symbol, price, ts in rows:
        conn.execute(
            "INSERT INTO tickers (symbol, price, change_24h, high_24h,"
            " low_24h, volume, ts) VALUES (?,?,?,?,?,?,?)",
            (symbol, price, 0, price, price, 1, ts),
        )
    conn.commit()

    one_hour = market.hourly_stats("BTC", 1)
    three_hours = market.hourly_stats("BTC", 3)
    assert one_hour["count"] == 2
    assert one_hour["min"] == 110.0 and one_hour["max"] == 120.0
    assert three_hours["count"] == 3


def test_candle_upsert_is_idempotent(market):
    conn = market._open_db()
    ts = int(time.time() * 1000)
    for close in (101.0, 102.0):
        conn.execute(
            "INSERT OR REPLACE INTO candles"
            " (symbol, interval, open, high, low, close, volume, ts, closed)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            ("BTCUSDT", "1m", 100.0, 103.0, 99.0, close, 5.0, ts, 1),
        )
    conn.commit()

    rows = market.query_history("BTC", "1m", 10)
    assert len(rows) == 1
    assert rows[0]["close"] == 102.0


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _kline_msg(ts, close, closed):
    return {
        "e": "kline",
        "k": {
            "s": "BTCUSDT", "i": "1m", "o": "100", "h": "102",
            "l": "99", "c": str(close), "v": "5", "x": closed, "t": ts,
        },
    }


def test_websocket_handler_persists_only_closed_candles(market):
    ts = int(time.time() * 1000)
    market._handle_ws_message(_kline_msg(ts, 100.5, closed=False))
    assert market.query_history("BTC", "1m", 10) == []
    assert market._latest_candles["btcusdt"]["1m"]["closed"] is False

    market._handle_ws_message(_kline_msg(ts, 101.0, closed=True))
    rows = market.query_history("BTC", "1m", 10)
    assert len(rows) == 1
    assert rows[0]["close"] == 101.0
    assert market._latest_candles["btcusdt"]["1m"]["closed"] is True


def test_websocket_ticker_fires_alerts(market, monkeypatch):
    alerts = []
    monkeypatch.setattr(market, "_fire_alert", alerts.extend)
    market._save_alerts({"btcusdt": {"above": 50}})
    market._handle_ws_message({
        "e": "24hrTicker", "s": "BTCUSDT", "c": "100", "P": "1",
        "h": "101", "l": "99", "v": "10", "E": int(time.time() * 1000),
    })
    assert alerts and "above" in alerts[0]


def test_rest_fetch_falls_back_to_market_data_mirror(market, monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=0):
        calls.append(req.full_url)
        if "api.binance.com" in req.full_url:
            raise OSError("blocked on this network")
        return _FakeResponse({"ok": True})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(bw, "_rest_host_idx", 0)

    assert bw._rest_get("/ping", timeout=1) == {"ok": True}
    assert len(calls) == 2
    assert bw._rest_host_idx == 1
    # The mirror stays preferred on the next call.
    assert bw._rest_get("/ping", timeout=1) == {"ok": True}
    assert len(calls) == 3


def test_http_poll_stores_only_closed_candles_and_stays_idempotent(
        market, monkeypatch):
    """The last /klines row is the in-progress candle; it must not be stored."""
    now = int(time.time() * 1000)

    def fake_urlopen(req, timeout=0):
        url = req.full_url
        if "/ticker/24hr" in url:
            return _FakeResponse([{
                "symbol": "BTCUSDT", "lastPrice": "100",
                "priceChangePercent": "1", "highPrice": "101",
                "lowPrice": "99", "volume": "10",
            }])
        if "/klines" in url:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            if query.get("symbol") != ["BTCUSDT"] or query.get("interval") != ["1m"]:
                return _FakeResponse([])
            return _FakeResponse([
                [now - 180_000, "1", "2", "0.5", "1.5", "10", now - 120_000, "0", 0, "0", "0", "0"],
                [now - 120_000, "1.5", "2.5", "1", "2", "11", now - 60_000, "0", 0, "0", "0", "0"],
                [now - 60_000, "2", "3", "1", "2.5", "12", now + 60_000, "0", 0, "0", "0", "0"],
            ])
        return _FakeResponse([])

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(bw, "_fire_alert", lambda lines: None)

    assert asyncio.run(market._do_poll_cycle()) is True
    assert asyncio.run(market._do_poll_cycle()) is True

    rows = market.query_history("BTC", "1m", 10)
    stored = {row["ts"] for row in rows}
    assert len(rows) == 2, rows
    assert now - 60_000 not in stored, "in-progress candle was stored as final"


# ── Hermes delivery ──────────────────────────────────────────────────────────
def test_hermes_send_uses_resolved_cli_and_fixed_env(monkeypatch):
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["env"] = kwargs.get("env")
        return SimpleNamespace(returncode=0, stdout="sent", stderr="")

    monkeypatch.setattr(bw, "_telegram_chat_id", lambda: "12345")
    monkeypatch.setattr(bw.subprocess, "run", fake_run)

    assert bw._send_via_hermes("hello") is True
    assert seen["args"] == [bw._HERMES_BIN, "send", "-t", "telegram:12345", "hello"]
    # House rule: a fixed minimal env, never the GUI process environment.
    assert set(seen["env"]) == {"PATH", "HOME"}


def test_hermes_send_is_off_the_event_loop(monkeypatch):
    gate = threading.Event()
    finished = threading.Event()

    def slow_send(text):
        gate.wait(2.0)
        finished.set()
        return True

    monkeypatch.setattr(bw, "_send_via_hermes", slow_send)

    started = time.monotonic()
    bw._queue_send("x")
    elapsed = time.monotonic() - started
    assert elapsed < 0.2, f"_queue_send blocked for {elapsed:.2f}s"

    gate.set()
    assert finished.wait(2.0)


# ── Jarvis action ────────────────────────────────────────────────────────────
def test_action_declaration_validates_and_runs_through_the_loader(market):
    rec = _validate(ba, "binance_analyzer.py")
    assert rec.valid, rec.error

    # Exactly the loader's calling convention: fn(parameters=..., player=...).
    out = _call_handler(rec.handler, {"action": "status"}, {"player": None})
    assert "DISCONNECTED" in out
    assert "btcusdt" in out


def test_action_query_passes_the_requested_hours(market, monkeypatch):
    seen = {}

    def fake_stats(symbol, hours=1):
        seen["symbol"] = symbol
        seen["hours"] = hours
        return {}

    monkeypatch.setattr(bw, "hourly_stats", fake_stats)
    monkeypatch.setattr(bw, "query_history", lambda *a, **k: [])
    out = ba.binance_query({"symbol": "ETH", "hours": 3})
    assert seen == {"symbol": "ETH", "hours": 3.0}
    assert "ETH" in out


@pytest.mark.parametrize("symbol", ["BTC", "ETH", "SOL"])
def test_action_query_covers_each_tracked_symbol(market, monkeypatch, symbol):
    monkeypatch.setattr(
        bw, "hourly_stats",
        lambda s, h=1: {
            "symbol": s, "min": 1.0, "max": 2.0, "avg": 1.5,
            "first": 1.0, "last": 2.0, "change_pct": 100.0, "count": 2,
        },
    )
    monkeypatch.setattr(bw, "query_history", lambda *a, **k: [])
    out = ba.binance_query({"symbol": symbol})
    assert f"📈 {symbol}" in out


def test_summary_action_queues_emit_summary(market, monkeypatch):
    called = []
    monkeypatch.setattr(bw, "emit_summary", lambda: called.append(True))
    out = ba.binance_summary({})
    assert called == [True]
    assert "queued" in out.lower()


def test_set_alert_ignores_the_action_envelope(market):
    ba.binance_set_alert({"action": "set_alert", "symbol": "BTC", "below": 50000})
    cfg = bw.get_alerts("btcusdt")
    assert cfg["below"] == 50000
    assert "action" not in cfg


def test_remove_alert_reports_a_missing_key(market):
    market._save_alerts({"btcusdt": {"below": 1}})
    out = ba.binance_rm_alert({"symbol": "BTC", "key": "above"})
    assert "no 'above' alert" in out
    assert "below" in bw.get_alerts("btcusdt")


def test_alert_defaults_are_not_mutated(market):
    original = bw._DEFAULT_ALERTS["btcusdt"]["below"]
    market.set_alert("BTC", below=1)
    assert bw._DEFAULT_ALERTS["btcusdt"]["below"] == original
    assert bw.get_alerts("btcusdt")["below"] == 1


def test_cli_status_is_machine_readable(market, capsys):
    assert bw._cli(["status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["transport"] == "none"
    assert payload["symbols_tracking"] == list(bw._SYMBOLS)


# ── Router ───────────────────────────────────────────────────────────────────
class _FakeTradingAgent(BaseAgent):
    name = "trading_agent"
    task_types = (TaskType.TRADING,)

    def _handle_impl(self, request):
        raise AssertionError("not executed by route()")


def _router():
    registry = AgentRegistry()
    registry.register(_FakeTradingAgent())
    registry.register(BinanceAgent())
    return TaskRouter(registry)


def test_router_prefers_binance_for_crypto():
    decision = _router().route(
        Classification(TaskType.TRADING, 0.9, "test"),
        _req("what is the BTC price"),
    )
    assert decision.chosen_agent == "binance_agent"
    assert decision.candidates[0] == "binance_agent"


def test_router_leaves_other_trading_asks_to_trading_agent():
    decision = _router().route(
        Classification(TaskType.TRADING, 0.9, "test"),
        _req("buy nifty at support"),
    )
    assert decision.chosen_agent == "trading_agent"


def test_router_leaves_crypto_analysis_to_trading_agent():
    decision = _router().route(
        Classification(TaskType.TRADING, 0.9, "test"),
        _req("BTC analysis and setup for today"),
    )
    assert decision.chosen_agent == "trading_agent"


# ── Agent behaviour ──────────────────────────────────────────────────────────
def _patch_actions(monkeypatch, calls):
    def record(name):
        def _fn(params, player=None):
            calls.append((name, dict(params)))
            return f"{name} ok"
        return _fn

    for name in ("binance_status", "binance_query", "binance_set_alert",
                 "binance_rm_alert", "binance_summary"):
        monkeypatch.setattr(ba, name, record(name))


def test_agent_handles_candles_without_a_number(monkeypatch):
    calls = []
    _patch_actions(monkeypatch, calls)
    result = BinanceAgent().handle(_req("show btc candles"))
    assert result.status is TaskStatus.COMPLETED
    assert calls[-1][0] == "binance_query"
    assert calls[-1][1]["bars"] == 20


def test_agent_parses_named_candles(monkeypatch):
    calls = []
    _patch_actions(monkeypatch, calls)
    BinanceAgent().handle(_req("show eth 50 candles"))
    assert calls[-1][1] == {"symbol": "ETH", "bars": 50}


def test_agent_removes_alerts_instead_of_setting_them(monkeypatch):
    calls = []
    _patch_actions(monkeypatch, calls)
    BinanceAgent().handle(_req("remove btc alert below"))
    assert [c[0] for c in calls] == ["binance_rm_alert"]
    assert calls[0][1] == {"symbol": "BTC", "key": "below"}


def test_agent_parses_price_level_alert(monkeypatch):
    calls = []
    _patch_actions(monkeypatch, calls)
    BinanceAgent().handle(_req("alert me if ethereum drops below 1,800"))
    assert calls[-1] == ("binance_set_alert", {"symbol": "ETH", "below": 1800.0})


def test_agent_parses_percent_move_alert(monkeypatch):
    calls = []
    _patch_actions(monkeypatch, calls)
    BinanceAgent().handle(_req("alert on sol 2% move in 5m"))
    assert calls[-1] == ("binance_set_alert", {"symbol": "SOL", "pct_5m": 2.0})


def test_agent_asks_for_a_level_instead_of_setting_an_empty_alert(monkeypatch):
    calls = []
    _patch_actions(monkeypatch, calls)
    result = BinanceAgent().handle(_req("alert me on btc"))
    assert result.status is TaskStatus.COMPLETED
    assert calls == []
    assert "Tell me the level" in result.text
