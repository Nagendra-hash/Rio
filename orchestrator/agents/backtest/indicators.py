"""
orchestrator/agents/backtest/indicators.py — vectorised indicators.

All functions are causal: the value at bar i uses bars ≤ i only. No centred
windows, no negative shifts, no future data. Wilder smoothing (RSI/ATR/ADX) is
implemented with ewm(alpha=1/n, adjust=False), which is the standard
recursive definition and is identical to the classic warm-up formula after
the first n bars.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(int(n), min_periods=int(n)).mean()


def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=int(n), adjust=False, min_periods=int(n)).mean()


def rsi(series: pd.Series, n: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / int(n), adjust=False, min_periods=int(n)).mean()
    avg_loss = loss.ewm(alpha=1.0 / int(n), adjust=False, min_periods=int(n)).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    return out.where(avg_loss != 0, 100.0).where(avg_gain.notna())


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    ranges = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1)
    return ranges.max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    tr = true_range(df)
    return tr.ewm(alpha=1.0 / int(n), adjust=False, min_periods=int(n)).mean()


def adx(df: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0),
                         index=df.index)
    tr = true_range(df)
    alpha = 1.0 / int(n)
    atr_n = tr.ewm(alpha=alpha, adjust=False, min_periods=int(n)).mean()
    plus_di = 100.0 * plus_dm.ewm(alpha=alpha, adjust=False,
                                  min_periods=int(n)).mean() / atr_n
    minus_di = 100.0 * minus_dm.ewm(alpha=alpha, adjust=False,
                                   min_periods=int(n)).mean() / atr_n
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_n = dx.ewm(alpha=alpha, adjust=False, min_periods=int(n)).mean()
    return pd.DataFrame({"adx": adx_n, "plus_di": plus_di, "minus_di": minus_di})


def bollinger(series: pd.Series, n: int = 20, k: float = 2.0
              ) -> pd.DataFrame:
    mid = sma(series, n)
    sd = series.rolling(int(n), min_periods=int(n)).std(ddof=0)
    return pd.DataFrame({"mid": mid, "upper": mid + k * sd,
                         "lower": mid - k * sd})


def macd(series: pd.Series, fast: int = 12, slow: int = 26,
         signal: int = 9) -> pd.DataFrame:
    line = ema(series, fast) - ema(series, slow)
    sig = line.ewm(span=int(signal), adjust=False, min_periods=int(signal)).mean()
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})


def donchian(df: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    upper = df["high"].rolling(int(n), min_periods=int(n)).max()
    lower = df["low"].rolling(int(n), min_periods=int(n)).min()
    return pd.DataFrame({"upper": upper, "lower": lower,
                         "mid": (upper + lower) / 2.0})


def realized_vol(series: pd.Series, n: int = 20) -> pd.Series:
    ret = np.log(series / series.shift(1))
    return ret.rolling(int(n), min_periods=int(n)).std(ddof=0)


def roc(series: pd.Series, n: int = 20) -> pd.Series:
    return series.pct_change(int(n))


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """VWAP anchored at each UTC calendar day (00:00 boundary)."""
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = typical * df["volume"].fillna(0.0)
    day = pd.Series(df.index.floor("1D"), index=df.index)
    cum_pv = pv.groupby(day).cumsum()
    cum_v = df["volume"].fillna(0.0).groupby(day).cumsum()
    return (cum_pv / cum_v.replace(0.0, np.nan)).ffill()


def rolling_vwap(df: pd.DataFrame, n: int = 20) -> pd.Series:
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = (typical * df["volume"].fillna(0.0)).rolling(int(n),
                                                      min_periods=int(n)).sum()
    v = df["volume"].fillna(0.0).rolling(int(n), min_periods=int(n)).sum()
    return pv / v.replace(0.0, np.nan)
