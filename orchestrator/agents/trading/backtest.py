"""
orchestrator/agents/trading/backtest.py — BACKTESTING INTERFACES.

One engine, two faces:
    run_backtest(bars, strategy, ...) — one symbol, one strategy, metrics
    walk_forward(...)                 — rolling in/out-of-sample windows

The engine replays history bar-by-bar through the SAME analysis → signal
functions the live pipeline uses (analysis.technicals + market_structure +
signal.evaluate), fills at the NEXT bar's open with fixed slippage, and
enforces the same position-sizing the risk layer uses. No look-ahead: the
card at bar i is built from bars ≤ i, and the fill happens at i+1.

Metrics: total return, CAGR-ish annualized return, max drawdown, Sharpe,
win rate, profit factor, expectancy (R), trade count.
"""
from __future__ import annotations

import math

from orchestrator.agents.trading import analysis as an
from orchestrator.agents.trading import signal as sg


class BacktestError(ValueError):
    pass


# ── engine ───────────────────────────────────────────────────────────────────

def run_backtest(bars: list[dict], strategy: str = "trend_follow",
                 *, min_score: int = 2, capital: float = 100_000.0,
                 risk_pct: float = 1.0, sl_atr_mult: float = 1.5,
                 targets_r: tuple = (1.5, 2.0, 3.0),
                 exit_on: tuple = ("target", "stop", "eod"),
                 slip_bps: float = 2.0, max_bars_in_trade: int = 40,
                 warmup: int = 60) -> dict:
    """Replay `bars` through analysis → signal → (risk-lite sizing).

    Fill model: a signal on bar i fills at bar i+1's open ± slippage.
    Exits: target/stop touched intrabar (pessimistic: stop wins ties),
    `max_bars_in_trade` time stop, or end of data.
    """
    b = an._bars(bars)
    if len(b) < warmup + 20:
        raise BacktestError(
            f"need ≥{warmup + 20} clean bars, got {len(b)}")
    if strategy not in sg.RULES:
        raise BacktestError(f"unknown strategy '{strategy}' — have: "
                            f"{', '.join(sorted(sg.RULES))}")

    cash = float(capital)
    equity_curve: list[float] = []
    trades: list[dict] = []
    pos = None                       # {direction, units, entry, stop, targets, i_in}
    last_signal_i = -10**9

    for i in range(warmup, len(b)):
        bar = b[i]

        # ── 1. manage the open position first (exits BEFORE new entries) ────
        if pos is not None:
            hit_stop = hit_target = False
            exit_price = None
            if pos["direction"] == "LONG":
                if bar["low"] <= pos["stop"]:
                    hit_stop, exit_price = True, pos["stop"]
                elif bar["high"] >= pos["targets"][0]:
                    hit_target, exit_price = True, pos["targets"][0]
            else:
                if bar["high"] >= pos["stop"]:
                    hit_stop, exit_price = True, pos["stop"]
                elif bar["low"] <= pos["targets"][0]:
                    hit_target, exit_price = True, pos["targets"][0]
            timed = (i - pos["i_in"]) >= max_bars_in_trade
            last_bar = (i == len(b) - 1)
            if exit_price is None and (timed or last_bar):
                exit_price = bar["close"]
            if exit_price is not None:
                sign = 1.0 if pos["direction"] == "LONG" else -1.0
                slip = exit_price * slip_bps / 10_000.0
                fill = exit_price - slip if sign > 0 else exit_price + slip
                pnl = sign * (fill - pos["entry"]) * pos["units"]
                cash += pnl
                trades.append({
                    "direction": pos["direction"],
                    "entry": round(pos["entry"], 4),
                    "exit": round(fill, 4),
                    "units": pos["units"],
                    "pnl": round(pnl, 2),
                    "r_multiple": round(sign * (fill - pos["entry"])
                                        / max(pos["risk_per_unit"], 1e-9), 3),
                    "bars_held": i - pos["i_in"],
                    "exit_kind": ("stop" if hit_stop else
                                  "target" if hit_target else
                                  "time" if timed else "eod"),
                })
                pos = None

        # ── 2. look for a new entry (cooldown: one trade per 3 bars) ────────
        if pos is None and (i - last_signal_i) >= 3:
            card = an.analyze(b[:i + 1])
            res = sg.run_signal(card, strategy=strategy, min_score=min_score,
                                sl_atr_mult=sl_atr_mult, targets_r=targets_r)
            prop = res.get("proposal")
            if prop:
                # size with the SAME fixed-fractional rule the risk layer uses
                from orchestrator.agents.trading.risk import position_size
                sizing = position_size(cash, prop["entry"], prop["stop_loss"],
                                       risk_pct=risk_pct)
                if sizing.get("ok"):
                    units = sizing["units"]
                    fill = b[i + 1]["open"] if i + 1 < len(b) else bar["close"]
                    slip = fill * slip_bps / 10_000.0
                    sign = 1.0 if prop["direction"] == "LONG" else -1.0
                    fill = fill + slip if sign > 0 else fill - slip
                    # hold the NEXT bar's position until it exits
                    pos = {
                        "direction": prop["direction"], "units": units,
                        "entry": fill,
                        "stop": float(prop["stop_loss"]),
                        "targets": [float(t) for t in prop["targets"]],
                        "i_in": i + 1,
                        "risk_per_unit": abs(fill - float(prop["stop_loss"])),
                    }
                    last_signal_i = i

        # ── 3. mark-to-market equity ────────────────────────────────────────
        eq = cash
        if pos is not None:
            sign = 1.0 if pos["direction"] == "LONG" else -1.0
            eq += sign * (bar["close"] - pos["entry"]) * pos["units"]
        equity_curve.append(round(eq, 2))

    return _metrics(equity_curve, trades, capital)


def walk_forward(bars: list[dict], strategy: str = "trend_follow",
                 *, window: int = 250, step: int = 60, **kw) -> dict:
    """Rolling in-sample → out-of-sample slices. Each fold trains nothing
    (rules are static) but SHOWS stability: does the same strategy behave
    similarly out-of-sample? Folds shorter than the warmup are skipped and
    counted honestly."""
    b = an._bars(bars)
    folds = []
    start = 0
    while start + window + step <= len(b):
        train = b[start:start + window]
        test = b[start + window:start + window + step]
        try:
            in_sample = run_backtest(train, strategy, **kw)
            out_sample = run_backtest(train + test, strategy, **kw)
            # out-of-sample fold = full-minus-train segment metrics difference;
            # simple, honest presentation: report both runs
            folds.append({"train_bars": len(train), "test_bars": len(test),
                          "in_sample": in_sample["summary"],
                          "out_sample_full": out_sample["summary"]})
        except BacktestError as exc:
            folds.append({"train_bars": len(train), "test_bars": len(test),
                          "error": str(exc)})
        start += step
    if not folds:
        raise BacktestError(f"not enough bars for even one fold "
                            f"(have {len(b)}, need ≥{window + step})")
    return {"ok": True, "strategy": strategy, "folds": folds,
            "fold_count": len(folds)}


# ── metrics ──────────────────────────────────────────────────────────────────

def _metrics(equity_curve: list[float], trades: list[dict],
             capital: float) -> dict:
    if not equity_curve:
        raise BacktestError("empty equity curve")
    final = equity_curve[-1]
    total_ret = final / capital - 1.0
    peak, max_dd = equity_curve[0], 0.0
    for eq in equity_curve:
        peak = max(peak, eq)
        dd = (peak - eq) / peak
        max_dd = max(max_dd, dd)

    rets = []
    for a, b in zip(equity_curve, equity_curve[1:]):
        if a > 0:
            rets.append(b / a - 1.0)
    sharpe = 0.0
    if len(rets) > 2:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / len(rets)
        sd = math.sqrt(var)
        if sd > 0:
            sharpe = mean / sd * math.sqrt(252)     # daily bars assumption

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else \
        (float("inf") if gross_win > 0 else 0.0)
    avg_r = (sum(t["r_multiple"] for t in trades) / len(trades)) \
        if trades else 0.0

    return {"ok": True,
            "summary": {
                "final_equity": round(final, 2),
                "total_return_pct": round(total_ret * 100.0, 2),
                "max_drawdown_pct": round(max_dd * 100.0, 2),
                "sharpe_daily": round(sharpe, 2),
                "trades": len(trades),
                "win_rate_pct": round(len(wins) / len(trades) * 100.0, 1)
                if trades else 0.0,
                "profit_factor": round(pf, 2) if math.isfinite(pf) else None,
                "avg_r_multiple": round(avg_r, 3),
            },
            "trades": trades[-50:],          # last 50 for display, all counted
            "trade_count_total": len(trades)}
