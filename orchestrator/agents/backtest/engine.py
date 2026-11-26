"""
orchestrator/agents/backtest/engine.py — the execution simulator.

TIMING (no look-ahead, no impossible fills)
    signal at bar i close  →  order filled at bar i+1 OPEN, with adverse
    slippage + half-spread. Stops are checked intrabar with stop-first
    pessimism when a bar touches both stop and target. A target cannot fill on
    the entry bar (OHLC cannot prove the high came after the open); a stop can,
    because assuming otherwise would hide risk. Gaps through a stop fill at the
    open, not at the stop price.

ACCOUNTING
    equity = cash + unrealised P&L
    notional at entry ≤ equity × max_leverage   (capital constraint)
    risk at entry     = equity × risk_pct        (fixed-fractional sizing)
    fees are charged on entry and exit notional; funding is charged on perp
    positions at real funding timestamps when history is available.

This module has no broker, no account and no order endpoint. It cannot place a
live order.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from orchestrator.agents.backtest.providers.base import tf_seconds


class BacktestError(ValueError):
    pass


@dataclass(frozen=True)
class Costs:
    fee_bps: float = 5.0
    slip_bps: float = 3.0
    spread_bps: float = 2.0

    @property
    def half_spread_bps(self) -> float:
        return self.spread_bps / 2.0

    def adverse(self) -> float:
        return (self.slip_bps + self.half_spread_bps) / 10_000.0

    def fee_rate(self) -> float:
        return self.fee_bps / 10_000.0

    def describe(self) -> dict[str, float]:
        return {"fee_bps": self.fee_bps, "slip_bps": self.slip_bps,
                "spread_bps": self.spread_bps,
                "round_trip_cost_bps": round(2 * (self.fee_bps +
                                                  self.slip_bps +
                                                  self.half_spread_bps), 2)}


@dataclass
class BacktestResult:
    ok: bool
    metrics: dict[str, Any] = field(default_factory=dict)
    trades: list[dict] = field(default_factory=list)
    equity: pd.Series = field(default_factory=lambda: pd.Series(dtype="float64"))
    error: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


def _max_drawdown(equity: np.ndarray) -> float:
    if len(equity) == 0:
        return 0.0
    peak = np.maximum.accumulate(equity)
    dd = (peak - equity) / np.maximum(peak, 1e-12)
    return float(np.max(dd))


def _sharpe(returns: np.ndarray, bars_per_year: float) -> float:
    if len(returns) < 3:
        return 0.0
    sd = float(np.std(returns, ddof=1))
    if sd <= 0:
        return 0.0
    return float(np.mean(returns) / sd * math.sqrt(max(bars_per_year, 1.0)))


def _sortino(returns: np.ndarray, bars_per_year: float) -> float:
    if len(returns) < 3:
        return 0.0
    downside = returns[returns < 0]
    if len(downside) == 0:
        return 0.0
    dd = float(np.std(downside, ddof=1)) if len(downside) > 1 else \
        float(abs(downside[0]))
    if dd <= 0:
        return 0.0
    return float(np.mean(returns) / dd * math.sqrt(max(bars_per_year, 1.0)))


def run_backtest(df: pd.DataFrame, signals: pd.DataFrame, *, timeframe: str,
                 costs: Costs, capital: float = 10_000.0, risk_pct: float = 1.0,
                 max_leverage: float = 1.0, allow_short: bool = False,
                 trade_start_idx: int = 0, time_stop_bars: int = 0,
                 target_r: float = 2.0, multiplier: float = 1.0,
                 funding: pd.Series | None = None,
                 bars_per_year: float | None = None) -> BacktestResult:
    """Replay one configuration. Never mutates the input frames."""
    n = len(df)
    if n < 3:
        return BacktestResult(ok=False, error="not enough bars")
    if len(signals) != n:
        return BacktestResult(ok=False, error="signals and candles misaligned")

    warmup = int(signals["warmup"].iloc[0]) if "warmup" in signals.columns else 0
    start_i = max(1, int(trade_start_idx), warmup)
    if start_i >= n - 1:
        return BacktestResult(ok=False,
                              error=f"no tradable bars after warm-up "
                                    f"({start_i} of {n})")

    o = df["open"].to_numpy(dtype=np.float64)
    h = df["high"].to_numpy(dtype=np.float64)
    l = df["low"].to_numpy(dtype=np.float64)
    c = df["close"].to_numpy(dtype=np.float64)
    entry_long = signals["long_entry"].fillna(False).to_numpy(dtype=bool)
    entry_short = signals["short_entry"].fillna(False).to_numpy(dtype=bool)
    exit_long = signals["long_exit"].fillna(False).to_numpy(dtype=bool)
    exit_short = signals["short_exit"].fillna(False).to_numpy(dtype=bool)
    stop_dist = signals["stop_distance"].to_numpy(dtype=np.float64)
    tgt_r = signals["target_r"].to_numpy(dtype=np.float64)
    idx = df.index

    if bars_per_year is None:
        span_s = max((idx[-1] - idx[0]).total_seconds(), 1.0)
        bars_per_year = max(len(df) / (span_s / (365.25 * 86400)), 1.0)

    adverse = costs.adverse()
    fee_rate = costs.fee_rate()

    cash = float(capital)
    units = 0.0
    direction = 0
    entry_price = 0.0
    stop = 0.0
    target = 0.0
    entry_i = -1
    entry_ts = None
    entry_fee = 0.0
    entry_notional = 0.0

    pending_entry = 0            # +1 long, -1 short
    pending_exit = False

    equity_points: list[float] = []
    equity_index: list[Any] = []
    trades: list[dict] = []
    bars_in_market = 0
    traded_notional = 0.0

    fund_ts = None
    fund_rate = None
    if funding is not None and len(funding):
        # Compare in NANOSECONDS explicitly. pandas 3 may infer a non-nano
        # index resolution (e.g. "us" from date_range), and `.asi8` then
        # returns those units — silently breaking the comparison with
        # Timestamp.value, which is always ns.
        fund_ts = funding.index.as_unit("ns").asi8
        fund_rate = funding.to_numpy(dtype=np.float64)

    def _close_position(i: int, price: float, kind: str) -> None:
        nonlocal cash, units, direction, entry_price, stop, target, entry_i
        nonlocal entry_fee, entry_notional, entry_ts, traded_notional
        if units <= 0 or direction == 0:
            return
        fill = price * (1.0 - adverse) if direction > 0 else price * (1.0 + adverse)
        pnl = direction * (fill - entry_price) * units * multiplier
        exit_notional = abs(fill * units * multiplier)
        fee = exit_notional * fee_rate
        cash += pnl - fee
        risk_per_unit = abs(entry_price - stop) * units * multiplier
        trades.append({
            "entry_time": entry_ts.isoformat() if hasattr(entry_ts, "isoformat") else str(entry_ts),
            "exit_time": idx[i].isoformat(),
            "direction": "LONG" if direction > 0 else "SHORT",
            "entry": round(entry_price, 6),
            "exit": round(fill, 6),
            "units": round(units, 8),
            "notional": round(entry_notional, 2),
            "pnl": round(pnl - fee - entry_fee, 2),
            "gross_pnl": round(pnl, 2),
            "fees": round(fee + entry_fee, 2),
            "r_multiple": round((pnl - fee - entry_fee) / risk_per_unit, 3)
            if risk_per_unit > 0 else 0.0,
            "bars_held": int(i - entry_i),
            "exit_kind": kind,
        })
        traded_notional += exit_notional
        units = 0.0
        direction = 0
        entry_price = 0.0
        stop = 0.0
        target = 0.0
        entry_i = -1
        entry_ts = None
        entry_fee = 0.0
        entry_notional = 0.0

    for i in range(start_i, n):
        # ── 1. execute yesterday's orders at today's open ────────────────
        if direction != 0 and pending_exit:
            _close_position(i, o[i], "signal")
            pending_exit = False
        if direction == 0 and pending_entry != 0:
            sig_i = i - 1
            sd = stop_dist[sig_i]
            if not (np.isfinite(sd) and sd > 0) and not (tgt_r[sig_i] == 0.0):
                pending_entry = 0
            else:
                side = pending_entry
                fill = o[i] * (1.0 + adverse) if side > 0 else o[i] * (1.0 - adverse)
                equity_now = cash
                if equity_now <= 0:
                    pending_entry = 0
                else:
                    if np.isfinite(sd) and sd > 0:
                        risk_cash = equity_now * (risk_pct / 100.0)
                        units_by_risk = risk_cash / (sd * multiplier)
                    else:
                        units_by_risk = float("inf")
                    units_by_notional = (equity_now * max_leverage) / max(
                        fill * multiplier, 1e-12)
                    u = min(units_by_risk, units_by_notional)
                    if u <= 0 or not np.isfinite(u) or units_by_risk == 0:
                        pending_entry = 0
                    else:
                        direction = side
                        units = u
                        entry_price = fill
                        entry_i = i
                        entry_ts = idx[i]
                        entry_notional = abs(fill * units * multiplier)
                        entry_fee = entry_notional * fee_rate
                        cash -= entry_fee
                        traded_notional += entry_notional
                        pending_entry = 0
                        if np.isfinite(sd) and sd > 0:
                            stop = fill - side * sd
                            tr = float(tgt_r[sig_i]) if np.isfinite(tgt_r[sig_i]) \
                                else float(target_r)
                            target = fill + side * sd * tr if tr > 0 else float("nan")
                        else:
                            stop = float("nan")
                            target = float("nan")

        # ── 2. intrabar risk management (stop first, then target) ────────
        if direction != 0:
            bars_in_market += 1
            if direction > 0:
                if np.isfinite(stop) and l[i] <= stop:          # stop touched
                    price = min(o[i], stop) if o[i] <= stop else stop
                    _close_position(i, price, "gap_stop" if o[i] <= stop else "stop")
                elif np.isfinite(target) and i > entry_i and h[i] >= target:
                    _close_position(i, target, "target")
                elif time_stop_bars and (i - entry_i) >= time_stop_bars:
                    _close_position(i, c[i], "time")
            else:
                if np.isfinite(stop) and h[i] >= stop:
                    price = max(o[i], stop) if o[i] >= stop else stop
                    _close_position(i, price, "gap_stop" if o[i] >= stop else "stop")
                elif np.isfinite(target) and i > entry_i and l[i] <= target:
                    _close_position(i, target, "target")
                elif time_stop_bars and (i - entry_i) >= time_stop_bars:
                    _close_position(i, c[i], "time")

        # ── 3. funding (perp only, real timestamps) ──────────────────────
        if direction != 0 and fund_ts is not None and i > 0:
            lo = idx[i - 1].value
            hi = idx[i].value
            mask = (fund_ts > lo) & (fund_ts <= hi)
            if mask.any():
                rate = float(np.nansum(fund_rate[mask]))
                notional = abs(c[i] * units * multiplier)
                cash -= direction * rate * notional

        # ── 4. mark to market ────────────────────────────────────────────
        eq = cash + (direction * (c[i] - entry_price) * units * multiplier
                     if direction != 0 else 0.0)
        equity_points.append(eq)
        equity_index.append(idx[i])

        # ── 5. tomorrow's orders from today's close ─────────────────────
        if direction != 0:
            if (direction > 0 and exit_long[i]) or (direction < 0 and exit_short[i]):
                pending_exit = True
            if direction > 0 and entry_short[i] and allow_short:
                pending_exit = True
            if direction < 0 and entry_long[i]:
                pending_exit = True
        else:
            want = 0
            if entry_long[i]:
                want = 1
            elif entry_short[i] and allow_short:
                want = -1
            if want != 0 and i < n - 1:
                pending_entry = want

    # close any residual position at the last close
    if direction != 0:
        _close_position(n - 1, c[n - 1], "eod")
        equity_points[-1] = cash
        equity_index[-1] = idx[n - 1]

    equity = pd.Series(equity_points, index=pd.DatetimeIndex(equity_index),
                       name="equity", dtype="float64")
    returns = equity.pct_change().dropna().to_numpy(dtype=np.float64)
    total_return = float(equity.iloc[-1] / capital - 1.0)
    days = max((equity.index[-1] - equity.index[0]).total_seconds() / 86400.0, 1e-9)
    cagr = float((equity.iloc[-1] / capital) ** (365.25 / days) - 1.0) \
        if equity.iloc[-1] > 0 else -1.0

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else \
        (float("inf") if gross_win > 0 else 0.0)
    r_multiples = [t["r_multiple"] for t in trades]
    # Realised risk/reward per trade, in R (the engine's own risk unit):
    # average R on winners ÷ average |R| on losers. `None` when there is no
    # losing trade to divide by — a table should print "—", not infinity.
    win_rs = [float(t["r_multiple"]) for t in trades if t["pnl"] > 0]
    loss_rs = [-float(t["r_multiple"]) for t in trades if t["pnl"] < 0]
    avg_win_r = float(np.mean(win_rs)) if win_rs else 0.0
    avg_loss_r = float(np.mean(loss_rs)) if loss_rs else 0.0
    risk_reward = round(avg_win_r / avg_loss_r, 3) if avg_loss_r > 0 else None
    exposure = bars_in_market / max(len(equity), 1)

    monthly = equity.resample("ME").last().pct_change().dropna() \
        if len(equity) > 2 else pd.Series(dtype="float64")

    metrics = {
        "net_return": round(total_return, 5),
        "cagr": round(cagr, 5),
        "max_drawdown": round(_max_drawdown(equity.to_numpy()), 5),
        "sharpe": round(_sharpe(returns, bars_per_year), 3),
        "sortino": round(_sortino(returns, bars_per_year), 3),
        "profit_factor": round(profit_factor, 3) if math.isfinite(profit_factor)
        else None,
        "win_rate": round(len(wins) / len(trades), 4) if trades else 0.0,
        "trades": len(trades),
        "avg_win": round(float(np.mean([t["pnl"] for t in wins])), 2) if wins else 0.0,
        "avg_loss": round(float(np.mean([t["pnl"] for t in losses])), 2) if losses else 0.0,
        "expectancy_r": round(float(np.mean(r_multiples)), 3) if r_multiples else 0.0,
        "avg_win_r": round(avg_win_r, 3),
        "avg_loss_r": round(avg_loss_r, 3),
        "risk_reward": risk_reward,
        "exposure": round(exposure, 4),
        "turnover": round(traded_notional / max(capital, 1e-9) /
                          max(days / 365.25, 1e-9), 3),
        "final_equity": round(float(equity.iloc[-1]), 2),
        "bars": int(len(equity)),
        "bars_per_year": round(float(bars_per_year), 2),
        "start": equity.index[0].isoformat(),
        "end": equity.index[-1].isoformat(),
        "monthly_returns": {str(k)[:7]: round(float(v), 5)
                            for k, v in monthly.items()},
        "exit_breakdown": _exit_breakdown(trades),
    }
    return BacktestResult(ok=True, metrics=metrics, trades=trades,
                          equity=equity,
                          meta={"costs": costs.describe(),
                                "capital": capital, "risk_pct": risk_pct,
                                "max_leverage": max_leverage,
                                "multiplier": multiplier,
                                "time_stop_bars": time_stop_bars,
                                "trade_start": equity.index[0].isoformat()})


def _exit_breakdown(trades: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in trades:
        k = str(t.get("exit_kind") or "?")
        out[k] = out.get(k, 0) + 1
    return out


def buy_and_hold(df: pd.DataFrame, *, trade_start_idx: int = 0,
                 capital: float = 10_000.0, costs: Costs | None = None,
                 multiplier: float = 1.0) -> dict[str, Any]:
    """Benchmark: buy at the first tradable open, hold to the last close."""
    if len(df) < 2:
        return {"net_return": 0.0, "max_drawdown": 0.0, "sharpe": 0.0,
                "trades": 0, "bars": 0}
    start = max(1, int(trade_start_idx))
    costs = costs or Costs(0.0, 0.0, 0.0)
    entry = df["open"].iloc[start] * (1 + costs.adverse())
    equity = (df["close"].iloc[start:] / entry) * capital
    rets = equity.pct_change().dropna().to_numpy()
    span = max((equity.index[-1] - equity.index[0]).total_seconds(), 1.0)
    bpy = max(len(equity) / (span / (365.25 * 86400)), 1.0)
    return {
        "net_return": round(float(equity.iloc[-1] / capital - 1), 5),
        "max_drawdown": round(_max_drawdown(equity.to_numpy()), 5),
        "sharpe": round(_sharpe(rets, bpy), 3),
        "sortino": round(_sortino(rets, bpy), 3),
        "trades": 1,
        "bars": int(len(equity)),
        "start": equity.index[0].isoformat(),
        "end": equity.index[-1].isoformat(),
        "entry": round(float(entry), 6),
    }


def bars_per_year_for(df: pd.DataFrame, timeframe: str) -> float:
    """Data-driven annualisation: observed bars / observed years."""
    if len(df) < 2:
        return 365.25 * 86400 / tf_seconds(timeframe)
    span = max((df.index[-1] - df.index[0]).total_seconds(), 1.0)
    return max(len(df) / (span / (365.25 * 86400)), 1.0)
