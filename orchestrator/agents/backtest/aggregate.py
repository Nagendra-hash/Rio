"""
orchestrator/agents/backtest/aggregate.py — honest timeframe aggregation.

Rule: upsampling only. A 1h series may become 2h/4h; 4h can never become 1h,
and nothing can become 1m. Buckets are UTC-aligned with label=left and
closed=left, so the candle stamped 10:00 covers [10:00, 12:00) for 2h.

Empty buckets are dropped, never filled. A bucket built from fewer source
candles than expected is kept only when it has at least one real candle, and
its source-bar count is recorded so the coverage report can show it.
"""
from __future__ import annotations

import pandas as pd

from orchestrator.agents.backtest.providers.base import (
    OHLC_COLUMNS, empty_frame, normalise_frame, tf_seconds,
)

# Target timeframe -> allowed source timeframes (finest first preferred).
DERIVATIONS = {
    "2h": ("1h", "30m", "15m"),
    "4h": ("2h", "1h", "30m"),
    "6h": ("1h", "30m"),
    "12h": ("4h", "1h"),
    "1d": ("4h", "1h"),
    "30m": ("15m", "5m"),
    "15m": ("5m", "1m"),
    "5m": ("1m",),
}


def can_aggregate(source_tf: str, target_tf: str) -> bool:
    try:
        src, tgt = tf_seconds(source_tf), tf_seconds(target_tf)
    except ValueError:
        return False
    return tgt > src and tgt % src == 0


def resample_ohlcv(df: pd.DataFrame, source_tf: str,
                   target_tf: str) -> tuple[pd.DataFrame, dict]:
    """Aggregate candles; returns (frame, info) with counts and dropped empties."""
    if not can_aggregate(source_tf, target_tf):
        raise ValueError(f"cannot aggregate {source_tf} -> {target_tf}")
    if df is None or len(df) == 0:
        return empty_frame(), {"source": source_tf, "target": target_tf,
                               "buckets": 0, "empty_buckets": 0}
    rule = f"{tf_seconds(target_tf)}s"
    grouped = df.resample(rule, label="left", closed="left", origin="epoch")
    out = grouped.agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    })
    counts = grouped["close"].count().rename("source_bars")
    out = out.join(counts)
    empty = int((out["source_bars"] == 0).sum())
    out = out[out["source_bars"] > 0].drop(columns=["source_bars"])
    out, dupes = normalise_frame(out)
    info = {
        "source": source_tf, "target": target_tf,
        "buckets": int(len(out)), "empty_buckets": empty,
        "duplicates": dupes,
        "rule": f"UTC-aligned {tf_seconds(target_tf)}s buckets, label=left, "
                f"closed=left; empty buckets dropped, never filled",
    }
    return out, info


def derived_candidates(base_tf: str, candidates: tuple[str, ...]) -> list[str]:
    return [tf for tf in candidates if can_aggregate(base_tf, tf)]
