"""
tests/test_trading_monitor_api.py — offline wiring tests for the continuous
trading monitor's REST surface, dashboard navigation, and worker gating.

No network, no order path.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import core.trading_monitor as tm  # noqa: E402
from dashboard.monitor_api import register as register_monitor_api  # noqa: E402


@pytest.fixture()
def client(monkeypatch, tmp_path):
    cfg = json.loads(json.dumps(tm.DEFAULT_CONFIG))
    cfg_path = tmp_path / "monitor.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    paper_path = tmp_path / "paper.json"
    paper_path.write_text(json.dumps({
        "enabled": True, "initial_balance": 10_000.0,
        "default_risk_percent": 1.0, "leverage": 1.0,
        "simulated_fee_percent": 0.1, "slippage_percent": 0.05,
        "allowed_symbols": ["BTCUSDT"], "min_notional": 10.0,
        "maximum_position_percent": 25.0,
        "symbol_rules": {"BTCUSDT": {"min_quantity": 0.00001,
                                     "quantity_precision": 5}},
    }), encoding="utf-8")

    monitor = tm.TradingMonitor(
        cfg_path, tmp_path / "market.db",
        alert_sender=lambda text: True,
        feed_provider=lambda: {"live": True, "transport": "ws",
                               "last_tick_age_s": 1.0, "latest": {}},
        price_provider=lambda s: {"price": 64_100.0, "ts": 1, "source": "test"},
        paper_config_path=paper_path, clock=lambda: 1_765_000_000.0)
    monkeypatch.setattr(tm, "_monitor", monitor)

    app = FastAPI()
    register_monitor_api(app, lambda req: True)
    with TestClient(app) as c:
        yield c
    monitor.close()
    monkeypatch.setattr(tm, "_monitor", None)


def test_health_endpoint(client):
    r = client.get("/api/rio/monitor/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    health = body["trading_monitor"]
    assert health["trading_monitor"] == "STOPPED"
    assert set(health["symbols"]) == {"BTCUSDT", "ETHUSDT", "SOLUSDT",
                                      "PAXGUSDT"}
    assert health["confirmed_signals_today"] == 0
    assert health["alerts_sent_today"] == 0
    assert health["monitor_errors"] == 0


def test_state_endpoint_has_all_symbol_fields(client):
    r = client.get("/api/rio/monitor/state")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    btc = body["symbols"]["BTCUSDT"]
    for key in ("price", "change_24h", "trend", "ssl_zone", "bsl_level",
                "sweep_zone", "volume", "avg_volume", "rel_volume", "delta",
                "candle_timeframe", "liquidity_sweep_state",
                "rejection_state", "entry_confirmation_state",
                "monitoring_status"):
        assert key in btc, f"missing {key}"


def test_signals_and_lifecycle_endpoints(client):
    for path in ("/api/rio/monitor/signals", "/api/rio/monitor/lifecycle"):
        r = client.get(path)
        assert r.status_code == 200
        assert r.json()["ok"] is True


def test_endpoints_reject_unauthenticated():
    app = FastAPI()
    register_monitor_api(app, lambda req: False)
    with TestClient(app) as c:
        for path in ("/api/rio/monitor/health", "/api/rio/monitor/state",
                     "/api/rio/monitor/signals", "/api/rio/monitor/lifecycle"):
            assert c.get(path).status_code == 401


def test_dashboard_registers_monitor_api():
    source = (BASE / "dashboard" / "server.py").read_text(encoding="utf-8")
    assert "dashboard.monitor_api import register" in source


def test_main_wires_monitor_start_and_shutdown():
    source = (BASE / "main.py").read_text(encoding="utf-8")
    assert "from core import trading_monitor" in source
    assert "get_monitor().start()" in source
    assert "reset_monitor()" in source


def test_monitor_config_is_valid_json_and_matches_defaults():
    on_disk = json.loads(
        (BASE / "config" / "trading_monitor.json").read_text(encoding="utf-8"))
    assert on_disk["symbols"] == tm.DEFAULT_CONFIG["symbols"]
    assert on_disk["verbosity"] in tm.VERBOSITY
    # Gold must be the exchange-traded tokenised instrument, not a spot symbol
    # this feed cannot serve.
    assert "PAXGUSDT" in on_disk["symbols"]
    assert "XAUUSD" not in on_disk["symbols"]
    # No short rule is invented for SOL or gold.
    for sym in ("SOLUSDT", "PAXGUSDT"):
        rule = on_disk["setup_b"]["symbols"][sym]
        assert rule["enabled"] is False
        assert rule["trigger"] is None


def test_liquidity_config_declares_sol_and_gold_without_invented_zones():
    cfg = json.loads(
        (BASE / "config" / "liquidity_sweep.json").read_text(encoding="utf-8"))
    for sym in ("SOLUSDT", "PAXGUSDT"):
        assert sym in cfg["profiles"]
        assert cfg["profiles"][sym]["zone_low"] is None
        assert cfg["profiles"][sym]["zone_high"] is None


def test_rio_health_never_reports_active_for_a_stopped_worker(client, monkeypatch):
    """The existing RIO health probe must agree with the monitor's own state."""
    from dashboard import rio_api

    stopped = rio_api._probe_trading_monitor()
    assert stopped["state"] == "down"
    assert stopped["worker"] == "STOPPED"

    from core.trading_monitor import get_monitor
    monitor = get_monitor()
    monitor.start()
    try:
        running = rio_api._probe_trading_monitor()
        assert running["state"] == "up"
        assert running["worker"] == "RUNNING"
        assert running["market_data"] == "CONNECTED"
    finally:
        monitor.stop()


def test_rio_health_probe_reports_degraded_on_stale_feed(tmp_path, monkeypatch):
    from dashboard import rio_api
    monitor = tm.TradingMonitor(
        tmp_path / "c.json", tmp_path / "m.db",
        alert_sender=lambda text: True,
        feed_provider=lambda: {"live": True, "transport": "ws",
                               "last_tick_age_s": 99_999.0, "latest": {}},
        price_provider=lambda s: {"price": 1.0, "ts": 1, "source": "test"},
        paper_config_path=tmp_path / "p.json", clock=lambda: 1_765_000_000.0)
    monitor.start()
    monkeypatch.setattr(tm, "_monitor", monitor)
    try:
        probe = rio_api._probe_trading_monitor()
        assert probe["state"] == "degraded"
        assert probe["connected"] is False
    finally:
        monitor.close()
        monkeypatch.setattr(tm, "_monitor", None)
