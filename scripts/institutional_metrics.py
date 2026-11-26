#!/usr/bin/env python3
"""
scripts/institutional_metrics.py — the numbers behind an institutional read.

Fetches REAL daily bars for a basket plus a benchmark, then prints one JSON
report containing everything a buy-side desk wants before sizing a position:
per-name return/vol/Sharpe/drawdown/beta/correlation/ATR, the benchmark's
trend x volatility regime and the posture it implies, the pairwise correlation
matrix, portfolio-level risk (vol, beta, VaR95, CVaR95, max drawdown), and
vol-targeted / fixed-fractional SIZING suggestions plus optional stress shocks.

This is the engine the `institutional-trading` skill (Hermes + JARVIS) calls:
the LLM composes the narrative, but every number here is arithmetic over the
bars — nothing is invented, and a metric that cannot be computed honestly is
null, never a guess.

USAGE
    # one basket vs SPY, risk sizing for a 10L book at 1% risk/trade:
    .venv/bin/python scripts/institutional_metrics.py \
        --symbols RELIANCE.NS,TCS.NS,INFY.NS --benchmark ^NSEI \
        --period 2y --capital 1000000 --target-vol 0.12

    # explicit weights + a scenario ("rates up, tech down 15%"):
    .venv/bin/python scripts/institutional_metrics.py --symbols NVDA,MSFT \
        --weights NVDA=0.6,MSFT=0.4 --stress "NVDA=-0.15,MSFT=-0.10"

    # offline / no network (bars from a JSON file: {"SYM": [bars...], ...}):
    .venv/bin/python scripts/institutional_metrics.py --from-json bars.json

    # deterministic fake market, for a demo or a smoke test:
    .venv/bin/python scripts/institutional_metrics.py --simulated --symbols A,B

OUTPUT: JSON on stdout (indent 2), the report from
orchestrator/agents/trading/institutional.py::build_report, with `data`
provenance and per-symbol fetch errors recorded under `_fetch`.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from orchestrator.agents.trading import institutional as inst   # noqa: E402


def _sym_list(raw: str | None) -> list[str]:
    return [s.strip() for s in (raw or "").split(",") if s.strip()]


def _kv_pairs(raw: str | None) -> dict:
    out: dict = {}
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        k, v = chunk.split("=", 1)
        out[k.strip()] = float(v)
    return out


def _fetch(symbols: list[str], benchmark: str, period: str, interval: str,
           simulated: bool) -> tuple[dict, dict, dict]:
    """Return (bars_by_symbol, benchmark_bars, meta). Network via data.py."""
    from orchestrator.agents.trading.data import MarketData
    md = MarketData(prefer_sim=simulated)
    bars: dict[str, list[dict]] = {}
    meta: dict = {"provenance": "simulated" if simulated else "delayed",
                  "symbols": {}, "benchmark": None}
    for s in symbols:
        res = md.history(s, period=period, interval=interval,
                         prefer_sim=simulated)
        if res.ok and res.data and res.data.get("bars"):
            bars[s] = res.data["bars"]
            meta["symbols"][s] = {"ok": True, "source": res.source,
                                  "provenance": res.provenance.value,
                                  "bars": len(res.data["bars"]),
                                  "as_of": res.as_of}
        else:
            meta["symbols"][s] = {"ok": False, "error": res.error,
                                  "source": res.source}
    bench_res = md.history(benchmark, period=period, interval=interval,
                           prefer_sim=simulated)
    bench_bars: list[dict] = []
    if bench_res.ok and bench_res.data and bench_res.data.get("bars"):
        bench_bars = bench_res.data["bars"]
        meta["benchmark"] = {"ok": True, "symbol": benchmark,
                             "source": bench_res.source,
                             "provenance": bench_res.provenance.value,
                             "bars": len(bench_bars), "as_of": bench_res.as_of}
    else:
        meta["benchmark"] = {"ok": False, "symbol": benchmark,
                             "error": bench_res.error, "source": bench_res.source}
    return bars, bench_bars, meta


def _load_json(path: str) -> tuple[dict, list[dict], str | None]:
    doc = json.loads(Path(path).read_text())
    if not isinstance(doc, dict):
        raise SystemExit("--from-json must contain an object of symbol → bars")
    bench_symbol = doc.get("_benchmark")
    bench_bars = doc.get(bench_symbol, []) if bench_symbol else []
    # NOTE: the benchmark is read, never removed — it may legitimately be one
    # of the traded names (benchmarking a name against itself is valid).
    assets = {k: v for k, v in doc.items() if not k.startswith("_")}
    return assets, bench_bars, bench_symbol


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", help="comma-separated tickers, e.g. NVDA,MSFT")
    ap.add_argument("--benchmark", default="SPY")
    ap.add_argument("--period", default="1y")
    ap.add_argument("--interval", default="1d")
    ap.add_argument("--weights", help='e.g. "NVDA=0.6,MSFT=0.4"')
    ap.add_argument("--capital", type=float, help="book size for sizing math")
    ap.add_argument("--target-vol", type=float, default=0.10,
                    help="portfolio target annualised vol (default 0.10)")
    ap.add_argument("--risk-pct", type=float, default=1.0,
                    help="%% of capital risked per trade (default 1.0)")
    ap.add_argument("--max-weight", type=float, default=0.25,
                    help="single-name cap for vol targeting (default 0.25)")
    ap.add_argument("--rf", type=float, default=0.0, help="annual risk-free rate")
    ap.add_argument("--stress", help='scenario shocks, e.g. "NVDA=-0.15,MSFT=-0.10"')
    ap.add_argument("--from-json", help="offline bars file (no network)")
    ap.add_argument("--simulated", action="store_true",
                    help="use the deterministic simulated market")
    ap.add_argument("--out", help="also write the JSON report here")
    args = ap.parse_args(argv)

    fetch_meta: dict = {}
    benchmark = args.benchmark
    if args.from_json:
        bars, bench_bars, file_bench = _load_json(args.from_json)
        if file_bench:
            benchmark = file_bench
        fetch_meta = {"provenance": "file", "path": args.from_json,
                      "benchmark": benchmark}
        symbols = list(bars)
    else:
        symbols = _sym_list(args.symbols)
        if not symbols:
            ap.error("--symbols (or --from-json) is required")
        bars, bench_bars, fetch_meta = _fetch(
            symbols, benchmark, args.period, args.interval, args.simulated)

    weights = _kv_pairs(args.weights) or None
    report = inst.build_report(
        bars, benchmark_symbol=benchmark, benchmark_bars=bench_bars,
        weights=weights, capital=args.capital,
        target_ann_vol=args.target_vol, risk_pct=args.risk_pct,
        max_weight=args.max_weight, rf_annual=args.rf)

    report["_fetch"] = fetch_meta
    if args.stress:
        shocks = _kv_pairs(args.stress)
        report["stress"] = inst.stress_test(
            (report.get("portfolio") or {}).get("weights") or {}, shocks)

    text = json.dumps(report, indent=2, default=str)
    print(text)
    if args.out:
        Path(args.out).write_text(text)
    return 0 if report.get("assets") else 1


if __name__ == "__main__":
    raise SystemExit(main())
