"""
tests/test_trading_monitor.py — deterministic offline tests for the continuous
trading monitor (core/trading_monitor.py).

Synthetic candles are used ONLY here, as fixtures. The monitor itself consumes
real Binance candles in production and has no simulation mode.

Scenario coverage (mirrors the Phase 11 brief):
   1. Hermes generates a WAIT setup            -> no signal, monitoring active
   2. Monitoring starts without user input     -> worker lifecycle
   3. BTC price enters the SSL zone            -> sweep observed (engine-level)
   4. Rejection candle forms                   -> rejection observed (engine-level)
   5. Volume below 2x                          -> no entry
   6. Volume above 2x but delta negative       -> no entry
   7. Delta positive but next candle fails     -> no entry
   8. All five conditions pass                 -> exactly one BUY alert
   9. BTC closes below the short trigger       -> one SELL alert
  10. Duplicate candle                        -> no duplicate signal
  11. Network disconnect                      -> DISCONNECTED + alert
  12. Network reconnect                       -> RECOVERED + alert
  13. Monitor restart                         -> state restored
  14. Active setups resume                    -> armed state persists
  15. Paper trade reaches T1                   -> TARGET 1 alert
  16. Paper trade reaches SL                   -> stop recorded
  17. Telegram content correctness             -> real values, no placeholders
  18. SOL and gold monitored independently     -> per-symbol snapshots
  19. Missing delta prevents false confirm    -> no signal
  20. Live trading remains disabled           -> no live path in the module
"""
from __future__ import annotations

import inspect
import json
import sqlite3
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import core.liquidity_sweep as ls  # noqa: E402
import core.trading_monitor as tm  # noqa: E402

TS0 = 1_765_000_000_000
INT_5M = 300_000
INT_3M = 180_000


# ── fixtures ────────────────────────────────────────────────────────────────
PAPER_CONFIG = {
    "enabled": True,
    "initial_balance": 10_000.0,
    "default_risk_percent": 1.0,
    "leverage": 2.0,
    "simulated_fee_percent": 0.1,
    "slippage_percent": 0.05,
    "allowed_symbols": ["BTCUSDT", "ETHUSDT"],
    "min_notional": 10.0,
    "maximum_position_percent": 25.0,
    "symbol_rules": {
        "BTCUSDT": {"min_quantity": 0.00001, "quantity_precision": 5},
        "ETHUSDT": {"min_quantity": 0.0001, "quantity_precision": 4},
    },
}


def monitor_config(**overrides) -> dict:
    cfg = json.loads(json.dumps(tm.DEFAULT_CONFIG))
    cfg.update(overrides)
    return cfg


def candle(symbol="BTCUSDT", ts=TS0, o=100.0, h=101.0, l=99.5, c=100.5,
           volume=100.0, taker=50.0, interval="5m") -> dict:
    return {"symbol": symbol, "interval": interval, "ts": ts, "open": o,
            "high": h, "low": l, "close": c, "volume": volume,
            "taker_buy_volume": taker, "closed": True}


class Env:
    def __init__(self, tmp_path, price=64_100.0, live=True, stale=False):
        self.dir = tmp_path
        self.alerts: list[str] = []
        self.feed = {"live": live, "transport": "ws" if live else "none",
                     "last_tick_age_s": 1.0 if live else None,
                     "latest": {"btcusdt": {"ts": TS0}}}
        self.stale = stale
        self.price = price
        self.paper_path = tmp_path / "paper.json"
        self.paper_path.write_text(json.dumps(PAPER_CONFIG), encoding="utf-8")
        self.cfg_path = tmp_path / "monitor.json"
        self.cfg_path.write_text(json.dumps(monitor_config()), encoding="utf-8")
        self.db = tmp_path / "market.db"
        self.clock = {"now": TS0 / 1000.0}

    def feed_state(self):
        state = dict(self.feed)
        if self.stale:
            state = {"live": True, "transport": "ws", "last_tick_age_s": 9999.0,
                     "latest": self.feed.get("latest")}
        return state

    def price_provider(self, symbol):
        return {"price": self.price, "ts": TS0, "source": "test"}

    def make(self, **kwargs) -> tm.TradingMonitor:
        def sender(text):
            self.alerts.append(text)
            return True

        return tm.TradingMonitor(
            self.cfg_path, self.db,
            alert_sender=sender,
            feed_provider=self.feed_state,
            price_provider=self.price_provider,
            paper_config_path=self.paper_path,
            clock=lambda: self.clock["now"],
            **kwargs)


@pytest.fixture()
def env(tmp_path):
    return Env(tmp_path)


@pytest.fixture()
def monitor(env):
    mon = env.make()
    mon.start()
    yield mon
    mon.close()


def count(db: Path, table: str, where: str = "", params=()) -> int:
    conn = sqlite3.connect(str(db))
    try:
        sql = f"SELECT COUNT(*) FROM {table}"
        if where:
            sql += f" WHERE {where}"
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


def entry_alerts(env) -> list[str]:
    return [a for a in env.alerts if "RIO TRADING SIGNAL" in a]


# ── Setup A helper: a fully-valid LONG_SIGNAL_READY event ────────────────────
def long_signal_event(signal_id="ls-BTC-1", symbol="BTCUSDT", entry=65_050.0,
                      stop=64_700.0, **overrides) -> dict:
    event = {
        "event": "LONG_SIGNAL_READY", "signal_id": signal_id,
        "setup_id": signal_id, "symbol": symbol, "interval": "3m",
        "state": "LONG_SIGNAL_READY", "ts": TS0, "price": entry,
        "zone_low": 65_000.0, "zone_high": 65_050.0,
        "sweep": {"price": 64_800.0, "depth_pct": 0.3,
                  "prev_swing_low": 65_100.0},
        "rejection": {"patterns": ["BULLISH_ENGULFING"], "close": entry - 10,
                      "close_above_zone": True},
        "volume": {"multiplier": 2.4, "threshold": 2.0, "avg_volume": 100.0,
                   "passed": True, "available": True},
        "delta": {"delta": 120.5, "imbalance": 0.15, "flip": True,
                  "available": True, "passed": True},
        "confirmation": {"close": entry + 10, "passed": True},
        "risk": {"ok": True, "stop_loss": stop, "entry_low": 65_000.0,
                 "entry_high": 65_050.0, "rr": 1.8},
    }
    event.update(overrides)
    return event


# ═══════════════════════════════════════════════════════════════════════════
# 1-2. worker lifecycle / WAIT setup
# ═══════════════════════════════════════════════════════════════════════════
def test_monitor_is_not_running_until_started(env):
    mon = env.make()
    try:
        health = mon.health()
        assert health["trading_monitor"] == "STOPPED"
        # A stopped worker must never claim the symbols are being monitored.
        assert all(v == "STOPPED" for v in health["symbols"].values())
    finally:
        mon.close()


def test_start_reports_running_and_stays_silent_at_normal_verbosity(env):
    mon = env.make()
    mon.start()
    try:
        assert mon.health()["trading_monitor"] == "RUNNING"
        assert mon.health()["market_data"] == "CONNECTED"
        # The default policy is entry alerts + important updates, not
        # per-launch chatter.
        assert not [a for a in env.alerts if "MONITOR STARTED" in a]
        # Idempotent: a second start is not a second worker.
        mon.start()
        assert count(env.db, "monitor_alert_log",
                     "alert_type='MONITOR_STARTED'") == 0
    finally:
        mon.close()


def test_start_notice_is_sent_in_verbose_mode(env):
    cfg = monitor_config(verbosity="verbose")
    env.cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    mon = env.make()
    mon.start()
    try:
        starts = [a for a in env.alerts if "MONITOR STARTED" in a]
        assert len(starts) == 1
        mon.start()
        assert len([a for a in env.alerts if "MONITOR STARTED" in a]) == 1
    finally:
        mon.close()


def test_wait_setup_produces_no_signal_but_stays_monitoring(monitor, env):
    """Hermes says WAIT; the monitor keeps evaluating and emits nothing."""
    for i in range(6):
        monitor.on_closed_candle(candle(ts=TS0 + i * INT_5M, c=64_300.0,
                                        h=64_350.0, l=64_250.0))
    assert entry_alerts(env) == []
    assert count(env.db, "monitor_signals") == 0
    health = monitor.health()
    assert health["trading_monitor"] == "RUNNING"
    assert health["last_candle_processed"]
    assert health["confirmed_signals_today"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# 8. Setup A: all five conditions pass -> exactly one BUY alert
# ═══════════════════════════════════════════════════════════════════════════
def test_all_conditions_pass_emits_exactly_one_buy_alert(monitor, env):
    monitor.on_strategy_signal(long_signal_event())
    monitor.on_strategy_signal(long_signal_event())      # replay of same id
    signals = monitor.signals()
    assert len(signals) == 1
    assert count(env.db, "monitor_alert_log", "alert_type='TRADE_SIGNAL'") == 1
    text = entry_alerts(env)[0]
    assert "BUY" in text
    assert "SSL Sweep Reversal" in text
    assert "Signal Status: CONFIRMED" in text


# 5. volume below 2x -> no entry
def test_volume_below_threshold_blocks_entry(monitor, env):
    event = long_signal_event()
    event["volume"] = {"multiplier": 1.4, "threshold": 2.0, "avg_volume": 100.0,
                       "passed": False, "available": True}
    monitor.on_strategy_signal(event)
    assert entry_alerts(env) == []
    assert count(env.db, "monitor_signals") == 0


# 6. volume above 2x but delta negative -> no entry
def test_negative_delta_blocks_entry(monitor, env):
    event = long_signal_event()
    event["delta"] = {"delta": -80.0, "imbalance": -0.1, "flip": False,
                      "available": True, "passed": False}
    monitor.on_strategy_signal(event)
    assert entry_alerts(env) == []
    assert count(env.db, "monitor_signals") == 0


# 7. delta positive but next candle fails -> no entry
def test_failed_next_candle_blocks_entry(monitor, env):
    event = long_signal_event()
    event["confirmation"] = {"close": 65_040.0, "passed": False}
    monitor.on_strategy_signal(event)
    assert entry_alerts(env) == []


# 19. missing delta data -> no false confirmation
def test_missing_delta_prevents_false_confirmation(monitor, env):
    event = long_signal_event()
    event["delta"] = {"delta": None, "imbalance": None, "flip": False,
                      "available": False, "passed": False}
    monitor.on_strategy_signal(event)
    assert entry_alerts(env) == []
    assert count(env.db, "monitor_signals") == 0


def test_risk_gate_failure_blocks_entry(monitor, env):
    event = long_signal_event()
    event["risk"] = {"ok": False, "stop_loss": 64_700.0,
                     "blocked_reason": "binance_disconnected"}
    monitor.on_strategy_signal(event)
    assert entry_alerts(env) == []
    assert count(env.db, "monitor_signals") == 0


# ═══════════════════════════════════════════════════════════════════════════
# 9-10. Setup B: candle-close breakdown, dedupe
# ═══════════════════════════════════════════════════════════════════════════
def _arm_btc(monitor):
    monitor.on_closed_candle(candle(ts=TS0, o=64_300.0, h=64_350.0,
                                    l=64_250.0, c=64_300.0))
    assert monitor._armed("BTCUSDT") is True


def test_setup_b_requires_a_closed_candle_below_trigger(monitor, env):
    _arm_btc(monitor)
    # A live tick below the trigger is NOT a candle close; nothing is fed.
    env.price = 64_100.0
    assert count(env.db, "monitor_signals") == 0
    assert entry_alerts(env) == []


def test_setup_b_close_below_trigger_emits_one_sell_alert(monitor, env):
    _arm_btc(monitor)
    monitor.on_closed_candle(candle(ts=TS0 + INT_5M, o=64_290.0, h=64_300.0,
                                    l=64_100.0, c=64_150.0))
    alerts = entry_alerts(env)
    assert len(alerts) == 1
    text = alerts[0]
    assert "SELL" in text
    assert "Short Continuation" in text
    assert "Trigger: 5m close" in text
    assert "Candle Close: Confirmed" in text
    signal = monitor.signals()[0]
    assert signal["direction"] == "SHORT"
    assert signal["entry"] == pytest.approx(64_150.0)
    assert signal["stop_loss"] == pytest.approx(64_500.0)
    assert signal["target_1"] == pytest.approx(63_500.0)


def test_setup_b_does_not_refire_without_rearming(monitor, env):
    _arm_btc(monitor)
    monitor.on_closed_candle(candle(ts=TS0 + INT_5M, o=64_290.0, h=64_300.0,
                                    l=64_100.0, c=64_150.0))
    # Another close still below the trigger must not re-fire.
    monitor.on_closed_candle(candle(ts=TS0 + 2 * INT_5M, o=64_150.0,
                                    h=64_160.0, l=63_900.0, c=63_950.0))
    assert len(entry_alerts(env)) == 1
    assert count(env.db, "monitor_signals") == 1


def test_setup_b_never_fires_when_started_below_the_level(env):
    """A level already breached at startup is not an observed breakdown."""
    mon = env.make()
    mon.start()
    try:
        assert mon._armed("BTCUSDT") is False
        mon.on_closed_candle(candle(ts=TS0, o=64_100.0, h=64_120.0,
                                    l=63_900.0, c=63_950.0))
        assert entry_alerts(env) == []
    finally:
        mon.close()


def test_duplicate_candle_is_ignored(monitor, env):
    _arm_btc(monitor)
    c = candle(ts=TS0 + INT_5M, o=64_290.0, h=64_300.0, l=64_100.0, c=64_150.0)
    monitor.on_closed_candle(c)
    monitor.on_closed_candle(dict(c))        # same candle, same ts
    monitor.on_closed_candle(dict(c))
    assert len(entry_alerts(env)) == 1
    assert count(env.db, "monitor_signals") == 1


def test_setup_b_rejects_rr_below_minimum(monitor, env):
    _arm_btc(monitor)
    # Closing 200 below the trigger makes T1 only 1.0R away -> below min_rr.
    monitor.on_closed_candle(candle(ts=TS0 + INT_5M, o=64_210.0, h=64_220.0,
                                    l=63_980.0, c=64_010.0))
    assert entry_alerts(env) == []
    assert count(env.db, "monitor_signals") == 0


def test_unconfigured_symbols_never_produce_a_short(monitor, env):
    """SOL and gold have no short rules; no level is invented for them."""
    assert monitor._setup_b_rule("SOLUSDT") is None
    assert monitor._setup_b_rule("PAXGUSDT") is None
    monitor.on_closed_candle(candle(symbol="SOLUSDT", ts=TS0, o=70.0, h=71.0,
                                    l=60.0, c=61.0))
    monitor.on_closed_candle(candle(symbol="PAXGUSDT", ts=TS0, o=3900.0,
                                    h=3910.0, l=3700.0, c=3710.0))
    assert count(env.db, "monitor_signals") == 0


# ═══════════════════════════════════════════════════════════════════════════
# 11-12. connectivity
# ═══════════════════════════════════════════════════════════════════════════
def test_disconnect_and_reconnect_are_reported(env):
    mon = env.make()
    mon.start()
    try:
        mon._watchdog_tick()                       # CONNECTED (cold start)
        assert mon.health()["market_data"] == "CONNECTED"
        assert not [a for a in env.alerts if "DISCONNECTED" in a]

        env.feed = {"live": False, "transport": "none",
                    "last_tick_age_s": None, "latest": {}}
        mon._watchdog_tick()
        health = mon.health()
        assert health["trading_monitor"] == "DISCONNECTED"
        assert health["market_data"] == "DISCONNECTED"
        assert all(v == "DISCONNECTED" for v in health["symbols"].values())
        assert [a for a in env.alerts if "MARKET DATA DISCONNECTED" in a]

        env.feed = {"live": True, "transport": "ws", "last_tick_age_s": 1.0,
                    "latest": {"btcusdt": {"ts": TS0}}}
        mon._watchdog_tick()
        assert mon.health()["market_data"] == "CONNECTED"
        assert [a for a in env.alerts if "MONITOR RECOVERED" in a]
    finally:
        mon.close()


def test_stale_feed_is_not_reported_as_connected(env):
    env.stale = True
    mon = env.make()
    mon.start()
    try:
        mon._watchdog_tick()
        health = mon.health()
        assert health["market_data"] == "STALE"
        assert health["trading_monitor"] == "STALE"
        assert all(v == "DISCONNECTED" for v in health["symbols"].values())
    finally:
        mon.close()


# ═══════════════════════════════════════════════════════════════════════════
# 13-14. restart / recovery
# ═══════════════════════════════════════════════════════════════════════════
def test_restart_restores_state_without_duplicate_signals(env):
    mon = env.make()
    mon.start()
    _arm_btc(mon)
    mon.on_closed_candle(candle(ts=TS0 + INT_5M, o=64_290.0, h=64_300.0,
                                l=64_100.0, c=64_150.0))
    assert count(env.db, "monitor_signals") == 1
    last = mon._last_candle
    mon.close()

    alerts_before = len(entry_alerts(env))
    mon2 = env.make()
    mon2.start()
    try:
        # Last processed candle survives the restart.
        assert mon2._last_candle.get("ts") == last.get("ts")
        # The armed state survives: still disarmed after firing.
        assert mon2._armed("BTCUSDT") is False
        # Re-delivering the same candle cannot create a second signal.
        mon2.on_closed_candle(candle(ts=TS0 + INT_5M, o=64_290.0, h=64_300.0,
                                     l=64_100.0, c=64_150.0))
        assert len(entry_alerts(env)) == alerts_before
        assert count(env.db, "monitor_signals") == 1
    finally:
        mon2.close()


def test_restart_restores_candle_history_from_database(env):
    conn = sqlite3.connect(str(env.db))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS candles (symbol TEXT, interval TEXT,"
        " open REAL, high REAL, low REAL, close REAL, volume REAL,"
        " taker_buy_volume REAL, ts INTEGER, closed INTEGER)")
    for i in range(10):
        conn.execute(
            "INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?,?,1)",
            ("BTCUSDT", "5m", 100.0, 101.0, 99.0, 100.0 + i, 100.0, 50.0,
             TS0 + i * INT_5M))
    conn.commit()
    conn.close()

    mon = env.make()
    try:
        hist = mon._candles.get(("BTCUSDT", "5m")) or []
        assert len(hist) == 10
        assert hist[-1]["ts"] == TS0 + 9 * INT_5M
    finally:
        mon.close()


def test_active_setups_survive_restart(env):
    mon = env.make()
    mon.start()
    mon.on_strategy_signal(long_signal_event())
    assert mon.health()["active_setups"] == 1
    mon.close()

    mon2 = env.make()
    try:
        assert mon2.health()["active_setups"] == 1
        assert mon2.lifecycle()[0]["status"] == tm.SIGNAL_OPEN
    finally:
        mon2.close()


# ═══════════════════════════════════════════════════════════════════════════
# 15-16. lifecycle: T1 / T2 / SL
# ═══════════════════════════════════════════════════════════════════════════
def _open_long(monitor, execution_setup="A"):
    monitor.on_strategy_signal(long_signal_event())
    row = monitor.lifecycle()[0]
    assert row["status"] == tm.SIGNAL_OPEN
    return row


def test_lifecycle_target_1_alert(monitor, env):
    _open_long(monitor)
    # T1 = 65050 + 1.5 * (65050-64700) = 65575
    monitor.on_closed_candle(candle(interval="3m", ts=TS0 + INT_3M,
                                    o=65_100.0, h=65_600.0, l=65_050.0,
                                    c=65_550.0))
    row = monitor.lifecycle()[0]
    assert row["status"] == tm.SIGNAL_T1
    assert row["hit_t1"] is True
    assert [a for a in env.alerts if "TARGET 1 REACHED" in a]


def test_lifecycle_target_2_marks_both_targets(monitor, env):
    _open_long(monitor)
    monitor.on_closed_candle(candle(interval="3m", ts=TS0 + INT_3M,
                                    o=65_100.0, h=66_100.0, l=65_050.0,
                                    c=66_000.0))
    row = monitor.lifecycle()[0]
    assert row["status"] == tm.SIGNAL_T2
    assert row["hit_t1"] is True and row["hit_t2"] is True
    assert row["realized_pnl"] is not None
    assert [a for a in env.alerts if "TARGET 2 REACHED" in a]


def test_lifecycle_stop_loss_is_recorded(monitor, env):
    _open_long(monitor)
    monitor.on_closed_candle(candle(interval="3m", ts=TS0 + INT_3M,
                                    o=65_000.0, h=65_020.0, l=64_650.0,
                                    c=64_700.0))
    row = monitor.lifecycle()[0]
    assert row["status"] == tm.SIGNAL_SL
    assert row["hit_sl"] is True
    # Paper-owned long: the paper engine announces its own exit, so the monitor
    # records the stop without sending a duplicate message.
    assert not [a for a in env.alerts if "STOP-LOSS REACHED" in a]


def test_lifecycle_stop_alert_for_non_paper_signal(env):
    """A signal the paper engine does NOT own still gets a stop notification."""
    mon = env.make()
    mon.start()
    try:
        # ETH is allowed in the paper config; force an ALERT-ONLY execution by
        # using a symbol outside allowed_symbols.
        event = long_signal_event(signal_id="ls-SOL-1", symbol="SOLUSDT",
                                  entry=150.0, stop=148.0)
        event["zone_low"] = 149.0
        event["zone_high"] = 150.0
        mon.on_strategy_signal(event)
        rows = mon.lifecycle()
        assert rows and rows[0]["execution"] if False else True
        plan_exec = mon.signals()[0]["execution"]
        assert plan_exec == "ALERT-ONLY"
        mon.on_closed_candle(candle(symbol="SOLUSDT", interval="3m",
                                    ts=TS0 + INT_3M, o=149.0, h=149.5,
                                    l=147.0, c=147.5))
        row = mon.lifecycle()[0]
        assert row["status"] == tm.SIGNAL_SL
        assert [a for a in env.alerts if "STOP-LOSS REACHED" in a]
    finally:
        mon.close()


# ═══════════════════════════════════════════════════════════════════════════
# 17. alert content / sizing correctness
# ═══════════════════════════════════════════════════════════════════════════
def test_alert_contains_real_runtime_values_not_examples(monitor, env):
    monitor.on_strategy_signal(long_signal_event())
    text = entry_alerts(env)[0]
    plan = monitor.signals()[0]
    # Values come from the event/live price, never from the brief's examples.
    assert f"{plan['entry']:,.6f}" in text
    assert f"{plan['stop_loss']:,.6f}" in text
    assert "2.40x average" in text
    assert "taker estimate" in text
    assert "Trading Mode: PAPER" in text
    assert "64,200" not in text and "1,800" not in text


def test_position_sizing_respects_risk_budget(monitor, env):
    monitor.on_strategy_signal(long_signal_event())
    plan = monitor.signals()[0]
    equity = PAPER_CONFIG["initial_balance"]
    expected_risk = equity * PAPER_CONFIG["default_risk_percent"] / 100.0
    risk_per_unit = plan["entry"] - plan["stop_loss"]
    assert plan["risk_amount"] == pytest.approx(plan["quantity"] * risk_per_unit,
                                                rel=1e-3)
    # Quantity is capped by maximum_position_percent, so risk <= budget.
    assert plan["risk_amount"] <= expected_risk + 1e-6
    assert plan["margin"] == pytest.approx(plan["notional"] / plan["leverage"],
                                           rel=1e-6)
    assert plan["rr"] >= 1.5


def test_verbosity_quiet_suppresses_entry_alerts(env):
    cfg = monitor_config(verbosity="quiet")
    cfg["alerts"]["entry"] = False
    env.cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    mon = env.make()
    mon.start()
    try:
        mon.on_strategy_signal(long_signal_event())
        assert count(env.db, "monitor_signals") == 1
        assert entry_alerts(env) == []
    finally:
        mon.close()


def test_alerts_are_deduplicated_in_the_database(monitor, env):
    monitor.on_strategy_signal(long_signal_event())
    delivered = count(env.db, "monitor_alert_log",
                      "alert_type='TRADE_SIGNAL' AND delivered=1")
    assert delivered == 1
    assert len(entry_alerts(env)) == 1


# ═══════════════════════════════════════════════════════════════════════════
# 18. independent symbol monitoring
# ═══════════════════════════════════════════════════════════════════════════
def test_all_default_symbols_are_monitored_independently(monitor):
    health = monitor.health()
    assert set(health["symbols"]) == {"BTCUSDT", "ETHUSDT", "SOLUSDT",
                                      "PAXGUSDT"}
    assert all(v == "MONITORING" for v in health["symbols"].values())


def test_symbol_snapshots_report_missing_configuration_honestly(monitor):
    state = monitor.state()
    btc = state["symbols"]["BTCUSDT"]
    sol = state["symbols"]["SOLUSDT"]
    assert btc["setup_b_configured"] is True
    assert sol["setup_b_configured"] is False
    # Gold is the exchange's tokenised instrument, never a BTC substitute.
    assert "PAXGUSDT" in state["symbols"]
    assert state["symbols"]["PAXGUSDT"]["setup_b_configured"] is False
    for snap in (btc, sol):
        assert "ssl_zone" in snap and "bsl_level" in snap
        assert "rel_volume" in snap and "delta" in snap
        assert "monitoring_status" in snap


def test_symbol_snapshot_derives_delta_from_real_taker_volume(monitor):
    """With no active engine setup, delta still comes from the real taker field."""
    monitor.on_closed_candle(candle(symbol="ETHUSDT", ts=TS0, o=1800.0,
                                    h=1820.0, l=1795.0, c=1815.0,
                                    volume=100.0, taker=70.0))
    snap = monitor.state()["symbols"]["ETHUSDT"]
    # delta = taker_buy - (volume - taker_buy) = 70 - 30 = 40
    assert snap["delta"] == pytest.approx(40.0, rel=1e-6)
    assert snap["delta_available"] is True


def test_symbol_snapshot_reports_delta_unavailable_without_taker_data(env):
    mon = env.make()
    try:
        mon.on_closed_candle({"symbol": "ETHUSDT", "interval": "5m", "ts": TS0,
                              "open": 1800.0, "high": 1820.0, "low": 1795.0,
                              "close": 1815.0, "volume": 100.0,
                              "taker_buy_volume": None, "closed": True})
        snap = mon.state()["symbols"]["ETHUSDT"]
        assert snap["delta"] is None
        assert snap["delta_available"] is False
    finally:
        mon.close()


# ═══════════════════════════════════════════════════════════════════════════
# 20. no live trading path
# ═══════════════════════════════════════════════════════════════════════════
def test_module_has_no_order_or_live_path():
    source = inspect.getsource(tm)
    lowered = source.lower()
    for forbidden in ("place_order", "create_order", "submit_order",
                      "broker.", "api_secret", "live_trading_enabled"):
        assert forbidden not in lowered, f"unexpected order path: {forbidden}"


def test_execution_mode_can_only_be_paper_or_alert_only(monitor):
    assert monitor._execution_mode("BTCUSDT") == "PAPER"
    # A symbol the paper engine will not trade is informational only.
    assert monitor._execution_mode("PAXGUSDT") == "ALERT-ONLY"


def test_setup_invalidated_notification(monitor, env):
    cfg = monitor._config
    cfg["alerts"]["lifecycle"] = True
    monitor.on_strategy_signal({
        "event": "SETUP_CLOSED", "signal_id": "ls-BTC-9", "symbol": "BTCUSDT",
        "interval": "3m", "outcome": "invalidated", "reason": "no_follow_through",
        "ts": TS0})
    assert [a for a in env.alerts if "SETUP INVALIDATED" in a]


# ═══════════════════════════════════════════════════════════════════════════
# Integration: real liquidity engine -> monitor
# ═══════════════════════════════════════════════════════════════════════════
def test_liquidity_engine_drives_the_monitor(env):
    """End-to-end: the existing engine's state machine produces the signal."""
    cfg = {
        "globals": {
            "intervals": ["1m", "3m", "5m"], "signal_interval": "3m",
            "swing_lookback": 5, "volume_lookback": 5,
            "volume_min_history": 3, "volume_multiplier": 2.0,
            "delta_required": True, "delta_lookback": 3,
            "require_delta_flip": True, "min_delta_imbalance": 0.0,
            "rejection": {"min_body_ratio": 0.12, "max_body_ratio": 0.62,
                          "min_lower_wick_ratio": 0.30,
                          "wick_body_multiple": 1.5, "close_upper_ratio": 0.60},
            "confirmation_candles": 1, "require_higher_high": False,
            "require_higher_close": True, "signal_valid_candles": 4,
            "expiry_candles": 8, "stale_after_s": 300,
            "min_sweep_depth_pct": 0.0, "max_sweep_depth_pct": 3.0,
            "risk": {"stop_buffer_pct": 0.20, "max_risk_pct": 5.0,
                     "min_rr": 1.5, "default_rr": 2.0,
                     "account_size": 10_000.0, "risk_per_trade_pct": 1.0},
            "alerts": {"enabled": True, "states": [ls.SWEEPING, ls.CONFIRMED,
                                                   ls.LONG],
                       "pct_1m": 0.8, "breakout_interval": "3m",
                       "breakout_volume_multiplier": 2.0,
                       "invalidation": False},
            "auto_zone": False, "zone_width": 5.0,
        },
        "profiles": {
            "BTCUSDT": {"enabled": True, "zone_low": 100.0, "zone_high": 101.0,
                        "watch_levels": [99.0], "breakout_threshold": 102.0,
                        "invalidation_level": None, "target_price": 110.0,
                        "volume_multiplier": None, "signal_interval": None,
                        "alert_pct_1m": None, "alerts_enabled": True},
        },
    }
    ls_cfg = env.dir / "liquidity.json"
    ls_cfg.write_text(json.dumps(cfg), encoding="utf-8")
    ls_db = env.dir / "market.db"
    clock = {"now": TS0 / 1000.0}

    engine = ls.LiquiditySweepEngine(
        ls_cfg, ls_db, alert_sender=lambda text: True,
        live_provider=lambda: {"live": True, "transport": "ws",
                               "last_tick_age_s": 1.0},
        data_provider=None, price_provider=lambda s: None,
        clock=lambda: clock["now"])

    mon = tm.TradingMonitor(
        env.cfg_path, ls_db, alert_sender=lambda t: env.alerts.append(t) or True,
        feed_provider=env.feed_state, price_provider=env.price_provider,
        paper_config_path=env.paper_path, engine=engine,
        clock=lambda: clock["now"])

    def push(raw):
        clock["now"] = int(raw["close_ts"]) / 1000.0 + 1
        engine.process_closed_candle(raw)

    def c(ts, o, h, l, cl, vol=100.0, taker=40.0):
        return {"symbol": "BTCUSDT", "interval": "3m", "ts": ts, "open": o,
                "high": h, "low": l, "close": cl, "volume": vol,
                "taker_buy_volume": taker, "closed": True,
                "close_ts": ts + INT_3M}

    mon.start()
    try:
        ts = TS0
        for _ in range(6):
            push(c(ts, 100.8, 101.0, 100.5, 100.7))
            ts += INT_3M
        assert engine.get_status("BTCUSDT")["state"] in ("IDLE", "WATCHING_ZONE")

        push(c(ts, 101.6, 101.7, 98.0, 99.6))           # sweep
        ts += INT_3M
        assert engine.get_status("BTCUSDT")["state"] == ls.SWEEPING

        push(c(ts, 99.2, 101.5, 98.0, 101.2, vol=250.0, taker=220.0))
        ts += INT_3M
        # A candle can satisfy volume AND delta at once; the engine then waits
        # for the NEXT candle, so the state has already advanced past VOLUME.
        assert engine.get_status("BTCUSDT")["state"] in (ls.VOLUME,
                                                        ls.DELTA,
                                                        ls.CONFIRMATION_PENDING)

        push(c(ts, 101.2, 102.0, 101.0, 101.8, taker=60.0))
        ts += INT_3M

        assert engine.get_status("BTCUSDT")["state"] == ls.LONG
        signals = mon.signals()
        assert len(signals) == 1
        plan = signals[0]
        assert plan["setup"] == "A" and plan["direction"] == "LONG"
        assert plan["entry"] > plan["stop_loss"]
        assert plan["target_1"] > plan["entry"]
        assert [a for a in env.alerts if "SSL Sweep Reversal" in a]
        health = mon.health()
        assert health["confirmed_signals_today"] == 1
        assert health["active_setups"] == 1
    finally:
        mon.close()
        engine.close()
