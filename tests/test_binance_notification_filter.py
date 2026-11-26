"""
tests/test_binance_notification_filter.py — alerts-only Telegram policy.

No network, no real Telegram calls. These tests cover both market-notification
senders:

  * core/binance_ws.py — Type 1 Market Summary + Type 2 threshold alerts
  * /home/nagendra/binance_ws_analyzer.py — Type 3 standalone price updates +
    Type 2 threshold alerts

Only the summary dispatch is disabled. Alert detection and the market-data
pipeline are exercised and must remain functional.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import core.binance_ws as bw  # noqa: E402
import actions.binance_analyzer as ba  # noqa: E402

STANDALONE = Path("/home/nagendra/binance_ws_analyzer.py")


# ── repo sender: core/binance_ws.py ────────────────────────────────────────
def test_repo_summary_dispatch_is_disabled(monkeypatch):
    sent = []
    monkeypatch.setattr(bw, "_SEND_MARKET_SUMMARIES", False)
    monkeypatch.setattr(bw, "_queue_send", sent.append)
    bw._fire_summary(["BTCUSDT $100"])
    assert sent == []


def test_repo_emit_summary_returns_false_and_sends_nothing(monkeypatch):
    sent = []
    monkeypatch.setattr(bw, "_SEND_MARKET_SUMMARIES", False)
    monkeypatch.setattr(bw, "_queue_send", sent.append)
    assert bw.emit_summary() is False
    assert sent == []


def test_repo_alert_dispatch_still_works(monkeypatch):
    sent = []
    monkeypatch.setattr(bw, "_SEND_MARKET_SUMMARIES", False)
    monkeypatch.setattr(bw, "_queue_send", sent.append)
    bw._fire_alert(["🔴 *BTCUSDT* dropped **below $66,000** → `$63,846.04`"])
    assert len(sent) == 1
    assert sent[0].startswith("⚡ *ALERT*")
    assert "BTCUSDT" in sent[0]


def test_repo_summary_can_be_reenabled_by_flag(monkeypatch):
    sent = []
    monkeypatch.setattr(bw, "_SEND_MARKET_SUMMARIES", True)
    monkeypatch.setattr(bw, "_queue_send", sent.append)
    bw._fire_summary(["BTCUSDT $100"])
    assert sent and sent[0].startswith("📊 *Market Summary*")


@pytest.mark.parametrize("sym,price,level", [
    ("btcusdt", 60000.0, 66000),
    ("ethusdt", 1700.0, 1850),
    ("solusdt", 60.0, 70),
    ("paxgusdt", 3700.0, 3800),
    ("xautusdt", 3700.0, 3800),
])
def test_repo_threshold_alert_all_five_symbols(monkeypatch, tmp_path,
                                               sym, price, level):
    alerts_file = tmp_path / "alerts.json"
    alerts_file.write_text(json.dumps({
        sym: {"below": level, "above": level * 2}}), encoding="utf-8")
    monkeypatch.setattr(bw, "_ALERTS_FILE", alerts_file)
    monkeypatch.setattr(bw, "_pending_alerts", {})
    ticker = {"symbol": sym.upper(), "price": price, "change_24h": -1.0}
    lines = bw._check_alerts(sym, ticker, None, None)
    assert any(sym.upper() in line and "dropped" in line for line in lines)


def test_repo_summary_action_reports_disabled(monkeypatch):
    monkeypatch.setattr(ba._ws, "emit_summary", lambda: False)
    out = ba.binance_summary({})
    assert "alerts-only" in out.lower()
    assert "threshold alerts remain active" in out.lower()


# ── standalone sender: /home/nagendra/binance_ws_analyzer.py ───────────────
@pytest.fixture(scope="module")
def standalone_module():
    if not STANDALONE.exists():
        pytest.skip(f"standalone analyzer not present: {STANDALONE}")
    spec = importlib.util.spec_from_file_location(
        "binance_ws_analyzer_under_test", STANDALONE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["binance_ws_analyzer_under_test"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def standalone(standalone_module, tmp_path, monkeypatch):
    module = standalone_module
    sent = []
    monkeypatch.setattr(module, "SEND_MARKET_SUMMARIES", False)
    monkeypatch.setattr(module, "DB_PATH", str(tmp_path / "market.db"))
    monkeypatch.setattr(module, "ALERT_FILE", str(tmp_path / "alerts.json"))
    monkeypatch.setattr(
        module.Telegram, "send",
        lambda self, text, parse_mode="Markdown": sent.append(text))
    (tmp_path / "alerts.json").write_text(json.dumps({
        "btcusdt": {"below": 66000, "above": 90000},
        "ethusdt": {"below": 1850, "above": 3000},
        "solusdt": {"below": 70, "above": 120},
        "paxgusdt": {"below": 3800, "above": 5000},
        "xautusdt": {"below": 3800, "above": 5000},
    }), encoding="utf-8")
    analyzer = module.Analyzer()
    analyzer._sent = sent
    return analyzer


def _ticker(module, symbol, price):
    return module.TickerData(
        symbol=symbol, price=price, change_24h=-1.0, high_24h=price * 1.1,
        low_24h=price * 0.9, volume=10.0, timestamp=1700000000000)


def test_standalone_stream_hosts_keep_primary_and_official_fallback(
        standalone_module):
    assert standalone_module.STREAM_HOSTS[0] == "wss://stream.binance.com:9443"
    assert "data-stream.binance.vision:9443" in standalone_module.STREAM_HOSTS[1]
    for symbol in ("btcusdt", "ethusdt", "solusdt", "paxgusdt", "xautusdt"):
        assert f"{symbol}@ticker" in standalone_module.STREAM_PATH
        assert f"{symbol}@kline_1m" in standalone_module.STREAM_PATH


def test_standalone_summary_is_disabled(standalone):
    standalone.latest_ticker = {
        "BTCUSDT": _ticker(standalone_module_for(standalone), "BTCUSDT", 60000),
        "ETHUSDT": _ticker(standalone_module_for(standalone), "ETHUSDT", 1700),
    }
    standalone.maybe_summary()
    assert standalone._sent == []


def test_standalone_send_summary_is_disabled(standalone):
    standalone.tg.send_summary(["BTCUSDT $60,000"])
    assert standalone._sent == []


def test_standalone_status_notices_are_disabled(standalone, monkeypatch):
    module = standalone_module_for(standalone)
    monkeypatch.setattr(module, "SEND_STATUS_NOTICES", False)
    standalone._status("🤖 *Binance Analyzer Started*")
    standalone._status("🛑 *Binance Analyzer Stopped*")
    assert standalone._sent == []


def test_standalone_threshold_alerts_all_five_symbols(standalone):
    module = standalone_module_for(standalone)
    cases = [("BTCUSDT", 60000.0), ("ETHUSDT", 1700.0),
             ("SOLUSDT", 60.0), ("PAXGUSDT", 3700.0),
             ("XAUTUSDT", 3700.0)]
    for symbol, price in cases:
        standalone.alerts.last_alert.clear()
        t = _ticker(module, symbol, price)
        standalone.handle_message({"data": {
            "e": "24hrTicker", "s": symbol, "c": str(price), "P": "-1.0",
            "h": str(price * 1.1), "l": str(price * 0.9), "v": "10",
            "E": 1700000000000}})
    assert len(standalone._sent) == 5
    for symbol, _ in cases:
        assert any(symbol in msg and "ALERT" in msg for msg in standalone._sent), symbol
    # No standalone price-update block was emitted.
    assert not any(msg.startswith("BTCUSDT ") for msg in standalone._sent)


def test_standalone_monitoring_still_processes_candles(standalone):
    module = standalone_module_for(standalone)
    candle = {"e": "kline", "k": {
        "s": "BTCUSDT", "i": "1m", "o": "60000", "h": "60100",
        "l": "59900", "c": "60050", "v": "2", "x": True, "t": 1700000000000}}
    standalone.handle_message({"data": candle})
    assert standalone.latest_candles["btcusdt"]["1m"].close == 60050
    assert standalone._sent == []


def test_standalone_summary_gate_still_commits_market_data(standalone):
    module = standalone_module_for(standalone)
    standalone.last_commit = 0
    standalone.handle_message({"data": {
        "e": "24hrTicker", "s": "ETHUSDT", "c": "1900", "P": "-1.0",
        "h": "2000", "l": "1800", "v": "10", "E": 1700000000000}})
    standalone.maybe_summary()
    with sqlite3.connect(module.DB_PATH) as conn:
        count = conn.execute("SELECT COUNT(*) FROM tickers").fetchone()[0]
    assert count == 1
    assert standalone._sent == []


def test_standalone_alert_detection_logic_unchanged(standalone):
    module = standalone_module_for(standalone)
    engine = module.AlertEngine()
    ticker = _ticker(module, "BTCUSDT", 60000.0)
    engine.last_alert.clear()
    lines = engine.check(ticker, None, None)
    assert lines and "below $66,000" in lines[0]


# helper: recover the loaded standalone module from an Analyzer instance
def standalone_module_for(analyzer):
    return sys.modules.get("binance_ws_analyzer_under_test")
