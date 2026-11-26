"""
orchestrator/agents/backtest/providers/twelvedata.py — XAUUSD spot via
Twelve Data, only when a key is configured.

    GET https://api.twelvedata.com/time_series
        symbol=XAU/USD&interval=1h&start_date=…&end_date=…&timezone=UTC

Plan depth is NOT assumed. The adapter requests from the desired start, pages
backwards with the returned cursor, and reports the coverage it actually got;
the coverage gate then decides whether one year of usable data exists. A free
plan usually returns only a short intraday window — that will be reported as
INSUFFICIENT DATA, never patched with another venue.

Key: TWELVEDATA_API_KEY (environment) or "twelvedata_api_key" in
config/api_keys.json. Never logged.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from orchestrator.agents.backtest.config import api_key
from orchestrator.agents.backtest.providers.base import (
    FetchResult, Provider, drop_incomplete, failed, normalise_frame,
    tf_seconds, validate_frame,
)

BASE = "https://api.twelvedata.com/time_series"
INTERVALS = {"1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min",
             "1h": "1h", "2h": "2h", "4h": "4h", "1d": "1day"}
MAX_OUTPUT = 5000


class TwelveDataXauUsd(Provider):
    name = "twelvedata_xauusd"
    venue = "Twelve Data (XAU/USD spot)"
    supported_markets = ("gold_xauusd",)
    native_timeframes = tuple(INTERVALS)

    def __init__(self, cfg=None) -> None:
        self._cfg = cfg
        self._key = api_key("TWELVEDATA_API_KEY")

    def available(self) -> tuple[bool, str]:
        if not self._key:
            return False, ("TWELVEDATA_API_KEY is not configured — XAUUSD spot "
                           "cannot be fetched from Twelve Data")
        return True, "key present (plan depth is verified per request, not assumed)"

    def fetch(self, instrument, timeframe: str, start: datetime,
              end: datetime, *, max_candles: int | None = None) -> FetchResult:
        ok, detail = self.available()
        if not ok:
            return failed(self.name, timeframe, detail)
        if timeframe not in INTERVALS:
            return failed(self.name, timeframe,
                          f"Twelve Data has no mapped {timeframe} interval")

        symbol = instrument.provider_symbols.get(self.name, "XAU/USD")
        rows: list[dict] = []
        cursor_end = end
        requests_made = retries = 0
        limitations: list[str] = []
        pages = 0

        while cursor_end > start and pages < 60:
            payload, n, r, err = self._http_json(
                BASE,
                {"symbol": symbol, "interval": INTERVALS[timeframe],
                 "start_date": start.strftime("%Y-%m-%dT%H:%M:%S"),
                 "end_date": cursor_end.strftime("%Y-%m-%dT%H:%M:%S"),
                 "outputsize": MAX_OUTPUT, "timezone": "UTC", "order": "ASC",
                 "apikey": self._key},
                retries=2, pause=0.3)
            requests_made += n
            retries += r
            pages += 1
            if err:
                if rows:
                    limitations.append(f"paging stopped: {err}")
                    break
                return failed(self.name, timeframe, err)
            if not isinstance(payload, dict):
                return failed(self.name, timeframe, "malformed time_series response")
            if str(payload.get("status") or "").lower() == "error" or \
                    payload.get("code"):
                return failed(self.name, timeframe,
                              f"{payload.get('message') or payload.get('code')}")
            values = payload.get("values") or []
            if not values:
                break
            rows = values + rows
            oldest = pd.Timestamp(values[0]["datetime"], tz="UTC")
            if oldest <= pd.Timestamp(start, tz="UTC"):
                break
            cursor_end = (oldest - timedelta(seconds=tf_seconds(timeframe)))
            if len(values) < MAX_OUTPUT:
                # provider had no more rows before this point
                break

        if not rows:
            return failed(self.name, timeframe,
                          f"Twelve Data returned no XAU/USD candles")

        idx = pd.to_datetime([r.get("datetime") for r in rows], utc=True,
                             errors="coerce")
        frame = pd.DataFrame({
            "open": pd.to_numeric([r.get("open") for r in rows], errors="coerce"),
            "high": pd.to_numeric([r.get("high") for r in rows], errors="coerce"),
            "low": pd.to_numeric([r.get("low") for r in rows], errors="coerce"),
            "close": pd.to_numeric([r.get("close") for r in rows], errors="coerce"),
            "volume": pd.to_numeric([r.get("volume") or 0 for r in rows],
                                    errors="coerce").fillna(0.0),
        }, index=idx)
        frame = frame.dropna(how="all")
        frame, dupes = normalise_frame(frame)
        frame, dropped = drop_incomplete(frame, timeframe)
        problem = validate_frame(frame, timeframe)
        if problem:
            return failed(self.name, timeframe, problem)

        limitations.append(
            "XAUUSD is an OTC/benchmark spot quote from the selected provider, "
            "not an exchange-traded contract. Session gaps are reported.")
        limitations.append(
            "Plan depth is verified, not assumed — the returned window is the "
            "evidence used by the coverage gate.")
        return FetchResult(
            ok=True, provider=self.name, timeframe=timeframe, frame=frame,
            requests_made=requests_made, retries=retries,
            duplicates_dropped=dupes, incomplete_dropped=dropped,
            venue_symbol=symbol, limitations=limitations, note=detail)
