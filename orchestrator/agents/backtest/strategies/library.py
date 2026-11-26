"""
orchestrator/agents/backtest/strategies/library.py — reviewed strategy code.

Every strategy here is ordinary, auditable Python. There is no LLM code
execution path anywhere: the research loop can only choose a registered
strategy key and numeric parameters from that strategy's declared grid.

Signal contract
---------------
`build(df, params, ctx) -> pd.DataFrame` with the columns

    long_entry, short_entry, long_exit, short_exit   bool (signal at bar close)
    stop_distance                                    price units (ATR-based)
    target_r                                         reward multiple (float)

The engine fills at the NEXT bar's open. Strategies only see bars ≤ i, so a
signal cannot use the candle it is filled on. `short_entry` is honoured only
when the instrument allows shorting (perpetual futures / futures); spot crypto
and XAUUSD are long-only.

Every strategy declares: version, market suitability, allowed timeframes,
parameter grid, warm-up and exit rules — enough to reproduce a result from
(provider, instrument, timeframe, strategy, version, params).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Any, Callable, Iterable

import numpy as np
import pandas as pd

from orchestrator.agents.backtest import indicators as ind

STRATEGY_VERSION = "1.0.0"


@dataclass(frozen=True)
class StrategyContext:
    market_class: str
    timeframe: str
    allow_short: bool
    session_market: bool = False


@dataclass(frozen=True)
class StrategySpec:
    key: str
    name: str
    version: str
    description: str
    param_grid: dict[str, tuple]
    build: Callable[[pd.DataFrame, dict, StrategyContext], pd.DataFrame]
    markets: tuple[str, ...] = ("crypto_spot", "crypto_perp",
                                "delta_spot", "delta_perp",
                                "gold_futures", "gold_xauusd", "gold_mcx")
    timeframes: tuple[str, ...] = ()
    intraday_only: bool = False
    warmup: int = 200
    target_r: float = 2.0
    time_stop_bars: int = 0
    long_only_default: bool = True

    def allows(self, market_class: str, timeframe: str) -> bool:
        if self.markets and market_class not in self.markets:
            return False
        if self.timeframes and timeframe not in self.timeframes:
            return False
        if self.intraday_only and timeframe not in ("1m", "3m", "5m", "15m",
                                                    "30m", "1h"):
            return False
        return True

    def param_sets(self, limit: int = 8) -> list[dict[str, Any]]:
        return sample_grid(self.param_grid, limit)

    def describe(self) -> dict[str, Any]:
        return {
            "key": self.key, "name": self.name, "version": self.version,
            "description": self.description,
            "param_grid": {k: list(v) for k, v in self.param_grid.items()},
            "markets": list(self.markets),
            "timeframes": list(self.timeframes),
            "intraday_only": self.intraday_only,
            "warmup": self.warmup,
            "target_r": self.target_r,
            "time_stop_bars": self.time_stop_bars,
        }


# ── helpers ──────────────────────────────────────────────────────────────────

def sample_grid(grid: dict[str, tuple], limit: int) -> list[dict[str, Any]]:
    """Deterministic, reproducible subset of the cartesian grid.

    Small grids are searched exhaustively. Larger grids are sampled at evenly
    spaced indices (first and last always included) and the FULL grid size is
    recorded by the caller, so truncation is never hidden.
    """
    if not grid:
        return [{}]
    keys = list(grid)
    combos = [dict(zip(keys, values))
              for values in product(*(grid[k] for k in keys))]
    # Drop logically invalid pairs (fast >= slow, lower <= upper, etc.)
    combos = [c for c in combos if _valid(c)]
    if len(combos) <= limit:
        return combos
    idx = np.linspace(0, len(combos) - 1, num=limit).round().astype(int)
    return [combos[i] for i in sorted(set(int(i) for i in idx))]


def _valid(p: dict) -> bool:
    for a, b in (("fast", "slow"), ("ema_fast", "ema_slow"),
                 ("n_fast", "n_slow")):
        if a in p and b in p and p[a] >= p[b]:
            return False
    if "entry" in p and "exit" in p and p["entry"] <= p["exit"]:
        return False
    return True


def _blank(df: pd.DataFrame, warmup: int, stop: pd.Series,
           target_r: float) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    for col in ("long_entry", "short_entry", "long_exit", "short_exit"):
        out[col] = False
    out["stop_distance"] = stop.astype("float64")
    out["target_r"] = float(target_r)
    out["warmup"] = int(warmup)
    return out


def _cross_up(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a > b) & (a.shift(1) <= b.shift(1))


def _cross_down(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a < b) & (a.shift(1) >= b.shift(1))


def _clean(series: pd.Series) -> pd.Series:
    return series.fillna(False).astype(bool)


# ── 1. EMA/SMA crossover ─────────────────────────────────────────────────────

def _ema_cross(df: pd.DataFrame, p: dict, ctx: StrategyContext) -> pd.DataFrame:
    fast_n, slow_n = int(p["fast"]), int(p["slow"])
    fast = ind.ema(df["close"], fast_n)
    slow = ind.ema(df["close"], slow_n)
    atr = ind.atr(df, 14)
    warmup = slow_n + 20
    out = _blank(df, warmup, atr * 2.0, 2.0)
    out["long_entry"] = _clean(_cross_up(fast, slow))
    out["short_entry"] = _clean(_cross_down(fast, slow))
    out["long_exit"] = _clean(_cross_down(fast, slow))
    out["short_exit"] = _clean(_cross_up(fast, slow))
    return out


# ── 2. EMA + ADX trend following ─────────────────────────────────────────────

def _ema_adx(df: pd.DataFrame, p: dict, ctx: StrategyContext) -> pd.DataFrame:
    fast_n, slow_n = int(p["ema_fast"]), int(p["ema_slow"])
    adx_n = int(p.get("adx_period", 14))
    fast = ind.ema(df["close"], fast_n)
    slow = ind.ema(df["close"], slow_n)
    dmi = ind.adx(df, adx_n)
    atr = ind.atr(df, 14)
    trend_up = (fast > slow) & (dmi["adx"] > float(p["adx_min"])) & \
               (dmi["plus_di"] > dmi["minus_di"])
    trend_dn = (fast < slow) & (dmi["adx"] > float(p["adx_min"])) & \
               (dmi["minus_di"] > dmi["plus_di"])
    warmup = max(slow_n, adx_n * 3) + 20
    out = _blank(df, warmup, atr * float(p.get("stop_atr", 2.5)), 2.5)
    out["long_entry"] = _clean(trend_up & ~trend_up.shift(1).fillna(False))
    out["short_entry"] = _clean(trend_dn & ~trend_dn.shift(1).fillna(False))
    out["long_exit"] = _clean(~trend_up)
    out["short_exit"] = _clean(~trend_dn)
    return out


# ── 3. RSI mean reversion ────────────────────────────────────────────────────

def _rsi_meanrev(df: pd.DataFrame, p: dict, ctx: StrategyContext) -> pd.DataFrame:
    n = int(p["rsi"])
    oversold = float(p["oversold"])
    exit_level = float(p.get("exit", 55))
    r = ind.rsi(df["close"], n)
    atr = ind.atr(df, 14)
    trend = ind.ema(df["close"], 200)
    use_filter = int(p.get("trend_filter", 0)) == 1
    above = _clean(df["close"] > trend)
    below = _clean(df["close"] < trend)
    long_ok = above if use_filter else pd.Series(True, index=df.index)
    short_ok = below if use_filter else pd.Series(True, index=df.index)
    warmup = max(n * 3, 220 if use_filter else 0)
    out = _blank(df, warmup, atr * 2.0, 1.5)
    out["long_entry"] = _clean((r < oversold) & long_ok)
    out["short_entry"] = _clean((r > 100 - oversold) & short_ok)
    out["long_exit"] = _clean(r > exit_level)
    out["short_exit"] = _clean(r < 100 - exit_level)
    return out


# ── 4. Bollinger Band mean reversion ─────────────────────────────────────────

def _bb_meanrev(df: pd.DataFrame, p: dict, ctx: StrategyContext) -> pd.DataFrame:
    n, k = int(p["period"]), float(p["std"])
    bands = ind.bollinger(df["close"], n, k)
    atr = ind.atr(df, 14)
    warmup = n + 40
    out = _blank(df, warmup, atr * float(p.get("stop_atr", 2.0)), 1.5)
    out["long_entry"] = _clean(df["close"] < bands["lower"])
    out["short_entry"] = _clean(df["close"] > bands["upper"])
    out["long_exit"] = _clean(df["close"] > bands["mid"])
    out["short_exit"] = _clean(df["close"] < bands["mid"])
    return out


# ── 5. MACD momentum ─────────────────────────────────────────────────────────

def _macd_momentum(df: pd.DataFrame, p: dict, ctx: StrategyContext) -> pd.DataFrame:
    fast, slow, sig = int(p["fast"]), int(p["slow"]), int(p["signal"])
    m = ind.macd(df["close"], fast, slow, sig)
    atr = ind.atr(df, 14)
    warmup = slow + sig + 30
    out = _blank(df, warmup, atr * 2.0, 2.0)
    out["long_entry"] = _clean(_cross_up(m["hist"], pd.Series(0.0, index=df.index)))
    out["short_entry"] = _clean(_cross_down(m["hist"], pd.Series(0.0, index=df.index)))
    out["long_exit"] = _clean(_cross_down(m["hist"], pd.Series(0.0, index=df.index)))
    out["short_exit"] = _clean(_cross_up(m["hist"], pd.Series(0.0, index=df.index)))
    return out


# ── 6. Donchian channel breakout ─────────────────────────────────────────────

def _donchian_breakout(df: pd.DataFrame, p: dict,
                       ctx: StrategyContext) -> pd.DataFrame:
    entry_n, exit_n = int(p["entry"]), int(p["exit"])
    entry_ch = ind.donchian(df, entry_n)
    exit_ch = ind.donchian(df, exit_n)
    atr = ind.atr(df, 14)
    warmup = entry_n + 30
    out = _blank(df, warmup, atr * float(p.get("stop_atr", 2.0)), 2.0)
    out["long_entry"] = _clean(df["close"] > entry_ch["upper"].shift(1))
    out["short_entry"] = _clean(df["close"] < entry_ch["lower"].shift(1))
    out["long_exit"] = _clean(df["close"] < exit_ch["lower"].shift(1))
    out["short_exit"] = _clean(df["close"] > exit_ch["upper"].shift(1))
    return out


# ── 7. ATR breakout / trend ignition ─────────────────────────────────────────

def _atr_breakout(df: pd.DataFrame, p: dict, ctx: StrategyContext) -> pd.DataFrame:
    n = int(p["lookback"])
    k = float(p["atr_buffer"])
    atr = ind.atr(df, 14)
    prior_high = df["high"].rolling(n, min_periods=n).max().shift(1)
    prior_low = df["low"].rolling(n, min_periods=n).min().shift(1)
    ema_fast = ind.ema(df["close"], 20)
    warmup = n + 30
    out = _blank(df, warmup, atr * float(p.get("stop_atr", 2.0)), 2.5)
    out["long_entry"] = _clean(df["close"] > prior_high + k * atr)
    out["short_entry"] = _clean(df["close"] < prior_low - k * atr)
    out["long_exit"] = _clean(df["close"] < ema_fast)
    out["short_exit"] = _clean(df["close"] > ema_fast)
    return out


# ── 8. Volatility-filtered trend following ───────────────────────────────────

def _vol_trend(df: pd.DataFrame, p: dict, ctx: StrategyContext) -> pd.DataFrame:
    fast_n, slow_n = int(p["ema_fast"]), int(p["ema_slow"])
    vol_n, vol_pct = int(p["vol_lookback"]), float(p["vol_pct"])
    fast = ind.ema(df["close"], fast_n)
    slow = ind.ema(df["close"], slow_n)
    atr = ind.atr(df, 14)
    rv = ind.realized_vol(df["close"], vol_n)
    vol_ceiling = rv.rolling(vol_n * 5, min_periods=vol_n * 3).quantile(vol_pct)
    calm = rv <= vol_ceiling
    up = (fast > slow) & calm
    down = (fast < slow) & calm
    warmup = max(slow_n, vol_n * 5) + 20
    out = _blank(df, warmup, atr * float(p.get("stop_atr", 2.5)), 2.5)
    out["long_entry"] = _clean(up & ~up.shift(1).fillna(False))
    out["short_entry"] = _clean(down & ~down.shift(1).fillna(False))
    out["long_exit"] = _clean(~(fast > slow) | ~calm)
    out["short_exit"] = _clean(~(fast < slow) | ~calm)
    return out


# ── 9. VWAP intraday ─────────────────────────────────────────────────────────

def _vwap_intraday(df: pd.DataFrame, p: dict,
                   ctx: StrategyContext) -> pd.DataFrame:
    anchor = str(p.get("anchor", "session"))
    ema_n = int(p["ema"])
    vwap = ind.session_vwap(df) if anchor == "session" else \
        ind.rolling_vwap(df, int(p.get("window", 20)))
    ema = ind.ema(df["close"], ema_n)
    atr = ind.atr(df, 14)
    warmup = max(ema_n * 3, 20)
    out = _blank(df, warmup, atr * 1.5, 1.5)
    out["long_entry"] = _clean((df["close"] > vwap) & _cross_up(df["close"], ema))
    out["short_entry"] = _clean((df["close"] < vwap) & _cross_down(df["close"], ema))
    out["long_exit"] = _clean(df["close"] < vwap)
    out["short_exit"] = _clean(df["close"] > vwap)
    return out


# ── 10. Buy & hold benchmark ─────────────────────────────────────────────────

def _buy_hold(df: pd.DataFrame, p: dict, ctx: StrategyContext) -> pd.DataFrame:
    atr = ind.atr(df, 14)
    out = _blank(df, 0, atr * 1000.0, 0.0)   # no stop, no target
    if len(df):
        out.iloc[0, out.columns.get_loc("long_entry")] = True
    out["long_exit"] = False
    out["short_entry"] = False
    out["short_exit"] = False
    out["stop_distance"] = np.nan
    out["target_r"] = 0.0
    return out


# ── registry ─────────────────────────────────────────────────────────────────

STRATEGIES: dict[str, StrategySpec] = {}


def register(spec: StrategySpec) -> StrategySpec:
    STRATEGIES[spec.key] = spec
    return spec


register(StrategySpec(
    key="ema_cross", name="EMA/SMA crossover", version=STRATEGY_VERSION,
    description="Long when fast EMA crosses above slow EMA; flat/short on the "
                "opposite cross. ATR stop (2×ATR14), 2R target.",
    param_grid={"fast": (8, 12, 20), "slow": (21, 34, 55, 89, 144)},
    build=_ema_cross, warmup=164, target_r=2.0,
))
register(StrategySpec(
    key="ema_adx_trend", name="EMA + ADX trend following", version=STRATEGY_VERSION,
    description="Trade only when EMA alignment and ADX (>threshold) agree with "
                "DI direction; exit when the trend state breaks. ATR stop.",
    param_grid={"ema_fast": (10, 20), "ema_slow": (50, 100),
                "adx_min": (18, 25), "stop_atr": (2.5,)},
    build=_ema_adx, warmup=200, target_r=2.5,
))
register(StrategySpec(
    key="rsi_meanrev", name="RSI mean reversion", version=STRATEGY_VERSION,
    description="Buy oversold RSI, exit at the midline (and mirror for short "
                "markets). Optional 200-EMA regime filter.",
    param_grid={"rsi": (7, 14), "oversold": (25, 30), "exit": (50, 55),
                "trend_filter": (0, 1)},
    build=_rsi_meanrev, warmup=220, target_r=1.5, time_stop_bars=30,
))
register(StrategySpec(
    key="bb_meanrev", name="Bollinger Band mean reversion", version=STRATEGY_VERSION,
    description="Fade closes outside the Bollinger band, exit at the middle "
                "band. ATR stop protects against trend runaway.",
    param_grid={"period": (20, 30), "std": (1.5, 2.0, 2.5),
                "stop_atr": (2.0,)},
    build=_bb_meanrev, warmup=80, target_r=1.5, time_stop_bars=30,
))
register(StrategySpec(
    key="macd_momentum", name="MACD momentum", version=STRATEGY_VERSION,
    description="Long when the MACD histogram crosses above zero, exit/short on "
                "the opposite cross.",
    param_grid={"fast": (8, 12, 16), "slow": (21, 26, 34), "signal": (9,)},
    build=_macd_momentum, warmup=100, target_r=2.0,
))
register(StrategySpec(
    key="donchian_breakout", name="Donchian channel breakout",
    version=STRATEGY_VERSION,
    description="Breakout of the prior N-bar high, exit on the prior M-bar low "
                "(the classic turtle structure). ATR stop.",
    param_grid={"entry": (20, 40, 55), "exit": (10, 20), "stop_atr": (2.0,)},
    build=_donchian_breakout, warmup=90, target_r=2.0,
))
register(StrategySpec(
    key="atr_breakout", name="ATR breakout", version=STRATEGY_VERSION,
    description="Close above prior N-bar high plus k×ATR (volatility-adjusted "
                "breakout); exit back below the 20-EMA. ATR stop.",
    param_grid={"lookback": (20, 50), "atr_buffer": (0.0, 0.5, 1.0),
                "stop_atr": (1.5, 2.5)},
    build=_atr_breakout, warmup=90, target_r=2.5,
))
register(StrategySpec(
    key="vol_trend", name="Volatility-filtered trend", version=STRATEGY_VERSION,
    description="EMA trend only while realised volatility is below its own "
                "rolling quantile; exits on trend break or a volatility spike.",
    param_grid={"ema_fast": (20, 50), "ema_slow": (100, 200),
                "vol_lookback": (20, 60), "vol_pct": (0.6, 0.8),
                "stop_atr": (2.5,)},
    build=_vol_trend, warmup=320, target_r=2.5,
))
register(StrategySpec(
    key="vwap_intraday", name="VWAP intraday", version=STRATEGY_VERSION,
    description="Intraday only: long above session VWAP when price crosses up "
                "the fast EMA; exit back below VWAP. Not applied to daily bars.",
    param_grid={"ema": (9, 21), "anchor": ("session", "rolling"),
                "window": (20,)},
    build=_vwap_intraday, warmup=70, target_r=1.5,
    intraday_only=True, time_stop_bars=40,
))
register(StrategySpec(
    key="buy_hold", name="Buy & hold benchmark", version=STRATEGY_VERSION,
    description="Buy the first valid bar and hold to the end. The benchmark "
                "every active strategy must beat on a risk-adjusted basis.",
    param_grid={}, build=_buy_hold, warmup=0, target_r=0.0,
))


def get(key: str) -> StrategySpec:
    try:
        return STRATEGIES[key]
    except KeyError:
        raise KeyError(f"unknown strategy '{key}'") from None


def for_market(market_class: str, timeframe: str,
               keys: Iterable[str] | None = None) -> list[StrategySpec]:
    out = []
    for key in (keys or STRATEGIES.keys()):
        spec = STRATEGIES.get(key)
        if spec and spec.allows(market_class, timeframe):
            out.append(spec)
    return out


def describe_all() -> list[dict[str, Any]]:
    return [s.describe() for s in STRATEGIES.values()]
