#!/usr/bin/env python3
"""Regression test for memory/trading/price_levels.jsonl (JARVIS price-level journal).

The journal is written by ~/browserbase-demos/india-market-tracker. Before
2026-09-20 its day_change_pct was a range-anchored (5-bar) change instead of a
true day change — this test exists so that class of bug can never silently
return.

Two tiers:

1. Internal consistency (offline, always run): every row's day_change_pct must
   equal the day change implied by its own prev_close/price. Catches
   corruption, hand edits, and partial writes.

2. External consistency (Yahoo, needs network): every row's day_change_pct
   must match the day change recomputed from Yahoo daily bars vs ANY of the
   last 3 bar closes at-or-before the record time. The 3-candidate window
   tolerates session-boundary ambiguity and Yahoo's late daily-bar backfills
   (e.g. a weekend bar appearing after the fact) while still catching the
   range-anchor bug, whose prev_close lands 4-5 bars back. The window is
   deliberately SMALLER than 4: the 5d-range anchor sits exactly 4 bars back,
   so a 4-deep window would legitimize the very bug this test guards against.
   TOL_PP is 0.15pp (not 0.05) because Yahoo's current-day bar close drifts
   intraday on 24/7 symbols (BTC, FX) and weekend bars can appear/disappear
   between a record being written and being checked — observed ~0.1pp swings.

Exit codes: 0 = pass, 1 = journal regression, 2 = environment error
(network down / Yahoo unreachable / journal missing) so automation can
distinguish "data is wrong" from "can't check right now".

Usage:
    python3 tests/test_price_levels_journal.py
    python3 tests/test_price_levels_journal.py --journal <other.jsonl>

Also pytest-compatible (functions test_internal_consistency /
test_external_consistency; the network tier is skipped if Yahoo is
unreachable).
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JOURNAL = ROOT / "memory" / "trading" / "price_levels.jsonl"
UA = "Mozilla/5.0 (X11; Linux x86_64) jarvis-journal-test/1.0"

TOL_PP = 0.15           # external day-change tolerance, percentage points
                        # (covers live-bar drift on 24/7 symbols; quirk errors
                        # were >= 0.35pp, typically >= 0.8pp)
CANDIDATES = 3          # last-N bar closes accepted as the "previous close"
                        # (must stay < 4 — see module docstring)
INTERNAL_TOL = 0.011    # stored pct vs recompute from the row's own prev_close

REQUIRED_ROW_KEYS = ("symbol", "price", "prev_close", "day_change_pct")


class EnvError(Exception):
    """Network / data-availability problem — not a journal regression."""


# ---------- journal loading ----------

def load_records(path: Path) -> list[dict]:
    if not path.exists():
        raise EnvError(f"journal not found: {path}")
    records = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            records.append({"_parse_error": str(exc), "_line_no": line_no})
    return records


def record_epoch(ts: str) -> float:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


# ---------- Yahoo daily bars ----------

def fetch_bars(symbol: str) -> list[tuple[float, float]]:
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{urllib.request.quote(symbol)}?range=1mo&interval=1d"
    )
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.load(resp)
    except Exception as exc:  # network, HTTP, JSON — all environmental
        raise EnvError(f"Yahoo fetch failed for {symbol}: {exc}") from exc
    try:
        result = payload["chart"]["result"][0]
        stamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError) as exc:
        raise EnvError(f"Yahoo returned no bar data for {symbol}") from exc
    return sorted((t, float(c)) for t, c in zip(stamps, closes) if c is not None and c > 0)


# ---------- tier 1: internal consistency (offline) ----------

def check_internal(records: list[dict]) -> list[str]:
    failures: list[str] = []
    for rec in records:
        if "_parse_error" in rec:
            failures.append(f"line {rec['_line_no']}: invalid JSON ({rec['_parse_error']})")
            continue
        ts = rec.get("ts", "<missing ts>")
        prices = rec.get("prices")
        if not isinstance(prices, list) or not prices:
            failures.append(f"{ts}: record has no prices[] list")
            continue
        for row in prices:
            if not isinstance(row, dict):
                failures.append(f"{ts}: prices[] contains a non-object entry")
                continue
            sym = row.get("symbol", "?")
            missing = [k for k in REQUIRED_ROW_KEYS if k not in row]
            if missing:
                failures.append(f"{ts} {sym}: missing keys {missing}")
                continue
            price, prev, pct = row["price"], row["prev_close"], row["day_change_pct"]
            if not all(isinstance(v, (int, float)) for v in (price, prev, pct)):
                failures.append(f"{ts} {sym}: non-numeric price/prev_close/day_change_pct")
                continue
            if prev == 0:
                failures.append(f"{ts} {sym}: prev_close is zero")
                continue
            implied = round((price - prev) / prev * 100, 2)
            if abs(implied - pct) > INTERNAL_TOL:
                failures.append(
                    f"{ts} {sym}: day_change_pct {pct} != implied by its own "
                    f"prev_close ({implied}) — corrupted or hand-edited row"
                )
    return failures


# ---------- tier 2: external consistency vs Yahoo bars ----------

def check_external(records: list[dict]) -> tuple[list[str], int]:
    failures: list[str] = []
    symbols = sorted({
        row["symbol"]
        for rec in records
        if isinstance(rec, dict) and "_parse_error" not in rec
        for row in rec.get("prices", [])
        if isinstance(row, dict) and "symbol" in row
    })
    bars_by_symbol = {sym: fetch_bars(sym) for sym in symbols}

    checked = 0
    for rec in records:
        if not isinstance(rec, dict) or "_parse_error" in rec:
            continue
        ts = rec.get("ts", "")
        try:
            epoch = record_epoch(ts)
        except (ValueError, AttributeError):
            failures.append(f"{ts}: unparseable timestamp")
            continue
        for row in rec.get("prices", []):
            if not isinstance(row, dict) or not all(k in row for k in REQUIRED_ROW_KEYS):
                continue  # structural problems are tier-1's job
            sym = row["symbol"]
            bars = bars_by_symbol.get(sym)
            if not bars:
                continue
            asof = [(t, c) for t, c in bars if t <= epoch]
            if not asof:
                failures.append(f"{ts} {sym}: no Yahoo bars at-or-before record time")
                continue
            checked += 1
            price, pct = row["price"], row["day_change_pct"]
            candidates = [
                round((price - close) / close * 100, 2) for _, close in asof[-CANDIDATES:]
            ]
            if all(abs(pct - cand) > TOL_PP for cand in candidates):
                failures.append(
                    f"{ts} {sym}: day_change_pct {pct} matches none of the last "
                    f"{CANDIDATES} bar closes {candidates} — stale/range-anchored prev_close"
                )
    return failures, checked


# ---------- runners ----------

def run_checks(journal: Path) -> tuple[list[str], int, int]:
    records = load_records(journal)
    if not records:
        return [], 0, 0
    failures = check_internal(records)
    ext_failures, checked = check_external(records)
    return failures + ext_failures, checked, len(records)


def main(argv: list[str]) -> int:
    args = list(argv[1:])
    journal = DEFAULT_JOURNAL
    if "--journal" in args:
        i = args.index("--journal")
        journal = Path(args[i + 1])
        del args[i:i + 2]

    try:
        failures, checked, total = run_checks(journal)
    except EnvError as exc:
        print(f"ENVIRONMENT ERROR (exit 2): {exc}")
        return 2

    if failures:
        print(f"JOURNAL REGRESSION — {len(failures)} problem(s) in {journal}:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"journal OK — {total} record(s), {checked} row(s) verified against Yahoo bars")
    return 0


def test_internal_consistency():
    problems = check_internal(load_records(DEFAULT_JOURNAL))
    assert not problems, "\n".join(problems)


def test_external_consistency():
    records = load_records(DEFAULT_JOURNAL)
    try:
        problems, _ = check_external(records)
    except EnvError as exc:
        try:
            import pytest
            pytest.skip(str(exc))
        except ImportError:
            raise
    assert not problems, "\n".join(problems)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
