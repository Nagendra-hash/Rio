"""
tests/test_paper_trading.py — deterministic offline tests for paper trading.

Synthetic candles/quotes are used ONLY here. The production engine only
consumes real Binance quotes and closed candles from the existing daemon.

Coverage: all 22 scenarios in the brief plus reset/daily-report/start-stop.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import core.paper_trading as pt  # noqa: E402

BASE_TS = 1_765_000_000_000


def make_config(**overrides) -> dict:
    cfg = {
        "version": 1,
        "enabled": True,
        "initial_balance": 10000.0,
        "quote_currency": "USDT",
        "default_risk_percent": 1.0,
        "maximum_open_positions": 2,
        "maximum_daily_trades": 5,
        "maximum_daily_loss_percent": 3.0,
        "maximum_total_exposure_percent": 50.0,
        "maximum_position_percent": 25.0,
        "simulated_fee_percent": 0.1,
        "slippage_model": {"type": "fixed_bps", "value": 2.0},
        "stop_loss_buffer": 0.0,
        "default_reward_risk_ratio": 2.0,
        "quantity_mode": "AUTO",
        "fixed_quantity": 0.01,
        "leverage": 1.0,
        "slippage_percent": 0.02,
        "allowed_symbols": ["BTCUSDT", "ETHUSDT"],
        "position_size_mode": "risk_percent",
        "min_notional": 10.0,
        "symbol_rules": {
            "BTCUSDT": {"min_quantity": 0.00001, "quantity_precision": 5},
            "ETHUSDT": {"min_quantity": 0.0001, "quantity_precision": 4},
        },
        "execution_price_max_age_s": 10.0,
        "stale_after_s": 300.0,
        "data_unreliable_action": "suspend",
        "daily_report_enabled": False,
        "daily_report_hour_utc": 0,
    }
    cfg.update(overrides)
    return cfg


def signal_event(signal_id="sig-0001", **overrides) -> dict:
    event = {
        "event": "LONG_SIGNAL_READY",
        "signal_id": signal_id,
        "setup_id": signal_id,
        "symbol": "BTCUSDT",
        "interval": "1m",
        "state": "LONG_SIGNAL_READY",
        "ts": BASE_TS,
        "timestamp": pt.utc_iso(BASE_TS),
        "price": 100.0,
        "zone_low": 99.0,
        "zone_high": 100.0,
        "invalidation_level": 97.0,
        "sweep": {"price": 98.0, "prev_swing_low": 99.5, "depth_pct": 1.5},
        "rejection": {"close_above_zone": True, "patterns": ["STRONG_REJECTION"],
                      "close": 100.0},
        "volume": {"passed": True, "available": True, "multiplier": 2.5,
                   "threshold": 2.0},
        "delta": {"passed": True, "available": True, "delta": 100.0,
                  "imbalance": 0.2},
        "confirmation": {"passed": True, "close": 100.0},
        "risk": {"ok": True, "stop_loss": 98.0, "entry_low": 99.0,
                 "entry_high": 100.0, "target": 104.0, "rr": 2.0,
                 "risk_amount": 100.0},
    }
    event.update(overrides)
    return event


def candle(ts, o, h, l, c, interval="1m", symbol="BTCUSDT"):
    return {"symbol": symbol, "interval": interval, "ts": ts,
            "close_ts": ts + (60_000 if interval == "1m" else 180_000),
            "open": o, "high": h, "low": l, "close": c, "volume": 10.0,
            "taker_buy_volume": 5.0, "closed": True}


def build(base: Path, cfg=None, *, alerts=None, quotes=None, feed=None,
          clock=None, alert_sender=None):
    base.mkdir(parents=True, exist_ok=True)
    cfg = cfg or make_config()
    cfg_path = base / "paper.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    clock = clock or {"now": BASE_TS}
    quotes = quotes if quotes is not None else {
        "BTCUSDT": {"price": 100.0, "ts": BASE_TS, "source": "test_ticker"},
        "ETHUSDT": {"price": 200.0, "ts": BASE_TS, "source": "test_ticker"},
    }
    feed = feed if feed is not None else {
        "live": True, "transport": "ws", "last_tick_age_s": 1.0}
    alerts = alerts if alerts is not None else []

    def price(symbol):
        q = quotes.get(symbol)
        return dict(q) if q else None

    if alert_sender is None:
        def alert_sender(text):
            alerts.append(text)
            return True

    engine = pt.PaperTradingEngine(
        cfg_path, base / "paper.db",
        alert_sender=alert_sender,
        price_provider=price,
        feed_provider=lambda: dict(feed),
        strategy_provider=lambda symbol: {
            "ok": True, "symbol": symbol, "state": "IDLE",
            "state_color": "GREY"},
        clock=lambda: clock["now"] / 1000,
    )
    return SimpleNamespace(engine=engine, cfg=cfg, cfg_path=cfg_path,
                           db=base / "paper.db", alerts=alerts, quotes=quotes,
                           feed=feed, clock=clock, base=base)


@pytest.fixture()
def paper(tmp_path):
    env = build(tmp_path / "default")
    yield env
    env.engine.close()


def open_trade(env, signal_id="sig-0001", **overrides):
    return env.engine.on_signal(signal_event(signal_id, **overrides))


def feed_candle(env, ts, o, h, l, c, symbol="BTCUSDT"):
    env.clock["now"] = ts + 60_000
    env.engine.on_closed_candle(candle(ts, o, h, l, c, symbol=symbol))


def table_count(env, table, where="1=1"):
    conn = sqlite3.connect(str(env.db))
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]
    finally:
        conn.close()


# ── 1-5. no entry unless every gate is complete ─────────────────────────────
@pytest.mark.parametrize("state", [
    "SWEEPING_LIQUIDITY", "REJECTION_DETECTED", "VOLUME_CONFIRMED",
    "DELTA_CONFIRMED", "CONFIRMATION_PENDING",
])
def test_no_entry_outside_long_signal_ready(paper, state):
    result = open_trade(paper, signal_id=f"sig-{state}", state=state)
    assert result["opened"] is False
    assert result["reasons"]
    assert paper.engine.positions() == []


def test_no_entry_without_rejection(paper):
    result = open_trade(paper, signal_id="sig-no-rej", rejection=None)
    assert result["opened"] is False
    assert "missing_rejection_confirmation" in result["reasons"]
    assert table_count(paper, "paper_positions") == 0


def test_no_entry_without_volume(paper):
    result = open_trade(paper, signal_id="sig-no-vol",
                        volume={"passed": False, "available": True,
                                "multiplier": 1.2})
    assert result["opened"] is False
    assert "volume_not_confirmed" in result["reasons"]


def test_no_entry_without_positive_delta(paper):
    result = open_trade(paper, signal_id="sig-no-delta",
                        delta={"passed": False, "available": True,
                               "delta": -10.0})
    assert result["opened"] is False
    assert "positive_delta_not_confirmed" in result["reasons"]


def test_no_entry_without_next_candle_confirmation(paper):
    result = open_trade(paper, signal_id="sig-no-conf",
                        confirmation={"passed": False, "close": 99.5})
    assert result["opened"] is False
    assert "next_candle_not_confirmed" in result["reasons"]


# ── 6-7. one trade per signal ───────────────────────────────────────────────
def test_valid_signal_opens_exactly_one_paper_trade(paper):
    result = open_trade(paper)
    assert result["opened"] is True
    positions = paper.engine.positions()
    assert len(positions) == 1
    assert positions[0]["direction"] == "LONG"
    assert table_count(paper, "paper_positions") == 1
    assert table_count(paper, "paper_orders") == 1
    assert any("PAPER TRADE OPENED" in m for m in paper.alerts)


def test_duplicate_signal_does_not_open_another_trade(paper):
    first = open_trade(paper)
    second = open_trade(paper)
    assert first["opened"] is True
    assert second is None
    assert len(paper.engine.positions()) == 1
    assert table_count(paper, "paper_orders") == 1


# ── 8. position sizing ──────────────────────────────────────────────────────
def test_position_sizing_uses_equity_and_strategy_stop(tmp_path):
    env = build(tmp_path / "sizing", make_config(
        maximum_position_percent=100.0,
        maximum_total_exposure_percent=100.0))
    try:
        result = open_trade(env)
        assert result["opened"] is True
        pos = env.engine.positions()[0]
        entry = 100.0 * (1.0 + 2.0 / 10_000.0)       # 100.02
        stop = 98.0
        expected_qty = pt._round_down(
            100.0 / abs(entry - stop), 5)             # risk 1% of 10k
        assert pos["entry_price"] == pytest.approx(entry)
        assert pos["quantity"] == pytest.approx(expected_qty, rel=1e-7)
        assert pos["stop_loss"] == pytest.approx(stop)
        assert pos["take_profit"] == pytest.approx(entry + (entry - stop) * 2.0)
    finally:
        env.engine.close()


# ── 9-11. SL / TP / fees ────────────────────────────────────────────────────
def test_stop_loss_closes_position(paper):
    open_trade(paper)
    ts = BASE_TS + 60_000
    feed_candle(paper, ts, 99.8, 100.2, 97.0, 97.5)
    assert paper.engine.positions() == []
    trade = paper.engine.history()[0]
    assert trade["exit_reason"] == "STOP_LOSS"
    assert trade["exit_price"] < trade["entry_price"]
    assert any("PAPER TRADE CLOSED" in m for m in paper.alerts)


def test_take_profit_closes_position(paper):
    open_trade(paper)
    ts = BASE_TS + 60_000
    feed_candle(paper, ts, 100.5, 105.0, 100.4, 104.5)
    assert paper.engine.positions() == []
    trade = paper.engine.history()[0]
    assert trade["exit_reason"] == "TAKE_PROFIT"
    assert trade["exit_price"] > trade["entry_price"]
    assert trade["net_pnl"] > 0


def test_fee_and_slippage_calculations(paper):
    open_trade(paper)
    pos = paper.engine.positions()[0]
    entry = pos["entry_price"]
    qty = pos["quantity"]
    expected_entry_fee = qty * entry * 0.001
    ts = BASE_TS + 60_000
    feed_candle(paper, ts, 100.5, 105.0, 100.4, 104.5)
    trade = paper.engine.history()[0]
    tp_ref = pos["take_profit"]
    expected_exit = tp_ref * (1.0 - 2.0 / 10_000.0)
    expected_exit_fee = qty * expected_exit * 0.001
    expected_gross = (expected_exit - entry) * qty
    expected_net = expected_gross - expected_entry_fee - expected_exit_fee
    assert trade["entry_fee"] == pytest.approx(expected_entry_fee, rel=1e-7)
    assert trade["exit_fee"] == pytest.approx(expected_exit_fee, rel=1e-7)
    assert trade["gross_pnl"] == pytest.approx(expected_gross, rel=1e-7)
    assert trade["net_pnl"] == pytest.approx(expected_net, rel=1e-7)


def test_both_sl_and_tp_in_one_candle_uses_stop_first(paper):
    open_trade(paper)
    ts = BASE_TS + 60_000
    feed_candle(paper, ts, 100.5, 105.0, 97.0, 104.0)
    trade = paper.engine.history()[0]
    assert trade["exit_reason"] == "STOP_LOSS"
    conn = sqlite3.connect(str(paper.db))
    try:
        ambiguous = conn.execute(
            "SELECT ambiguous_candle FROM paper_positions WHERE status='CLOSED'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert ambiguous == 1


# ── 12-15. account limits ───────────────────────────────────────────────────
def test_daily_loss_limit_blocks_new_entries(tmp_path):
    env = build(tmp_path / "daily-loss", make_config(
        maximum_daily_loss_percent=0.5))
    try:
        open_trade(env)
        feed_candle(env, BASE_TS + 60_000, 99.8, 100.2, 97.0, 97.5)
        # A new signal would arrive on a fresh ticker in production.
        env.quotes["BTCUSDT"]["ts"] = env.clock["now"]
        result = open_trade(env, signal_id="sig-after-loss")
        assert result["opened"] is False
        assert "maximum_daily_loss" in result["reasons"]
    finally:
        env.engine.close()


def test_maximum_open_positions_enforced(tmp_path):
    env = build(tmp_path / "max-open", make_config(maximum_open_positions=1))
    try:
        assert open_trade(env)["opened"] is True
        result = open_trade(env, signal_id="sig-second")
        assert result["opened"] is False
        assert "maximum_open_positions" in result["reasons"]
        assert len(env.engine.positions()) == 1
    finally:
        env.engine.close()


def test_maximum_daily_trades_enforced(tmp_path):
    env = build(tmp_path / "max-daily", make_config(maximum_daily_trades=1))
    try:
        open_trade(env)
        result = open_trade(env, signal_id="sig-second")
        assert result["opened"] is False
        assert "maximum_daily_trades" in result["reasons"]
    finally:
        env.engine.close()


def test_maximum_exposure_is_enforced(tmp_path):
    env = build(tmp_path / "exposure", make_config(
        default_risk_percent=10.0,
        maximum_total_exposure_percent=20.0,
        maximum_position_percent=100.0))
    try:
        result = open_trade(env)
        assert result["opened"] is True
        pos = env.engine.positions()[0]
        # The exposure cap is computed on pre-entry equity.
        assert pos["quantity"] * pos["entry_price"] <= 10000.0 * 0.20 + 1e-6
    finally:
        env.engine.close()


def test_too_small_position_is_rejected(tmp_path):
    env = build(tmp_path / "tiny", make_config(
        maximum_total_exposure_percent=0.05,
        maximum_position_percent=100.0))
    try:
        result = open_trade(env)
        assert result["opened"] is False
        assert "below_min_notional" in result["reasons"]
    finally:
        env.engine.close()


# ── 16-17. disconnected / stale ─────────────────────────────────────────────
def test_disconnection_blocks_new_entries(paper):
    paper.feed["live"] = False
    paper.feed["transport"] = "none"
    result = open_trade(paper)
    assert result["opened"] is False
    assert "binance_disconnected" in result["reasons"]


def test_stale_market_data_blocks_new_entries(paper):
    paper.feed["last_tick_age_s"] = 9999.0
    result = open_trade(paper)
    assert result["opened"] is False
    assert "stale_market_data" in result["reasons"]


def test_stale_execution_quote_is_rejected(paper):
    paper.quotes["BTCUSDT"]["ts"] = BASE_TS - 20_000
    result = open_trade(paper)
    assert result["opened"] is False
    assert "stale_execution_price" in result["reasons"]


# ── 18. restart recovery ────────────────────────────────────────────────────
def test_restart_recovers_open_position(paper):
    open_trade(paper)
    paper.engine.close()
    restarted = build(paper.base, paper.cfg, alerts=paper.alerts,
                      quotes=paper.quotes, feed=paper.feed, clock=paper.clock)
    try:
        assert len(restarted.engine.positions()) == 1
        feed_candle(restarted, BASE_TS + 60_000, 99.8, 100.2, 97.0, 97.5)
        assert restarted.engine.positions() == []
        assert restarted.engine.history()[0]["exit_reason"] == "STOP_LOSS"
    finally:
        restarted.engine.close()


# ── 19. telegram failure isolation ─────────────────────────────────────────
def test_telegram_failure_does_not_block_entry(tmp_path):
    def boom(text):
        raise RuntimeError("telegram down")

    env = build(tmp_path / "tg-fail", alert_sender=boom)
    try:
        result = open_trade(env)
        assert result["opened"] is True
        assert len(env.engine.positions()) == 1
        conn = sqlite3.connect(str(env.db))
        try:
            delivered = conn.execute(
                "SELECT delivered FROM paper_notifications"
                " WHERE kind='TRADE_OPENED'").fetchone()[0]
        finally:
            conn.close()
        assert delivered == 0
    finally:
        env.engine.close()


# ── 20. stop blocks entries, keeps managing ────────────────────────────────
def test_stop_blocks_new_entries_but_manages_open_positions(paper):
    open_trade(paper)
    paper.engine.stop_trading()
    result = open_trade(paper, signal_id="sig-after-stop")
    assert result["opened"] is False
    assert "paper_trading_stopped" in result["reasons"]
    feed_candle(paper, BASE_TS + 60_000, 99.8, 100.2, 97.0, 97.5)
    assert paper.engine.positions() == []
    assert paper.engine.history()[0]["exit_reason"] == "STOP_LOSS"


# ── 21. transaction failure leaves the balance intact ─────────────────────
class _FailingConn:
    def __init__(self, conn, needle):
        self._conn = conn
        self._needle = needle

    def execute(self, sql, *args, **kwargs):
        if self._needle in sql:
            raise sqlite3.OperationalError("simulated write failure")
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_database_transaction_failure_does_not_corrupt_balance(paper):
    original = paper.engine._conn
    paper.engine._conn = _FailingConn(original, "INSERT INTO paper_positions")
    try:
        result = open_trade(paper)
        assert result["opened"] is False
        assert "database_transaction_failed" in result["reasons"]
        status = paper.engine.status()
        assert status["account"]["balance"] == pytest.approx(10000.0)
        assert status["positions"] == []
        assert table_count(paper, "paper_orders") == 0
    finally:
        paper.engine._conn = original


# ── 22. no real order path ─────────────────────────────────────────────────
def test_no_real_order_path_in_paper_engine():
    text = (BASE / "core" / "paper_trading.py").read_text(encoding="utf-8")
    for forbidden in ("place_order", "create_order", "execute_approved",
                      "orchestrator.agents.trading.broker",
                      "plugins.binance_trader", "/api/v3/order"):
        assert forbidden not in text


# ── extra: reset / report / start-stop / multiple symbols ──────────────────
def test_reset_requires_confirmation_and_archives(paper):
    open_trade(paper)
    with pytest.raises(ValueError):
        paper.engine.reset(confirm=False)
    result = paper.engine.reset(confirm=True)
    assert result["ok"] is True
    assert result["archived_account"] != result["account_id"]
    status = paper.engine.status()
    assert status["account"]["account_id"] == result["account_id"]
    assert status["positions"] == []
    assert status["account"]["balance"] == pytest.approx(10000.0)
    conn = sqlite3.connect(str(paper.db))
    try:
        rows = conn.execute(
            "SELECT status FROM paper_accounts ORDER BY created_at").fetchall()
    finally:
        conn.close()
    assert [r[0] for r in rows] == ["archived", "active"]


def test_daily_report_contains_required_fields(paper):
    open_trade(paper)
    feed_candle(paper, BASE_TS + 60_000, 100.5, 105.0, 100.4, 104.5)
    report = paper.engine.daily_report()
    for field in ("RIO DAILY PAPER TRADING REPORT", "Total trades",
                  "Winning trades", "Losing trades", "Win rate",
                  "Gross profit", "Gross loss", "Total fees", "Net P&L",
                  "Maximum drawdown", "Current equity", "Open positions",
                  "PAPER ONLY"):
        assert field in report


def test_multiple_symbols_are_managed_independently(paper):
    assert open_trade(paper, signal_id="sig-btc")["opened"] is True
    assert open_trade(paper, signal_id="sig-eth",
                      symbol="ETHUSDT")["opened"] is True
    assert len(paper.engine.positions()) == 2
    feed_candle(paper, BASE_TS + 60_000, 99.8, 100.2, 97.0, 97.5,
                symbol="BTCUSDT")
    remaining = paper.engine.positions()
    assert len(remaining) == 1 and remaining[0]["symbol"] == "ETHUSDT"


def test_start_stop_lifecycle_subscribes_without_duplicate_workers(paper,
                                                                   monkeypatch):
    subs = []
    monkeypatch.setattr(pt.bw, "subscribe_closed_candles",
                        lambda cb: subs.append(("add", cb)))
    monkeypatch.setattr(pt.bw, "unsubscribe_closed_candles",
                        lambda cb: subs.append(("remove", cb)))
    fake_liq = SimpleNamespace(
        subscribe_signals=lambda cb: subs.append(("signal_add", cb)),
        unsubscribe_signals=lambda cb: subs.append(("signal_remove", cb)))
    paper.engine._liquidity_engine = fake_liq
    paper.engine.start()
    paper.engine.start()       # idempotent
    assert sum(1 for kind, _ in subs if kind == "add") == 1
    assert sum(1 for kind, _ in subs if kind == "signal_add") == 1
    paper.engine.stop()
    assert any(kind == "remove" for kind, _ in subs)
    assert any(kind == "signal_remove" for kind, _ in subs)
