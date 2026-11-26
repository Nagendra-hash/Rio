"""
orchestrator/agents/trading/institutional.py — institutional portfolio &
risk analytics (buy-side math, no network, no LLM).

WHY THIS EXISTS
    The trading agent's analysis.py answers "what is this chart doing?" — a
    single-instrument, technical question. An institution asks a different
    question before it risks a rupee: *how does this position change the
    portfolio?* That needs return/vol/Sharpe, drawdown, beta/correlation to a
    benchmark, VaR/CVaR, realised-vol percentile, risk-parity weights and
    vol-targeted position sizes — computed on real closes, reproducibly, with
    no model in the loop.

    Every function here is deterministic arithmetic over bars that were
    fetched elsewhere (data.py labels the provenance). Nothing is invented:
    a metric whose inputs are missing returns None, never a guess.

CONVENTIONS
    returns           decimal fractions (0.01 = +1%), simple returns unless a
                      function says otherwise
    periods_per_year  252 for daily bars (the default everywhere)
    VaR / CVaR        reported as NEGATIVE decimals (a loss), 1-period horizon
                      — so VaR95 = -0.031 means "on the worst 5% of days you
                      lose at least 3.1%" and is directly comparable to returns
    drawdown          negative decimals (max_drawdown()["max_dd"] <= 0)

    Pure stdlib (math/statistics). The engine must run inside any venv, on a
    box with no numpy, and inside tests with zero setup.

READ THIS WITH
    risk.py      — sizing/safety rails (this module proposes; risk.py gates)
    analysis.py  — per-instrument technicals (this module is portfolio-level)
    scripts/institutional_metrics.py — the CLI that fetches bars and prints a
                                       build_report() JSON for the agent/skill
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone

TRADING_DAYS = 252


# ── primitives ───────────────────────────────────────────────────────────────

def closes_from_bars(bars: list[dict] | None) -> list[float]:
    """Closes in order, dropping rows with no usable close."""
    out: list[float] = []
    for b in bars or []:
        try:
            c = b.get("close")
            if c is None:
                continue
            c = float(c)
        except (TypeError, ValueError):
            continue
        if c > 0 and math.isfinite(c):
            out.append(c)
    return out


def simple_returns(closes: list[float]) -> list[float]:
    return [closes[i] / closes[i - 1] - 1.0
            for i in range(1, len(closes)) if closes[i - 1] > 0]


def log_returns(closes: list[float]) -> list[float]:
    return [math.log(closes[i] / closes[i - 1])
            for i in range(1, len(closes)) if closes[i - 1] > 0]


def _sd(xs: list[float], sample: bool = True) -> float | None:
    if len(xs) < 2:
        return None
    return statistics.stdev(xs) if sample else statistics.pstdev(xs)


def total_return(closes: list[float]) -> float | None:
    if len(closes) < 2 or closes[0] <= 0:
        return None
    return closes[-1] / closes[0] - 1.0


def cagr(closes: list[float], periods_per_year: int = TRADING_DAYS) -> float | None:
    """Compound annual growth from first to last close."""
    n = len(closes) - 1
    if n <= 0 or closes[0] <= 0:
        return None
    return (closes[-1] / closes[0]) ** (periods_per_year / n) - 1.0


def annualized_vol(returns: list[float],
                   periods_per_year: int = TRADING_DAYS) -> float | None:
    sd = _sd(returns)
    return None if sd is None else sd * math.sqrt(periods_per_year)


def downside_deviation(returns: list[float], mar: float = 0.0,
                       periods_per_year: int = TRADING_DAYS) -> float | None:
    """Annualised deviation of returns below the minimum acceptable return."""
    if len(returns) < 2:
        return None
    downside = [min(0.0, r - mar) ** 2 for r in returns]
    return math.sqrt(statistics.fmean(downside)) * math.sqrt(periods_per_year)


def sharpe(returns: list[float], rf_annual: float = 0.0,
           periods_per_year: int = TRADING_DAYS) -> float | None:
    vol = _sd(returns)
    if vol in (None, 0.0):
        return None
    excess = (statistics.fmean(returns) * periods_per_year) - rf_annual
    return excess / (vol * math.sqrt(periods_per_year))


def sortino(returns: list[float], rf_annual: float = 0.0,
            periods_per_year: int = TRADING_DAYS) -> float | None:
    dd = downside_deviation(returns, mar=rf_annual / periods_per_year,
                            periods_per_year=periods_per_year)
    if not dd:
        return None
    excess = (statistics.fmean(returns) * periods_per_year) - rf_annual
    return excess / dd


def max_drawdown(closes: list[float]) -> dict:
    """Deepest peak-to-trough decline. `max_dd` is <= 0; also returns the
    peak/trough INDEXES and the current drawdown from the running peak."""
    if len(closes) < 2:
        return {"max_dd": None, "peak_index": None, "trough_index": None,
                "current_dd": None}
    peak = closes[0]
    peak_i = 0
    best_peak_i = best_trough_i = 0
    worst = 0.0
    for i, c in enumerate(closes):
        if c > peak:
            peak, peak_i = c, i
        dd = c / peak - 1.0
        if dd < worst:
            worst, best_peak_i, best_trough_i = dd, peak_i, i
    return {"max_dd": worst, "peak_index": best_peak_i,
            "trough_index": best_trough_i,
            "current_dd": closes[-1] / max(closes) - 1.0}


def calmar(cagr_value: float | None, max_dd: float | None) -> float | None:
    if cagr_value is None or not max_dd:
        return None
    return cagr_value / abs(max_dd)


def covariance(a: list[float], b: list[float]) -> float | None:
    n = min(len(a), len(b))
    if n < 2:
        return None
    a, b = a[-n:], b[-n:]
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (n - 1)


def correlation(a: list[float], b: list[float]) -> float | None:
    n = min(len(a), len(b))
    if n < 2:
        return None
    a, b = a[-n:], b[-n:]
    sa, sb = _sd(a), _sd(b)
    cov = covariance(a, b)
    if cov is None or not sa or not sb:
        return None
    return max(-1.0, min(1.0, cov / (sa * sb)))


def beta(asset_returns: list[float], bench_returns: list[float]) -> float | None:
    n = min(len(asset_returns), len(bench_returns))
    if n < 2:
        return None
    varb = _sd(bench_returns[-n:], sample=True)
    cov = covariance(asset_returns[-n:], bench_returns[-n:])
    if cov is None or not varb or varb == 0:
        return None
    return cov / (varb ** 2)


def alpha_annualized(asset_returns: list[float], bench_returns: list[float],
                     rf_annual: float = 0.0,
                     periods_per_year: int = TRADING_DAYS) -> float | None:
    """Jensen's alpha, annualised, simple (non-compounded) form."""
    b = beta(asset_returns, bench_returns)
    n = min(len(asset_returns), len(bench_returns))
    if b is None or n < 2:
        return None
    ra = statistics.fmean(asset_returns[-n:]) * periods_per_year
    rb = statistics.fmean(bench_returns[-n:]) * periods_per_year
    return (ra - rf_annual) - b * (rb - rf_annual)


def tracking_error(asset_returns: list[float], bench_returns: list[float],
                   periods_per_year: int = TRADING_DAYS) -> float | None:
    n = min(len(asset_returns), len(bench_returns))
    if n < 2:
        return None
    diffs = [x - y for x, y in zip(asset_returns[-n:], bench_returns[-n:])]
    sd = _sd(diffs)
    return None if sd is None else sd * math.sqrt(periods_per_year)


def information_ratio(asset_returns: list[float],
                      bench_returns: list[float]) -> float | None:
    te = tracking_error(asset_returns, bench_returns)
    n = min(len(asset_returns), len(bench_returns))
    if not te or n < 2:
        return None
    active = statistics.fmean(x - y for x, y in
                              zip(asset_returns[-n:], bench_returns[-n:]))
    return (active * TRADING_DAYS) / te


# ── tail risk ────────────────────────────────────────────────────────────────

def var_cvar(returns: list[float], level: float = 0.95,
             method: str = "historical", periods_per_year: int = TRADING_DAYS) -> dict:
    """One-period Value-at-Risk and Expected Shortfall (CVaR).

    Returns are decimal losses: `var` is the level's quantile (negative on
    equity-like series), `cvar` the mean of everything at/below it. Both are
    None when the sample is too small to say anything honest.
    """
    if len(returns) < 20:
        return {"var": None, "cvar": None, "level": level, "method": method}
    if method == "gaussian":
        mu = statistics.fmean(returns)
        sd = _sd(returns)
        if not sd:
            return {"var": None, "cvar": None, "level": level, "method": method}
        z = statistics.NormalDist().inv_cdf(1.0 - level)   # negative
        var = mu + z * sd
        cvar = mu - sd * (statistics.NormalDist().pdf(z) / (1.0 - level))
        return {"var": var, "cvar": cvar, "level": level, "method": "gaussian"}
    ordered = sorted(returns)
    idx = max(0, min(len(ordered) - 1, int(math.floor((1.0 - level) * len(ordered)))))
    tail = ordered[: idx + 1] or [ordered[0]]
    return {"var": ordered[idx], "cvar": statistics.fmean(tail),
            "level": level, "method": "historical"}


# ── volatility regime ────────────────────────────────────────────────────────

def realized_vol(returns: list[float], window: int = 21,
                 periods_per_year: int = TRADING_DAYS) -> list[float]:
    """Rolling annualised vol (one value per completed window)."""
    out: list[float] = []
    for i in range(window, len(returns) + 1):
        sd = _sd(returns[i - window:i])
        out.append(sd * math.sqrt(periods_per_year) if sd else 0.0)
    return out


def vol_percentile(returns: list[float], window: int = 21,
                   history: int = 252) -> float | None:
    """Where today's realised vol sits in its own trailing history (0..1).

    > 0.90 means the instrument is in a top-decile volatility regime right now
    — the institutional reason to cut size, not average down.
    """
    series = realized_vol(returns, window=window)
    if len(series) < 10:
        return None
    current = series[-1]
    hist = series[-history:]
    below = sum(1 for v in hist if v <= current)
    return below / len(hist)


def atr_from_bars(bars: list[dict] | None, period: int = 14) -> float | None:
    """Wilder's Average True Range from bars carrying high/low/close."""
    rows = [b for b in (bars or [])
            if b.get("high") is not None and b.get("low") is not None
            and b.get("close") is not None]
    if len(rows) < 2:
        return None
    trs: list[float] = []
    prev_close = float(rows[0]["close"])
    for b in rows[1:]:
        h, l, c = float(b["high"]), float(b["low"]), float(b["close"])
        trs.append(max(h - l, abs(h - prev_close), abs(l - prev_close)))
        prev_close = c
    if not trs:
        return None
    period = max(1, min(period, len(trs)))
    atr = statistics.fmean(trs[:period])
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


# ── regime ───────────────────────────────────────────────────────────────────

def _sma(values: list[float], period: int) -> float | None:
    if len(values) < period or period <= 0:
        return None
    return statistics.fmean(values[-period:])


def trend_regime(closes: list[float], fast: int = 50, slow: int = 200) -> dict:
    """Trend label from the fast/slow moving-average stack + 20-bar slope."""
    f, s = _sma(closes, fast), _sma(closes, slow)
    label, detail = "unknown", []
    if f is not None and s is not None:
        if f > s * 1.005:
            label = "uptrend"
        elif f < s * 0.995:
            label = "downtrend"
        else:
            label = "range"
        detail.append(f"{fast}MA {'>' if f > s else '<'} {slow}MA")
    slope = None
    if len(closes) >= 21 and closes[-21] > 0:
        slope = closes[-1] / closes[-21] - 1.0
        detail.append(f"20-bar return {slope * 100:+.1f}%")
    return {"label": label, "fast_ma": f, "slow_ma": s, "slope_20": slope,
            "detail": "; ".join(detail)}


def vol_regime(returns: list[float], window: int = 21,
               history: int = 252) -> dict:
    """Current annualised vol + its percentile → a regime label.

    The label is deliberately coarse (institutions act on regime, not on the
    third decimal): low (<50th pct), normal (50-80th), high (80-95th),
    extreme (>=95th).
    """
    current = None
    if len(returns) >= window:
        sd = _sd(returns[-window:])
        current = sd * math.sqrt(TRADING_DAYS) if sd else 0.0
    pct = vol_percentile(returns, window=window, history=history)
    if pct is None:
        label = "unknown"
    elif pct < 0.50:
        label = "low"
    elif pct < 0.80:
        label = "normal"
    elif pct < 0.95:
        label = "high"
    else:
        label = "extreme"
    return {"label": label, "ann_vol": current, "percentile": pct}


def market_regime(benchmark_bars: list[dict]) -> dict:
    """One institutional regime read from the benchmark: trend x vol."""
    closes = closes_from_bars(benchmark_bars)
    if len(closes) < 30:
        return {"label": "unknown", "trend": trend_regime(closes),
                "vol": vol_regime(simple_returns(closes)),
                "posture": "insufficient history — do not trade the regime"}
    trend = trend_regime(closes)
    vol = vol_regime(simple_returns(closes))
    posture = _regime_posture(trend["label"], vol["label"])
    return {"label": f"{trend['label']}/{vol['label']}",
            "trend": trend, "vol": vol, "posture": posture}


def _regime_posture(trend: str, vol: str) -> str:
    if trend == "uptrend":
        if vol == "extreme":
            return ("Risk-on trend but extreme vol: trade smaller, prefer "
                    "stops over targets, avoid adding to extended positions.")
        if vol == "high":
            return ("Trend intact, vol elevated: normal size with wider ATR "
                    "stops; take partials into strength.")
        return ("Constructive: trend up, vol contained — pullbacks to the "
                "fast MA are the add zone with normal size.")
    if trend == "downtrend":
        if vol in ("high", "extreme"):
            return ("Capital preservation: downtrend + high vol. Reduce net "
                    "exposure, hedge or stay flat; longs are counter-trend.")
        return ("Defensive: downtrend with calm vol can grind lower. Only "
                "counter-trend scalps at reduced size, or wait for a base.")
    return ("Range regime: mean-revert the edges with tight risk, fade "
            "breakouts until a close beyond the range confirms.")


# ── sizing & construction ────────────────────────────────────────────────────

def fixed_fractional_position(capital: float, risk_pct: float, entry: float,
                              stop: float) -> dict:
    """Risk a fixed % of capital between entry and stop (rounds shares DOWN)."""
    if not all(isinstance(x, (int, float)) for x in (capital, risk_pct, entry, stop)):
        return {"ok": False, "error": "capital/risk_pct/entry/stop must be numbers"}
    risk_per_share = abs(entry - stop)
    if capital <= 0 or entry <= 0 or risk_per_share <= 0 or risk_pct <= 0:
        return {"ok": False,
                "error": "need capital>0, entry>0, stop!=entry, risk_pct>0"}
    risk_amount = capital * (risk_pct / 100.0)
    shares = math.floor(risk_amount / risk_per_share)
    return {"ok": True, "shares": shares, "risk_amount": risk_amount,
            "risk_per_share": risk_per_share,
            "notional": shares * entry,
            "notional_pct_of_capital": (shares * entry) / capital}


def vol_target_position(capital: float, price: float, asset_ann_vol: float,
                        target_ann_vol: float, max_weight: float = 1.0) -> dict:
    """Position weight that scales asset vol to a portfolio target vol.

    notional = capital * target_vol / asset_vol, capped at max_weight*capital.
    A 30%-vol name gets a third of the notional a 10%-vol name gets at the same
    target — the institutional answer to "how much".
    """
    if not all(isinstance(x, (int, float)) for x in
               (capital, price, asset_ann_vol, target_ann_vol)) or capital <= 0 \
            or price <= 0 or asset_ann_vol <= 0 or target_ann_vol <= 0:
        return {"ok": False, "error": "need capital/price/target/asset vol > 0"}
    raw_weight = target_ann_vol / asset_ann_vol
    weight = max(0.0, min(raw_weight, max_weight))
    notional = capital * weight
    return {"ok": True, "weight": weight, "raw_weight": raw_weight,
            "capped": raw_weight > max_weight, "notional": notional,
            "shares": math.floor(notional / price),
            "target_ann_vol": target_ann_vol, "asset_ann_vol": asset_ann_vol}


def risk_parity_weights(vols: dict[str, float],
                        max_weight: float = 0.40) -> dict[str, float]:
    """Inverse-vol weights with a HARD single-name cap.

    A naive equal-weight book concentrates risk in its most volatile name;
    inverse-vol equalises the *risk* contribution. Above the cap, weight is
    redistributed to the remaining names (iterative, converges fast).

    Institutions respect the mandate: if no fully-invested book can keep every
    name under the cap (cap < 1/n), the returned weights sum to LESS than 1 and
    the remainder is **cash** — the cap is never breached, and the shortfall is
    made explicit rather than hidden by scaling the cap up.
    """
    clean = {k: float(v) for k, v in (vols or {}).items()
             if isinstance(v, (int, float)) and v and v > 0}
    if not clean:
        return {}
    cap = float(max_weight) if max_weight > 0 else 1.0
    if len(clean) == 1:
        return {next(iter(clean)): min(1.0, cap)}
    # Water-filling: allocate the budget ∝ inverse-vol; whenever a name would
    # exceed the cap, freeze it at the cap and re-spread the budget over the
    # rest. If the cap freezes everyone before the book is full, the leftover
    # is cash (sum < 1) — the cap is never breached.
    inv = {k: 1.0 / v for k, v in clean.items()}
    weights = {k: 0.0 for k in clean}
    free = dict(inv)
    remaining = 1.0
    while free:
        total = sum(free.values())
        if total <= 0:
            break
        proposed = {k: free[k] / total * remaining for k in free}
        over = [k for k, w in proposed.items() if w > cap + 1e-12]
        if not over:
            weights.update(proposed)
            break
        for k in over:
            weights[k] = cap
            remaining -= cap
            del free[k]
        if remaining <= 1e-12:                  # no budget left → rest is cash
            for k in free:
                weights[k] = 0.0
            break
    return weights


def kelly_capped(win_rate: float, payoff_ratio: float,
                 cap: float = 0.25) -> dict:
    """Fractional-MKelly suggestion, hard-capped. Never a licence to max out:
    the cap is the institution, the formula is only an input."""
    if not (0 < win_rate < 1) or payoff_ratio <= 0 or cap <= 0:
        return {"ok": False, "error": "need 0<win_rate<1, payoff_ratio>0"}
    full = win_rate - (1.0 - win_rate) / payoff_ratio
    used = max(0.0, min(full, cap))
    return {"ok": True, "full_kelly": full, "capped": full > cap,
            "fraction": used, "cap": cap}


def correlation_matrix(series: dict[str, list[float]]) -> dict:
    """Symmetric matrix of pairwise correlations, diagonal exactly 1.0."""
    names = list(series)
    m = {a: {} for a in names}
    for i, a in enumerate(names):
        for b in names[i:]:
            c = 1.0 if a == b else correlation(series[a], series[b])
            m[a][b] = c
            m[b][a] = c
    return m


def portfolio_returns(weights: dict[str, float],
                      returns_by_symbol: dict[str, list[float]]) -> list[float]:
    """Weighted portfolio return series over the common tail."""
    usable = {k: v for k, v in (weights or {}).items()
              if k in returns_by_symbol and returns_by_symbol[k]}
    if not usable:
        return []
    n = min(len(returns_by_symbol[k]) for k in usable)
    out: list[float] = []
    for i in range(n):
        r = 0.0
        for k, w in usable.items():
            r += w * returns_by_symbol[k][len(returns_by_symbol[k]) - n + i]
        out.append(r)
    return out


def portfolio_stats(weights: dict[str, float],
                    returns_by_symbol: dict[str, list[float]],
                    benchmark_returns: list[float] | None = None,
                    rf_annual: float = 0.0) -> dict:
    rets = portfolio_returns(weights, returns_by_symbol)
    if not rets:
        return {"ok": False, "error": "no usable return series"}
    vol = annualized_vol(rets)
    stats = {
        "ok": True,
        "ann_vol": vol,
        "sharpe": sharpe(rets, rf_annual=rf_annual),
        "sortino": sortino(rets, rf_annual=rf_annual),
        "var_95_1d": var_cvar(rets, 0.95)["var"],
        "cvar_95_1d": var_cvar(rets, 0.95)["cvar"],
        "max_dd": max_drawdown([1.0 + 0.0] + _cumulative(rets))["max_dd"],
        "effective_names": 1.0 / sum(w * w for w in weights.values())
        if weights and sum(weights.values()) > 0 else None,
    }
    if benchmark_returns:
        stats["beta"] = beta(rets, benchmark_returns)
        stats["corr_to_benchmark"] = correlation(rets, benchmark_returns)
        stats["alpha_ann"] = alpha_annualized(rets, benchmark_returns,
                                              rf_annual=rf_annual)
        stats["information_ratio"] = information_ratio(rets, benchmark_returns)
    return stats


def _cumulative(returns: list[float]) -> list[float]:
    out, level = [], 1.0
    for r in returns:
        level *= (1.0 + r)
        out.append(level)
    return out


def stress_test(weights: dict[str, float],
                shocks: dict[str, float]) -> dict:
    """Apply instantaneous % shocks per name; report the portfolio hit.

    shocks are decimal (e.g. {"NVDA": -0.20}) — a scenario grid, not a
    forecast. Names with no shock are treated as unmoved and listed, never
    silently zeroed-and-hidden."""
    if not weights:
        return {"ok": False, "error": "no weights"}
    pnl = 0.0
    applied: dict[str, float] = {}
    untouched: list[str] = []
    for k, w in weights.items():
        s = shocks.get(k)
        if s is None:
            untouched.append(k)
            continue
        contribution = w * s
        pnl += contribution
        applied[k] = contribution
    return {"ok": True, "portfolio_pnl_pct": pnl,
            "contributions": applied, "unshocked": untouched}


# ── assembly ─────────────────────────────────────────────────────────────────

def _stats_block(closes: list[float], bench_returns: list[float] | None,
                 rf_annual: float) -> dict:
    rets = simple_returns(closes)
    return {
        "n_bars": len(closes),
        "last_close": closes[-1] if closes else None,
        "total_return": total_return(closes),
        "cagr": cagr(closes),
        "ann_vol": annualized_vol(rets),
        "sharpe": sharpe(rets, rf_annual=rf_annual),
        "sortino": sortino(rets, rf_annual=rf_annual),
        "max_dd": max_drawdown(closes)["max_dd"],
        "calmar": calmar(cagr(closes), max_drawdown(closes)["max_dd"]),
        "var_95_1d_hist": var_cvar(rets, 0.95, "historical")["var"],
        "cvar_95_1d_hist": var_cvar(rets, 0.95, "historical")["cvar"],
        "vol_regime": vol_regime(rets),
        "trend": trend_regime(closes),
        "beta": beta(rets, bench_returns) if bench_returns else None,
        "corr_to_benchmark": correlation(rets, bench_returns) if bench_returns else None,
        "info_ratio": information_ratio(rets, bench_returns) if bench_returns else None,
    }


def build_report(bars_by_symbol: dict[str, list[dict]],
                 benchmark_symbol: str = "SPY",
                 benchmark_bars: list[dict] | None = None,
                 weights: dict[str, float] | None = None,
                 capital: float | None = None,
                 target_ann_vol: float = 0.10,
                 risk_pct: float = 1.0,
                 max_weight: float = 0.25,
                 rf_annual: float = 0.0) -> dict:
    """The institutional report: per-name risk, portfolio risk, sizing.

    Returns a JSON-safe dict. Any metric that cannot be computed honestly is
    None with enough context to see why — never a padded number.
    """
    report: dict = {
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "benchmark": benchmark_symbol,
        "assets": {},
        "portfolio": {},
        "correlation": {},
        "sizing": {},
        "flags": [],
        "notes": [],
    }
    bench_closes = closes_from_bars(benchmark_bars)
    bench_returns = simple_returns(bench_closes)
    report["regime"] = market_regime(benchmark_bars) if bench_closes else {
        "label": "unknown", "posture": "no benchmark bars supplied"}

    returns_by_symbol: dict[str, list[float]] = {}
    for sym, bars in (bars_by_symbol or {}).items():
        closes = closes_from_bars(bars)
        if len(closes) < 5:
            report["assets"][sym] = {
                "ok": False, "error": f"need >=5 closes, got {len(closes)}"}
            continue
        rets = simple_returns(closes)
        returns_by_symbol[sym] = rets
        block = _stats_block(closes, bench_returns, rf_annual)
        block["ok"] = True
        block["atr14"] = atr_from_bars(bars, 14)
        report["assets"][sym] = block

    names = [s for s in returns_by_symbol]
    if not names:
        report["notes"].append("no asset had enough history for any metric")
        return report

    if weights:
        w = {s: float(weights.get(s, 0.0)) for s in names}
        total = sum(w.values())
        if total <= 0:
            report["notes"].append("weights summed to 0 — using equal weight")
            w = {s: 1.0 / len(names) for s in names}
        else:
            w = {s: v / total for s, v in w.items()}
    else:
        w = {s: 1.0 / len(names) for s in names}
    report["portfolio"]["weights"] = w
    report["portfolio"]["stats"] = portfolio_stats(w, returns_by_symbol,
                                                   bench_returns, rf_annual)
    report["correlation"] = correlation_matrix(returns_by_symbol)

    # risk-parity alternative (equal risk, not equal money)
    vols = {s: (report["assets"][s].get("ann_vol") or 0.0) for s in names}
    rp = risk_parity_weights(vols, max_weight=max_weight)
    report["portfolio"]["risk_parity_weights"] = rp

    # vol-targeted notional per name (using each name's own vol)
    report["sizing"]["target_ann_vol"] = target_ann_vol
    report["sizing"]["risk_pct_per_trade"] = risk_pct
    report["sizing"]["max_weight"] = max_weight
    for s in names:
        price = report["assets"][s].get("last_close") or 0.0
        av = report["assets"][s].get("ann_vol") or 0.0
        entry = {"price": price, "ann_vol": av,
                 "risk_parity_weight": rp.get(s)}
        if capital and price > 0:
            vt = vol_target_position(capital, price, av, target_ann_vol,
                                     max_weight=max_weight)
            entry["vol_target"] = vt
            atr = report["assets"][s].get("atr14")
            if atr and atr > 0:
                stop = price - 1.5 * atr
                ff = fixed_fractional_position(capital, risk_pct, price, stop)
                entry["stop_1_5atr"] = stop
                entry["fixed_fractional"] = ff
        report["sizing"][s] = entry

    _add_flags(report, names)
    report["notes"].append(
        "Decision support only — no orders, no guarantees. Metrics are "
        "computed from the bars supplied; check the data's provenance and "
        "as-of time before acting.")
    return report


def _add_flags(report: dict, names: list[str]) -> None:
    flags: list[str] = report["flags"]
    vol = report["regime"].get("vol", {}) or {}
    if vol.get("label") in ("high", "extreme"):
        flags.append(f"benchmark volatility regime = {vol['label']} "
                     f"({(vol.get('percentile') or 0) * 100:.0f}th pct) — "
                     "reduce gross exposure / widen stops")
    if report["regime"].get("trend", {}).get("label") == "downtrend":
        flags.append("benchmark trend = downtrend — longs are counter-trend; "
                     "keep net exposure low or hedge")
    for s in names:
        a = report["assets"][s]
        if a.get("vol_regime", {}).get("label") in ("high", "extreme"):
            flags.append(f"{s}: own vol regime {a['vol_regime']['label']} "
                         f"({(a['vol_regime'].get('percentile') or 0) * 100:.0f}th pct)")
        b = a.get("beta")
        if b is not None and abs(b) > 1.3:
            flags.append(f"{s}: beta {b:.2f} vs benchmark — portfolio gains "
                         "leverage to benchmark moves")
    # concentration / correlation
    corr = report.get("correlation") or {}
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            c = (corr.get(a) or {}).get(b)
            if c is not None and c > 0.85:
                flags.append(f"{a}/{b} correlation {c:.2f} — not two bets, "
                             "one bet; size the pair as a single position")
    w = report["portfolio"].get("weights") or {}
    for s, weight in w.items():
        if weight > 0.35:
            flags.append(f"{s} is {weight * 100:.0f}% of the book — "
                         "single-name concentration above 35%")
    st = report["portfolio"].get("stats") or {}
    if (st.get("ann_vol") or 0) > 0.35:
        flags.append(f"portfolio vol {st['ann_vol'] * 100:.0f}% — above a "
                     "typical mandate; scale gross exposure down")
    # infeasible single-name cap: a fully-invested n-name book cannot keep every
    # name under a cap smaller than 1/n — risk parity then leaves cash instead.
    mw = report["sizing"].get("max_weight")
    n = len(names)
    if mw is not None and n and mw * n < 1.0 - 1e-9:
        rp = report["portfolio"].get("risk_parity_weights") or {}
        rp_total = sum(rp.values())
        flags.append(f"single-name cap {mw * 100:.0f}% is below 1/{n} "
                     f"({100.0 / n:.0f}%) — unreachable for a fully-invested "
                     f"{n}-name book; risk parity holds "
                     f"{max(0.0, 1.0 - rp_total) * 100:.0f}% cash instead")
    # independent per-name vol targeting can ask for >100% gross
    vt_total = 0.0
    for s in names:
        v = ((report["sizing"].get(s) or {}).get("vol_target") or {})
        if v.get("ok"):
            vt_total += v.get("weight") or 0.0
    if vt_total > 1.0 + 1e-9:
        flags.append(f"per-name vol targets sum to {vt_total * 100:.0f}% of "
                     "capital — scale all sizes by "
                     f"{100.0 / vt_total:.0f}% to stay fully invested")
