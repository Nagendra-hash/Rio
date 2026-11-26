"""
orchestrator/agents/backtest/providers/base.py — the provider contract.

Every adapter is READ-ONLY market data: public GET/POST endpoints that return
candles. There is no order, account, balance or position code path in this
package by construction (see safety.py and the tests that scan for it).

Contract:
    supports(instrument)          → can this provider serve this exact market?
    timeframes(instrument)        → native intervals (no fake upsampling)
    fetch(instrument, tf, start, end)
                                  → FetchResult with candles + honest notes

A provider that cannot serve the request returns ok=False with a reason. No
adapter ever invents, forward-fills or smooths a price.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pandas as pd

TIMEFRAME_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "12h": 43200,
    "1d": 86400,
}

OHLC_COLUMNS = ["open", "high", "low", "close", "volume"]


def tf_seconds(timeframe: str) -> int:
    try:
        return TIMEFRAME_SECONDS[str(timeframe)]
    except KeyError:
        raise ValueError(f"unknown timeframe '{timeframe}'") from None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def floor_ts(ts: float, seconds: int) -> int:
    return int(math.floor(ts / seconds) * seconds)


def to_utc(ts: float | int) -> datetime:
    return datetime.fromtimestamp(float(ts), timezone.utc)


def empty_frame() -> pd.DataFrame:
    idx = pd.DatetimeIndex([], tz="UTC", name="ts")
    return pd.DataFrame({c: pd.Series(dtype="float64") for c in OHLC_COLUMNS},
                        index=idx)


def normalise_frame(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Canonicalise a provider frame: UTC index, numeric OHLCV, sorted, unique.

    Returns (frame, duplicates_removed). The LAST row wins on a duplicate
    timestamp — identical semantics to an exchange re-sending a candle.
    Rows with a non-finite OHLC value are dropped (never repaired).
    """
    if df is None or len(df) == 0:
        return empty_frame(), 0
    out = df.copy()
    if not isinstance(out.index, pd.DatetimeIndex):
        raise ValueError("provider frame must use a DatetimeIndex")
    if out.index.tz is None:
        out.index = out.index.tz_localize("UTC")
    else:
        out.index = out.index.tz_convert("UTC")
    out.index.name = "ts"
    out = out[[c for c in OHLC_COLUMNS if c in out.columns]].copy()
    for c in OHLC_COLUMNS:
        if c not in out.columns:
            out[c] = 0.0
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out.replace([math.inf, -math.inf], float("nan"))
    out = out.dropna(subset=["open", "high", "low", "close"])
    before = len(out)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    duplicates = int(before - len(out))
    out = out[["open", "high", "low", "close", "volume"]]
    return out.astype("float64"), duplicates


def drop_incomplete(df: pd.DataFrame, timeframe: str,
                    now: datetime | None = None) -> tuple[pd.DataFrame, bool]:
    """Remove the currently-forming candle. Completed candles only."""
    if df is None or len(df) == 0:
        return empty_frame(), False
    now = now or utc_now()
    last = df.index[-1]
    if last + pd.Timedelta(seconds=tf_seconds(timeframe)) > now:
        return df.iloc[:-1], True
    return df, False


@dataclass
class FetchResult:
    ok: bool
    provider: str = ""
    timeframe: str = ""
    frame: pd.DataFrame = field(default_factory=empty_frame)
    error: str = ""
    limitations: list[str] = field(default_factory=list)
    requests_made: int = 0
    retries: int = 0
    truncated: bool = False
    incomplete_dropped: bool = False
    duplicates_dropped: int = 0
    venue_symbol: str = ""
    note: str = ""

    @property
    def rows(self) -> int:
        return int(len(self.frame))

    def summary(self) -> dict[str, Any]:
        first = self.frame.index[0].isoformat() if self.rows else ""
        last = self.frame.index[-1].isoformat() if self.rows else ""
        return {"ok": self.ok, "provider": self.provider, "timeframe": self.timeframe,
                "rows": self.rows, "first": first, "last": last,
                "requests": self.requests_made, "retries": self.retries,
                "error": self.error, "limitations": list(self.limitations),
                "truncated": self.truncated,
                "incomplete_dropped": self.incomplete_dropped,
                "duplicates_dropped": self.duplicates_dropped,
                "venue_symbol": self.venue_symbol}


def failed(provider: str, timeframe: str, error: str,
           limitations: list[str] | None = None) -> FetchResult:
    return FetchResult(ok=False, provider=provider, timeframe=timeframe,
                       error=str(error)[:400],
                       limitations=list(limitations or []))


class Provider:
    """Base adapter. Subclasses implement fetch(); everything else is shared."""

    name = "provider"
    venue = ""
    native_timeframes: tuple[str, ...] = ()
    supported_markets: tuple[str, ...] = ()

    def supports(self, instrument) -> bool:
        return instrument.market_class in self.supported_markets

    def timeframes(self, instrument) -> list[str]:
        return list(self.native_timeframes) if self.supports(instrument) else []

    def fetch(self, instrument, timeframe: str, start: datetime,
              end: datetime) -> FetchResult:
        raise NotImplementedError

    def latest(self, instrument, timeframe: str) -> FetchResult:
        """Small window ending now — used by the scheduler's new-data check."""
        end = utc_now()
        start = end - pd.Timedelta(seconds=tf_seconds(timeframe) * 3)
        return self.fetch(instrument, timeframe, start.to_pydatetime(), end)

    def health(self) -> dict[str, Any]:
        return {"provider": self.name, "venue": self.venue,
                "timeframes": list(self.native_timeframes)}

    # ── HTTP helper (shared by REST adapters) ────────────────────────────
    def _http_json(self, url: str, params: dict | None = None, *,
                   timeout: float = 25, retries: int = 3,
                   pause: float = 0.15, method: str = "GET",
                   json_body: dict | None = None,
                   headers: dict | None = None) -> tuple[Any, int, int, str]:
        """(payload, requests, retries_used, error). Backoff on 429/5xx."""
        import requests

        last_err = ""
        used = 0
        for attempt in range(max(1, retries)):
            used += 1
            try:
                req_headers = {"User-Agent": "JARVIS-BacktestLab/1.0 (read-only research)"}
                req_headers.update(headers or {})
                if method.upper() == "POST":
                    resp = requests.post(url, params=params, json=json_body,
                                         timeout=timeout, headers=req_headers)
                else:
                    resp = requests.get(url, params=params, timeout=timeout,
                                        headers=req_headers)
                if resp.status_code == 429:
                    last_err = "rate limited (HTTP 429)"
                    retry_after = resp.headers.get("Retry-After", "")
                    delay = float(retry_after) if retry_after.replace(".", "").isdigit() else 2.0 * attempt + 1
                    time.sleep(min(delay, 10.0))
                    continue
                if resp.status_code == 403 and attempt == 0:
                    # Seen intermittently from exchange edge/WAF nodes; one
                    # retry separates a transient block from a permanent one.
                    last_err = f"HTTP 403: {(resp.text or '')[:120]}"
                    time.sleep(1.5)
                    continue
                if resp.status_code >= 500:
                    last_err = f"provider error HTTP {resp.status_code}"
                    time.sleep(min(1.5 * (attempt + 1), 6.0))
                    continue
                if resp.status_code >= 400:
                    body = (resp.text or "")[:200]
                    return None, used, attempt, f"HTTP {resp.status_code}: {body}"
                if pause:
                    time.sleep(pause)
                return resp.json(), used, attempt, ""
            except Exception as exc:                      # network / JSON
                last_err = f"{type(exc).__name__}: {str(exc)[:180]}"
                time.sleep(min(1.5 * (attempt + 1), 6.0))
        return None, used, max(0, used - 1), last_err or "request failed"


def validate_frame(df: pd.DataFrame, timeframe: str) -> str:
    """Cheap structural sanity check. Returns '' when it looks like candles."""
    if df is None or len(df) == 0:
        return "no candles returned"
    if (df["high"] < df["low"]).any():
        return "high < low in returned candles"
    bad = ((df["open"] <= 0) | (df["close"] <= 0)).sum()
    if bad:
        return f"{int(bad)} non-positive prices"
    step = df.index.to_series().diff().dropna()
    if len(step) and (step <= pd.Timedelta(0)).any():
        return "non-increasing timestamps"
    return ""
