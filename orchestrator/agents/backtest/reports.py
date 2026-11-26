"""
orchestrator/agents/backtest/reports.py — Markdown + CSV artifacts.

Every job writes:
    result.json        machine-readable result (also what Jarvis answers from)
    summary.md         the concise report a human reads in a message
    report.md          the detailed report
    experiments.csv    every configuration tested, with its metrics
    trades_<key>.csv   trade list of the champion (and shortlist)
    equity_<key>.csv   equity curve of the champion

Numbers are copied from the engine; nothing here recomputes or rounds up a
result. Limitations and cost assumptions travel with the numbers.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from orchestrator.agents.backtest.store import report_dir, write_json

DISCLAIMER = (
    "A backtest is historical evidence, not a guarantee of future profit. "
    "Results depend on the modelled costs, the provider's candles and the "
    "chosen validation protocol; they are research output, not advice and not "
    "an order.")


def _pct(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return "n/a"


def _num(value: Any, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _rr(value: Any) -> str:
    """Risk/reward written as 1 : x — reward per one unit of risk."""
    if value is None:
        return "n/a"
    try:
        return f"1 : {float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def render_summary(result: dict[str, Any]) -> str:
    inst = result.get("instrument") or {}
    lines = [
        f"# Backtest research — {result.get('symbol', '?')} "
        f"({result.get('status', '?')})",
        "",
        f"- **Job**: `{result.get('job_id', '')}`",
        f"- **Instrument**: {inst.get('instrument_type', 'n/a')} "
        f"({inst.get('exchange', 'n/a')})",
        f"- **Providers used**: "
        f"{', '.join(sorted({c.get('provider', '') for c in result.get('candidates', []) if c.get('provider')})) or ', '.join(result.get('providers_used', [])) or 'none'}",
        f"- **Timeframes tested**: "
        f"{', '.join(result.get('timeframes_tested') or []) or 'none'}",
        f"- **Timeframes rejected**: "
        f"{', '.join(t.get('timeframe', '') for t in result.get('timeframes_rejected') or []) or 'none'}",
        f"- **Strategies evaluated**: {len(result.get('candidates') or [])} "
        f"candidates across {len(result.get('strategies') or [])} strategies",
        f"- **Configurations tested**: {result.get('experiments_total', 0)} "
        f"(failures: {result.get('failures_total', 0)})",
        f"- **Runtime**: {_num(result.get('runtime_s'), 1)}s on "
        f"{result.get('limits', {}).get('max_workers', 1)} worker(s)",
        "",
    ]

    champion = result.get("champion")
    if champion:
        oos = champion.get("oos") or {}
        bench = champion.get("benchmark_oos") or {}
        lines += [
            "## Best validated candidate",
            "",
            f"**{champion.get('name')} v{champion.get('version')}** on "
            f"{champion.get('timeframe')} — {champion.get('strategy')} "
            f"`{champion.get('params')}`",
            "",
            "| Out-of-sample metric | Strategy | Buy & hold |",
            "|---|---|---|",
            f"| Net return | {_pct(oos.get('net_return'))} | "
            f"{_pct(bench.get('net_return'))} |",
            f"| CAGR | {_pct(oos.get('cagr'))} | n/a |",
            f"| Max drawdown | {_pct(oos.get('max_drawdown'))} | "
            f"{_pct(bench.get('max_drawdown'))} |",
            f"| Sharpe | {_num(oos.get('sharpe'))} | {_num(bench.get('sharpe'))} |",
            f"| Sortino | {_num(oos.get('sortino'))} | {_num(bench.get('sortino'))} |",
            f"| Profit factor | {_num(oos.get('profit_factor'))} | n/a |",
            f"| Win rate | {_pct(oos.get('win_rate'))} | n/a |",
            f"| Trades (out-of-sample) | {oos.get('trades', 0)} | 1 |",
            f"| Expectancy (R) | {_num(oos.get('expectancy_r'))} | n/a |",
            f"| Risk/reward (realised) | {_rr(oos.get('risk_reward'))} | n/a |",
            f"| Exposure | {_pct(oos.get('exposure'))} | 100% |",
            f"| Parameter stability | {_num(champion.get('stability'), 2)} | n/a |",
            "",
            f"- Out-of-sample window: {oos.get('start', 'n/a')} → {oos.get('end', 'n/a')}",
            f"- Cost model: {champion.get('costs', {}).get('round_trip_cost_bps', 'n/a')} bps round trip "
            f"({champion.get('costs', {})})",
            "",
        ]
    else:
        lines += [
            "## Result",
            "",
            f"**{result.get('headline', 'NO ROBUST STRATEGY FOUND')}**",
            "",
            f"{result.get('reason', '')}",
            "",
        ]

    if result.get("warnings"):
        lines += ["## Warnings", ""]
        lines += [f"- {w}" for w in result["warnings"]]
        lines += [""]

    lines += [
        "## Files",
        "",
        f"- Detailed report: `{result.get('paths', {}).get('report_md', '')}`",
        f"- Summary: `{result.get('paths', {}).get('summary_md', '')}`",
        f"- Machine-readable: `{result.get('paths', {}).get('result_json', '')}`",
        "",
        f"_{DISCLAIMER}_",
        "",
    ]
    return "\n".join(lines)


def render_detailed(result: dict[str, Any]) -> str:
    inst = result.get("instrument") or {}
    out: list[str] = [
        f"# Backtest research report — {result.get('symbol', '?')}",
        "",
        f"- Job id: `{result.get('job_id', '')}`",
        f"- Status: **{result.get('status', '')}**",
        f"- Requested symbol: {result.get('symbol', '')}"
        f" (market: {result.get('market') or 'resolved'})",
        f"- Instrument: {inst.get('instrument_type', 'n/a')}",
        f"- Exchange / venue: {inst.get('exchange', 'n/a')}",
        f"- Quote currency: {inst.get('quote_currency', 'n/a')}",
        f"- Asset class: {inst.get('asset_class', 'n/a')}",
        f"- Created: {result.get('created_at', '')}",
        f"- Runtime: {_num(result.get('runtime_s'), 1)} s, "
        f"peak RSS {_num(result.get('ram_peak_mb'), 0)} MB",
        "",
        "## 1. Data coverage (verified from returned timestamps)",
        "",
        "| Timeframe | Status | Provider | Instrument | Earliest | Latest | Days | Candles | Missing | Duplicates |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for tf, rep in (result.get("coverage") or {}).items():
        out.append(
            f"| {tf} | {rep.get('status')} | {rep.get('provider')} | "
            f"{rep.get('instrument_type')} | {str(rep.get('earliest'))[:10]} | "
            f"{str(rep.get('latest'))[:10]} | {rep.get('coverage_days')} | "
            f"{rep.get('candles')} | "
            f"{rep.get('missing_candles') if rep.get('missing_candles') is not None else 'n/a (session market)'} | "
            f"{rep.get('duplicates')} |")
    out += [""]

    if result.get("timeframes_rejected"):
        out += ["### Rejected timeframes", ""]
        for item in result["timeframes_rejected"]:
            out.append(f"- **{item.get('timeframe')}** — {item.get('reason')}")
        out += [""]

    if result.get("coverage_notes"):
        out += ["### Coverage notes", ""]
        for note in result["coverage_notes"]:
            out.append(f"- {note}")
        out += [""]

    out += ["## 2. Strategy lab", "",
            f"Strategies available for this market/timeframe set: "
            f"{', '.join(s.get('key', '') for s in result.get('strategies') or [])}",
            "",
            "| Strategy | Timeframe | Status | Params | OOS return | OOS Sharpe | OOS max DD | Trades | R:R | Stability |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for cand in result.get("candidates") or []:
        oos = cand.get("oos") or {}
        out.append(
            f"| {cand.get('strategy')} | {cand.get('timeframe')} | "
            f"{cand.get('status')} | `{cand.get('params')}` | "
            f"{_pct(oos.get('net_return'))} | {_num(oos.get('sharpe'))} | "
            f"{_pct(oos.get('max_drawdown'))} | {oos.get('trades', 0)} | "
            f"{_rr(oos.get('risk_reward'))} | "
            f"{_num(cand.get('stability'), 2)} |")
    out += [""]

    for cand in result.get("candidates") or []:
        out += [
            f"### {cand.get('strategy')} / {cand.get('timeframe')}",
            "",
            f"- Version: {cand.get('version')} — {cand.get('name')}",
            f"- Status: {cand.get('status')} — {cand.get('reason')}",
            f"- Parameters: `{cand.get('params')}`",
            f"- Configurations tested: {cand.get('tested')} of grid "
            f"{cand.get('grid_size')} (failures: {cand.get('failures')})",
            f"- Train / validation / out-of-sample metrics:",
            f"  - train: {_pct((cand.get('train') or {}).get('net_return'))} "
            f"return, Sharpe {_num((cand.get('train') or {}).get('sharpe'))}",
            f"  - validation: {_pct((cand.get('validation') or {}).get('net_return'))} "
            f"return, Sharpe {_num((cand.get('validation') or {}).get('sharpe'))}",
            f"  - out-of-sample: {_pct((cand.get('oos') or {}).get('net_return'))} "
            f"return, Sharpe {_num((cand.get('oos') or {}).get('sharpe'))}, "
            f"max DD {_pct((cand.get('oos') or {}).get('max_drawdown'))}, "
            f"trades {(cand.get('oos') or {}).get('trades', 0)}, "
            f"R:R {_rr((cand.get('oos') or {}).get('risk_reward'))}",
            f"- Buy & hold (same out-of-sample window): "
            f"{_pct((cand.get('benchmark_oos') or {}).get('net_return'))}",
            f"- Walk-forward: {cand.get('walk_forward', {}).get('fold_count', 0)} folds, "
            f"consistency {cand.get('walk_forward', {}).get('consistency', 'n/a')}",
        ]
        if cand.get("multiple_testing_risk"):
            out.append("- ⚠ multiple-testing risk flagged for this candidate")
        out += [""]

    out += [
        "## 3. Selection protocol",
        "",
        "- Chronological split: 60% train → 20% validation → 20% untouched test.",
        "- Parameter search happened ONLY on the training slice; every tested "
        "configuration is listed in `experiments.csv`.",
        "- The champion per strategy was chosen on the validation slice with a "
        "parameter-stability term, then run ONCE on the untouched test slice.",
        "- Walk-forward folds live inside train+validation; the test slice is "
        "never used for tuning or stability.",
        "",
        "## 4. Costs and execution assumptions",
        "",
        f"- Capital: {result.get('capital')}, risk per trade: "
        f"{result.get('risk_pct')}% of equity, max leverage: "
        f"{result.get('max_leverage')}",
        "- Signal at bar close → fill at the NEXT bar open, with adverse "
        "slippage plus half-spread.",
        "- Stops are checked intrabar with stop-first pessimism; a gap through "
        "the stop fills at the open.",
        "- Fees are charged on entry and exit notional; funding is charged on "
        "perpetual positions at real funding timestamps when history exists.",
        f"- Cost table: `{result.get('costs')}`",
        "",
        "## 5. Limitations and statistical uncertainty",
        "",
    ]
    for lim in result.get("limitations") or []:
        out.append(f"- {lim}")
    out += [""]
    if result.get("warnings"):
        out += ["## 6. Warnings", ""]
        out += [f"- {w}" for w in result["warnings"]]
        out += [""]
    out += [
        "## 7. Reproducibility",
        "",
        "Re-run the identical research with:",
        "",
        "```bash",
        f".venv/bin/python scripts/backtest_run.py {result.get('symbol', '')} "
        f"--market {result.get('market') or 'auto'} --no-notify",
        "```",
        "",
        "Provider data is cached under `memory/backtest/cache/` with a metadata "
        "sidecar (rows, first/last timestamp, digest) so the same candles can "
        "be replayed offline.",
        "",
        f"_{DISCLAIMER}_",
        "",
    ]
    return "\n".join(out)


def _json_public(result: dict[str, Any]) -> dict[str, Any]:
    """A JSON-safe copy: pandas objects become summaries, trades capped."""
    def clean(value: Any) -> Any:
        if isinstance(value, pd.Series):
            return {"points": int(len(value)),
                    "first": value.index[0].isoformat() if len(value) else "",
                    "last": value.index[-1].isoformat() if len(value) else "",
                    "final": float(value.iloc[-1]) if len(value) else None}
        if isinstance(value, pd.DataFrame):
            return {"rows": int(len(value))}
        if isinstance(value, dict):
            return {str(k): clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [clean(v) for v in list(value)[:2000]]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)

    public = clean(result)
    for cand in public.get("candidates") or []:
        cand.pop("equity", None)
    return public


def write_artifacts(result: dict[str, Any]) -> dict[str, str]:
    """Write every artifact for one job; returns the path map."""
    job_id = str(result.get("job_id") or "job")
    d = report_dir(job_id)
    paths: dict[str, str] = {}

    result_path = d / "result.json"
    write_json(result_path, _json_public(result))
    paths["result_json"] = str(result_path)

    summary_path = d / "summary.md"
    paths["summary_md"] = str(summary_path)

    report_path = d / "report.md"
    paths["report_md"] = str(report_path)

    # Publish the paths before rendering so a first-write summary (for example
    # an INSUFFICIENT_DATA result) still links to its own artifacts.
    result["paths"] = dict(paths)
    summary_path.write_text(render_summary(result), encoding="utf-8")
    report_path.write_text(render_detailed(result), encoding="utf-8")

    rows: list[dict[str, Any]] = []
    for cand in result.get("candidates") or []:
        for exp in cand.get("experiments") or []:
            row = {
                "strategy": cand.get("strategy"), "version": cand.get("version"),
                "timeframe": cand.get("timeframe"),
                "provider": cand.get("provider"),
                "market": cand.get("market"),
                "params": str(exp.get("params")),
                "ok": exp.get("ok"),
                "error": exp.get("error", ""),
            }
            for split_name in ("train", "validation"):
                metrics = exp.get(split_name) or {}
                for key, value in metrics.items():
                    if key == "monthly_returns":
                        continue
                    row[f"{split_name}_{key}"] = value
            row["train_score"] = exp.get("train_score")
            row["validation_score"] = exp.get("validation_score")
            row["combined_score"] = exp.get("combined_score")
            row["stability"] = exp.get("stability")
            rows.append(row)
    if rows:
        exp_path = d / "experiments.csv"
        pd.DataFrame(rows).to_csv(exp_path, index=False)
        paths["experiments_csv"] = str(exp_path)

    # Champion + shortlist trade and equity exports.
    for cand in result.get("candidates") or []:
        if cand.get("status") != "CANDIDATE":
            continue
        tag = f"{cand.get('strategy')}_{cand.get('timeframe')}"
        trades = cand.get("trades") or []
        if trades:
            t_path = d / f"trades_{tag}.csv"
            pd.DataFrame(trades).to_csv(t_path, index=False)
            paths[f"trades_{tag}"] = str(t_path)
        equity = cand.get("equity")
        if isinstance(equity, pd.Series) and len(equity):
            e_path = d / f"equity_{tag}.csv"
            equity.rename("equity").to_csv(e_path)
            paths[f"equity_{tag}"] = str(e_path)

    result["paths"] = paths
    write_json(result_path, _json_public(result))   # re-write with paths
    return paths


def read_report(job_id: str, kind: str = "summary") -> str:
    d = report_dir(job_id)
    name = "report.md" if kind == "detailed" else "summary.md"
    try:
        return (d / name).read_text(encoding="utf-8")
    except Exception:
        return ""
