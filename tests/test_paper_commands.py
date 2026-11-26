"""
tests/test_paper_commands.py — deterministic /paper_* commands, Jarvis action,
dashboard API and UI wiring. PAPER ONLY.
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

import actions.paper_trading as action  # noqa: E402
import core.paper_commands as commands  # noqa: E402
import core.paper_trading as pt  # noqa: E402
from core.action_loader import _call_handler, _validate  # noqa: E402
from dashboard.paper_api import register as register_paper_api  # noqa: E402


class FakeEngine:
    def __init__(self):
        self.started = 0
        self.stopped = 0
        self.resets = 0
        self.reset_calls = []

    def status(self):
        return {
            "ok": True,
            "paper_only": True,
            "account": {
                "account_id": "paper-test", "initial_balance": 10000.0,
                "balance": 10000.0, "equity": 10000.0,
                "available_balance": 10000.0, "realized_pnl": 0.0,
                "unrealized_pnl": 0.0, "quote_currency": "USDT",
                "peak_equity": 10000.0, "max_drawdown": 0.0,
                "status": "active",
            },
            "positions": [],
            "performance": {
                "total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
                "gross_profit": 0.0, "gross_loss": 0.0, "fees": 0.0,
                "net_pnl": 0.0, "profit_factor": None, "max_drawdown": 0.0,
                "daily": [],
            },
            "daily": {"trades": 0, "net_pnl": 0.0},
            "strategy": {"BTCUSDT": {"ok": True, "state": "IDLE"}},
            "feed": {"live": True, "transport": "ws", "stale": False,
                     "last_tick_age_s": 1.0},
            "monitoring": {"started": True, "accepting_entries": True,
                           "enabled": True,
                           "allowed_symbols": ["BTCUSDT", "ETHUSDT"],
                           "last_candle": None},
            "last_update": "2026-11-14T18:00:00+00:00",
            "config": {},
        }

    def positions(self):
        return []

    def history(self, limit=20):
        return []

    def performance(self):
        return self.status()["performance"]

    def daily_report(self, day=None):
        return "RIO DAILY PAPER TRADING REPORT\n\nMode: PAPER ONLY"

    def start_trading(self):
        self.started += 1

    def stop_trading(self):
        self.stopped += 1

    def reset(self, confirm=False):
        self.reset_calls.append(confirm)
        if not confirm:
            raise ValueError("confirm required")
        self.resets += 1
        return {"ok": True, "archived_account": "old", "account_id": "new"}


@pytest.fixture()
def fake(monkeypatch):
    engine = FakeEngine()
    monkeypatch.setattr(commands, "get_engine", lambda: engine)
    monkeypatch.setattr(pt, "get_engine", lambda: engine)
    monkeypatch.setattr(action, "get_engine", lambda: engine)
    return engine


# ── Telegram commands ──────────────────────────────────────────────────────
@pytest.mark.parametrize("command,expect", [
    ("/paper_status", "RIO PAPER TRADING STATUS"),
    ("/paper_balance", "RIO PAPER BALANCE"),
    ("/paper_positions", "No open paper positions"),
    ("/paper_history", "No closed paper trades"),
    ("/paper_performance", "RIO PAPER PERFORMANCE"),
    ("/paper_report", "RIO DAILY PAPER TRADING REPORT"),
])
def test_paper_commands_return_replies(fake, command, expect):
    reply = commands.handle_command(f"[INBOUND telegram:12345] {command}")
    assert reply is not None and expect in reply
    assert "PAPER" in reply


def test_paper_start_and_stop_only_toggle_entries(fake):
    start = commands.handle_command("/paper_start")
    assert "PAPER entries enabled" in start
    assert fake.started == 1
    stop = commands.handle_command("/paper_stop")
    assert "New PAPER entries stopped" in stop
    assert fake.stopped == 1


def test_paper_reset_requires_confirmation(fake):
    reply = commands.handle_command("/paper_reset")
    assert "explicit confirmation" in reply
    assert fake.reset_calls == []
    ok = commands.handle_command("/paper_reset confirm")
    assert "Paper account reset" in ok
    assert fake.reset_calls == [True]


def test_non_paper_text_is_not_a_command():
    assert commands.handle_command("[INBOUND telegram:1] hello RIO") is None


def test_reply_target_parses_inbound_tag():
    assert commands.reply_target("[INBOUND telegram:12345] /paper_status") == "telegram:12345"
    assert commands.reply_target("/paper_status") == "telegram"


# ── Jarvis action ──────────────────────────────────────────────────────────
def test_paper_action_is_valid_and_uses_loader_contract(fake):
    rec = _validate(action, "paper_trading.py")
    assert rec.valid, rec.error
    out = _call_handler(rec.handler, {"action": "status"}, {})
    assert "RIO PAPER TRADING STATUS" in out
    assert "PAPER ONLY" in out


def test_paper_action_reset_requires_confirm(fake):
    out = action.paper_trading({"action": "reset"})
    assert "explicit confirmation" in out
    out = action.paper_trading({"action": "reset", "confirm": True})
    assert "Paper account reset" in out
    assert fake.resets == 1


# ── dashboard API ──────────────────────────────────────────────────────────
def _client(allow=True):
    app = FastAPI()
    register_paper_api(app, lambda req: allow)
    return TestClient(app)


def test_dashboard_paper_state_history_and_controls(fake):
    client = _client()
    state = client.get("/api/rio/paper/state")
    assert state.status_code == 200
    body = state.json()
    assert body["ok"] is True and body["paper_only"] is True
    assert body["account"]["equity"] == 10000.0

    hist = client.get("/api/rio/paper/history?limit=5")
    assert hist.status_code == 200 and hist.json()["rows"] == []

    assert client.post("/api/rio/paper/start").json()["accepting_entries"] is True
    assert fake.started == 1
    assert client.post("/api/rio/paper/stop").json()["accepting_entries"] is False
    assert fake.stopped == 1

    denied = client.post("/api/rio/paper/reset", json={})
    assert denied.status_code == 400
    reset = client.post("/api/rio/paper/reset", json={"confirm": True})
    assert reset.status_code == 200 and reset.json()["ok"] is True
    assert fake.resets == 1


def test_dashboard_paper_requires_auth(fake):
    assert _client(False).get("/api/rio/paper/state").status_code == 401


# ── UI / no order path ─────────────────────────────────────────────────────
def test_rio_shell_serves_paper_route_and_asset():
    from dashboard.rio_api import register as register_rio

    app = FastAPI()
    register_rio(app, lambda req: True)
    client = TestClient(app)
    page = client.get("/paper")
    assert page.status_code == 200
    assert "paper.js" in page.text
    asset = client.get("/rio/assets/paper.js")
    assert asset.status_code == 200
    assert "RIOPaper" in asset.text


def test_paper_ui_and_registration_are_wired():
    ui = (BASE / "dashboard" / "static" / "rio" / "paper.js").read_text()
    index = (BASE / "dashboard" / "static" / "rio" / "index.html").read_text()
    app_js = (BASE / "dashboard" / "static" / "rio" / "app.js").read_text()
    rio_api = (BASE / "dashboard" / "rio_api.py").read_text()
    server = (BASE / "dashboard" / "server.py").read_text()
    assert "/api/rio/paper/state" in ui
    assert "/rio/assets/paper.js" in index
    assert 'path: "/paper"' in app_js
    assert 'paper: "RIOPaper"' in app_js
    assert '"/paper"' in rio_api
    assert '"paper.js"' in rio_api
    assert "dashboard.paper_api" in server


def test_paper_surfaces_have_no_order_or_live_path():
    files = [
        BASE / "core" / "paper_trading.py",
        BASE / "core" / "paper_commands.py",
        BASE / "actions" / "paper_trading.py",
        BASE / "dashboard" / "paper_api.py",
    ]
    forbidden = ("place_order", "create_order", "execute_approved",
                 "orchestrator.agents.trading.broker",
                 "plugins.binance_trader", "/api/v3/order", "live_order")
    for path in files:
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{path.name}: {token}"
