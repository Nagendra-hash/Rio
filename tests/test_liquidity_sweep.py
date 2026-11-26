"""
tests/test_liquidity_sweep.py — deterministic offline tests for the liquidity
sweep / reversal engine.

Synthetic candles are used ONLY here, for unit tests. The engine itself only
consumes real Binance candles in production; it has no simulation mode.

Coverage:
  1. price approaches the SSL zone          10. duplicate alert prevention
  2. sweep below the zone                   11. Binance disconnection
  3. sweep without rejection                12. stale market data
  4. rejection without volume spike         13. missing trade-side data
  5. volume spike without positive delta    14. multiple symbols independent
  6. delta without next-candle confirmation 15. 1m / 3m / 5m processing
  7. complete valid reversal                16. daemon restart / state recovery
  8. invalidated setup                      17. breakout + pct alerts
  9. expired setup                          18. insufficient data / no order path
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

import core.liquidity_sweep as ls  # noqa: E402

BASE_TS = 1_765_000_000_000
INT = ls.INTERVAL_MS


# ── config / fixtures ────────────────────────────────────────────────────────
def make_config(**globals_overrides) -> dict:
    profiles = {
        "BTCUSDT": {
            "enabled": True, "zone_low": 100.0, "zone_high": 101.0,
            "watch_levels": [99.0, 98.0, 97.0], "breakout_threshold": 102.0,
            "invalidation_level": None, "target_price": 110.0,
            "volume_multiplier": None, "signal_interval": None,
            "alert_pct_1m": None, "alerts_enabled": True,
        },
        "ETHUSDT": {
            "enabled": True, "zone_low": 200.0, "zone_high": 201.0,
            "watch_levels": [198.0, 195.0], "breakout_threshold": 202.0,
            "invalidation_level": None, "target_price": 210.0,
            "volume_multiplier": None, "signal_interval": None,
            "alert_pct_1m": None, "alerts_enabled": True,
        },
    }
    config = {
        "version": 1,
        "globals": {
            "intervals": ["1m", "3m", "5m"],
            "signal_interval": "3m",
            "swing_lookback": 5,
            "volume_lookback": 5,
            "volume_min_history": 3,
            "volume_multiplier": 2.0,
            "delta_required": True,
            "delta_lookback": 3,
            "require_delta_flip": True,
            "min_delta_imbalance": 0.0,
            "rejection": {
                "min_body_ratio": 0.12, "max_body_ratio": 0.62,
                "min_lower_wick_ratio": 0.30, "wick_body_multiple": 1.5,
                "close_upper_ratio": 0.60,
            },
            "confirmation_candles": 1,
            "require_higher_high": False,
            "require_higher_close": True,
            "signal_valid_candles": 4,
            "expiry_candles": 8,
            "stale_after_s": 300,
            "min_sweep_depth_pct": 0.0,
            "max_sweep_depth_pct": 3.0,
            "risk": {
                "stop_buffer_pct": 0.20, "max_risk_pct": 5.0, "min_rr": 1.5,
                "default_rr": 2.0, "account_size": 10000.0,
                "risk_per_trade_pct": 1.0,
            },
            "alerts": {
                "enabled": True,
                "states": [ls.SWEEPING, ls.CONFIRMED, ls.LONG],
                "pct_1m": 0.8, "breakout_interval": "3m",
                "breakout_volume_multiplier": 2.0, "invalidation": False,
            },
            "auto_zone": False,
            "zone_width": 5.0,
        },
        "profiles": profiles,
    }
    config["globals"].update(globals_overrides)
    return config


def candle(symbol, ts, o, h, l, c, volume=100.0, taker=40.0,
           interval="3m") -> dict:
    return {
        "symbol": symbol, "interval": interval, "ts": ts,
        "open": o, "high": h, "low": l, "close": c, "volume": volume,
        "taker_buy_volume": taker, "closed": True,
        "close_ts": ts + INT[interval],
    }


def feed(env, raw, engine=None):
    eng = engine or env.engine
    env.clock["now"] = int(raw["close_ts"]) + 1000
    return eng.process_closed_candle(raw)


def warmup(env, symbol="BTCUSDT", interval="3m", n=6, start=BASE_TS,
           engine=None, base=100.7, low=100.5, high=101.0):
    ts = start
    for _ in range(n):
        feed(env, candle(symbol, ts, base + 0.1, high, low, base,
                         100.0, 40.0, interval), engine)
        ts += INT[interval]
    return ts


def new_engine(env, alerts=None):
    alerts = alerts if alerts is not None else env.alerts

    def sender(text):
        alerts.append(text)
        return True

    return ls.LiquiditySweepEngine(
        env.cfg_path, env.db,
        alert_sender=sender,
        live_provider=lambda: dict(env.feed),
        data_provider=None,
        price_provider=lambda s: None,
        clock=lambda: env.clock["now"] / 1000,
    )


@pytest.fixture()
def env(tmp_path):
    cfg_path = tmp_path / "liquidity.json"
    cfg_path.write_text(json.dumps(make_config()), encoding="utf-8")
    clock = {"now": BASE_TS}
    feed_state = {"live": True, "transport": "ws", "last_tick_age_s": 1.0}
    alerts: list[str] = []

    def sender(text):
        alerts.append(text)
        return True

    engine = ls.LiquiditySweepEngine(
        cfg_path, tmp_path / "market.db",
        alert_sender=sender,
        live_provider=lambda: dict(feed_state),
        data_provider=None,
        price_provider=lambda s: None,
        clock=lambda: clock["now"] / 1000,
    )
    e = SimpleNamespace(
        engine=engine, cfg_path=cfg_path, db=tmp_path / "market.db",
        clock=clock, feed=feed_state, alerts=alerts, tmp=tmp_path)
    yield e
    engine.close()


def sweep_candle(symbol, ts, interval="3m", low=98.0, close=99.6,
                 volume=120.0, taker=30.0):
    return candle(symbol, ts, close + 1.0, close + 1.1, low, close,
                  volume, taker, interval)


def rejection_candle(symbol, ts, interval="3m", volume=250.0, taker=220.0):
    return candle(symbol, ts, 99.2, 101.5, 98.0, 101.2, volume, taker, interval)


def confirm_candle(symbol, ts, interval="3m", close=101.8, taker=60.0):
    return candle(symbol, ts, 101.2, 102.0, 101.0, close, 100.0, taker, interval)


def build_to_pending(env, symbol="BTCUSDT", interval="3m"):
    ts = warmup(env, symbol, interval)
    feed(env, sweep_candle(symbol, ts, interval))
    ts += INT[interval]
    feed(env, rejection_candle(symbol, ts, interval))
    ts += INT[interval]
    return ts


def count_table(env, table):
    conn = sqlite3.connect(str(env.db))
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


# ── 1. approach / 2. sweep / 3. sweep without rejection ─────────────────────
def test_price_approaches_ssl_zone_without_signal(env):
    warmup(env)
    status = env.engine.get_status("BTC")
    assert status["state"] in ("WATCHING_ZONE", "IDLE")
    assert status["zone_entered"] is True
    assert status["signal_ready"] is False
    assert status["conditions"][1]["status"] == "PENDING"


def test_sweep_below_ssl_starts_sweeping_state(env):
    ts = warmup(env)
    status = feed(env, sweep_candle("BTCUSDT", ts))
    assert status["state"] == ls.SWEEPING
    assert status["state_color"] == "RED"
    assert status["signal_ready"] is False
    assert status["sweep"]["depth_pct"] > 0
    assert any("LIQUIDITY SWEEP DETECTED" in m for m in env.alerts)
    assert count_table(env, "liquidity_sweeps") == 1


def test_sweep_without_rejection_stays_sweeping(env):
    ts = warmup(env)
    feed(env, sweep_candle("BTCUSDT", ts))
    ts += INT["3m"]
    # Red candle closing below the zone, no bullish rejection.
    status = feed(env, candle("BTCUSDT", ts, 99.6, 99.8, 99.0, 99.2,
                              100, 40, "3m"))
    assert status["state"] == ls.SWEEPING
    assert status["rejection"] is None
    assert status["signal_ready"] is False


def test_plain_green_candle_is_not_a_rejection(env):
    ts = warmup(env)
    feed(env, sweep_candle("BTCUSDT", ts))
    ts += INT["3m"]
    # Bullish and back above the zone, but almost no lower wick and a large
    # body: must not be classified as any rejection pattern.
    status = feed(env, candle("BTCUSDT", ts, 100.0, 100.6, 99.9, 100.5,
                              250, 220, "3m"))
    assert status["state"] == ls.SWEEPING
    assert status["rejection"] is None
    assert status["signal_ready"] is False


@pytest.mark.parametrize("shape,expected", [
    ((99.8, 100.8, 98.5, 100.6), "HAMMER"),
    ((100.0, 100.3, 98.0, 100.2), "PIN_BAR"),
    ((99.4, 101.2, 99.3, 101.0), "BULLISH_ENGULFING"),
    ((99.2, 101.5, 98.0, 101.2), "STRONG_REJECTION"),
])
def test_rejection_patterns_are_detected(env, shape, expected):
    ts = warmup(env)
    feed(env, sweep_candle("BTCUSDT", ts))
    ts += INT["3m"]
    o, h, l, c = shape
    status = feed(env, candle("BTCUSDT", ts, o, h, l, c, 250, 220, "3m"))
    assert status["rejection"] is not None
    assert expected in status["rejection"]["patterns"], status["rejection"]


# ── 4. rejection without volume / 5. volume without delta ───────────────────
def test_rejection_without_volume_spike_does_not_advance(env):
    ts = warmup(env)
    feed(env, sweep_candle("BTCUSDT", ts))
    ts += INT["3m"]
    status = feed(env, rejection_candle("BTCUSDT", ts, volume=120.0,
                                        taker=100.0))
    assert status["state"] == ls.REJECTION
    assert status["rejection"] is not None
    assert status["volume"]["passed"] is False
    assert status["signal_ready"] is False


def test_volume_spike_without_positive_delta_does_not_advance(env):
    ts = warmup(env)
    feed(env, sweep_candle("BTCUSDT", ts))
    ts += INT["3m"]
    status = feed(env, rejection_candle("BTCUSDT", ts, volume=250.0,
                                        taker=50.0))
    assert status["state"] == ls.VOLUME
    assert status["volume"]["passed"] is True
    assert status["delta"]["available"] is True
    assert status["delta"]["passed"] is False
    assert status["signal_ready"] is False


# ── 6. delta without confirmation / 7. complete reversal ────────────────────
def test_positive_delta_without_next_candle_confirmation_invalidates(env):
    ts = build_to_pending(env)
    assert env.engine.get_status("BTC")["state"] == ls.CONFIRMATION_PENDING
    status = feed(env, confirm_candle("BTCUSDT", ts, close=100.8))
    assert status["signal_ready"] is False
    assert status["state"] != ls.LONG
    rows = env.engine.get_history("BTC")
    assert any(r["kind"] == "outcome" and r["outcome"] == "invalidated"
               and r["reason"] == "no_follow_through" for r in rows)


def test_complete_valid_reversal_reaches_long_signal(env):
    ts = build_to_pending(env)
    status = feed(env, confirm_candle("BTCUSDT", ts))
    assert status["state"] == ls.LONG
    assert status["state_color"] == "GREEN"
    assert status["signal_ready"] is True
    risk = status["risk"]
    assert risk["ok"] is True
    assert risk["entry_low"] < risk["entry_high"]
    assert risk["stop_loss"] < risk["entry_low"]
    assert risk["rr"] >= 1.5
    assert any("REVERSAL CONFIRMATION" in m for m in env.alerts)
    assert any("NO real order was placed" in m for m in env.alerts)
    assert count_table(env, "reversal_signals") == 1


# ── 8. invalidation / 9. expiry ─────────────────────────────────────────────
def test_setup_invalidated_when_price_breaks_invalidation(env):
    ts = warmup(env)
    feed(env, sweep_candle("BTCUSDT", ts))
    ts += INT["3m"]
    status = feed(env, candle("BTCUSDT", ts, 99.0, 99.2, 97.5, 97.8,
                              100, 40, "3m"))
    assert status["state"] != ls.SWEEPING
    assert status["signal_ready"] is False
    rows = env.engine.get_history("BTC")
    assert any(r["kind"] == "outcome"
               and r["reason"] == "price_broke_invalidation" for r in rows)


def test_setup_expires_after_configured_candles(env):
    env.engine._expiry_candles = 5
    ts = warmup(env)
    feed(env, sweep_candle("BTCUSDT", ts))
    for _ in range(5):
        ts += INT["3m"]
        # Bearish doji: stays above the zone, never a bullish rejection.
        status = feed(env, candle("BTCUSDT", ts, 100.8, 100.9, 100.5, 100.7,
                                  100, 40, "3m"))
    assert status["signal_ready"] is False
    assert status["state"] != ls.SWEEPING
    rows = env.engine.get_history("BTC")
    assert any(r["kind"] == "outcome" and r["outcome"] == "expired"
               and r["reason"] == "max_candles" for r in rows)


# ── 10. duplicate alerts ────────────────────────────────────────────────────
def test_duplicate_alert_prevention_and_restart(env):
    ts = build_to_pending(env)
    confirm = confirm_candle("BTCUSDT", ts)
    feed(env, confirm)
    first_alerts = sum("REVERSAL CONFIRMATION" in m for m in env.alerts)
    assert first_alerts == 1
    assert count_table(env, "liquidity_sweeps") == 1
    assert count_table(env, "reversal_signals") == 1

    # Same candle again in the same engine: deduped by candle timestamp.
    feed(env, confirm)
    assert sum("REVERSAL CONFIRMATION" in m for m in env.alerts) == 1

    # New engine, same DB: the alert log and candle history prevent a resend.
    env.engine.close()
    restarted = new_engine(env)
    try:
        restarted.process_closed_candle(confirm)
        assert sum("REVERSAL CONFIRMATION" in m for m in env.alerts) == 1
        assert count_table(env, "liquidity_sweeps") == 1
        assert count_table(env, "reversal_signals") == 1
    finally:
        restarted.close()


# ── 11. disconnection / 12. stale data ──────────────────────────────────────
def test_binance_disconnection_blocks_long_signal(env):
    ts = build_to_pending(env)
    env.feed["live"] = False
    env.feed["transport"] = "none"
    status = feed(env, confirm_candle("BTCUSDT", ts))
    assert status["state"] == ls.CONFIRMED
    assert status["signal_ready"] is False
    assert status["signal_blocked"] == "binance_disconnected"
    assert any("RISK BLOCKED" in m for m in env.alerts)

    # Reconnect and a fresh candle: risk is re-evaluated, then the signal is
    # released (still informational only).
    env.feed["live"] = True
    env.feed["transport"] = "ws"
    env.feed["last_tick_age_s"] = 1.0
    ts += INT["3m"]
    status = feed(env, confirm_candle("BTCUSDT", ts, close=102.0))
    assert status["state"] == ls.LONG
    assert status["signal_ready"] is True


def test_stale_market_data_blocks_long_signal(env):
    ts = build_to_pending(env)
    env.feed["last_tick_age_s"] = 9999.0
    status = feed(env, confirm_candle("BTCUSDT", ts))
    assert status["state"] == ls.CONFIRMED
    assert status["signal_blocked"] == "stale_market_data"
    assert status["signal_ready"] is False


# ── 13. missing trade data ──────────────────────────────────────────────────
def test_risk_limits_block_long_signal(env):
    env.engine._risk_cfg["max_risk_pct"] = 0.1
    env.engine._risk_cfg["min_rr"] = 99.0
    ts = build_to_pending(env)
    status = feed(env, confirm_candle("BTCUSDT", ts))
    assert status["state"] == ls.CONFIRMED
    assert status["signal_ready"] is False
    assert "risk_pct_above_max" in status["signal_blocked"]
    assert "risk_reward_below_min" in status["signal_blocked"]
    assert count_table(env, "reversal_signals") == 1


def test_missing_trade_data_marks_delta_unavailable(env):
    ts = warmup(env)
    feed(env, sweep_candle("BTCUSDT", ts))
    ts += INT["3m"]
    status = feed(env, rejection_candle("BTCUSDT", ts, taker=None))
    assert status["state"] == ls.VOLUME
    assert status["volume"]["passed"] is True
    assert status["delta"]["available"] is False
    assert status["delta"]["passed"] is False
    assert any(c["name"] == "Buying pressure / delta"
               and c["status"] == "UNAVAILABLE" for c in status["conditions"])
    assert status["signal_ready"] is False


# ── 14. independent symbols ─────────────────────────────────────────────────
def test_multiple_symbols_operate_independently(env):
    btc_ts = warmup(env, "BTCUSDT", "3m")
    feed(env, sweep_candle("BTCUSDT", btc_ts, "3m"))
    eth_ts = warmup(env, "ETHUSDT", "3m", base=200.7, low=200.5, high=201.0)
    btc = env.engine.get_status("BTC")
    eth = env.engine.get_status("ETH")
    assert btc["state"] == ls.SWEEPING
    assert eth["state"] in ("WATCHING_ZONE", "IDLE")
    assert eth["signal_ready"] is False

    feed(env, sweep_candle("ETHUSDT", eth_ts, "3m", low=198.0, close=199.0))
    assert env.engine.get_status("ETH")["state"] == ls.SWEEPING
    assert env.engine.get_status("BTC")["state"] == ls.SWEEPING


# ── 15. all three timeframes ────────────────────────────────────────────────
def test_one_three_five_minute_processing(env):
    ts = warmup(env, "BTCUSDT", "1m")
    feed(env, sweep_candle("BTCUSDT", ts, "1m"))
    status = env.engine.get_status("BTC")
    assert status["timeframe"] == "1m"
    assert status["state"] == ls.SWEEPING
    assert status["history_by_interval"]["1m"] >= 7

    # Rejection-shaped candles on 3m/5m must not advance a 1m setup.
    feed(env, rejection_candle("BTCUSDT", ts + 60_000, "3m"))
    feed(env, rejection_candle("BTCUSDT", ts + 120_000, "5m"))
    status = env.engine.get_status("BTC")
    assert status["state"] == ls.SWEEPING
    assert status["history_by_interval"]["3m"] == 1
    assert status["history_by_interval"]["5m"] == 1

    # The same interval does advance.
    status = feed(env, rejection_candle("BTCUSDT", ts + 60_000, "1m"))
    assert status["state"] in (ls.CONFIRMATION_PENDING, ls.REJECTION,
                               ls.VOLUME, ls.DELTA)


# ── 16. restart / state recovery ────────────────────────────────────────────
def test_daemon_restart_recovers_confirmation_pending(env):
    ts = build_to_pending(env)
    assert env.engine.get_status("BTC")["state"] == ls.CONFIRMATION_PENDING
    env.engine.close()
    restarted = new_engine(env)
    try:
        status = restarted.get_status("BTC")
        assert status["state"] == ls.CONFIRMATION_PENDING
        assert status["sweep"] is not None
        status = feed(env, confirm_candle("BTCUSDT", ts), restarted)
        assert status["state"] == ls.LONG
        assert status["signal_ready"] is True
        assert sum("LIQUIDITY SWEEP DETECTED" in m for m in env.alerts) == 1
        assert sum("REVERSAL CONFIRMATION" in m for m in env.alerts) == 1
    finally:
        restarted.close()


# ── 17. breakout / percentage alerts ────────────────────────────────────────
def test_breakout_alert_requires_volume_and_positive_pressure(env):
    ts = warmup(env)
    breakout = candle("BTCUSDT", ts, 100.9, 103.1, 100.6, 103.0,
                      250.0, 220.0, "3m")
    feed(env, breakout)
    assert any("BREAKOUT ALERT" in m for m in env.alerts)
    assert any("NOT a reversal confirmation" in m for m in env.alerts)
    assert count_table(env, "reversal_signals") == 0

    # Negative delta must not produce a second breakout alert.
    ts += INT["3m"]
    feed(env, candle("BTCUSDT", ts, 103.0, 103.5, 102.8, 103.2,
                     250.0, 30.0, "3m"))
    assert sum("BREAKOUT ALERT" in m for m in env.alerts) == 1


def test_pct_1m_alert_is_never_a_reversal(env):
    ts = warmup(env, "BTCUSDT", "1m")
    status = feed(env, candle("BTCUSDT", ts, 100.0, 101.2, 99.9, 101.0,
                              100.0, 60.0, "1m"))
    assert any("MOVE ALERT" in m for m in env.alerts)
    assert any("NOT a reversal confirmation" in m for m in env.alerts)
    assert status["signal_ready"] is False


# ── 18. insufficient data / bounds / no order path / dashboard ─────────────
def test_insufficient_history_never_starts_a_setup(env):
    status = feed(env, sweep_candle("BTCUSDT", BASE_TS, "3m"))
    assert status["state"] in ("IDLE", "WATCHING_ZONE")
    assert status["signal_ready"] is False
    assert count_table(env, "liquidity_sweeps") == 0


def test_volume_multiplier_is_bounded_to_2x_3x(env):
    assert env.engine._effective_volume_multiplier(
        {"volume_multiplier": 1.0}) == 2.0
    assert env.engine._effective_volume_multiplier(
        {"volume_multiplier": 5.0}) == 3.0
    assert env.engine._effective_volume_multiplier(
        {"volume_multiplier": 2.5}) == 2.5


def test_module_contains_no_order_path():
    text = (BASE / "core" / "liquidity_sweep.py").read_text(encoding="utf-8")
    for forbidden in ("place_order", "create_order", "execute_approved",
                      "orchestrator.agents.trading.broker"):
        assert forbidden not in text


def test_dashboard_payload_has_required_fields(env):
    warmup(env)
    status = env.engine.get_status("BTC")
    for key in ("zone", "price", "state", "state_color", "sweep", "rejection",
                "volume", "delta", "conditions", "risk", "last_update",
                "feed", "signal_ready", "timeframe"):
        assert key in status
    assert status["state_color"] in ("RED", "YELLOW", "GREEN", "GREY")
    payload = env.engine.dashboard_state()
    assert "BTCUSDT" in payload["symbols"] and "ETHUSDT" in payload["symbols"]


def test_manual_zone_update_and_auto_zone(env):
    ts = warmup(env)
    feed(env, sweep_candle("BTCUSDT", ts))
    status = env.engine.set_zone("BTC", 99.0, 100.5)
    assert status["zone"] == {"low": 99.0, "high": 100.5}
    assert status["state"] != ls.SWEEPING          # old setup invalidated
    saved = json.loads(env.cfg_path.read_text())
    assert saved["profiles"]["BTCUSDT"]["zone_low"] == 99.0

    auto = env.engine.auto_set_zone("BTC", persist=True)
    assert auto["zone"]["low"] == 100.5             # recent swing low
    assert auto["zone"]["high"] == 105.5            # low + zone_width


def test_alert_sender_failure_does_not_break_engine(env):
    eng = ls.LiquiditySweepEngine(
        env.cfg_path, env.tmp / "fail.db",
        alert_sender=lambda text: (_ for _ in ()).throw(RuntimeError("down")),
        live_provider=lambda: dict(env.feed),
        data_provider=None,
        price_provider=lambda s: None,
        clock=lambda: env.clock["now"] / 1000,
    )
    try:
        ts = warmup(env, engine=eng)
        status = feed(env, sweep_candle("BTCUSDT", ts), eng)
        assert status["state"] == ls.SWEEPING
        conn = sqlite3.connect(str(eng.db_path))
        try:
            row = conn.execute(
                "SELECT delivered FROM alert_log WHERE alert_type='SWEEP'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None and row[0] == 0
    finally:
        eng.close()
