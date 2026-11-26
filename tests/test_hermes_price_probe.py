#!/usr/bin/env python3
"""Tests for the price-source probe in actions/hermes_health.py.

Background: Hermes answers price questions via the live-prices skill
(~/.hermes/skills/live-prices/SKILL.md), whose endpoints (Binance, Gold API,
CoinGecko) are key-free GETs. Two failure modes actually bit:

  1. Dead source — an endpoint unreachable or returning malformed JSON.
  2. Frozen snapshot — HTTP 200 but the payload is hours old (the Gold API
     `updatedAt` field exposes this; a plain reachability probe misses it).
  3. Silently-wrong data — one source serving garbage while HTTP stays green;
     caught by cross-checking BTC between two independent sources (±5%).

Guards here (all offline — _http_get_json is monkeypatched, no network):

  test_probe_passes_on_live_sources   — well-formed fresh payloads → OK
  test_probe_detects_unreachable      — dead source → FAIL with source named
  test_probe_detects_stale_snapshot   — 200 + old updatedAt → FAIL
  test_probe_detects_malformed        — 200 + garbage payload → FAIL
  test_probe_detects_source_disagreement — BTC sources >5% apart → FAIL
  test_probe_registered_in_jobs       — probe wired into hermes_health()

Run:  python3 tests/test_hermes_price_probe.py
Also pytest-compatible.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "actions"))

import hermes_health as hh  # noqa: E402


def _fresh_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _old_iso(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


class _FakeHttp:
    """Route-table stand-in for hermes_health._http_get_json."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, url, headers=None):
        self.calls.append(url)
        for needle, resp in self.routes.items():
            if needle in url:
                return resp
        return False, f"no route for {url}"


class PriceProbeTests(unittest.TestCase):
    def setUp(self):
        self._orig_http = hh._http_get_json

    def tearDown(self):
        hh._http_get_json = self._orig_http

    def _patch(self, routes):
        fake = _FakeHttp(routes)
        hh._http_get_json = fake
        return fake

    def test_probe_passes_on_live_sources(self):
        self._patch(
            {
                "binance": (True, {"symbol": "BTCUSDT", "price": "84000.0"}),
                "gold-api.com": (True, {"price": 4280.0, "updatedAt": _fresh_iso()}),
                "coingecko.com": (True, {"bitcoin": {"usd": 83950}}),
            }
        )
        ok, detail = hh._probe_price_sources()
        self.assertTrue(ok, detail)
        self.assertIn("agree", detail)

    def test_probe_detects_unreachable(self):
        self._patch(
            {
                "binance": (False, "connection timed out"),
                "gold-api.com": (True, {"price": 4280.0, "updatedAt": _fresh_iso()}),
                "coingecko.com": (True, {"bitcoin": {"usd": 84000}}),
            }
        )
        ok, detail = hh._probe_price_sources()
        self.assertFalse(ok)
        self.assertIn("Binance unreachable", detail)

    def test_probe_detects_stale_snapshot(self):
        # HTTP 200, price looks fine — but updatedAt is 70 minutes old.
        self._patch(
            {
                "binance": (True, {"price": "84000.0"}),
                "gold-api.com": (True, {"price": 4280.0, "updatedAt": _old_iso(70)}),
                "coingecko.com": (True, {"bitcoin": {"usd": 84000}}),
            }
        )
        ok, detail = hh._probe_price_sources()
        self.assertFalse(ok)
        self.assertIn("stale", detail)

    def test_probe_detects_malformed(self):
        self._patch(
            {
                "binance": (True, {"error": "limit"}),
                "gold-api.com": (
                    True,
                    {"price": "not-a-number", "updatedAt": _fresh_iso()},
                ),
                "coingecko.com": (True, {"unexpected": "shape"}),
            }
        )
        ok, detail = hh._probe_price_sources()
        self.assertFalse(ok)
        self.assertIn("malformed", detail)

    def test_probe_detects_source_disagreement(self):
        self._patch(
            {
                "binance": (True, {"price": "84000.0"}),
                "gold-api.com": (True, {"price": 4280.0, "updatedAt": _fresh_iso()}),
                "coingecko.com": (True, {"bitcoin": {"usd": 95000}}),
            }
        )
        ok, detail = hh._probe_price_sources()
        self.assertFalse(ok)
        self.assertIn("disagree", detail)

    def test_probe_registered_in_jobs(self):
        # The handler must wire the probe in, or JARVIS never surfaces it.
        import inspect

        src = inspect.getsource(hh.hermes_health)
        self.assertIn("Price sources", src)
        self.assertIn("_probe_price_sources", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
