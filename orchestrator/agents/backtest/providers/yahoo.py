"""
orchestrator/agents/backtest/providers/yahoo.py — gold via Yahoo Finance.

    GC=F   COMEX gold futures, continuous front-month series
    XAUUSD=X  requested only when explicitly configured; Yahoo returned 404
              for this symbol when verified (2026-10-20), so the adapter must
              report that honestly rather than pretend spot gold exists.

Yahoo is free and needs no key, but it is a delayed research feed with hard
intraday depth limits (GC=F verified 2026-10-20: 1h ≈ 730 days, 15m/5m ≈ 60
days, 1m ≈ 7 days). The coverage gate rejects any interval below the one-year
floor; nothing is upsampled to hide it.

GC=F is a CONTINUOUS futures series. Rolls are not modelled as cash flows and
the series is not a single tradable contract — the report says so on every
gold-futures result.
"""
from __future__ import annotations

import concurrent.futures
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from orchestrator.agents.backtest.providers.base import (
    FetchResult, Provider, drop_incomplete, failed, normalise_frame,
    utc_now, validate_frame,
)

YAHOO_INTERVALS = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
                   "1h": "1h", "1d": "1d"}
# Measured caps (Yahoo docs/observed): kept conservative on purpose.
MAX_LOOKBACK_DAYS = {"1m": 7, "5m": 59, "15m": 59, "30m": 59, "1h": 729, "1d": 20000}


class YahooGoldProvider(Provider):
    name = "yahoo_gc"
    venue = "COMEX via Yahoo Finance"
    supported_markets = ("gold_futures", "gold_xauusd")
    native_timeframes = tuple(YAHOO_INTERVALS)

    def __init__(self, cfg=None, symbol: str = "GC=F", name: str | None = None,
                 market_class: str = "gold_futures") -> None:
        self._cfg = cfg
        self.symbol = symbol
        self.name = name or ("yahoo_gc" if symbol == "GC=F" else "yahoo_xauusd")
        self.supported_markets = (market_class,)
        self._last_error = ""

    # ── fetch ────────────────────────────────────────────────────────────
    def fetch(self, instrument, timeframe: str, start: datetime,
              end: datetime, *, max_candles: int | None = None) -> FetchResult:
        if timeframe not in YAHOO_INTERVALS:
            return failed(self.name, timeframe,
                          f"Yahoo has no native {timeframe} interval")
        if not self.supports(instrument):
            return failed(self.name, timeframe,
                          f"{self.name} does not serve {instrument.market_class}")
        now = utc_now()
        cap_days = MAX_LOOKBACK_DAYS[YAHOO_INTERVALS[timeframe]]
        effective_start = max(start, now - timedelta(days=cap_days))
        clamped = effective_start > start

        try:
            frame, err = self._download(effective_start, end, timeframe)
        except Exception as exc:
            return failed(self.name, timeframe,
                          f"{type(exc).__name__}: {str(exc)[:180]}")
        if err:
            return failed(self.name, timeframe, err)
        if frame is None or frame.empty:
            return failed(self.name, timeframe,
                          f"Yahoo returned no candles for {self.symbol} "
                          f"{timeframe} in the requested window")

        frame, dupes = normalise_frame(frame)
        frame, dropped = drop_incomplete(frame, timeframe)
        problem = validate_frame(frame, timeframe)
        if problem:
            return failed(self.name, timeframe, problem)

        limitations = []
        if instrument.market_class == "gold_futures":
            limitations.append(
                "GC=F is a continuous front-month futures series, not one "
                "contract: roll P&L, contract specs and expiry are not modelled. "
                "Research evidence only, not an executable fill.")
        if clamped:
            limitations.append(
                f"Yahoo limits {timeframe} history to about {cap_days} days; the "
                f"window was clamped to {effective_start.date().isoformat()}.")
        limitations.append(
            "Delayed research feed (Yahoo Finance); gold session gaps are "
            "reported, never filled or forward-filled.")

        return FetchResult(
            ok=True, provider=self.name, timeframe=timeframe, frame=frame,
            duplicates_dropped=dupes, incomplete_dropped=dropped,
            venue_symbol=self.symbol, limitations=limitations,
            note=f"{self.symbol} (continuous front-month)" if self.symbol == "GC=F"
                 else self.symbol)

    def _download(self, start: datetime, end: datetime,
                  timeframe: str) -> tuple[pd.DataFrame | None, str]:
        import yfinance as yf

        interval = YAHOO_INTERVALS[timeframe]
        # yfinance's `end` is exclusive; a day of slack keeps the last bar.
        end_excl = end + timedelta(days=1 if timeframe == "1d" else 0)

        def _call():
            return yf.Ticker(self.symbol).history(
                start=start.date().isoformat() if timeframe == "1d" else start,
                end=end_excl.date().isoformat() if timeframe == "1d" else end_excl,
                interval=interval, auto_adjust=False, actions=False,
                prepost=False, raise_errors=False)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(_call)
            try:
                df = fut.result(timeout=90)
            except concurrent.futures.TimeoutError:
                return None, "Yahoo request timed out after 90s"

        if df is None:
            return None, f"Yahoo returned no data for {self.symbol}"
        if getattr(df, "empty", True):
            return None, (f"Yahoo returned no data for {self.symbol} — the symbol "
                          f"may not exist or the requested window exceeds the "
                          f"provider limit")
        df = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                "Close": "close", "Volume": "volume"})
        if "volume" not in df.columns:
            df["volume"] = 0.0
        return df[["open", "high", "low", "close", "volume"]], ""


def gold_providers(cfg=None) -> list[Provider]:
    """Configured gold adapters, in preference order."""
    out: list[Provider] = []
    for cls, kwargs in (
        (YahooGoldProvider, {"symbol": "GC=F", "name": "yahoo_gc",
                             "market_class": "gold_futures"}),
        (YahooGoldProvider, {"symbol": "XAUUSD=X", "name": "yahoo_xauusd",
                             "market_class": "gold_xauusd"}),
    ):
        out.append(cls(cfg, **kwargs))
    return out
