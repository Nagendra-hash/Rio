"""
orchestrator/agents/backtest/providers/kraken.py — Kraken public data.

Two paths, both read-only:

1. REST OHLC      https://api.kraken.com/0/public/OHLC
                  Public, no key. IMPORTANT: the endpoint serves at most 720
                  candles per interval regardless of `since`, so it cannot
                  supply a year of 1h data by itself. We say so instead of
                  pretending otherwise.

2. OHLCVT dataset https://support.kraken.com/articles/360047124832
                  Official downloadable files (1, 5, 15, 30, 60, 240, 720,
                  1440 minute intervals) with full history to 30 Jun 2026 +
                  quarterly increments. The archive is ~10 GB across 5 parts,
                  so we never download it automatically; if the user places
                  the CSV (or its original .zip) under
                  memory/backtest/kraken_csv/, this adapter streams the needed
                  pair/interval from it, chunk by chunk.

Kraken quotes USD for these pairs; Binance quotes USDT. They are separate
datasets and are never averaged into one price series.
"""
from __future__ import annotations

import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from orchestrator.agents.backtest.providers.base import (
    FetchResult, Provider, drop_incomplete, failed, normalise_frame,
    tf_seconds, utc_now, validate_frame,
)

BASE = "https://api.kraken.com/0/public"

# timeframes we can serve, and the Kraken interval string for each
TF_TO_KRAKEN = {"1m": 1, "5m": 5, "15m": 15, "30m": 30,
                "1h": 60, "4h": 240, "12h": 720, "1d": 1440}
KRAKEN_TO_TF = {v: k for k, v in TF_TO_KRAKEN.items()}
REST_MAX_CANDLES = 720


class KrakenProvider(Provider):
    name = "kraken_spot"
    venue = "Kraken Spot (USD)"
    supported_markets = ("crypto_spot",)
    native_timeframes = tuple(TF_TO_KRAKEN)

    def __init__(self, cfg=None, csv_dir: str | Path | None = None) -> None:
        self._cfg = cfg
        from orchestrator.agents.backtest.config import base_dir
        self.csv_dir = Path(csv_dir) if csv_dir else base_dir() / "kraken_csv"
        self._pairs: dict[str, str] | None = None
        self._last_error = ""

    # ── pair metadata ────────────────────────────────────────────────────
    def _asset_pairs(self) -> dict[str, str]:
        if self._pairs is not None:
            return self._pairs
        payload, _n, _r, err = self._http_json(f"{BASE}/AssetPairs")
        pairs: dict[str, str] = {}
        if not err and isinstance(payload, dict):
            for key, info in (payload.get("result") or {}).items():
                pairs[key] = str(info.get("wsname") or key)
        self._pairs = pairs
        return pairs

    def _resolve_pair(self, venue_symbol: str) -> tuple[str, str]:
        pairs = self._asset_pairs()
        if venue_symbol in pairs:
            return venue_symbol, pairs[venue_symbol]
        # XBTUSD is accepted by the API but the canonical result key is XXBTZUSD
        for key, ws in pairs.items():
            if ws.replace("/", "").replace("XBT", "BTC") == \
                    venue_symbol.replace("XBT", "BTC"):
                return key, ws
        return venue_symbol, venue_symbol

    # ── CSV dataset ──────────────────────────────────────────────────────
    def _csv_candidates(self, pair: str, interval: int,
                        timeframe: str) -> list[Path]:
        if not self.csv_dir.exists():
            return []
        names = {
            f"{pair}_{interval}.csv", f"{pair}_{timeframe}.csv",
            f"{pair}_{interval}.csv.gz", f"{pair}_{timeframe}.csv.gz",
        }
        out: list[Path] = []
        for p in self.csv_dir.rglob("*"):
            if p.is_file() and p.name in names:
                out.append(p)
        return out

    def _read_csv(self, path: Path, start: datetime,
                  end: datetime) -> tuple[pd.DataFrame, str]:
        lo, hi = int(start.timestamp()), int(end.timestamp())
        frames: list[pd.DataFrame] = []

        def _consume(handle) -> None:
            for chunk in pd.read_csv(handle, header=None, chunksize=250_000,
                                     names=["ts", "open", "high", "low",
                                            "close", "volume", "trades"],
                                     on_bad_lines="skip"):
                if chunk.empty:
                    continue
                chunk["ts"] = pd.to_numeric(chunk["ts"], errors="coerce")
                chunk = chunk.dropna(subset=["ts"])
                mask = (chunk["ts"] >= lo) & (chunk["ts"] <= hi)
                if mask.any():
                    frames.append(chunk.loc[mask])

        if path.suffix == ".zip":
            with zipfile.ZipFile(path) as zf:
                for member in zf.namelist():
                    if member.lower().endswith(".csv"):
                        with zf.open(member) as fh:
                            _consume(fh)
        else:
            _consume(path.open("r", encoding="utf-8", errors="replace"))

        if not frames:
            return pd.DataFrame(), "no rows in the requested window"
        df = pd.concat(frames, ignore_index=True)
        idx = pd.to_datetime(df["ts"].astype("int64"), unit="s", utc=True)
        out = pd.DataFrame({
            "open": pd.to_numeric(df["open"], errors="coerce").values,
            "high": pd.to_numeric(df["high"], errors="coerce").values,
            "low": pd.to_numeric(df["low"], errors="coerce").values,
            "close": pd.to_numeric(df["close"], errors="coerce").values,
            "volume": pd.to_numeric(df["volume"], errors="coerce").values,
        }, index=idx)
        return out, ""

    # ── fetch ────────────────────────────────────────────────────────────
    def fetch(self, instrument, timeframe: str, start: datetime,
              end: datetime, *, max_candles: int | None = None) -> FetchResult:
        if timeframe not in TF_TO_KRAKEN:
            return failed(self.name, timeframe,
                          f"Kraken has no native {timeframe} interval")
        venue = instrument.provider_symbols.get(self.name, "")
        if not venue:
            return failed(self.name, timeframe,
                          f"no Kraken pair for {instrument.label}")
        pair, wsname = self._resolve_pair(venue)
        interval = TF_TO_KRAKEN[timeframe]

        # 1) official dataset, when the user placed it locally
        for candidate in self._csv_candidates(pair, interval, timeframe):
            raw, err = self._read_csv(candidate, start, end)
            if raw.empty:
                continue
            frame, dupes = normalise_frame(raw)
            frame, dropped = drop_incomplete(frame, timeframe)
            problem = validate_frame(frame, timeframe)
            if problem:
                return failed(self.name, timeframe, problem)
            return FetchResult(
                ok=True, provider=self.name, timeframe=timeframe, frame=frame,
                requests_made=0, duplicates_dropped=dupes,
                incomplete_dropped=dropped, venue_symbol=pair,
                limitations=[
                    "Source: Kraken official downloadable OHLCVT dataset "
                    f"({candidate.name}); gaps mean no trades occurred and are "
                    "reported, never filled.",
                    "Kraken quotes USD; Binance quotes USDT — separate venues, "
                    "never averaged.",
                ],
                note=f"{wsname} from local OHLCVT dataset")

        # 2) public REST (recent window only)
        payload, n, r, err = self._http_json(
            f"{BASE}/OHLC",
            {"pair": pair, "interval": interval,
             "since": int(start.timestamp())},
            retries=3, pause=0.2)
        if err or not isinstance(payload, dict):
            return failed(self.name, timeframe, err or "malformed OHLC response")
        result = payload.get("result") or {}
        rows = next((v for k, v in result.items() if k != "last"), [])
        if not rows:
            return failed(self.name, timeframe,
                          f"no Kraken candles for {pair} {timeframe}")
        idx = pd.to_datetime([int(row[0]) for row in rows], unit="s", utc=True)
        frame = pd.DataFrame({
            "open": [float(x[1]) for x in rows],
            "high": [float(x[2]) for x in rows],
            "low": [float(x[3]) for x in rows],
            "close": [float(x[4]) for x in rows],
            "volume": [float(x[6]) for x in rows],
        }, index=idx)
        frame, dupes = normalise_frame(frame)
        frame, dropped = drop_incomplete(frame, timeframe)
        frame = frame.loc[(frame.index >= start) & (frame.index <= end)]
        if frame.empty:
            return failed(self.name, timeframe,
                          "requested window is older than the REST window")
        limitations = [
            f"Kraken REST OHLC returns at most {REST_MAX_CANDLES} candles per "
            "interval; the window is older than that, so this is a recent "
            "slice, not full history.",
            "For long history, place the official OHLCVT CSV under "
            "memory/backtest/kraken_csv/ (never auto-downloaded).",
            "Kraken quotes USD; separate venue from Binance USDT.",
        ]
        note = f"{wsname} REST window"
        if (frame.index[-1] - frame.index[0]).days < 365:
            note += " (less than one year of coverage)"
        return FetchResult(
            ok=True, provider=self.name, timeframe=timeframe, frame=frame,
            requests_made=n, retries=r, duplicates_dropped=dupes,
            incomplete_dropped=dropped, venue_symbol=pair,
            limitations=limitations, note=note)
