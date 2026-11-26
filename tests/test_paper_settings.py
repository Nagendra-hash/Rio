"""
tests/test_paper_settings.py — Telegram paper-settings commands.

Deterministic offline tests. Synthetic candles/quotes are fixtures only.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import core.paper_commands as commands  # noqa: E402
import core.paper_trading as pt  # noqa: E402

BASE_TS = 1_765_000_000_000


def make_config(**overrides) -> dict:
    cfg = {
        "version": 1, "enabled": True, "initial_balance": 10000.0,
        "quote_currency": "USDT", "default_risk_percent": 1.0,
        "maximum_open_positions": 2, "maximum_daily_trades": 5,
        "maximum_daily_loss_percent": 3.0,
        "maximum_total_exposure_percent": 50.0,
        "maximum_position_percent": 25.0, "simulated_fee_percent": 0.1,
        "slippage_model": {"type": "percent", "value": 0.05},
        "slippage_percent": 0.05, "stop_loss_buffer": 0.0,
        "default_reward_risk_ratio": 2.0, "quantity_mode": "AUTO",
        "fixed_quantity": 0.01, "leverage": 1.0,
        "allowed_symbols": ["BTCUSDT", "ETHUSDT"],
        "position_size_mode": "risk_percent", "min_notional": 10.0,
        "symbol_rules": {
            "BTCUSDT": {"min_quantity": 0.00001, "quantity_precision": 5},
            "ETHUSDT": {"min_quantity": 0.0001, "quantity_precision": 4},
        },
        "execution_price_max_age_s": 10.0, "stale_after_s": 300.0,
        "data_unreliable_action": "suspend",
        "daily_report_enabled": False, "daily_report_hour_utc": 0,
    }
    cfg.update(overrides)
    return cfg


def signal(signal_id="sig-1", **overrides) -> dict:
    event = {
        "event": "LONG_SIGNAL_READY", "signal_id": signal_id,
        "setup_id": signal_id, "symbol": "BTCUSDT", "interval": "1m",
        "state": "LONG_SIGNAL_READY", "ts": BASE_TS,
        "price": 100.0, "zone_low": 99.0, "zone_high": 100.0,
        "invalidation_level": 97.0,
        "sweep": {"price": 98.0}, "rejection": {"close_above_zone": True},
        "volume": {"passed": True, "available": True, "multiplier": 2.5},
        "delta": {"passed": True, "available": True, "delta": 10.0},
        "confirmation": {"passed": True, "close": 100.0},
        "risk": {"ok": True, "stop_loss": 98.0, "risk_amount": 100.0},
    }
    event.update(overrides)
    return event


class Harness:
    def __init__(self, base: Path, cfg=None, quotes=None):
        base.mkdir(parents=True, exist_ok=True)
        self.cfg_path = base / "paper.json"
        self.cfg_path.write_text(json.dumps(cfg or make_config()),
                                 encoding="utf-8")
        self.db = base / "paper.db"
        self.clock = {"now": BASE_TS}
        self.quotes = quotes or {
            "BTCUSDT": {"price": 100.0, "ts": BASE_TS, "source": "fixture"},
            "ETHUSDT": {"price": 200.0, "ts": BASE_TS, "source": "fixture"},
        }
        self.alerts: list[str] = []
        self.engine = self._engine()

    def _engine(self):
        def sender(text):
            self.alerts.append(text)
            return True

        return pt.PaperTradingEngine(
            self.cfg_path, self.db, alert_sender=sender,
            price_provider=lambda s: (dict(self.quotes[s])
                                      if s in self.quotes else None),
            feed_provider=lambda: {"live": True, "transport": "ws",
                                   "last_tick_age_s": 1.0},
            strategy_provider=lambda s: {"ok": True, "symbol": s,
                                         "state": "IDLE"},
            clock=lambda: self.clock["now"] / 1000,
        )

    def restart(self):
        self.engine.close()
        self.engine = self._engine()
        return self.engine

    def close(self):
        self.engine.close()


@pytest.fixture()
def harness(tmp_path, monkeypatch):
    h = Harness(tmp_path / "paper")
    monkeypatch.setattr(commands, "get_engine", lambda: h.engine)
    monkeypatch.setattr(pt, "get_engine", lambda: h.engine)
    yield h
    h.close()


def cmd(harness, text):
    return commands.handle_command(f"[INBOUND telegram:42] {text}")


# ── 1. every setting command (valid input) ─────────────────────────────────
@pytest.mark.parametrize("text,key,expected", [
    ("/paper_capital 25000", "starting_capital", 25000.0),
    ("/paper_quantity 0.05", "fixed_quantity", 0.05),
    ("/paper_leverage 3", "leverage", 3.0),
    ("/paper_rr 1:3", "reward_risk", 3.0),
    ("/paper_risk 2", "risk_percent", 2.0),
    ("/paper_max_trades 8", "max_daily_trades", 8),
    ("/paper_max_positions 4", "max_open_positions", 4),
    ("/paper_daily_loss 5", "max_daily_loss_percent", 5.0),
    ("/paper_max_exposure 40", "max_exposure_percent", 40.0),
    ("/paper_fee 0.2", "fee_percent", 0.2),
    ("/paper_slippage 0.1", "slippage_percent", 0.1),
])
def test_each_setting_command_updates_value(harness, text, key, expected):
    reply = cmd(harness, text)
    assert "updated successfully" in reply
    assert "New:" in reply and "Existing positions: Unchanged." in reply
    assert harness.engine.get_setting(key)["value"] == expected
    saved = json.loads(harness.cfg_path.read_text())
    spec = pt.SETTING_SPECS[key]
    assert saved[spec["config_key"]] == expected


def test_quantity_auto_and_fixed_mode(harness):
    reply = cmd(harness, "/paper_quantity 0.02")
    assert "fixed quantity updated" in reply
    assert harness.engine.get_setting("quantity_mode")["value"] == "FIXED"
    assert harness.engine.get_setting("fixed_quantity")["value"] == 0.02
    reply = cmd(harness, "/paper_quantity auto")
    assert "AUTO" in reply
    assert harness.engine.get_setting("quantity_mode")["value"] == "AUTO"


# ── 2. invalid input ───────────────────────────────────────────────────────
@pytest.mark.parametrize("text,current_key", [
    ("/paper_leverage 0", "leverage"),
    ("/paper_leverage 99", "leverage"),
    ("/paper_risk abc", "risk_percent"),
    ("/paper_risk -1", "risk_percent"),
    ("/paper_max_trades 2.5", "max_daily_trades"),
    ("/paper_rr nonsense", "reward_risk"),
    ("/paper_max_exposure 200", "max_exposure_percent"),
    ("/paper_fee 9", "fee_percent"),
])
def test_invalid_values_are_rejected_and_unchanged(harness, text, current_key):
    before = harness.engine.settings_snapshot()
    reply = cmd(harness, text)
    assert "Rejected" in reply or "AVAILABLE COMMANDS" in reply
    assert harness.engine.settings_snapshot() == before


def test_invalid_input_mentions_bounds(harness):
    reply = cmd(harness, "/paper_leverage 50")
    assert "Rejected" in reply
    assert "20x" in reply


# ── 3. unauthorized users ──────────────────────────────────────────────────
def test_unauthorized_target_cannot_change_settings(harness, monkeypatch):
    class Denied:
        def update_setting(self, *a, **k):
            raise PermissionError("unauthorized chat")

        def __getattr__(self, name):
            raise AssertionError("settings must not be read either")

    monkeypatch.setattr(commands, "get_engine", lambda: Denied())
    reply = cmd(harness, "/paper_risk 2")
    assert "Paper command failed" in reply
    assert "No setting was changed" in reply


def test_approved_chat_allowlist_enforced(harness, monkeypatch):
    monkeypatch.setattr(commands, "_approved_chat_ids", lambda: ["42"])
    ok = commands.handle_command("[INBOUND telegram:42] /paper_risk 2")
    assert "updated successfully" in ok
    denied = commands.handle_command("[INBOUND telegram:999] /paper_risk 5")
    assert "Unauthorized" in denied
    assert harness.engine.get_setting("risk_percent")["value"] == 2.0
    # Read-only commands stay available to any chat.
    monkeypatch.setattr(commands, "get_engine", lambda: harness.engine)
    assert "PAPER TRADING STATUS" in commands.handle_command(
        "[INBOUND telegram:999] /paper_status")


def test_no_allowlist_configured_does_not_block(harness, monkeypatch):
    monkeypatch.setattr(commands, "_approved_chat_ids", lambda: [])
    reply = commands.handle_command("[INBOUND telegram:999] /paper_risk 2")
    assert "updated successfully" in reply


def test_dashboard_command_without_chat_is_allowed(harness, monkeypatch):
    monkeypatch.setattr(commands, "_approved_chat_ids", lambda: ["42"])
    reply = commands.handle_command("/paper_risk 2")
    assert "updated successfully" in reply


def test_actor_is_recorded_from_the_inbound_chat(harness):
    commands.handle_command("[INBOUND telegram:777] /paper_risk 3")
    history = harness.engine.settings_history()
    assert history[0]["setting"] == "risk_percent"
    assert history[0]["actor"] == "telegram:777"


# ── 4. persistence across restart ─────────────────────────────────────────
def test_settings_persist_after_restart(harness):
    cmd(harness, "/paper_risk 2.5")
    cmd(harness, "/paper_leverage 5")
    cmd(harness, "/paper_quantity 0.03")
    engine = harness.restart()
    assert engine.get_setting("risk_percent")["value"] == 2.5
    assert engine.get_setting("leverage")["value"] == 5.0
    assert engine.get_setting("fixed_quantity")["value"] == 0.03
    assert engine.get_setting("quantity_mode")["value"] == "FIXED"


# ── 5. AUTO and FIXED sizing ──────────────────────────────────────────────
def balanced(harness):
    harness.engine.update_setting("max_exposure_percent", 100)
    # 0.5 * ~100 = ~50 USDT, above the 10 USDT min notional.
    harness.engine.update_setting("fixed_quantity", 0.5)


def test_auto_mode_sizes_from_risk(harness):
    # Widen the position cap so the risk-based size is what binds.
    harness.engine.update_setting("max_exposure_percent", 100)
    harness.engine._config["maximum_position_percent"] = 100.0
    result = harness.engine.on_signal(signal())
    assert result["opened"] is True
    pos = harness.engine.positions()[0]
    entry = 100.0 * (1 + 0.0005)
    # 1% of 10,000 = 100 risk; risk distance = entry - 98.
    expected = pt._round_down(100.0 / (entry - 98.0), 5)
    assert pos["quantity"] == pytest.approx(expected)


def test_fixed_mode_uses_configured_quantity(harness):
    balanced(harness)
    engine = harness.engine
    engine.update_setting("quantity_mode", "FIXED")
    result = engine.on_signal(signal())
    assert result["opened"] is True
    assert engine.positions()[0]["quantity"] == pytest.approx(0.5)
    assert engine.positions()[0]["quantity_mode"] == "FIXED"


def test_exposure_cap_binds_auto_quantity(harness):
    """Default 25% position cap limits the AUTO size."""
    result = harness.engine.on_signal(signal())
    assert result["opened"] is True
    pos = harness.engine.positions()[0]
    notional = pos["quantity"] * pos["entry_price"]
    assert notional <= 10000.0 * 0.25 + 1e-6


def test_fixed_mode_rejects_oversized_quantity(harness):
    harness.engine.update_setting("quantity_mode", "FIXED")
    harness.engine.update_setting("fixed_quantity", 500)   # > risk/caps
    result = harness.engine.on_signal(signal())
    assert result["opened"] is False
    assert result["reasons"]
    assert harness.engine.positions() == []


# ── 6. leverage and margin ────────────────────────────────────────────────
def test_leverage_reduces_margin_not_the_stop_distance(harness):
    harness.engine.update_setting("max_exposure_percent", 100)
    harness.engine.update_setting("leverage", 5)
    result = harness.engine.on_signal(signal())
    assert result["opened"] is True
    pos = harness.engine.positions()[0]
    notional = pos["quantity"] * pos["entry_price"]
    assert pos["leverage"] == 5.0
    assert pos["margin"] == pytest.approx(notional / 5.0, rel=1e-9)
    # Loss at the stop is still the FULL price distance (leverage never hides it)
    loss_at_stop = (pos["entry_price"] - pos["stop_loss"]) * pos["quantity"]
    assert loss_at_stop > 0
    assert pos["liquidation_price"] == pytest.approx(
        pos["entry_price"] * (1 - 1 / 5.0))
    status = harness.engine.status()
    assert status["account"]["used_margin"] == pytest.approx(pos["margin"])


def test_stop_beyond_liquidation_is_rejected(harness):
    # Leverage 2x puts the simulated liquidation at 50% of entry; a stop at or
    # beyond it is refused instead of opening an unmanageable position.
    harness.engine.update_setting("max_exposure_percent", 100)
    harness.engine.update_setting("leverage", 2)
    ev = signal()
    ev["risk"] = dict(ev["risk"], stop_loss=100.0 * 0.4)
    result = harness.engine.on_signal(ev)
    assert result["opened"] is False
    assert "stop_beyond_liquidation" in result["reasons"]


# ── 7-9. limit enforcement through settings ───────────────────────────────
def test_daily_trade_limit_from_settings(harness):
    harness.engine.update_setting("max_daily_trades", 1)
    assert harness.engine.on_signal(signal("sig-a"))["opened"] is True
    second = harness.engine.on_signal(signal("sig-b"))
    assert second["opened"] is False
    assert "maximum_daily_trades" in second["reasons"]


def test_daily_loss_limit_from_settings(harness):
    harness.engine.update_setting("max_daily_loss_percent", 0.2)
    assert harness.engine.on_signal(signal("sig-a"))["opened"] is True
    harness.clock["now"] = BASE_TS + 60_000
    harness.engine.on_closed_candle({
        "symbol": "BTCUSDT", "interval": "1m", "closed": True, "ts": BASE_TS,
        "close_ts": BASE_TS + 60_000, "open": 100.0, "high": 100.0,
        "low": 90.0, "close": 91.0, "volume": 1.0, "taker_buy_volume": 1.0,
    })
    harness.quotes["BTCUSDT"]["ts"] = harness.clock["now"]
    result = harness.engine.on_signal(signal("sig-b"))
    assert result["opened"] is False
    assert "maximum_daily_loss" in result["reasons"]


def test_exposure_limit_from_settings(harness):
    # 1% of 10,000 = 100 USDT of notional room; the AUTO size is capped to it.
    harness.engine.update_setting("max_exposure_percent", 1)
    result = harness.engine.on_signal(signal())
    assert result["opened"] is True
    pos = harness.engine.positions()[0]
    notional = pos["quantity"] * pos["entry_price"]
    assert notional <= 100.0 + 1e-6


# ── 10. existing positions unchanged by setting changes ───────────────────
def test_existing_position_snapshot_survives_settings_change(harness):
    assert harness.engine.on_signal(signal())["opened"] is True
    before = harness.engine.positions()[0]
    cmd(harness, "/paper_leverage 4")
    cmd(harness, "/paper_risk 5")
    cmd(harness, "/paper_quantity 0.5")
    cmd(harness, "/paper_fee 0.5")
    cmd(harness, "/paper_slippage 1")
    after = harness.engine.positions()[0]
    for field in ("entry_price", "quantity", "stop_loss", "take_profit",
                  "leverage", "margin", "notional"):
        assert after[field] == before[field], field
    assert after["quantity_mode"] == before["quantity_mode"]
    conn = sqlite3.connect(str(harness.db))
    try:
        snapshot = json.loads(conn.execute(
            "SELECT entry_settings_json FROM paper_positions"
            " WHERE status='OPEN'").fetchone()[0])
    finally:
        conn.close()
    assert snapshot["leverage"] == 1.0
    assert snapshot["risk_percent"] == 1.0


# ── 11. capital change preserves balance and P&L ──────────────────────────
def test_capital_change_preserves_balance_equity_and_pnl(harness):
    assert harness.engine.on_signal(signal())["opened"] is True
    before = harness.engine.status()["account"]
    reply = cmd(harness, "/paper_capital 50000")
    assert "updated successfully" in reply
    after = harness.engine.status()["account"]
    assert after["starting_capital"] == 50000.0
    assert after["balance"] == before["balance"]
    assert after["equity"] == before["equity"]
    assert after["realized_pnl"] == before["realized_pnl"]
    assert after["unrealized_pnl"] == before["unrealized_pnl"]


# ── 12. concurrent updates ────────────────────────────────────────────────
def test_concurrent_settings_updates_are_serialized(harness):
    errors = []

    def worker(value):
        try:
            for _ in range(10):
                harness.engine.update_setting("risk_percent", value,
                                              actor=f"worker-{value}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(v,))
               for v in (1.5, 2.5, 3.5, 4.5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    # Config file is always valid JSON and within range.
    config = json.loads(harness.cfg_path.read_text())
    assert 0.1 <= config["default_risk_percent"] <= 10.0
    # Every accepted change was audited.
    history = harness.engine.settings_history(limit=200)
    assert len(history) >= 1
    assert all(h["setting"] == "risk_percent" for h in history)


# ── 13. notification failures ─────────────────────────────────────────────
def test_telegram_failure_does_not_break_settings_or_engine(tmp_path):
    def boom(text):
        raise RuntimeError("telegram down")

    cfg_path = tmp_path / "paper.json"
    cfg_path.write_text(json.dumps(make_config()), encoding="utf-8")
    engine = pt.PaperTradingEngine(
        cfg_path, tmp_path / "paper.db", alert_sender=boom,
        price_provider=lambda s: {"price": 100.0, "ts": BASE_TS,
                                  "source": "fixture"},
        feed_provider=lambda: {"live": True, "transport": "ws",
                               "last_tick_age_s": 1.0},
        strategy_provider=lambda s: {"ok": True, "state": "IDLE"},
        clock=lambda: BASE_TS / 1000)
    try:
        assert engine.update_setting("risk_percent", 2)["changed"] is True
        assert engine.on_signal(signal())["opened"] is True
        assert engine.get_setting("risk_percent")["value"] == 2.0
    finally:
        engine.close()


# ── 14. action + dashboard surfaces ───────────────────────────────────────
def test_action_supports_settings_and_set(harness, monkeypatch):
    import actions.paper_trading as action

    monkeypatch.setattr(action, "get_engine", lambda: harness.engine)
    shown = action.paper_trading({"action": "settings"})
    assert "PAPER TRADING CONFIGURATION" in shown
    help_text = action.paper_trading({"action": "config"})
    assert "AVAILABLE COMMANDS" in help_text
    out = action.paper_trading({"action": "set", "setting": "risk",
                                "value": "2"})
    assert "updated" in out
    assert harness.engine.get_setting("risk_percent")["value"] == 2.0
    bad = action.paper_trading({"action": "set", "setting": "leverage",
                                "value": "99"})
    assert "Rejected" in bad
    assert harness.engine.get_setting("leverage")["value"] == 1.0


def test_dashboard_settings_routes(harness, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from dashboard.paper_api import register

    monkeypatch.setattr(pt, "get_engine", lambda: harness.engine)
    app = FastAPI()
    register(app, lambda req: True)
    client = TestClient(app)

    state = client.get("/api/rio/paper/settings")
    assert state.status_code == 200
    body = state.json()
    assert body["ok"] is True and body["paper_only"] is True
    assert body["settings"]["leverage"] == 1.0
    assert "risk_percent" in body["specs"]

    ok = client.post("/api/rio/paper/settings",
                     json={"setting": "leverage", "value": 2})
    assert ok.status_code == 200 and ok.json()["changed"] is True
    assert harness.engine.get_setting("leverage")["value"] == 2.0

    bad = client.post("/api/rio/paper/settings",
                      json={"setting": "leverage", "value": 99})
    assert bad.status_code == 400
    missing = client.post("/api/rio/paper/settings", json={})
    assert missing.status_code == 400


# ── 15. no real-money execution ───────────────────────────────────────────
def test_settings_surface_has_no_real_order_path():
    for name in ("paper_commands.py", "paper_trading.py"):
        text = (BASE / "core" / name).read_text(encoding="utf-8")
        for forbidden in ("place_order", "create_order", "execute_approved",
                          "orchestrator.agents.trading.broker",
                          "plugins.binance_trader", "/api/v3/order"):
            assert forbidden not in text, f"{name}: {forbidden}"


# ── report / reset / help ─────────────────────────────────────────────────
def test_paper_settings_report_shows_stored_values(harness):
    assert harness.engine.on_signal(signal())["opened"] is True
    reply = cmd(harness, "/paper_settings")
    for token in ("PAPER TRADING CONFIGURATION", "Starting capital",
                  "Current balance", "Equity", "Quantity mode",
                  "Leverage", "Trading fee", "Slippage", "Risk per trade",
                  "Risk/reward", "Maximum trades/day",
                  "Maximum open positions", "Maximum daily loss",
                  "Maximum exposure", "Paper trading:", "New entries allowed",
                  "Existing positions: 1", "PAPER TRADING ONLY"):
        assert token in reply, token


def test_paper_config_lists_commands_and_examples(harness):
    reply = cmd(harness, "/paper_config")
    for token in ("/paper_capital", "/paper_quantity auto",
                  "/paper_leverage", "/paper_rr", "/paper_risk",
                  "/paper_settings", "Examples:", "NO REAL ORDERS"):
        assert token in reply


def test_reset_settings_requires_confirmation_and_restores_defaults(harness):
    cmd(harness, "/paper_leverage 4")
    cmd(harness, "/paper_risk 3")
    warn = cmd(harness, "/paper_reset_settings")
    assert "confirm" in warn
    assert harness.engine.get_setting("leverage")["value"] == 4.0
    reply = cmd(harness, "/paper_reset_settings confirm")
    assert "restored to defaults" in reply
    assert harness.engine.get_setting("leverage")["value"] == 1.0
    assert harness.engine.get_setting("risk_percent")["value"] == 1.0


def test_unknown_paper_command_returns_usage(harness):
    reply = commands.handle_command("/paper_nonsense")
    assert "PAPER TRADING COMMANDS" in reply
