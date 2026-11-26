"""
orchestrator/agents/backtest/providers/binance.py — Binance public market data.

READ-ONLY. Two separate markets, never mixed:

    binance_spot      /api/v3/klines            (spot, max 1000/call)
    binance_futures   /fapi/v1/klines           (USDⓈ-M perp, max 1500/call)

Pagination is explicit start/end paging with a per-request pause; symbol
availability is checked against exchangeInfo before any backtest. Funding
history comes from /fapi/v1/fundingRate and is used only for perpetual
futures, only when it is actually returned.

Docs:
    https://github.com/binance/binance-spot-api-docs
    https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Kline-Candlestick-Data
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import pandas as pd

from orchestrator.agents.backtest.providers.base import (
    FetchResult, Provider, drop_incomplete, failed, normalise_frame,
    tf_seconds, validate_frame,
)

SPOT_BASE = "https://api.binance.com"
FUT_BASE = "https://fapi.binance.com"
INTERVALS = ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d")


class BinanceBase(Provider):
    supported_markets = ("crypto_spot", "crypto_perp")
    native_timeframes = INTERVALS
    page_limit = 1000
    base_url = SPOT_BASE

    def __init__(self, cfg=None) -> None:
        self._cfg = cfg
        self._symbols: dict[str, dict] = {}
        self._last_error = ""

    # ── symbol availability ──────────────────────────────────────────────
    def _symbol_info(self, symbol: str) -> dict | None:
        if symbol in self._symbols:
            return self._symbols[symbol]
        if self.name == "binance_spot":
            payload, _n, _r, err = self._http_json(
                f"{SPOT_BASE}/api/v3/exchangeInfo", {"symbol": symbol})
        else:
            payload, _n, _r, err = self._http_json(
                f"{FUT_BASE}/fapi/v1/exchangeInfo", {"symbol": symbol})
        if err or not isinstance(payload, dict):
            self._last_error = err or "exchangeInfo failed"
            return None
        rows = payload.get("symbols") or []
        rows = [r for r in rows if str(r.get("symbol") or "") == symbol]
        if not rows:
            self._last_error = f"{symbol} not listed on {self.name}"
            return None
        info = rows[0]
        self._symbols[symbol] = info
        return info

    def available(self, symbol: str) -> tuple[bool, str]:
        info = self._symbol_info(symbol)
        if not info:
            return False, self._last_error or f"{symbol} unavailable"
        status = str(info.get("status") or "").upper()
        if status and status not in {"TRADING", "SETTLING"}:
            return False, f"{symbol} status={status}"
        contract = str(info.get("contractType") or "")
        if self.name == "binance_futures" and contract and contract != "PERPETUAL":
            return False, f"{symbol} is {contract}, not a perpetual future"
        return True, contract

    # ── candles ──────────────────────────────────────────────────────────
    def fetch(self, instrument, timeframe: str, start: datetime,
              end: datetime, *, max_candles: int | None = None) -> FetchResult:
        if timeframe not in INTERVALS:
            return failed(self.name, timeframe,
                          f"{timeframe} is not a native Binance interval")
        if not self.supports(instrument):
            return failed(self.name, timeframe,
                          f"{self.name} does not serve {instrument.market_class}")
        venue_symbol = instrument.provider_symbols.get(self.name, "")
        if not venue_symbol:
            return failed(self.name, timeframe,
                          f"no {self.name} symbol for {instrument.label}")

        ok, detail = self.available(venue_symbol)
        if not ok:
            # A transient edge/WAF error on exchangeInfo must not discard a
            # statically configured venue symbol. Permanent answers
            # ("not listed", "status=…") still stop the fetch.
            transient = any(token in str(detail).lower() for token in
                            ("http 4", "http 5", "timeout", "timed out",
                             "connection", "rate limit", "temporarily"))
            if not transient:
                return failed(self.name, timeframe, detail)
            detail = f"availability check deferred ({detail})"

        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        step_ms = tf_seconds(timeframe) * 1000
        rows: list[list[Any]] = []
        requests_made = retries = 0
        cursor = start_ms
        truncated = False
        max_candles = int(max_candles or 0)

        for _page in range(10_000):
            if end_ms - cursor <= 0:
                break
            payload, n, r, err = self._http_json(
                self.klines_url,
                {"symbol": venue_symbol, "interval": timeframe,
                 "startTime": cursor, "endTime": end_ms,
                 "limit": self.page_limit},
                retries=3, pause=0.15)
            requests_made += n
            retries += r
            if err or not isinstance(payload, list):
                if rows:
                    break                    # keep what we verified, flag below
                return failed(self.name, timeframe, err or "malformed klines")
            if not payload:
                break
            rows.extend(payload)
            next_cursor = int(payload[-1][0]) + step_ms
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            if max_candles and len(rows) >= max_candles:
                rows = rows[-max_candles:]
                truncated = True
                break
            if len(payload) < self.page_limit:
                break
        else:
            truncated = True

        if not rows:
            return failed(self.name, timeframe,
                          f"no candles for {venue_symbol} in the requested window")

        idx = pd.to_datetime([int(r[0]) for r in rows], unit="ms", utc=True)
        frame = pd.DataFrame({
            "open": [float(r[1]) for r in rows],
            "high": [float(r[2]) for r in rows],
            "low": [float(r[3]) for r in rows],
            "close": [float(r[4]) for r in rows],
            "volume": [float(r[5]) for r in rows],
        }, index=idx)
        frame, dupes = normalise_frame(frame)
        frame, dropped = drop_incomplete(frame, timeframe)
        problem = validate_frame(frame, timeframe)
        if problem:
            return failed(self.name, timeframe, problem)

        limitations = [
            "Exchange candles are UTC-aligned and include the currently forming "
            "candle, which is dropped before use.",
        ]
        if instrument.market == "perp":
            limitations.append(
                "USDⓈ-M perpetual: funding costs are applied only where the "
                "funding-rate history is available; liquidation is not modelled.")
        if truncated:
            limitations.append(
                "Series truncated to the configured candle cap; older candles "
                "were not loaded into this run.")
        if retries:
            limitations.append(f"{retries} request retries were needed.")

        return FetchResult(
            ok=True, provider=self.name, timeframe=timeframe, frame=frame,
            requests_made=requests_made, retries=retries, truncated=truncated,
            incomplete_dropped=dropped, duplicates_dropped=dupes,
            venue_symbol=venue_symbol, limitations=limitations,
            note=f"{venue_symbol} {detail}".strip())

    # ── funding (perpetual only) ─────────────────────────────────────────
    def funding_history(self, venue_symbol: str, start: datetime,
                        end: datetime) -> tuple[pd.Series, str]:
        """Funding rates indexed by funding time. Returns (series, error)."""
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        out: dict[pd.Timestamp, float] = {}
        cursor = start_ms
        for _ in range(200):
            payload, _n, _r, err = self._http_json(
                f"{FUT_BASE}/fapi/v1/fundingRate",
                {"symbol": venue_symbol, "startTime": cursor,
                 "endTime": end_ms, "limit": 1000},
                retries=3, pause=0.15)
            if err or not isinstance(payload, list) or not payload:
                return pd.Series(out).sort_index(), err if not out else ""
            for row in payload:
                try:
                    out[pd.Timestamp(int(row["fundingTime"]), unit="ms", tz="UTC")] = \
                        float(row["fundingRate"])
                except Exception:
                    continue
            nxt = int(payload[-1]["fundingTime"]) + 1
            if nxt <= cursor:
                break
            cursor = nxt
        return pd.Series(out).sort_index(), ""


class BinanceSpot(BinanceBase):
    name = "binance_spot"
    venue = "Binance Spot"
    supported_markets = ("crypto_spot",)
    page_limit = 1000
    base_url = SPOT_BASE
    klines_url = f"{SPOT_BASE}/api/v3/klines"


class BinanceFutures(BinanceBase):
    name = "binance_futures"
    venue = "Binance USDⓈ-M Futures"
    supported_markets = ("crypto_perp",)
    page_limit = 1500
    base_url = FUT_BASE
    klines_url = f"{FUT_BASE}/fapi/v1/klines"


def binance_providers(cfg=None) -> list[Provider]:
    return [BinanceSpot(cfg), BinanceFutures(cfg)]
