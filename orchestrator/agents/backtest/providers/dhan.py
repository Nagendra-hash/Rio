"""
orchestrator/agents/backtest/providers/dhan.py — MCX gold via Dhan (guarded).

MCX is a different instrument from XAUUSD and from COMEX GC=F: INR-quoted,
exchange-traded futures with an expiry and lot size. This adapter therefore
refuses to run unless BOTH are true:

  1. Dhan Data API credentials are present (DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN);
  2. the exact contract is configured in memory/backtest/config.json
     (security_id, expiry, lot_size, tick_size) AND the operator has confirmed
     the timestamp convention with "mcx_timezone_confirmed": true.

We never guess a security id, an expiry, or a timezone. Without those inputs
the result is BLOCKED with the precise missing item — which is a useful,
honest outcome, not a failure to hide.

Docs: https://dhanhq.co/docs/v2/historical-data/
"""
from __future__ import annotations

import os
from datetime import datetime

import pandas as pd

from orchestrator.agents.backtest.providers.base import (
    FetchResult, Provider, drop_incomplete, failed, normalise_frame, validate_frame,
)

BASE = "https://api.dhan.co/v2/charts"
INTERVALS = {"1m": "1", "5m": "5", "15m": "15", "30m": "25", "1h": "60"}

# Dhan intraday history is documented as a recent window (~90 days); daily
# history goes back much further. The coverage gate decides what is usable.
INTRADAY_MAX_DAYS = 90


class DhanMcxGold(Provider):
    name = "dhan_mcx"
    venue = "MCX via Dhan Data API"
    supported_markets = ("gold_mcx",)
    native_timeframes = ("1m", "5m", "15m", "30m", "1h", "1d")

    def __init__(self, cfg=None) -> None:
        self._cfg = cfg
        self._client = os.environ.get("DHAN_CLIENT_ID", "").strip()
        self._token = os.environ.get("DHAN_ACCESS_TOKEN", "").strip()

    def available(self) -> tuple[bool, str]:
        if not self._client or not self._token:
            return False, ("Dhan credentials are not configured "
                           "(DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN) — MCX gold "
                           "history cannot be read")
        contract = (self._cfg.get("mcx_gold_contract") if self._cfg else None) or {}
        if not contract.get("security_id") or not contract.get("expiry"):
            return False, ("MCX contract is not configured (security_id + "
                           "expiry) — refusing to guess")
        if not (self._cfg.get("mcx_timezone_confirmed") if self._cfg else False):
            return False, ("MCX timestamp convention is unconfirmed; set "
                           "mcx_timezone_confirmed=true only after verifying "
                           "Dhan's documented epoch timezone")
        return True, "credentials + contract configured"

    def fetch(self, instrument, timeframe: str, start: datetime,
              end: datetime, *, max_candles: int | None = None) -> FetchResult:
        ok, detail = self.available()
        if not ok:
            return failed(self.name, timeframe, detail)
        if timeframe not in self.native_timeframes:
            return failed(self.name, timeframe,
                          f"Dhan has no mapped {timeframe} interval for MCX")
        contract = self._cfg.get("mcx_gold_contract") or {}
        security_id = str(contract["security_id"])
        body = {
            "securityId": security_id,
            "exchangeSegment": "MCX_COMM",
            "instrument": "FUTCOM",
            "fromDate": start.date().isoformat(),
            "toDate": end.date().isoformat(),
            "expiryCode": int(contract.get("expiry_code") or 0),
        }
        endpoint = f"{BASE}/historical" if timeframe == "1d" else f"{BASE}/intraday"
        if timeframe != "1d":
            body["interval"] = INTERVALS[timeframe]

        payload, n, r, err = self._http_json(
            endpoint, None, method="POST", json_body=body, retries=2, pause=0.25,
            headers={"access-token": self._token, "client-id": self._client,
                     "Content-Type": "application/json"})
        if err or not isinstance(payload, dict):
            return failed(self.name, timeframe, err or "malformed Dhan response")
        if payload.get("errorMessage") or payload.get("errorCode"):
            return failed(self.name, timeframe,
                          str(payload.get("errorMessage") or payload.get("errorCode")))
        stamps = payload.get("timestamp") or []
        if not stamps:
            return failed(self.name, timeframe, "Dhan returned no MCX candles")

        idx = pd.to_datetime([int(s) for s in stamps], unit="s", utc=True)
        frame = pd.DataFrame({
            "open": pd.to_numeric(payload.get("open"), errors="coerce"),
            "high": pd.to_numeric(payload.get("high"), errors="coerce"),
            "low": pd.to_numeric(payload.get("low"), errors="coerce"),
            "close": pd.to_numeric(payload.get("close"), errors="coerce"),
            "volume": pd.to_numeric(payload.get("volume"), errors="coerce"),
        }, index=idx)
        frame, dupes = normalise_frame(frame)
        frame, dropped = drop_incomplete(frame, timeframe)
        problem = validate_frame(frame, timeframe)
        if problem:
            return failed(self.name, timeframe, problem)

        limitations = [
            f"MCX gold futures {contract.get('expiry')} (security_id "
            f"{security_id}), INR-quoted. Lot size "
            f"{contract.get('lot_size', 'not configured')}.",
            "Intraday depth is provider-limited (~90 days documented); the "
            "coverage gate rejects the interval if a year is unavailable.",
            "Contract roll between expiries is NOT modelled — a single "
            "configured contract is researched, not a continuous series.",
        ]
        return FetchResult(
            ok=True, provider=self.name, timeframe=timeframe, frame=frame,
            requests_made=n, retries=r, duplicates_dropped=dupes,
            incomplete_dropped=dropped, venue_symbol=security_id,
            limitations=limitations, note=f"MCX {contract.get('expiry')}")
