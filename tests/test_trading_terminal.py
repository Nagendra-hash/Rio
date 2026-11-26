"""tests/test_trading_terminal.py — offline checks for the redesigned
trading terminal data path.

Locks in:
  * /api/rio/trading/chart accepts a timeframe (interval) parameter
  * the interval is validated per venue, never passed through blindly
  * the payload still carries real OHLCV bars only
No network is used: MarketData is replaced with a fake adapter.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import dashboard.trading_api as ta                               # noqa: E402


class _FakeResult:
    def __init__(self, bars):
        self.ok = True
        self.provenance = SimpleNamespace(value="delayed")
        self.data = {"symbol": "TEST", "interval": "1d", "bars": bars}
        self.error = ""
        self.as_of = "2026-01-01T00:00:00"

    def to_dict(self):
        return {"ok": self.ok, "provenance": self.provenance.value,
                "data": self.data, "error": self.error, "as_of": self.as_of}


class _FakeMD:
    def __init__(self):
        self.calls = []

    def history(self, symbol, period="6mo", interval="1d", venue=None):
        self.calls.append({"symbol": symbol, "period": period,
                           "interval": interval, "venue": venue})
        bars = [{"ts": "2026-01-01T00:00:00", "open": 1.0, "high": 2.0,
                 "low": 0.5, "close": 1.5, "volume": 10}]
        return _FakeResult(bars)


@pytest.fixture()
def fake_md(monkeypatch):
    md = _FakeMD()
    monkeypatch.setattr(ta, "_md", lambda: md)
    return md


def test_chart_interval_defaults_to_daily(fake_md):
    out = ta._collect_chart("TEST", "1mo")
    assert out["ok"] is True
    assert fake_md.calls[-1]["interval"] == "1d"
    assert out["bars"] and out["bars"][0]["close"] == 1.5


def test_chart_interval_is_forwarded_when_supported(fake_md):
    out = ta._collect_chart("TEST", "5d", interval="15m")
    assert fake_md.calls[-1]["interval"] == "15m"
    assert out["ok"] is True


def test_chart_interval_falls_back_when_unsupported(fake_md):
    ta._collect_chart("TEST", "1mo", interval="7m")
    assert fake_md.calls[-1]["interval"] == "1d"


def test_chart_delta_interval_whitelist(fake_md):
    ta._collect_chart("BTCUSD", "5d", venue="delta", interval="4h")
    assert fake_md.calls[-1]["interval"] == "4h"
    ta._collect_chart("BTCUSD", "5d", venue="delta", interval="90m")
    assert fake_md.calls[-1]["interval"] == "1d"
