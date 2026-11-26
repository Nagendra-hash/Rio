"""
tests/test_trading_live_delta.py — offline acceptance tests for the
explicitly-armed Delta live path.

NO network and NO real order is sent anywhere in this file. Every signed
request test monkeypatches requests.request; every broker test uses a fake
in-memory client. What this locks in:

  1. SIGNING   — Delta's documented HMAC contract and exact query/body
                 handling; no secret ever appears in an error message.
  2. BASE URL  — demo → Delta India testnet, prod → India production.
  3. SIZING    — contract-aware fixed-fractional sizing and the live cap.
  4. GATING    — live switch semantics: paper default, mode=live alone is
                 insufficient, Auto Trade refusal, HALT refusal, credentials.
  5. BROKER    — limit-IOC entry, bracket attach, not-filled handling,
                 emergency close when the bracket fails, journaling.
  6. SWITCH    — live switch file normalisation and the status payload.

Run:  .venv/bin/python -m pytest tests/test_trading_live_delta.py -q
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from orchestrator.agents.trading import broker as bk            # noqa: E402
from orchestrator.agents.trading import risk as rk              # noqa: E402
from orchestrator.agents.trading import delta_private as dp      # noqa: E402

# ═══════════════════════════════════════════════════════════════ 1. SIGNING ═══


def test_signature_uses_hmac_sha256_over_method_ts_path_query_body():
    secret = "unit-test-secret"
    message = "GET1700000000/v2/orders?product_id=1&state=open"
    expected = hmac.new(secret.encode(), message.encode(),
                        hashlib.sha256).hexdigest()
    assert dp.sign_request(secret, "GET", "1700000000", "/v2/orders",
                           "?product_id=1&state=open", "") == expected


def test_signature_post_includes_compact_body():
    secret = "s"
    body = json.dumps({"size": 3, "side": "buy"}, separators=(",", ":"))
    expected = hmac.new(secret.encode(),
                        f"POST1700000000/v2/orders{body}".encode(),
                        hashlib.sha256).hexdigest()
    assert dp.sign_request(secret, "POST", "1700000000", "/v2/orders", "",
                           body) == expected


def test_demo_and_prod_base_urls_are_the_two_delta_environments(monkeypatch):
    monkeypatch.delenv("DELTA_DEMO_BASE_URL", raising=False)
    monkeypatch.delenv("DELTA_LIVE_BASE_URL", raising=False)
    monkeypatch.delenv("DELTA_BASE_URL", raising=False)
    assert dp.base_url_for("demo") == "https://cdn-ind.testnet.deltaex.org"
    assert dp.base_url_for("prod") == "https://api.india.delta.exchange"
    with pytest.raises(dp.DeltaPrivateError):
        dp.base_url_for("staging")


def test_signed_request_sends_exact_headers_and_body(monkeypatch):
    seen = {}

    def fake_request(method, url, data=None, headers=None, timeout=None):
        seen.update({"method": method, "url": url, "data": data,
                     "headers": headers or {}, "timeout": timeout})
        return SimpleNamespace(status_code=200,
                               json=lambda: {"success": True,
                                             "result": {"ok": True}})

    monkeypatch.setattr(dp.requests, "request", fake_request)
    client = dp.DeltaPrivateClient("demo", key="key-unit", secret="secret-unit")
    out = client._request("GET", "/v2/orders",
                          params={"product_id": 1, "state": "open"})
    assert out["result"] == {"ok": True}
    ts = seen["headers"]["timestamp"]
    assert seen["url"] == "https://cdn-ind.testnet.deltaex.org/v2/orders?product_id=1&state=open"
    assert seen["headers"]["api-key"] == "key-unit"
    expected = hmac.new(b"secret-unit",
                        f"GET{ts}/v2/orders?product_id=1&state=open".encode(),
                        hashlib.sha256).hexdigest()
    assert seen["headers"]["signature"] == expected


def test_http_error_never_contains_the_secret(monkeypatch):
    def fake_request(method, url, data=None, headers=None, timeout=None):
        return SimpleNamespace(status_code=401,
                               json=lambda: {"success": False,
                                             "error": {"code": "invalid_api_key"}})

    monkeypatch.setattr(dp.requests, "request", fake_request)
    client = dp.DeltaPrivateClient("demo", key="KEY-PLAIN", secret="SECRET-PLAIN")
    with pytest.raises(dp.DeltaPrivateError) as ei:
        client.wallet_balances()
    assert "invalid_api_key" in str(ei.value)
    assert "SECRET-PLAIN" not in str(ei.value)
    assert "KEY-PLAIN" not in str(ei.value)


def test_unconfigured_client_refuses_before_signing(monkeypatch):
    monkeypatch.delenv("DELTA_API_KEY", raising=False)
    monkeypatch.delenv("DELTA_API_SECRET", raising=False)
    monkeypatch.setattr(dp, "resolve_credentials", lambda: ("", ""))
    client = dp.DeltaPrivateClient("demo")
    with pytest.raises(dp.DeltaPrivateError) as ei:
        client.wallet_balances()
    assert "not configured" in str(ei.value)


# ── clock-skew handling (expired_signature → learn offset → retry once) ────

def _fake_clock(monkeypatch, start=1_700_000_000.0):
    state = {"now": start}
    monkeypatch.setattr(dp.time, "time", lambda: state["now"])
    return state


def _expired(seconds_behind: int, local_ts: int) -> dict:
    return {
        "success": False,
        "error": {
            "code": "expired_signature",
            "context": {"request_time": local_ts,
                        "server_time": local_ts + seconds_behind},
        },
    }


def test_expired_signature_retries_once_with_learned_offset(monkeypatch):
    _fake_clock(monkeypatch)
    dp._CLOCK_OFFSETS.clear()
    calls = []

    def fake_request(method, url, data=None, headers=None, timeout=None):
        ts = int(headers["timestamp"])
        calls.append(ts)
        if len(calls) == 1:
            return SimpleNamespace(
                status_code=401,
                json=lambda: _expired(17, ts))
        return SimpleNamespace(status_code=200,
                               json=lambda: {"success": True,
                                             "result": {"ok": True}})

    monkeypatch.setattr(dp.requests, "request", fake_request)
    client = dp.DeltaPrivateClient("demo", key="k", secret="s")
    out = client.wallet_balances()
    assert out["result"] == {"ok": True}
    assert len(calls) == 2
    assert calls[1] - calls[0] == 17          # corrected by server skew
    assert dp.clock_offset_for(client.base_url) == 17
    assert client.clock_offset_s == 17


def test_learned_offset_is_reused_by_a_new_client(monkeypatch):
    _fake_clock(monkeypatch)
    dp._CLOCK_OFFSETS.clear()
    tss = []

    def fake_request(method, url, data=None, headers=None, timeout=None):
        ts = int(headers["timestamp"])
        tss.append(ts)
        if len(tss) == 1:
            return SimpleNamespace(status_code=401,
                                   json=lambda: _expired(9, ts))
        return SimpleNamespace(status_code=200,
                               json=lambda: {"success": True, "result": {}})

    monkeypatch.setattr(dp.requests, "request", fake_request)
    first = dp.DeltaPrivateClient("demo", key="k", secret="s")
    assert first.wallet_balances()["success"] is True

    second = dp.DeltaPrivateClient("demo", key="k", secret="s")
    assert second.wallet_balances()["success"] is True
    # 3 calls total: expired → corrected retry → second client succeeds first
    # try because the learned offset is cached per base URL in-process.
    assert len(tss) == 3
    assert tss[1] - tss[0] == 9               # first client's corrected retry
    assert second.clock_offset_s == 9


def test_persistent_expired_signature_raises_after_one_retry(monkeypatch):
    _fake_clock(monkeypatch)
    dp._CLOCK_OFFSETS.clear()
    calls = []

    def fake_request(method, url, data=None, headers=None, timeout=None):
        ts = int(headers["timestamp"])
        calls.append(ts)
        return SimpleNamespace(status_code=401,
                               json=lambda: _expired(17, ts))

    monkeypatch.setattr(dp.requests, "request", fake_request)
    client = dp.DeltaPrivateClient("demo", key="k", secret="s")
    with pytest.raises(dp.DeltaPrivateError) as ei:
        client.wallet_balances()
    assert len(calls) == 2                    # exactly one retry, no loop
    assert "clock" in str(ei.value).lower()
    assert "ntp" in str(ei.value).lower()
    assert dp.clock_offset_for(client.base_url) == 17


def test_expired_signature_without_context_still_raises_cleanly(monkeypatch):
    _fake_clock(monkeypatch)
    dp._CLOCK_OFFSETS.clear()
    calls = []

    def fake_request(method, url, data=None, headers=None, timeout=None):
        calls.append(int(headers["timestamp"]))
        return SimpleNamespace(status_code=401,
                               json=lambda: {"success": False,
                                             "error": {"code":
                                                       "expired_signature"}})

    monkeypatch.setattr(dp.requests, "request", fake_request)
    client = dp.DeltaPrivateClient("demo", key="k", secret="s")
    with pytest.raises(dp.DeltaPrivateError) as ei:
        client.wallet_balances()
    assert len(calls) == 2
    assert "expired_signature" in str(ei.value)
    assert dp.clock_offset_for(client.base_url) == 0


def test_bracket_prices_are_tick_rounded_through_the_trigger():
    captured = {}

    class C(dp.DeltaPrivateClient):
        def _request(self, method, path, *, params=None, body=None):
            captured.update(body or {})
            return {"success": True}

    client = C("demo", key="k", secret="s")
    out = client.place_bracket("BTCUSD", side="buy", stop_loss=99.987,
                               take_profit=101.003, tick_size=0.5)
    assert out["success"] is True
    assert captured["stop_loss_order"]["stop_price"] == "100"
    assert captured["stop_loss_order"]["limit_price"] == "99.5"
    assert captured["take_profit_order"]["stop_price"] == "101"
    assert captured["take_profit_order"]["limit_price"] == "100.5"


# ═══════════════════════════════════════════════════════════════ 2. SIZING ═══

def test_contract_sizing_rounds_down_and_applies_cap():
    # 1% of 10,000 = $100 risk; $2 stop × 0.01 contract value = $0.02/contract
    s = rk.position_size_contracts(10_000.0, 100.0, 98.0, contract_value=0.01,
                                   risk_pct=1.0, max_position_usd=0)
    assert s["ok"] and s["contracts"] == 5_000
    assert s["units"] == s["contracts"]
    assert s["underlying_units"] == pytest.approx(50.0)
    # $1 per contract notional: a $50 cap cuts it to 50 contracts.
    s2 = rk.position_size_contracts(10_000.0, 100.0, 98.0, contract_value=0.01,
                                    risk_pct=1.0, max_position_usd=50.0)
    assert s2["contracts"] == 50 and s2["notional"] == pytest.approx(50.0)
    assert s2["risk_amount"] == pytest.approx(1.0)
    assert s2["capped_by_max_position"] is True


def test_contract_sizing_refuses_one_contract_when_cap_is_too_small():
    s = rk.position_size_contracts(10_000.0, 100.0, 98.0, contract_value=0.01,
                                   risk_pct=1.0, max_position_usd=0.5)
    assert not s["ok"] and "cannot buy 1" in s["error"]


def test_full_risk_check_applies_the_live_cap_when_armed(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    rk.save_live({"enabled": True, "environment": "demo",
                  "max_order_usd": 50.0})
    proposal = {"direction": "LONG", "entry": 100.0, "stop_loss": 98.0,
                "targets": [104.0], "score": 3, "strategy": "t"}
    res = rk.full_risk_check(proposal, "BTCUSD", 10_000.0, risk_pct=1.0,
                             contract_value=0.01)
    assert res["ok"] and res["sizing"]["contracts"] == 50
    assert res["mode"] == "live" and res["max_order_usd"] == 50.0
    # Disarm: the safety cap (default 1000) applies again, not the live cap.
    rk.save_live({"enabled": False})
    res2 = rk.full_risk_check(proposal, "BTCUSD", 10_000.0, risk_pct=1.0,
                              contract_value=0.01)
    assert res2["ok"] and res2["sizing"]["contracts"] == 1000


# ═══════════════════════════════════════════════════════════════ 3. GATING ═══

def test_live_switch_file_defaults_to_disabled_and_normalises(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    cfg = rk.load_live()
    assert cfg["enabled"] is False and cfg["allow_auto"] is False
    assert cfg["environment"] == "demo" and cfg["max_order_usd"] > 0
    cfg = rk.load_live_from({"enabled": "yes", "environment": "PRODUCTION",
                             "max_order_usd": "x"})
    assert cfg["enabled"] is True and cfg["environment"] == "demo"
    assert cfg["max_order_usd"] == rk.DEFAULT_LIVE["max_order_usd"]


def test_check_safety_warns_for_mode_live_without_the_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    rk.trading_dir().mkdir(parents=True, exist_ok=True)
    (rk.trading_dir() / rk.SAFETY_FILE).write_text(
        json.dumps({**rk.DEFAULT_SAFETY, "mode": "live"}))
    res = rk.check_safety("TEST", now=1_000.0)
    assert res["ok"]
    assert any("mode=live" in w and "disabled" in w for w in res["warnings"])


def test_execute_approved_stays_paper_when_switch_is_off(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    rk.save_live({"enabled": False})
    res = bk.execute_approved({
        "symbol": "TEST",
        "proposal": {"direction": "LONG", "entry": 100.0, "stop_loss": 98.0,
                     "targets": [104.0], "strategy": "t"},
        "sizing": {"units": 1}, "approved_by": "user-paper"})
    assert res["ok"] and res["mode"] == "paper"
    assert res["position"]["paper"] is True
    # mode=live still refuses while the switch is off.
    rk.trading_dir().mkdir(parents=True, exist_ok=True)
    (rk.trading_dir() / rk.SAFETY_FILE).write_text(
        json.dumps({**rk.DEFAULT_SAFETY, "mode": "live"}))
    res2 = bk.execute_approved({
        "symbol": "TEST",
        "proposal": {"direction": "LONG", "entry": 100.0, "stop_loss": 98.0,
                     "targets": [104.0], "strategy": "t"},
        "sizing": {"units": 1}, "approved_by": "user-live"})
    assert res2["ok"] is False and res2["mode"] == "paper"
    assert "live execution switch is disabled" in res2["error"]


def test_execute_approved_refuses_auto_trade_live_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    rk.save_live({"enabled": True})
    res = bk.execute_approved({"symbol": "X",
                               "proposal": {"entry": 1, "stop_loss": 1,
                                            "targets": [1]},
                               "sizing": {"units": 1},
                               "approved_by": "auto-trade"})
    assert res["ok"] is False and res["mode"] == "live"
    assert "Auto Trade cannot place live orders" in res["error"]


def test_execute_approved_live_constructs_the_live_broker(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    rk.save_live({"enabled": True})
    monkeypatch.setattr(bk, "live_halted", lambda: False)

    class FakeLiveBroker:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def open_position(self, **kwargs):
            return {"ok": True, "mode": "live", "environment": "demo",
                    "filled_size": 1, "entry_price": 100.0,
                    "bracket_ok": True}

    monkeypatch.setattr(bk, "DeltaLiveBroker", FakeLiveBroker)
    res = bk.execute_approved({
        "symbol": "BTCUSD",
        "proposal": {"direction": "LONG", "entry": 100.0, "stop_loss": 98.0,
                     "targets": [104.0], "strategy": "t"},
        "sizing": {"units": 1},
        "approved_by": "user-live", "contract_value": 0.01,
    })
    assert res["ok"] and res["mode"] == "live" and res["filled_size"] == 1


def test_execute_approved_refuses_a_paper_broker_when_live_is_armed(
        tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    rk.save_live({"enabled": True})
    monkeypatch.setattr(bk, "live_halted", lambda: False)
    res = bk.execute_approved({"symbol": "X",
                               "proposal": {"entry": 1, "stop_loss": 1,
                                            "targets": [1]},
                               "sizing": {"units": 1},
                               "approved_by": "user-live"},
                              broker=bk.PaperBroker())
    assert not res["ok"] and "DeltaLiveBroker" in res["error"]


# ═══════════════════════════════════════════════════════════════ 4. BROKER ═══

class FakeDeltaClient:
    def __init__(self, *, state="closed", unfilled=0, bracket_ok=True,
                 emergency_ok=True):
        self.calls = []
        self.state = state
        self.unfilled = unfilled
        self.bracket_ok = bracket_ok
        self.emergency_ok = emergency_ok

    def credentials_configured(self):
        return True

    def place_order(self, order):
        self.calls.append(("place", order))
        return {"success": True, "result": {
            "id": 12345, "size": order["size"],
            "unfilled_size": str(self.unfilled), "state": self.state,
            "average_fill_price": "100.25"}}

    def place_bracket(self, symbol, **kwargs):
        self.calls.append(("bracket", symbol, kwargs))
        if not self.bracket_ok:
            raise dp.DeltaPrivateError("bracket rejected")
        return {"success": True, "result": {"bracket_ids": [77, 78]}}

    def cancel_order(self, order_id):
        self.calls.append(("cancel", order_id))
        return {"success": True}

    def emergency_close(self, symbol, *, side, size):
        self.calls.append(("close", symbol, side, size))
        if not self.emergency_ok:
            raise dp.DeltaPrivateError("close rejected")
        return {"success": True, "result": {"id": 999}}


def _armed_env(tmp_path, monkeypatch, **patch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    rk.save_live({"enabled": True, "environment": "demo", **patch})
    monkeypatch.setattr(bk, "live_halted", lambda: False)


def _open_kwargs(**over):
    base = dict(symbol="BTCUSD", direction="LONG", entry=100.0,
                stop_loss=98.0, targets=[104.0, 106.0], units=3,
                strategy="trend_follow", signal_score=3,
                approved_by="user-live", signal_id="cp-123",
                venue="delta", product_id=27, contract_type="perpetual_futures",
                quote_currency="USD", contract_value=0.01,
                market_class="delta_perp", product_symbol="BTCUSD",
                tick_size=0.5)
    base.update(over)
    return base


def test_live_broker_fills_and_attaches_bracket(tmp_path, monkeypatch):
    _armed_env(tmp_path, monkeypatch)
    client = FakeDeltaClient()
    broker = bk.DeltaLiveBroker(environment="demo", client=client)
    res = broker.open_position(**_open_kwargs())
    assert res["ok"] and res["status"] == "filled"
    assert res["order_id"] == 12345 and res["filled_size"] == 3
    assert res["entry_price"] == pytest.approx(100.25)
    assert res["bracket_ok"] and res["bracket"]["result"]["bracket_ids"]
    place = next(c for c in client.calls if c[0] == "place")[1]
    assert place["order_type"] == "limit_order"
    assert place["time_in_force"] == "ioc"
    assert place["side"] == "buy" and place["size"] == 3
    assert place["limit_price"] == "100"
    assert place["client_order_id"].startswith("jarvis-")
    bracket = next(c for c in client.calls if c[0] == "bracket")
    assert bracket[1] == "BTCUSD" and bracket[2]["side"] == "buy"
    assert bracket[2]["stop_loss"] == pytest.approx(98.0)
    assert bracket[2]["take_profit"] == pytest.approx(104.0)
    journal = (rk.trading_dir() / "live_journal.jsonl").read_text()
    assert "LIVE_SUBMIT" in journal and "LIVE_FILLED" in journal
    state = rk.load_state()
    assert state["daily_trades"] == 1


def test_live_broker_emergency_closes_when_bracket_fails(tmp_path, monkeypatch):
    _armed_env(tmp_path, monkeypatch)
    client = FakeDeltaClient(bracket_ok=False)
    broker = bk.DeltaLiveBroker(environment="demo", client=client)
    res = broker.open_position(**_open_kwargs())
    assert res["ok"] is False and res["status"] == "bracket_failed"
    assert res["emergency_close"] == {"ok": True, "response": {"success": True,
                                                               "result": {"id": 999}}}
    assert any(c[0] == "close" for c in client.calls)
    journal = (rk.trading_dir() / "live_journal.jsonl").read_text()
    assert "LIVE_FILLED" in journal


def test_live_broker_reports_unprotected_close_failure(tmp_path, monkeypatch):
    _armed_env(tmp_path, monkeypatch)
    client = FakeDeltaClient(bracket_ok=False, emergency_ok=False)
    broker = bk.DeltaLiveBroker(environment="demo", client=client)
    res = broker.open_position(**_open_kwargs())
    assert res["ok"] is False and res["status"] == "bracket_failed"
    assert res["emergency_close"]["ok"] is False
    assert "UNPROTECTED" in res["error"].upper()


def test_live_broker_reports_not_filled_and_does_not_bracket(tmp_path, monkeypatch):
    _armed_env(tmp_path, monkeypatch)
    client = FakeDeltaClient(state="cancelled", unfilled=3)
    broker = bk.DeltaLiveBroker(environment="demo", client=client)
    res = broker.open_position(**_open_kwargs())
    assert res["ok"] is False and res["status"] == "not_filled"
    assert res["filled_size"] == 0
    assert not any(c[0] == "bracket" for c in client.calls)
    assert "LIVE_NOT_FILLED" in (rk.trading_dir() / "live_journal.jsonl").read_text()


def test_live_broker_protects_a_partial_cancelled_ioc_fill(tmp_path, monkeypatch):
    _armed_env(tmp_path, monkeypatch)
    client = FakeDeltaClient(state="open", unfilled=1)
    broker = bk.DeltaLiveBroker(environment="demo", client=client)
    res = broker.open_position(**_open_kwargs())
    assert res["ok"] is True and res["status"] == "filled"
    assert res["filled_size"] == 2 and res["cancelled"] is True
    assert any(c[0] == "cancel" for c in client.calls)
    assert any(c[0] == "bracket" for c in client.calls)


def test_live_broker_enforces_the_notional_cap(tmp_path, monkeypatch):
    _armed_env(tmp_path, monkeypatch, max_order_usd=1.0)
    client = FakeDeltaClient()
    broker = bk.DeltaLiveBroker(environment="demo", client=client)
    res = broker.open_position(**_open_kwargs())
    assert not res["ok"] and "notional" in res["error"]
    assert not client.calls


def test_live_broker_refuses_a_zero_live_cap(tmp_path, monkeypatch):
    _armed_env(tmp_path, monkeypatch, max_order_usd=0)
    client = FakeDeltaClient()
    broker = bk.DeltaLiveBroker(environment="demo", client=client)
    res = broker.open_position(**_open_kwargs())
    assert not res["ok"] and "max_order_usd" in res["error"]
    assert not client.calls


def test_live_broker_requires_credentials_and_delta_contract_value(
        tmp_path, monkeypatch):
    _armed_env(tmp_path, monkeypatch)
    client = FakeDeltaClient()
    client.credentials_configured = lambda: False
    broker = bk.DeltaLiveBroker(environment="demo", client=client)
    res = broker.open_position(**_open_kwargs())
    assert not res["ok"] and "not configured" in res["error"].lower()
    res2 = bk.DeltaLiveBroker(environment="demo",
                              client=FakeDeltaClient()).open_position(
                                  **_open_kwargs(contract_value=None))
    assert not res2["ok"] and "contract_value" in res2["error"]


def test_live_broker_short_side_uses_sell(tmp_path, monkeypatch):
    _armed_env(tmp_path, monkeypatch)
    client = FakeDeltaClient()
    broker = bk.DeltaLiveBroker(environment="demo", client=client)
    res = broker.open_position(**_open_kwargs(direction="SHORT"))
    assert res["ok"]
    place = next(c for c in client.calls if c[0] == "place")[1]
    assert place["side"] == "sell"
    bracket = next(c for c in client.calls if c[0] == "bracket")
    assert bracket[2]["side"] == "sell"


def test_entry_limit_rounding_is_marketable_per_side():
    assert bk._as_entry_price(104.7915, 0.5, "buy") == "105"
    assert bk._as_entry_price(104.7915, 0.5, "sell") == "104.5"
    assert bk._as_entry_price(100.0, None, "buy") == "100"


# ═══════════════════════════════════════════════════════════════ 5. API ══════

def _api(monkeypatch, tmp_path, auth=True):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import dashboard.trading_api as ta
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    app = FastAPI()
    ta.register(app, (lambda request: auth))
    return TestClient(app), ta


def test_live_api_routes_require_auth_and_respect_phrase(tmp_path, monkeypatch):
    client, ta = _api(monkeypatch, tmp_path, auth=True)
    assert client.get("/api/rio/trading/live/status").json()["ok"] is True

    r = client.post("/api/rio/trading/live/enable",
                    json={"confirm": True, "environment": "demo",
                          "phrase": "wrong phrase"})
    assert r.status_code == 400 and "ENABLE LIVE EXECUTION" in r.json()["error"]

    r = client.post("/api/rio/trading/live/enable",
                    json={"environment": "demo",
                          "phrase": "ENABLE LIVE EXECUTION"})
    assert r.status_code == 400 and "confirmation required" in r.json()["error"]

    denied = _api(monkeypatch, tmp_path, auth=False)[0]
    assert denied.get("/api/rio/trading/live/status").status_code == 401
    assert denied.post("/api/rio/trading/live/enable",
                       json={"confirm": True}).status_code == 401
    assert denied.post("/api/rio/trading/approve",
                       json={"confirm": True, "task_id": "x"}).status_code == 401


def test_live_enable_requires_probe_and_disables_cleanly(tmp_path, monkeypatch):
    client, ta = _api(monkeypatch, tmp_path, auth=True)
    import orchestrator.agents.delta_exchange as dxmod
    monkeypatch.setattr(dxmod, "resolve_credentials",
                        lambda: ("key-unit", "secret-unit"))
    calls = []
    monkeypatch.setattr(ta, "_live_probe", lambda env=None: calls.append(env) or {
        "ok": True, "environment": env or "demo",
        "net_equity": "1234.50", "assets": [], "positions": [],
        "position_count": 0, "note": "read-only"})
    r = client.post("/api/rio/trading/live/enable",
                    json={"confirm": True, "environment": "demo",
                          "phrase": "ENABLE LIVE EXECUTION"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["live"]["enabled"] is True
    assert body["live"]["armed"] is True
    assert body["account"]["note"] == "read-only"
    assert calls == ["demo"]
    assert (rk.trading_dir() / "live_execution.json").exists()

    d = client.post("/api/rio/trading/live/disable", json={})
    assert d.status_code == 200 and d.json()["live"]["enabled"] is False


def test_live_enable_refuses_when_credential_probe_fails(tmp_path, monkeypatch):
    client, ta = _api(monkeypatch, tmp_path, auth=True)
    monkeypatch.setattr(ta, "_live_probe", lambda env=None: {
        "ok": False, "environment": env or "demo",
        "error": "ip_not_whitelisted_for_api_key"})
    r = client.post("/api/rio/trading/live/enable",
                    json={"confirm": True, "environment": "demo",
                          "phrase": "ENABLE LIVE EXECUTION"})
    assert r.status_code == 400
    body = r.json()
    assert body["ok"] is False and "ip_not_whitelisted" in body["error"]
    assert rk.load_live()["enabled"] is False


def test_ip_whitelist_probe_is_structured_and_persisted(tmp_path, monkeypatch):
    """Delta's IP control must surface the exact IP + next action, not a
    generic 'disconnected' state. The probe stays read-only."""
    client, ta = _api(monkeypatch, tmp_path, auth=True)

    monkeypatch.setattr(dp.DeltaPrivateClient, "credentials_configured",
                        lambda self: True)

    def fail_probe(self):
        raise dp.DeltaPrivateError(
            "Delta demo: ip_not_whitelisted_for_api_key "
            "{'client_ip': '203.0.113.9'}")

    monkeypatch.setattr(dp.DeltaPrivateClient, "probe", fail_probe)

    out = ta._live_probe("demo")
    assert out["ok"] is False
    assert out["code"] == "ip_not_whitelisted"
    assert out["client_ip"] == "203.0.113.9"
    assert out["action_url"] == "https://demo.delta.exchange"
    assert "203.0.113.9" in out["hint"]
    assert "whitelist" in out["hint"].lower()

    live = ta._live_state()
    assert live["last_probe_ok"] is False
    assert live["last_probe_code"] == "ip_not_whitelisted"
    assert live["last_probe_client_ip"] == "203.0.113.9"
    assert "203.0.113.9" in live["last_probe_hint"]

    r = client.post("/api/rio/trading/live/probe",
                    json={"environment": "demo"})
    assert r.status_code == 400
    assert r.json()["client_ip"] == "203.0.113.9"


def test_live_enable_error_carries_ip_and_action(tmp_path, monkeypatch):
    client, ta = _api(monkeypatch, tmp_path, auth=True)
    monkeypatch.setattr(ta, "_live_probe", lambda env=None: {
        "ok": False, "environment": env or "demo",
        "error": "Delta demo: ip_not_whitelisted_for_api_key "
                 "{'client_ip': '203.0.113.9'}",
        "code": "ip_not_whitelisted", "client_ip": "203.0.113.9",
        "action_url": "https://demo.delta.exchange",
        "hint": "Add 203.0.113.9 to the API key's IP whitelist in Delta demo, "
                "then run Check account again."})
    r = client.post("/api/rio/trading/live/enable",
                    json={"confirm": True, "environment": "demo",
                          "phrase": "ENABLE LIVE EXECUTION"})
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "ip_not_whitelisted"
    assert body["client_ip"] == "203.0.113.9"
    assert "203.0.113.9" in body["error"]
    assert "whitelist" in body["error"].lower()
    assert rk.load_live()["enabled"] is False


def test_manual_approve_requires_existing_checkpoint(tmp_path, monkeypatch):
    client, ta = _api(monkeypatch, tmp_path, auth=True)
    r = client.post("/api/rio/trading/approve",
                    json={"confirm": True, "task_id": "no-such-checkpoint"})
    assert r.status_code == 404 and "no checkpoint" in r.json()["error"]
    r = client.post("/api/rio/trading/approve",
                    json={"task_id": "no-such-checkpoint"})
    assert r.status_code == 400 and "confirmation required" in r.json()["error"]


def test_manual_approve_paper_and_live_phrase(tmp_path, monkeypatch):
    client, ta = _api(monkeypatch, tmp_path, auth=True)
    from orchestrator.agents.trading import trading_agent as tam
    import orchestrator.agents.delta_exchange as dxmod
    monkeypatch.setattr(dxmod, "resolve_credentials",
                        lambda: ("key-unit", "secret-unit"))

    class FakeCP:
        def load(self, task_id):
            return {"status": "awaiting_approval"}

    class FakeStatus:
        value = "completed"

    class FakeAgent:
        def __init__(self):
            self._cp = FakeCP()

        def handle(self, request):
            assert request.metadata["resume"] == "cp-1"
            return SimpleNamespace(status=FakeStatus(), text="ok",
                                   data={"execution": {"ok": True,
                                                       "mode": "paper"},
                                         "mode": "paper"})

    monkeypatch.setattr(tam, "TradingAgent", FakeAgent)

    r = client.post("/api/rio/trading/approve",
                    json={"confirm": True, "task_id": "cp-1"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["approved_by"] == "user-paper"

    rk.save_live({"enabled": True, "environment": "demo"})
    r = client.post("/api/rio/trading/approve",
                    json={"confirm": True, "task_id": "cp-1",
                          "phrase": "wrong"})
    assert r.status_code == 400 and "PLACE LIVE ORDER" in r.json()["error"]
    r = client.post("/api/rio/trading/approve",
                    json={"confirm": True, "task_id": "cp-1",
                          "phrase": "PLACE LIVE ORDER"})
    assert r.status_code == 200 and r.json()["approved_by"] == "user-live"
