"""
tests/test_liquidity_integration.py — offline wiring tests.

Covers the Jarvis action contract, BinanceAgent/Orchestrator routing for the
new strategy asks, the dashboard REST surface, and the RIO UI/asset wiring.
No network and no order path.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import actions.liquidity_sweep as action  # noqa: E402
import core.liquidity_sweep as ls  # noqa: E402
from core.action_loader import _call_handler, _validate  # noqa: E402
from dashboard.liquidity_api import register as register_liquidity_api  # noqa: E402
from orchestrator.agents.trading.binance_agent import BinanceAgent  # noqa: E402
from orchestrator.base import BaseAgent  # noqa: E402
from orchestrator.classifier import Classification  # noqa: E402
from orchestrator.contracts import (  # noqa: E402
    AgentRequest, TaskStatus, TaskType,
)
from orchestrator.registry import AgentRegistry  # noqa: E402
from orchestrator.router import TaskRouter  # noqa: E402


def _request(text: str) -> AgentRequest:
    return AgentRequest("test-task-0001", TaskType.TRADING, text)


@pytest.fixture()
def engine(tmp_path):
    config = {
        "globals": {
            "intervals": ["1m", "3m", "5m"], "signal_interval": "3m",
            "swing_lookback": 5, "volume_lookback": 5,
            "volume_min_history": 3, "volume_multiplier": 2.0,
        },
        "profiles": {
            "BTCUSDT": {
                "enabled": True, "zone_low": 100.0, "zone_high": 101.0,
                "watch_levels": [99.0], "breakout_threshold": 102.0,
                "target_price": 110.0, "alerts_enabled": True,
            },
            "ETHUSDT": {
                "enabled": True, "zone_low": 200.0, "zone_high": 201.0,
                "watch_levels": [198.0], "breakout_threshold": 202.0,
                "target_price": 210.0, "alerts_enabled": True,
            },
        },
    }
    cfg_path = tmp_path / "liquidity.json"
    cfg_path.write_text(json.dumps(config), encoding="utf-8")
    eng = ls.LiquiditySweepEngine(
        cfg_path, tmp_path / "market.db",
        alert_sender=lambda text: True,
        live_provider=lambda: {"live": False, "transport": "none",
                               "last_tick_age_s": None},
        data_provider=None,
        price_provider=lambda s: None,
        clock=lambda: 1_765_000_000.0,
    )
    yield SimpleNamespace(engine=eng, path=cfg_path, db=tmp_path / "market.db")
    eng.close()


# ── Jarvis action contract ──────────────────────────────────────────────────
def test_action_is_discoverable_and_uses_the_loader_contract(engine, monkeypatch):
    rec = _validate(action, "liquidity_sweep.py")
    assert rec.valid, rec.error
    monkeypatch.setattr(action, "get_engine", lambda: engine.engine)
    out = _call_handler(rec.handler, {"action": "status", "symbol": "BTC"}, {})
    assert "BTCUSDT" in out
    assert "No signal" in out or "not being monitored" in out


def test_action_conditions_history_and_zone(engine, monkeypatch):
    monkeypatch.setattr(action, "get_engine", lambda: engine.engine)
    cond = action.liquidity_sweep({"action": "conditions", "symbol": "BTC"})
    assert "confirmation conditions" in cond
    hist = action.liquidity_sweep({"action": "history", "symbol": "BTC"})
    assert "history" in hist.lower()
    updated = action.liquidity_sweep(
        {"action": "set_zone", "symbol": "BTC", "low": 99, "high": 100.5})
    assert "Liquidity zone updated" in updated
    assert engine.engine.get_status("BTC")["zone"] == {"low": 99.0, "high": 100.5}
    toggle = action.liquidity_sweep(
        {"action": "set_reversal_alert", "symbol": "BTC", "enabled": "false"})
    assert "disabled" in toggle


# ── BinanceAgent / orchestrator routing ─────────────────────────────────────
def test_binance_agent_accepts_strategy_asks_but_not_order_requests():
    agent = BinanceAgent()
    assert agent.can_handle(_request("should I buy Bitcoin now?"))
    assert agent.can_handle(_request("what is the BTC liquidity sweep status?"))
    assert agent.can_handle(_request("explain the Ethereum reversal"))
    assert not agent.can_handle(_request("BTC analysis and setup for today"))
    assert not agent.can_handle(_request("buy nifty at support"))


def test_binance_agent_returns_engine_advice(engine, monkeypatch):
    monkeypatch.setattr(ls, "get_engine", lambda: engine.engine)
    result = BinanceAgent().handle(_request("should I buy Bitcoin now?"))
    assert result.status is TaskStatus.COMPLETED
    assert "Bitcoin" in result.text
    assert "No signal" in result.text or "not being monitored" in result.text


class _FakeTradingAgent(BaseAgent):
    name = "trading_agent"
    task_types = (TaskType.TRADING,)

    def _handle_impl(self, request):
        raise AssertionError("route() must not execute agents")


def test_classifier_routes_liquidity_phrases_to_trading_lane():
    from orchestrator.classifier import IntentClassifier

    classifier = IntentClassifier()
    for text in (
        "should I buy Bitcoin now?",
        "what is the BTC liquidity sweep status?",
        "explain the Ethereum reversal",
    ):
        assert classifier.classify(text).task_type is TaskType.TRADING


def test_router_strategy_vs_trading():
    registry = AgentRegistry()
    registry.register(_FakeTradingAgent())
    registry.register(BinanceAgent())
    router = TaskRouter(registry)
    classification = Classification(TaskType.TRADING, 0.9, "test")

    def chosen(text):
        return router.route(classification, _request(text)).chosen_agent

    assert chosen("should I buy Bitcoin now?") == "binance_agent"
    assert chosen("BTC liquidity sweep") == "binance_agent"
    assert chosen("BTC analysis and setup for today") == "trading_agent"
    assert chosen("buy nifty at support") == "trading_agent"


# ── dashboard API ───────────────────────────────────────────────────────────
def _client(engine, allow=True):
    app = FastAPI()
    register_liquidity_api(app, lambda req: allow)
    return TestClient(app)


def test_dashboard_state_and_history(engine, monkeypatch):
    monkeypatch.setattr(ls, "get_engine", lambda: engine.engine)
    client = _client(engine)
    state = client.get("/api/rio/liquidity/state")
    assert state.status_code == 200
    body = state.json()
    assert body["ok"] is True
    assert set(body["symbols"]) == {"BTCUSDT", "ETHUSDT"}
    assert body["symbols"]["BTCUSDT"]["state_color"] == "GREY"

    hist = client.get("/api/rio/liquidity/history?symbol=BTCUSDT&limit=5")
    assert hist.status_code == 200 and hist.json()["ok"] is True


def test_dashboard_zone_and_alert_toggle(engine, monkeypatch):
    monkeypatch.setattr(ls, "get_engine", lambda: engine.engine)
    client = _client(engine)
    zone = client.post("/api/rio/liquidity/zone",
                       json={"symbol": "BTC", "low": 99, "high": 100.5})
    assert zone.status_code == 200
    assert zone.json()["zone"] == {"low": 99.0, "high": 100.5}

    alerts = client.post("/api/rio/liquidity/alerts",
                         json={"symbol": "BTC", "enabled": False})
    assert alerts.status_code == 200
    assert alerts.json()["alerts_enabled"] is False

    bad = client.post("/api/rio/liquidity/zone",
                      json={"symbol": "BTC", "low": 101, "high": 100})
    assert bad.status_code == 400


def test_dashboard_requires_auth(engine, monkeypatch):
    monkeypatch.setattr(ls, "get_engine", lambda: engine.engine)
    client = _client(engine, allow=False)
    assert client.get("/api/rio/liquidity/state").status_code == 401


# ── UI / registration static wiring ────────────────────────────────────────
def test_rio_shell_serves_liquidity_route_and_asset():
    from dashboard.rio_api import register as register_rio

    app = FastAPI()
    register_rio(app, lambda req: True)
    client = TestClient(app)
    page = client.get("/liquidity")
    assert page.status_code == 200
    assert "liquidity.js" in page.text
    asset = client.get("/rio/assets/liquidity.js")
    assert asset.status_code == 200
    assert "RIOLiquidity" in asset.text


def test_liquidity_surfaces_have_no_order_path():
    files = [
        BASE / "core" / "liquidity_sweep.py",
        BASE / "actions" / "liquidity_sweep.py",
        BASE / "dashboard" / "liquidity_api.py",
    ]
    for path in files:
        text = path.read_text(encoding="utf-8")
        for forbidden in ("place_order", "create_order", "execute_approved",
                          "orchestrator.agents.trading.broker",
                          "/order"):
            assert forbidden not in text, f"{path.name}: {forbidden}"


def test_ui_and_assets_are_wired():
    ui = (BASE / "dashboard" / "static" / "rio" / "liquidity.js").read_text()
    index = (BASE / "dashboard" / "static" / "rio" / "index.html").read_text()
    app_js = (BASE / "dashboard" / "static" / "rio" / "app.js").read_text()
    rio_api = (BASE / "dashboard" / "rio_api.py").read_text()
    server = (BASE / "dashboard" / "server.py").read_text()

    assert "/api/rio/liquidity/state" in ui
    assert "/rio/assets/liquidity.js" in index
    assert 'path: "/liquidity"' in app_js
    assert 'liquidity: "RIOLiquidity"' in app_js
    assert '"/liquidity"' in rio_api
    assert '"liquidity.js"' in rio_api
    assert "dashboard.liquidity_api" in server
