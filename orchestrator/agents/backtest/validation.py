"""
orchestrator/agents/backtest/validation.py — chronological tuning and ranking.

The protocol is fixed before any result is seen:

    1. TRAIN      the first 60% of bars — the ONLY slice parameter search may
                  look at. Every tested configuration is recorded.
    2. VALIDATION the next 20% — used to choose among the top training
                  candidates and to measure parameter stability.
    3. TEST       the final 20% — untouched until one champion per strategy is
                  chosen. Reported once, never used for selection.

Walk-forward folds are built inside train+validation only; the test slice is
never part of a tuning or stability decision.

Ranking rewards out-of-sample risk-adjusted performance, stability in the
parameter neighbourhood and positive expectancy — not the highest in-sample
return. Multiple-testing risk is flagged when the experiment count is large
relative to the number of out-of-sample trades.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import pandas as pd

from orchestrator.agents.backtest.engine import (
    BacktestError, Costs, bars_per_year_for, buy_and_hold, run_backtest,
)
from orchestrator.agents.backtest.strategies import library


# ── score helpers ────────────────────────────────────────────────────────────

def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


def robust_score(metrics: dict[str, Any], *, stability: float = 0.0,
                 complexity: int = 0, min_trades: int = 10) -> float:
    """A deliberately conservative composite. Missing metrics score low."""
    if not metrics or int(metrics.get("trades") or 0) < min_trades:
        return -1e9
    sharpe = _clamp(metrics.get("sharpe") or 0.0, -2.0, 4.0)
    sortino = _clamp(metrics.get("sortino") or 0.0, -2.0, 5.0)
    ret = _clamp(metrics.get("net_return") or 0.0, -1.0, 3.0)
    dd = _clamp(metrics.get("max_drawdown") or 0.0, 0.0, 1.0)
    pf = metrics.get("profit_factor")
    pf_term = _clamp((float(pf) - 1.0) if pf is not None else -0.5, -1.0, 2.0)
    return round(0.55 * sharpe + 0.20 * sortino + 0.35 * ret
                 - 0.90 * dd + 0.20 * pf_term
                 + 0.35 * _clamp(stability, 0.0, 1.0)
                 - 0.02 * complexity, 4)


# ── splits ───────────────────────────────────────────────────────────────────

@dataclass
class Split:
    n: int
    train_end: int
    val_end: int

    @property
    def train(self) -> tuple[int, int]:
        return 0, self.train_end

    @property
    def validation(self) -> tuple[int, int]:
        return self.train_end, self.val_end

    @property
    def test(self) -> tuple[int, int]:
        return self.val_end, self.n

    def sizes(self) -> dict[str, int]:
        return {"train": self.train_end, "validation": self.val_end - self.train_end,
                "test": self.n - self.val_end}

    def describe(self, df: pd.DataFrame) -> dict[str, Any]:
        def rng(a: int, b: int) -> dict[str, str]:
            if b <= a or b > len(df):
                return {"start": "", "end": "", "bars": 0}
            return {"start": df.index[a].isoformat(),
                    "end": df.index[min(b - 1, len(df) - 1)].isoformat(),
                    "bars": int(b - a)}
        return {"train": rng(*self.train), "validation": rng(*self.validation),
                "test": rng(*self.test), "sizes": self.sizes()}


def make_split(n: int, train_frac: float = 0.60, val_frac: float = 0.20,
               min_segment_bars: int = 200) -> Split | None:
    """Chronological split with a floor that can fit a one-year daily series.

    The configured floor applies to intraday samples. For a short daily sample
    (the hard data floor is 365 bars) a fixed 200-bar validation/test slice
    would reject the whole dataset, so the floor scales down to 10% of the
    sample, never below 30 bars. The out-of-sample gate still requires a real
    number of trades, so a tiny test slice cannot masquerade as evidence.
    """
    train_end = int(n * train_frac)
    val_end = train_end + int(n * val_frac)
    floor_bars = min(int(min_segment_bars), max(30, n // 10))
    if train_end < floor_bars:
        return None
    if (val_end - train_end) < floor_bars:
        return None
    if (n - val_end) < floor_bars:
        return None
    return Split(n=n, train_end=train_end, val_end=val_end)


# ── one configuration ────────────────────────────────────────────────────────

def evaluate(df: pd.DataFrame, spec: library.StrategySpec, params: dict,
             *, timeframe: str, ctx: library.StrategyContext,
             costs: Costs, capital: float, risk_pct: float,
             max_leverage: float, trade_start_idx: int = 0,
             multiplier: float = 1.0, funding=None) -> dict[str, Any]:
    signals = spec.build(df, params, ctx)
    bp = bars_per_year_for(df, timeframe)
    res = run_backtest(
        df, signals, timeframe=timeframe, costs=costs, capital=capital,
        risk_pct=risk_pct, max_leverage=max_leverage,
        allow_short=ctx.allow_short, trade_start_idx=trade_start_idx,
        time_stop_bars=spec.time_stop_bars, target_r=spec.target_r,
        multiplier=multiplier, funding=funding, bars_per_year=bp)
    if not res.ok:
        return {"ok": False, "error": res.error, "metrics": {}}
    return {"ok": True, "metrics": res.metrics, "trades": res.trades,
            "equity": res.equity, "meta": res.meta}


# ── tuning + selection for one strategy/timeframe ────────────────────────────

def research_strategy(df: pd.DataFrame, spec: library.StrategySpec,
                      *, timeframe: str, ctx: library.StrategyContext,
                      costs: Costs, cfg, multiplier: float = 1.0,
                      funding=None, budget: dict | None = None
                      ) -> dict[str, Any]:
    """Tune one strategy on train, choose on validation, report test once."""
    budget = budget or {}
    capital = float(cfg.get("capital", 10_000))
    risk_pct = float(cfg.get("risk_pct", 1.0))
    max_lev = float(cfg.market_limit("max_leverage", ctx.market_class, 1.0))
    split = make_split(len(df), float(cfg.get("train_frac", 0.60)),
                       float(cfg.get("validation_frac", 0.20)),
                       int(cfg.get("min_segment_bars", 200)))
    base = {
        "strategy": spec.key, "name": spec.name, "version": spec.version,
        "timeframe": timeframe, "market_class": ctx.market_class,
        "status": "NOT_EVALUATED", "reason": "",
    }
    if split is None:
        base.update({"status": "REJECTED",
                     "reason": "not enough bars for a train/validation/test split"})
        return base

    limit = int(cfg.get("max_configs_per_combo", 8))
    grid = spec.param_sets(limit=limit)
    full_grid = 1
    for values in spec.param_grid.values():
        full_grid *= max(1, len(values))
    min_train = int(cfg.get("min_trades_train", 20))
    min_val = int(cfg.get("min_trades_validation", 10))
    min_oos = int(cfg.get("min_trades_oos", 10))
    max_experiments = int(budget.get("remaining", 10**9))

    experiments: list[dict[str, Any]] = []
    failures = 0
    for params in grid:
        if len(experiments) >= max_experiments:
            break
        try:
            # Each phase gets its own frozen frame: the training replay stops at
            # train_end, and validation stops at val_end. Passing the full frame
            # with only a start index would let later bars leak into the phase.
            train_df = df.iloc[:split.train_end]
            train = evaluate(train_df, spec, params, timeframe=timeframe, ctx=ctx,
                             costs=costs, capital=capital, risk_pct=risk_pct,
                             max_leverage=max_lev,
                             trade_start_idx=split.train[0],
                             multiplier=multiplier, funding=funding)
        except BacktestError as exc:
            failures += 1
            experiments.append({"params": params, "ok": False,
                                "error": str(exc)[:160]})
            continue
        except Exception as exc:                       # never kill the job
            failures += 1
            experiments.append({"params": params, "ok": False,
                                "error": f"{type(exc).__name__}: {exc}"[:160]})
            continue
        if not train["ok"]:
            failures += 1
            experiments.append({"params": params, "ok": False,
                                "error": train.get("error", "failed")})
            continue
        experiments.append({
            "params": params, "ok": True, "train": train["metrics"],
            "train_score": robust_score(train["metrics"],
                                        min_trades=min_train),
        })

    good = [e for e in experiments if e.get("ok")]
    if not good:
        base.update({"status": "REJECTED",
                     "reason": f"no configuration produced a valid backtest "
                               f"({failures} failures)",
                     "experiments": experiments, "grid_size": full_grid,
                     "tested": len(experiments)})
        return base

    good.sort(key=lambda e: e["train_score"], reverse=True)
    shortlist = good[:3]

    # Validation pass — every tested configuration gets a validation run, so
    # parameter-neighbourhood stability is measured on real evidence. The frame
    # is frozen at val_end: indicators may warm up on train bars, but no trade
    # and no metric may see the untouched test slice.
    for exp in good:
        val_df = df.iloc[:split.val_end]
        val = evaluate(val_df, spec, exp["params"], timeframe=timeframe, ctx=ctx,
                       costs=costs, capital=capital, risk_pct=risk_pct,
                       max_leverage=max_lev,
                       trade_start_idx=split.validation[0],
                       multiplier=multiplier, funding=funding)
        exp["validation"] = val.get("metrics", {}) if val.get("ok") else {}
        exp["validation_ok"] = bool(val.get("ok"))
        exp["validation_score"] = robust_score(exp.get("validation") or {},
                                               min_trades=min_val)

    # Parameter stability: tested neighbours differing in exactly one parameter.
    for exp in good:
        exp["stability"] = _stability(exp, good)

    for exp in good:
        exp["combined_score"] = round(
            exp.get("validation_score", -1e9)
            + 0.35 * exp.get("stability", 0.0), 4)
    good.sort(key=lambda e: e["combined_score"], reverse=True)
    shortlist = good[:3]
    chosen = shortlist[0]
    if int((chosen.get("validation") or {}).get("trades") or 0) < min_val:
        chosen = next((e for e in shortlist
                       if int((e.get("validation") or {}).get("trades") or 0)
                       >= min_val), chosen)

    # Final untouched out-of-sample run — exactly once for the champion.
    oos = evaluate(df, spec, chosen["params"], timeframe=timeframe, ctx=ctx,
                   costs=costs, capital=capital, risk_pct=risk_pct,
                   max_leverage=max_lev, trade_start_idx=split.test[0],
                   multiplier=multiplier, funding=funding)
    oos_metrics = oos.get("metrics", {}) if oos.get("ok") else {}
    benchmark = buy_and_hold(df, trade_start_idx=split.test[0], capital=capital,
                             costs=costs, multiplier=multiplier)

    wf = _walk_forward(df, spec, chosen["params"], split=split, ctx=ctx,
                       timeframe=timeframe, costs=costs, capital=capital,
                       risk_pct=risk_pct, max_leverage=max_lev,
                       cfg=cfg, multiplier=multiplier, funding=funding)

    oos_score = robust_score(oos_metrics, stability=chosen.get("stability", 0.0),
                             complexity=len(spec.param_grid), min_trades=min_oos)
    passed, failures_list = _passes(oos_metrics, cfg, min_oos)
    if not oos.get("ok"):
        failures_list.append(f"out-of-sample run failed: {oos.get('error')}")
    stability = float(chosen.get("stability", 0.0))
    if stability < float(cfg.get("stability_neighbour_min", 0.5)):
        failures_list.append(
            f"parameter neighbourhood unstable ({stability:.2f} of neighbours "
            f"profitable on validation)")

    base.update({
        "status": "CANDIDATE" if (passed and not failures_list) else "REJECTED",
        "reason": "; ".join(failures_list) if failures_list else
                  "passed every configured out-of-sample gate",
        "params": chosen["params"],
        "grid_size": full_grid,
        "tested": len(experiments),
        "failures": failures,
        "train": chosen.get("train", {}),
        "validation": chosen.get("validation", {}),
        "oos": oos_metrics,
        "benchmark_oos": benchmark,
        "trades": oos.get("trades") or [],
        "equity": oos.get("equity"),
        "oos_score": oos_score,
        "stability": round(stability, 3),
        "walk_forward": wf,
        "experiments": experiments,
        "shortlist": [{k: e.get(k) for k in
                       ("params", "train_score", "validation_score",
                        "combined_score", "stability", "validation")}
                      for e in shortlist],
        "complexity": len(spec.param_grid),
        "selection": "train → shortlist → validation → single OOS test",
    })
    if len(experiments) > int(cfg.get("multiple_testing_flag_at", 50)) and \
            int(oos_metrics.get("trades") or 0) < int(
                cfg.get("multiple_testing_min_trades", 30)):
        base["multiple_testing_risk"] = True
        base["reason"] += ("; multiple-testing risk: many configurations were "
                           "tried relative to the out-of-sample trade count")
    return base


def _passes(metrics: dict[str, Any], cfg, min_trades: int
            ) -> tuple[bool, list[str]]:
    if not metrics:
        return False, ["no out-of-sample metrics"]
    fails: list[str] = []
    if int(metrics.get("trades") or 0) < min_trades:
        fails.append(f"out-of-sample trades {metrics.get('trades', 0)} "
                     f"< required {min_trades}")
    if bool(cfg.get("require_positive_oos_return", True)) and \
            float(metrics.get("net_return") or 0) <= 0:
        fails.append(f"out-of-sample net return "
                     f"{float(metrics.get('net_return') or 0):.2%} ≤ 0")
    if float(metrics.get("sharpe") or 0) < float(cfg.get("min_oos_sharpe", 0.3)):
        fails.append(f"out-of-sample Sharpe {metrics.get('sharpe')} < "
                     f"{cfg.get('min_oos_sharpe')}")
    if float(metrics.get("max_drawdown") or 0) > float(cfg.get("max_drawdown", 0.5)):
        fails.append(f"out-of-sample drawdown "
                     f"{float(metrics.get('max_drawdown') or 0):.1%} > "
                     f"{float(cfg.get('max_drawdown', 0.5)):.0%}")
    pf = metrics.get("profit_factor")
    if pf is not None and float(pf) < float(cfg.get("min_profit_factor", 1.05)):
        fails.append(f"out-of-sample profit factor {pf} < "
                     f"{cfg.get('min_profit_factor')}")
    return (not fails), fails


def _stability(exp: dict, good: list[dict]) -> float:
    """Fraction of one-parameter neighbours whose validation score is positive.

    Neighbours are configurations actually tested, so the number is evidence
    from this dataset — not an assumption about parameter smoothness.
    """
    p = exp.get("params") or {}
    neighbours = []
    for other in good:
        if other is exp:
            continue
        q = other.get("params") or {}
        diff = [k for k in p if p.get(k) != q.get(k)]
        if len(diff) == 1 and set(p) == set(q):
            neighbours.append(other)
    scored = [n for n in neighbours if n.get("validation")]
    if not scored:
        return 0.0
    positive = sum(1 for n in scored
                   if float((n.get("validation") or {}).get("net_return") or 0) > 0)
    return round(positive / len(scored), 3)


def _walk_forward(df: pd.DataFrame, spec: library.StrategySpec, params: dict,
                  *, split: Split, ctx: library.StrategyContext,
                  timeframe: str, costs: Costs, capital: float,
                  risk_pct: float, max_leverage: float, cfg, multiplier: float,
                  funding=None) -> dict[str, Any]:
    wf_cfg = cfg.get("walk_forward", {}) or {}
    if not wf_cfg.get("enabled", True):
        return {"enabled": False, "reason": "disabled in config"}
    train_bars = int(wf_cfg.get("train_bars", 500))
    test_bars = int(wf_cfg.get("test_bars", 125))
    limit_end = split.val_end            # never touches the test slice
    if limit_end < train_bars + test_bars:
        return {"enabled": False,
                "reason": f"train+validation has {limit_end} bars; needs "
                          f"≥{train_bars + test_bars}"}
    folds: list[dict[str, Any]] = []
    start = 0
    while start + train_bars + test_bars <= limit_end and len(folds) < 12:
        test_start = start + train_bars
        res = evaluate(df.iloc[:test_start + test_bars], spec, params,
                       timeframe=timeframe, ctx=ctx, costs=costs,
                       capital=capital, risk_pct=risk_pct,
                       max_leverage=max_leverage, trade_start_idx=test_start,
                       multiplier=multiplier, funding=funding)
        m = res.get("metrics", {}) if res.get("ok") else {}
        folds.append({
            "test_start": df.index[test_start].isoformat(),
            "test_end": df.index[min(test_start + test_bars - 1, len(df) - 1)].isoformat(),
            "return": m.get("net_return"), "sharpe": m.get("sharpe"),
            "max_drawdown": m.get("max_drawdown"), "trades": m.get("trades", 0),
        })
        start += test_bars
    if not folds:
        return {"enabled": False, "reason": "no complete folds"}
    returns = [f["return"] for f in folds if f["return"] is not None]
    return {
        "enabled": True, "folds": folds, "fold_count": len(folds),
        "positive_folds": sum(1 for r in returns if r > 0),
        "consistency": round(sum(1 for r in returns if r > 0) / len(returns), 3)
        if returns else 0.0,
        "median_return": round(float(pd.Series(returns).median()), 5)
        if returns else None,
        "window": {"train_bars": train_bars, "test_bars": test_bars},
    }
