"""
orchestrator/agents/backtest/quality.py — the data-coverage gate.

This module is the reason a backtest can say "BLOCKED" instead of "done". It
looks at the timestamps that actually arrived and answers, per timeframe:

    * who served it, which exact instrument and venue symbol
    * earliest / latest COMPLETED candle
    * calendar coverage in days and the candle count
    * expected candles (24/7 markets only; session markets report gaps instead)
    * missing candles, duplicate timestamps, large gaps
    * whether the configured one-year floor was genuinely met
    * every known limitation

No requested date is ever mistaken for delivered coverage.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from orchestrator.agents.backtest.providers.base import tf_seconds


def _iso(ts) -> str:
    return ts.isoformat() if hasattr(ts, "isoformat") else str(ts)


def gap_scan(frame: pd.DataFrame, timeframe: str,
             max_gap_s: float | None = None) -> dict[str, Any]:
    """Largest gaps between consecutive candles (no filling, just evidence)."""
    step = tf_seconds(timeframe)
    if len(frame) < 3:
        return {"count": 0, "largest": [], "max_gap_hours": 0.0,
                "expected_step_s": step}
    deltas = frame.index.to_series().diff().dropna()
    threshold = pd.Timedelta(seconds=max_gap_s or step * 1.5)
    gaps = deltas[deltas > threshold]
    largest = []
    for ts, delta in gaps.sort_values(ascending=False).head(10).items():
        largest.append({
            "at": _iso(ts),
            "gap_hours": round(delta.total_seconds() / 3600.0, 2),
            "expected_bars_missing": max(
                0, int(round(delta.total_seconds() / step)) - 1),
        })
    return {
        "count": int(len(gaps)),
        "largest": largest,
        "max_gap_hours": round(float(gaps.max().total_seconds() / 3600.0), 2)
        if len(gaps) else 0.0,
        "expected_step_s": step,
    }


def coverage_report(frame: pd.DataFrame, *, instrument, provider: str,
                    timeframe: str, requested_start: datetime,
                    requested_end: datetime, duplicates_dropped: int = 0,
                    fetch_notes: list[str] | None = None,
                    min_days: int = 365, min_ratio: float = 0.95,
                    max_gap_s: float | None = None,
                    derived_from: str | None = None,
                    complete: bool = True) -> dict[str, Any]:
    """Build the per-timeframe coverage record used by every report."""
    notes: list[str] = list(fetch_notes or [])
    session_market = instrument.asset_class == "gold"
    rows = int(len(frame))

    if rows == 0:
        return {
            "requested_instrument": instrument.symbol,
            "instrument_type": instrument.instrument_type,
            "market": instrument.market,
            "venue": instrument.exchange,
            "provider": provider,
            "timeframe": timeframe,
            "earliest": "", "latest": "",
            "coverage_days": 0.0, "candles": 0,
            "requested_start": _iso(requested_start),
            "requested_end": _iso(requested_end),
            "duplicates": duplicates_dropped,
            "missing_candles": None, "missing_ratio": None,
            "gaps": {"count": 0, "largest": [], "max_gap_hours": 0.0},
            "one_year_ok": False, "usable": False,
            "status": "BLOCKED", "limitations": notes,
            "reason": "no completed candles returned",
        }

    first, last = frame.index[0], frame.index[-1]
    coverage_days = (last - first).total_seconds() / 86400.0
    gaps = gap_scan(frame, timeframe, max_gap_s)

    expected = None
    missing = None
    missing_ratio = None
    if not session_market:
        step = tf_seconds(timeframe)
        expected = int((last - first).total_seconds() // step) + 1
        missing = max(0, expected - rows)
        missing_ratio = round(missing / expected, 5) if expected else None

    one_year_ok = coverage_days >= (min_days - 1)
    ratio_ok = True if missing_ratio is None else missing_ratio <= (1.0 - min_ratio)
    gap_ok = True
    if max_gap_s and not session_market:
        # Session markets close overnight/weekends/holidays; their gaps are
        # evidence to report, not a reason to reject otherwise full history.
        gap_ok = gaps["max_gap_hours"] * 3600 <= max_gap_s
    usable = bool(one_year_ok and ratio_ok and gap_ok and rows >= 400)

    if usable:
        status = "OK"
        reason = ("one-year floor verified from returned timestamps"
                  if one_year_ok else "")
    elif not one_year_ok:
        status = "INSUFFICIENT_DATA"
        reason = (f"only {coverage_days:.0f} calendar days of completed candles "
                  f"(need ≥{min_days})")
    elif not ratio_ok:
        status = "INSUFFICIENT_DATA"
        reason = (f"{missing_ratio:.2%} of expected candles are missing "
                  f"(limit {1 - min_ratio:.0%})")
    elif not gap_ok:
        status = "INSUFFICIENT_DATA"
        reason = (f"largest gap {gaps['max_gap_hours']:.1f}h exceeds the "
                  f"{max_gap_s / 3600:.1f}h limit")
    else:
        status = "INSUFFICIENT_DATA"
        reason = f"only {rows} candles"

    if session_market:
        notes.append("Session market: expected-bar counts are not asserted; "
                     "gaps reflect weekends/holidays/daily maintenance breaks.")
    if derived_from:
        notes.append(f"Aggregated from {derived_from} candles on UTC boundaries; "
                     "no finer data was fabricated.")
    if not complete:
        notes.append("The final forming candle was dropped; only completed "
                     "candles were used.")

    return {
        "requested_instrument": instrument.symbol,
        "instrument_type": instrument.instrument_type,
        "market": instrument.market,
        "market_class": instrument.market_class,
        "venue": instrument.exchange,
        "provider": provider,
        "timeframe": timeframe,
        "earliest": _iso(first),
        "latest": _iso(last),
        "coverage_days": round(coverage_days, 2),
        "candles": rows,
        "expected_candles": expected,
        "missing_candles": missing,
        "missing_ratio": missing_ratio,
        "duplicates": int(duplicates_dropped),
        "gaps": gaps,
        "requested_start": _iso(requested_start),
        "requested_end": _iso(requested_end),
        "one_year_ok": bool(one_year_ok),
        "usable": usable,
        "status": status,
        "reason": reason,
        "derived_from": derived_from,
        "limitations": list(dict.fromkeys(notes)),
    }


def coverage_table(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compact rows for Markdown/CLI presentation."""
    out = []
    for r in reports:
        out.append({
            "timeframe": r.get("timeframe", ""),
            "status": r.get("status", ""),
            "provider": r.get("provider", ""),
            "instrument": r.get("instrument_type", ""),
            "earliest": str(r.get("earliest", ""))[:10],
            "latest": str(r.get("latest", ""))[:10],
            "days": r.get("coverage_days", 0),
            "candles": r.get("candles", 0),
            "missing": r.get("missing_candles"),
            "duplicates": r.get("duplicates", 0),
            "one_year_ok": bool(r.get("one_year_ok")),
            "reason": r.get("reason", ""),
        })
    return out
