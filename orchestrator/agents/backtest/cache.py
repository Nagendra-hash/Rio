"""
orchestrator/agents/backtest/cache.py — incremental on-disk candle cache.

One file per (provider, market, symbol, timeframe): CSV.gz + a JSON sidecar
with the provenance, coverage and fetch notes. Cache keys include the venue so
spot and perpetual — or Binance and Kraken — can never be confused.

Incremental rule:
    * cache covers the requested window  → no network call
    * cache ends before the window       → fetch only the missing tail
    * cache starts after the window      → refetch the whole window
    * anything unreadable                → delete and refetch (never repair)

Writes are atomic (tmp + replace) under an exclusive flock, so a scheduled
worker and a user-triggered job cannot corrupt each other's file.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from orchestrator.agents.backtest.config import Config, default_config
from orchestrator.agents.backtest.providers.base import (
    FetchResult, Provider, empty_frame, normalise_frame, tf_seconds, utc_now,
)
from orchestrator.agents.backtest.store import cache_dir


@dataclass
class CacheResult:
    ok: bool
    frame: pd.DataFrame = field(default_factory=empty_frame)
    meta: dict[str, Any] = field(default_factory=dict)
    fetch: FetchResult | None = None
    from_cache: bool = False
    updated: bool = False
    error: str = ""

    def summary(self) -> dict[str, Any]:
        return {"ok": self.ok, "from_cache": self.from_cache,
                "updated": self.updated, "rows": int(len(self.frame)),
                "error": self.error,
                "meta": {k: v for k, v in self.meta.items()
                         if k not in ("instrument",)}}


def _safe(value: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in str(value))


class CandleCache:
    def __init__(self, cfg: Config | None = None,
                 root: str | Path | None = None) -> None:
        self.cfg = cfg or default_config()
        self.root = Path(root) if root else cache_dir()

    # ── paths ────────────────────────────────────────────────────────────
    def _dir(self, provider: str, instrument, timeframe: str) -> Path:
        return (self.root / _safe(provider) / _safe(instrument.market_class) /
                _safe(instrument.symbol) / _safe(timeframe))

    def paths(self, provider: str, instrument, timeframe: str) -> tuple[Path, Path]:
        d = self._dir(provider, instrument, timeframe)
        return d / "candles.csv.gz", d / "candles.meta.json"

    # ── load / save ──────────────────────────────────────────────────────
    def load(self, provider: str, instrument, timeframe: str
             ) -> tuple[pd.DataFrame, dict[str, Any]]:
        csv_path, meta_path = self.paths(provider, instrument, timeframe)
        if not csv_path.exists():
            return empty_frame(), {}
        try:
            frame = pd.read_csv(csv_path, index_col=0)
            frame.index = pd.to_datetime(frame.index, utc=True, errors="coerce")
            frame = frame[frame.index.notna()]
            frame, _ = normalise_frame(frame)
            meta = json.loads(meta_path.read_text(encoding="utf-8")) \
                if meta_path.exists() else {}
            return frame, meta
        except Exception:
            for p in (csv_path, meta_path):
                try:
                    p.unlink()
                except OSError:
                    pass
            return empty_frame(), {}

    def save(self, provider: str, instrument, timeframe: str,
             frame: pd.DataFrame, meta: dict[str, Any]) -> None:
        csv_path, meta_path = self.paths(provider, instrument, timeframe)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        lock = csv_path.with_suffix(".lock")
        with open(lock, "w") as fh:
            try:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_EX)
            except Exception:
                pass
            tmp = csv_path.with_suffix(".csv.gz.tmp")
            frame.to_csv(tmp, compression="gzip")
            tmp.replace(csv_path)
            tmp_meta = meta_path.with_suffix(".json.tmp")
            tmp_meta.write_text(json.dumps(meta, indent=1, default=str),
                                encoding="utf-8")
            tmp_meta.replace(meta_path)

    # ── fetch with incremental merge ─────────────────────────────────────
    def get_or_fetch(self, provider: Provider, instrument, timeframe: str,
                     start: datetime, end: datetime, *,
                     max_candles: int | None = None,
                     force: bool = False) -> CacheResult:
        step = pd.Timedelta(seconds=tf_seconds(timeframe))
        max_candles = int(max_candles or self.cfg.get("max_candles_per_series",
                                                      600_000))
        start = start.astimezone(timezone.utc) if start.tzinfo else \
            start.replace(tzinfo=timezone.utc)
        end = end.astimezone(timezone.utc) if end.tzinfo else \
            end.replace(tzinfo=timezone.utc)

        cached, meta = self.load(provider.name, instrument, timeframe)
        if len(cached) and not force:
            have_start = cached.index[0]
            have_end = cached.index[-1]
            # A cache that already reaches the latest completed candle is fresh;
            # otherwise the tail is fetched incrementally.
            fresh = have_end + step > utc_now()
            if have_start <= start and (have_end >= end - step or fresh):
                subset = cached.loc[(cached.index >= start) & (cached.index <= end)]
                if len(subset) >= 2:
                    return CacheResult(ok=True, frame=subset.iloc[-max_candles:],
                                       meta=meta, from_cache=True)

        fetch_start = start
        if len(cached) and not force and cached.index[0] <= start:
            fetch_start = max(start, cached.index[-1])

        result = provider.fetch(instrument, timeframe,
                                fetch_start.to_pydatetime()
                                if hasattr(fetch_start, "to_pydatetime") else fetch_start,
                                end, max_candles=max_candles)
        if not result.ok:
            return CacheResult(ok=False, frame=empty_frame(),
                               meta=meta, fetch=result,
                               error=result.error)

        merged = result.frame
        if len(cached) and not force:
            merged = pd.concat([cached, result.frame])
            merged, dupes = normalise_frame(merged)
            result.duplicates_dropped += dupes
        merged = merged.loc[(merged.index >= start) & (merged.index <= end)]
        if len(merged) > max_candles:
            merged = merged[-max_candles:]
            result.truncated = True

        if len(merged) == 0:
            return CacheResult(ok=False, frame=empty_frame(), meta=meta,
                               fetch=result, error="no candles after merge")

        new_meta = {
            "provider": provider.name,
            "venue": provider.venue,
            "venue_symbol": result.venue_symbol,
            "instrument": instrument.describe(),
            "timeframe": timeframe,
            "rows": int(len(merged)),
            "first": merged.index[0].isoformat(),
            "first_ts": float(merged.index[0].timestamp()),
            "last": merged.index[-1].isoformat(),
            "last_ts": float(merged.index[-1].timestamp()),
            "fetched_at": utc_now().isoformat(timespec="seconds"),
            "limitations": list(dict.fromkeys(
                (meta.get("limitations") or []) + result.limitations)),
            "note": result.note,
            "truncated": bool(result.truncated or meta.get("truncated")),
            "digest": hashlib.sha256(
                f"{len(merged)}|{merged.index[0]}|{merged.index[-1]}|"
                f"{merged['close'].iloc[-1]}".encode()).hexdigest()[:16],
        }
        self.save(provider.name, instrument, timeframe, merged, new_meta)
        return CacheResult(ok=True, frame=merged, meta=new_meta,
                           fetch=result, from_cache=False, updated=True)

    # ── housekeeping ─────────────────────────────────────────────────────
    def latest_cached(self, provider: str, instrument, timeframe: str
                      ) -> dict[str, Any]:
        _frame, meta = self.load(provider, instrument, timeframe)
        return meta

    def size_mb(self) -> float:
        total = 0
        for root, _d, files in os.walk(self.root):
            for f in files:
                try:
                    total += (Path(root) / f).stat().st_size
                except OSError:
                    pass
        return total / (1024 * 1024)
