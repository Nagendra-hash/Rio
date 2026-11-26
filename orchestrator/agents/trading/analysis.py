"""
orchestrator/agents/trading/analysis.py — the ANALYSIS stage: pure math over
provenance-labeled frames.

Stock Analysis is a CAPABILITY of the trading agent, not a separate agent:
every function here takes bars (list of {ts, open, high, low, close, volume})
plus optional quote data and returns plain dicts. No network access, no side
effects — the same functions serve the live pipeline, the paper mode and the
backtester.

What lives here (phase brief capability map):
    technicals()      — EMA stack, RSI, ATR, Bollinger position, momentum
    vwap()            — session VWAP + deviation bands (needs intraday bars)
    volume_profile()  — POC, value area, effort-vs-result volume read
    market_structure()— swing highs/lows, HH/HL vs LH/LL, trend label, BOS
    price_action()    — last-bar context: candle shape, prior-bar rejection,
                        range position, confluence summary
    analyze()         — one call that runs the lot and returns a single
                        analysis card the SIGNAL stage consumes.
"""
from __future__ import annotations

import math
from typing import Any

# ── small helpers ────────────────────────────────────────────────────────────

def _ts_sort_key(ts: str):
    """Comparable key for a bar timestamp, or None when unparseable.
    Numeric epochs and ISO-8601 strings parse; anything else (synthetic ids,
    zero-padded sim labels) keeps INPUT order — string-sorting 't10' vs 't2'
    would silently scramble a chronological series."""
    import datetime as _dt
    try:
        return (0, float(ts))
    except (TypeError, ValueError):
        pass
    try:
        return (1, _dt.datetime.fromisoformat(
            str(ts).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _bars(bars: list[dict]) -> list[dict]:
    """Validated bars: close required. Sorted by timestamp ONLY when every
    ts parses (epoch or ISO); otherwise input order is trusted — both data
    adapters emit chronological bars. Malformed rows are dropped and the
    analysis layers refuse to guess around holes."""
    out = []
    for b in bars or []:
        try:
            c = float(b["close"])
            o = float(b.get("open", c))
            h = float(b.get("high", max(o, c)))
            l = float(b.get("low", min(o, c)))
            v = float(b.get("volume") or 0.0)
        except (TypeError, ValueError, KeyError):
            continue
        if not (math.isfinite(c) and c > 0):
            continue
        out.append({"ts": str(b.get("ts", "")), "open": o, "high": h,
                    "low": l, "close": c, "volume": v})
    if out and all(_ts_sort_key(b["ts"]) is not None for b in out):
        out.sort(key=lambda b: _ts_sort_key(b["ts"]))
    return out


def _ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    k = 2.0 / (period + 1.0)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def _rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0.0)) / period
        al = (al * (period - 1) + max(-d, 0.0)) / period
    if al == 0:
        return 100.0
    rs = ag / al
    return 100.0 - 100.0 / (1.0 + rs)


def _atr(bars: list[dict], period: int = 14) -> float | None:
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    # Wilder smoothing
    a = sum(trs[:period]) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a


def _round_or_none(x, nd: int = 4):
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return round(v, nd) if math.isfinite(v) else None


# ── technicals ────────────────────────────────────────────────────────────────

def technicals(bars: list[dict]) -> dict:
    """EMA 20/50/200 stack, RSI(14), ATR(14), Bollinger %b, ROC(10)."""
    b = _bars(bars)
    closes = [x["close"] for x in b]
    if len(closes) < 5:
        return {"ok": False, "error": f"need ≥5 clean bars, got {len(closes)}"}
    last = closes[-1]
    ema20, ema50, ema200 = (_ema(closes, p) for p in (20, 50, 200))
    rsi = _rsi(closes)
    atr = _atr(b)

    # Bollinger %b (20, 2)
    pct_b = None
    band_width = None
    if len(closes) >= 20:
        window = closes[-20:]
        mean = sum(window) / 20
        var = sum((x - mean) ** 2 for x in window) / 20
        sd = math.sqrt(var)
        if sd > 0:
            pct_b = (last - (mean - 2 * sd)) / (4 * sd)
            band_width = 4 * sd / mean

    # rate of change over 10 bars
    roc = None
    if len(closes) > 10 and closes[-11] > 0:
        roc = (last / closes[-11] - 1.0) * 100.0

    if ema20 and ema50 and ema200:
        if ema20 > ema50 > ema200:
            stack = "bullish"
        elif ema20 < ema50 < ema200:
            stack = "bearish"
        else:
            stack = "mixed"
    else:
        stack = "insufficient_history"

    return {
        "ok": True, "bars_used": len(b),
        "last_close": _round_or_none(last),
        "ema20": _round_or_none(ema20), "ema50": _round_or_none(ema50),
        "ema200": _round_or_none(ema200), "ema_stack": stack,
        "rsi14": _round_or_none(rsi),
        "rsi_label": ("oversold" if rsi is not None and rsi < 30 else
                      "overbought" if rsi is not None and rsi > 70 else "neutral"),
        "atr14": _round_or_none(atr),
        "atr_pct": _round_or_none(atr / last * 100.0) if atr and last else None,
        "bollinger_pct_b": _round_or_none(pct_b),
        "bollinger_band_width": _round_or_none(band_width),
        "roc10": _round_or_none(roc),
    }


# ── VWAP ──────────────────────────────────────────────────────────────────────

def vwap(bars: list[dict], session_bars: int = 0) -> dict:
    """Session VWAP (typical price × volume) over the LAST `session_bars`
    bars (0 = all bars given), plus ±1σ/±2σ bands weighted by volume.

    For daily bars this is an anchored VWAP over the window; hand it intraday
    bars of one session for the classic session VWAP."""
    b = _bars(bars)
    if session_bars > 0:
        b = b[-session_bars:]
    if not b:
        return {"ok": False, "error": "no bars"}
    pv = sum(((x["high"] + x["low"] + x["close"]) / 3.0) * x["volume"] for x in b)
    vol = sum(x["volume"] for x in b)
    if vol <= 0:
        return {"ok": False, "error": "zero volume — VWAP undefined"}
    v = pv / vol
    # volume-weighted variance of typical price
    var = sum(((x["high"] + x["low"] + x["close"]) / 3.0 - v) ** 2 * x["volume"]
              for x in b) / vol
    sd = math.sqrt(var)
    last_close = b[-1]["close"]
    return {"ok": True, "vwap": round(v, 4), "sd": round(sd, 4),
            "upper_1sd": round(v + sd, 4), "lower_1sd": round(v - sd, 4),
            "upper_2sd": round(v + 2 * sd, 4), "lower_2sd": round(v - 2 * sd, 4),
            "last_close": round(last_close, 4),
            "position": ("above" if last_close > v else
                         "below" if last_close < v else "at"),
            "bars_used": len(b)}


# ── volume profile ────────────────────────────────────────────────────────────

def volume_profile(bars: list[dict], bins: int = 24) -> dict:
    """Volume-by-price histogram → POC, value area (70%), effort-vs-result."""
    b = _bars(bars)
    if len(b) < 10:
        return {"ok": False, "error": f"need ≥10 bars, got {len(b)}"}
    lo = min(x["low"] for x in b)
    hi = max(x["high"] for x in b)
    if hi <= lo:
        return {"ok": False, "error": "degenerate range"}
    bins = max(5, min(int(bins), 60))
    step = (hi - lo) / bins
    vol = [0.0] * bins
    for x in b:
        # spread the bar's volume across the bins its range covers
        i0 = max(0, min(bins - 1, int((x["low"] - lo) / step)))
        i1 = max(0, min(bins - 1, int((x["high"] - lo) / step)))
        span = (i1 - i0 + 1)
        for i in range(i0, i1 + 1):
            vol[i] += x["volume"] / span
    total = sum(vol)
    poc_i = max(range(bins), key=lambda i: vol[i])
    poc_price = lo + (poc_i + 0.5) * step

    # value area: expand from POC until 70% of volume is covered
    target = total * 0.70
    lo_i = hi_i = poc_i
    covered = vol[poc_i]
    while covered < target and (lo_i > 0 or hi_i < bins - 1):
        up = vol[hi_i + 1] if hi_i < bins - 1 else -1.0
        dn = vol[lo_i - 1] if lo_i > 0 else -1.0
        if up >= dn:
            hi_i += 1
            covered += max(up, 0.0)
        else:
            lo_i -= 1
            covered += max(dn, 0.0)

    # effort vs result on the last 20 bars: big volume, small travel = absorption
    recent = b[-20:]
    travel = abs(recent[-1]["close"] - recent[0]["open"])
    avg_range = sum(x["high"] - x["low"] for x in recent) / len(recent)
    avg_vol = sum(x["volume"] for x in recent) / len(recent)
    last_vol = recent[-1]["volume"]
    effort_note = "normal"
    if avg_vol > 0:
        vol_ratio = last_vol / avg_vol
        if vol_ratio > 1.8 and travel < avg_range * 0.6:
            effort_note = "absorption"      # effort without result
        elif vol_ratio > 1.8:
            effort_note = "initiative"      # effort WITH result

    return {"ok": True, "poc": round(poc_price, 4),
            "value_area_low": round(lo + lo_i * step, 4),
            "value_area_high": round(lo + (hi_i + 1) * step, 4),
            "bin_size": round(step, 4),
            "last_close": round(b[-1]["close"], 4),
            "in_value_area": (lo + lo_i * step) <= b[-1]["close"] <= (lo + (hi_i + 1) * step),
            "effort_last_bar": effort_note}


# ── market structure ─────────────────────────────────────────────────────────

def _swings(bars: list[dict], k: int = 2) -> tuple[list[dict], list[dict]]:
    """Fractal pivots with left+right confirmation of k bars."""
    highs, lows = [], []
    for i in range(k, len(bars) - k):
        window = bars[i - k:i + k + 1]
        if bars[i]["high"] == max(w["high"] for w in window):
            highs.append({"i": i, "price": bars[i]["high"], "ts": bars[i]["ts"]})
        if bars[i]["low"] == min(w["low"] for w in window):
            lows.append({"i": i, "price": bars[i]["low"], "ts": bars[i]["ts"]})
    return highs, lows


def market_structure(bars: list[dict], pivot_k: int = 2) -> dict:
    """Swing sequence → trend label + last break-of-structure."""
    b = _bars(bars)
    if len(b) < 2 * pivot_k + 5:
        return {"ok": False,
                "error": f"need ≥{2 * pivot_k + 5} bars for structure, got {len(b)}"}
    highs, lows = _swings(b, pivot_k)

    def _seq(vals: list[float]) -> str:
        if len(vals) < 2:
            return "n/a"
        up = all(vals[i + 1] > vals[i] for i in range(len(vals) - 1))
        dn = all(vals[i + 1] < vals[i] for i in range(len(vals) - 1))
        if up:
            return "rising"
        if dn:
            return "falling"
        return "alternating"

    hh = _seq([h["price"] for h in highs[-3:]])
    hl = _seq([l["price"] for l in lows[-3:]])
    if hh == "rising" and hl in ("rising", "alternating"):
        trend = "uptrend"          # HH + HL
    elif hh == "falling" and hl in ("falling", "alternating"):
        trend = "downtrend"        # LH + LL
    elif hh == "falling" and hl == "rising":
        trend = "contracting"      # triangle / squeeze
    elif hh == "rising" and hl == "falling":
        trend = "expanding"
    else:
        trend = "range"

    # break of structure: close beyond the last confirmed pivot
    bos = None
    last_close = b[-1]["close"]
    if highs:
        last_high = highs[-1]["price"]
        if last_close > last_high:
            bos = {"type": "bullish", "level": round(last_high, 4),
                   "ts_confirmed": b[-1]["ts"]}
    if bos is None and lows:
        last_low = lows[-1]["price"]
        if last_close < last_low:
            bos = {"type": "bearish", "level": round(last_low, 4),
                   "ts_confirmed": b[-1]["ts"]}

    last_high_price = round(highs[-1]["price"], 4) if highs else None
    last_low_price = round(lows[-1]["price"], 4) if lows else None
    return {"ok": True, "trend": trend,
            "swing_highs": [round(h["price"], 4) for h in highs[-4:]],
            "swing_lows": [round(l["price"], 4) for l in lows[-4:]],
            "last_swing_high": last_high_price,
            "last_swing_low": last_low_price,
            "break_of_structure": bos,
            "pivots_confirmed_by": pivot_k}


# ── price action (last-bar context) ──────────────────────────────────────────

def price_action(bars: list[dict]) -> dict:
    """Candle shape, wick rejection, range position of the last close."""
    b = _bars(bars)
    if len(b) < 3:
        return {"ok": False, "error": f"need ≥3 bars, got {len(b)}"}
    last, prev = b[-1], b[-2]
    rng = max(last["high"] - last["low"], 1e-9)
    body = abs(last["close"] - last["open"])
    upper_wick = last["high"] - max(last["close"], last["open"])
    lower_wick = min(last["close"], last["open"]) - last["low"]

    if last["close"] > last["open"]:
        shape = "bullish"
    elif last["close"] < last["open"]:
        shape = "bearish"
    else:
        shape = "doji"

    if lower_wick > body * 2 and shape != "bearish":
        shape = "hammer_like"
    elif upper_wick > body * 2 and shape != "bullish":
        shape = "shooting_star_like"

    # position within the recent range (20 bars)
    window = b[-20:]
    rlo = min(x["low"] for x in window)
    rhi = max(x["high"] for x in window)
    rng_pos = (last["close"] - rlo) / (rhi - rlo) if rhi > rlo else None

    # engulfing check
    engulfing = None
    if (last["close"] > last["open"] and prev["close"] < prev["open"]
            and last["close"] >= prev["open"] and last["open"] <= prev["close"]):
        engulfing = "bullish"
    elif (last["close"] < last["open"] and prev["close"] > prev["open"]
          and last["close"] <= prev["open"] and last["open"] >= prev["close"]):
        engulfing = "bearish"

    return {"ok": True, "last_bar": {"ts": last["ts"],
                                     "o": round(last["open"], 4),
                                     "h": round(last["high"], 4),
                                     "l": round(last["low"], 4),
                                     "c": round(last["close"], 4)},
            "shape": shape,
            "body_pct_of_range": round(body / rng, 3),
            "upper_wick_pct": round(upper_wick / rng, 3),
            "lower_wick_pct": round(lower_wick / rng, 3),
            "range_position_20b": _round_or_none(rng_pos),
            "engulfing": engulfing,
            "gap_from_prev": round(last["open"] - prev["close"], 4)}


# ── the one-call analysis card ────────────────────────────────────────────────

def analyze(bars: list[dict], quote: dict | None = None,
            vwap_session_bars: int = 0) -> dict:
    """Full stock-analysis card. `quote` is an optional provenance-labeled
    quote payload (data.DataResult.data). Missing pieces are reported as
    absent — never padded with invented values."""
    b = _bars(bars)
    if len(b) < 5:
        return {"ok": False, "error": f"need ≥5 clean bars, got {len(b)}"}

    tech = technicals(b)
    struct = market_structure(b)
    pa = price_action(b)
    vp = volume_profile(b)
    vwap_res = vwap(b, session_bars=vwap_session_bars)

    # confluence: cheap vote tally used by the SIGNAL stage
    votes: dict[str, int] = {}
    if tech.get("ok"):
        stack = tech.get("ema_stack")
        votes["ema_stack"] = 1 if stack == "bullish" else -1 if stack == "bearish" else 0
        rsi = tech.get("rsi14")
        if rsi is not None:
            votes["rsi"] = 1 if rsi < 30 else -1 if rsi > 70 else 0
    if struct.get("ok"):
        t = struct.get("trend")
        votes["structure"] = 1 if t == "uptrend" else -1 if t == "downtrend" else 0
        if struct.get("break_of_structure"):
            votes["bos"] = 1 if struct["break_of_structure"]["type"] == "bullish" else -1
    if pa.get("ok"):
        votes["candle"] = 1 if pa.get("engulfing") == "bullish" else \
            -1 if pa.get("engulfing") == "bearish" else 0
    if vwap_res.get("ok"):
        votes["vwap"] = 1 if vwap_res.get("position") == "above" else \
            -1 if vwap_res.get("position") == "below" else 0
    if vp.get("ok"):
        votes["value_area"] = 1 if vp.get("in_value_area") else 0

    return {"ok": True,
            "technicals": tech, "structure": struct, "price_action": pa,
            "volume_profile": vp, "vwap": vwap_res,
            "confluence_votes": votes,
            "quote_overlay": {k: quote.get(k) for k in
                              ("last", "previous_close", "day_high", "day_low",
                               "volume", "currency")
                              if isinstance(quote, dict) and quote.get(k) is not None}}
