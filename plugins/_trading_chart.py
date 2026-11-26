"""
plugins/trading_chart.py — TradingView-style auto charting for the trading skill.

Renders a dark-theme candlestick chart PNG with ALL levels drawn automatically:
  · auto support/resistance swing levels (pivot highs/lows, freshness-weighted)
  · EMA fast/mid/slow + EMA200 from the method profile
  · volatility trail (BOSWaves port) as a dashed trend line
  · when a signal fires: the LONG/SHORT position tool — entry line, stop line,
    staged target lines (TP1/TP2/TP3), shaded risk (red) and reward (green)
    boxes, ⅓-scale partial fills as each target would bank, R badges and
    right-edge price tags with hit markers.
  · always: last-price tag, symbol/method/interval header, signal caption.

The drawing protocol mirrors TradingView primitives:
  hline(price, color, width, style)  — horizontal level
  zone(y0, y1, color, alpha)         — shaded box (risk/reward, S/R bands)
  label(text, price, side, color)    — right-edge / floating text tag

Saved under memory/trading/charts/ and copied into uploads/ when the dashboard
exists, so it's viewable from the phone: Files → chart_<SYMBOL>_<method>.png.
PIL-only (no matplotlib dependency).
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
_CHART_DIR = _BASE / "memory" / "trading" / "charts"


def _uploads_dirs() -> list[Path]:
    """Where published files should land. The dashboard's own UPLOADS_DIR is
    the canonical target (it decides Downloads/Documents/project-root by
    writability at startup); project-root uploads kept as a fallback."""
    dirs = []
    try:
        from dashboard.server import UPLOADS_DIR

        dirs.append(Path(UPLOADS_DIR))
    except Exception:
        pass
    dirs.append(_BASE / "uploads")
    return dirs


# ── TradingView-dark palette ──────────────────────────────────────────────────
BG = (19, 23, 34)  # #131722
PANEL = (24, 28, 40)
GRID = (36, 41, 56)
TEXT_DIM = (131, 141, 162)
TEXT = (222, 228, 240)
BULL = (38, 166, 154)  # teal green
BEAR = (239, 83, 80)  # red
ACCENT = (41, 98, 255)  # blue (entry)
ORANGE = (255, 152, 0)  # break-even / trail
YELLOW = (255, 213, 79)  # TP lines
PURPLE = (156, 92, 232)  # EMA slow
CYAN = (66, 165, 245)  # EMA fast
PINK = (233, 30, 99)  # EMA mid
SR_DEMAND = (0, 150, 136)
SR_SUPPLY = (190, 60, 60)
FVG_BULL = (0, 176, 120)  # fair value gap fills (distinct teal/green)
FVG_BEAR = (255, 111, 97)
SWEEP_COL = (255, 214, 10)  # liquidity sweep markers (bright gold)
SWEEP_COL_BULL = (255, 214, 10)

_CANDIDATE_FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
    "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
    "/Library/Fonts/Arial.ttf",
]


def _font(size: int):
    try:
        from PIL import ImageFont

        for p in _CANDIDATE_FONTS:
            if Path(p).exists():
                return ImageFont.truetype(p, size)
        return ImageFont.load_default()
    except Exception:
        from PIL import ImageFont

        return ImageFont.load_default()


def _hex2rgb(h) -> tuple:
    if isinstance(h, tuple):
        return h
    h = str(h).lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))


def _mix(c1, c2, t: float) -> tuple:
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))


def _with_alpha(rgb, alpha: float) -> tuple:
    """Blend a color toward the chart background — PIL has no alpha compositing
    on RGB images, so pre-blend (this is exactly what a translucent fill does
    on a known background)."""
    return _mix(BG, rgb, max(0.0, min(1.0, alpha)))


# ══════════════════════════════════════════════════════════════════════════════
# Level discovery — the "TradingView tools" the engine draws automatically
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# Structural zones — order blocks & reversal pivots (ported detectors)
# ══════════════════════════════════════════════════════════════════════════════


def detect_fvg(df, max_gaps: int = 8, min_gap_atr: float = 0.15) -> list[dict]:
    """Fair value gaps (ICT 3-candle imbalance). Bullish: bar[i-2].high <
    bar[i].low — the space between is untraded 'fair value' acting as support.
    Bearish mirrored. A gap is 'filled' once a later wick fully traverses it.
    Overlapping same-side gaps are deduped to the newest. Returns oldest-first:
    [{'dir': 1|−1, 'top', 'bottom', 'bar', 'filled'}]"""
    hi = df["High"].values
    lo = df["Low"].values
    cl = df["Close"].values
    atr = df["atr"].values if "atr" in df.columns else None
    n = len(df)

    def _a(i):
        return atr[i] if atr is not None and atr[i] > 0 else cl[i] * 0.02

    gaps: list[dict] = []
    for i in range(2, n):
        a = _a(i)
        g_top, g_bot = float(lo[i]), float(hi[i - 2])  # bullish gap
        if g_top - g_bot >= min_gap_atr * a:
            last = next((g for g in reversed(gaps) if g["dir"] == 1), None)
            overlaps = last is not None and min(last["top"], g_top) - max(
                last["bottom"], g_bot
            ) > 0.5 * min(last["top"] - last["bottom"], g_top - g_bot)
            if overlaps and last:
                gaps.remove(last)  # keep the freshest imbalance
            gaps.append(
                {
                    "dir": 1,
                    "top": g_top,
                    "bottom": g_bot,
                    "bar": int(i - 1),
                    "filled": False,
                }
            )
        g_top, g_bot = float(lo[i - 2]), float(hi[i])  # bearish gap
        if g_bot - g_top >= min_gap_atr * a:
            last = next((g for g in reversed(gaps) if g["dir"] == -1), None)
            overlaps = last is not None and min(last["top"], g_bot) - max(
                last["bottom"], g_top
            ) > 0.5 * min(last["top"] - last["bottom"], g_bot - g_top)
            if overlaps and last:
                gaps.remove(last)
            gaps.append(
                {
                    "dir": -1,
                    "top": g_bot,
                    "bottom": g_top,
                    "bar": int(i - 1),
                    "filled": False,
                }
            )
    # fill state: any later wick that fully traverses the gap
    for g in gaps:
        for j in range(g["bar"] + 2, n):
            if g["dir"] == 1 and lo[j] <= g["bottom"]:
                g["filled"] = True
                break
            if g["dir"] == -1 and hi[j] >= g["top"]:
                g["filled"] = True
                break
    return gaps[-max_gaps:]


def detect_liquidity_sweeps(
    df, k: int = 3, max_sweeps: int = 6, search_win: int = 40
) -> list[dict]:
    """Stop-hunt sweeps: a wick takes out a prior swing high/low but the bar
    CLOSES back on the other side — liquidity grabbed, level failed.
    kind −1 = swept a high (bearish), kind 1 = swept a low (bullish).
    Returns oldest-first: [{'kind', 'level', 'wick', 'bar', 'pivot_bar'}]"""
    hi = df["High"].values
    lo = df["Low"].values
    cl = df["Close"].values
    n = len(df)
    sweeps: list[dict] = []
    for p in range(k, n - k - 1):
        if hi[p] == hi[p - k : p + k + 1].max():
            for i in range(p + k + 1, min(n, p + k + search_win)):
                if cl[i] > hi[p]:
                    break  # level broken for real — no sweep
                if hi[i] > hi[p] and cl[i] < hi[p]:
                    sweeps.append(
                        {
                            "kind": -1,
                            "level": float(hi[p]),
                            "wick": float(hi[i]),
                            "bar": int(i),
                            "pivot_bar": int(p),
                        }
                    )
                    break
        if lo[p] == lo[p - k : p + k + 1].min():
            for i in range(p + k + 1, min(n, p + k + search_win)):
                if cl[i] < lo[p]:
                    break
                if lo[i] < lo[p] and cl[i] > lo[p]:
                    sweeps.append(
                        {
                            "kind": 1,
                            "level": float(lo[p]),
                            "wick": float(lo[i]),
                            "bar": int(i),
                            "pivot_bar": int(p),
                        }
                    )
                    break
    seen, out = set(), []
    for s in sweeps:
        if s["bar"] not in seen:
            seen.add(s["bar"])
            out.append(s)
    return out[-max_sweeps:]


def detect_order_blocks(
    df, swing_len: int = 10, max_blocks: int = 6, max_atr_mult: float = 3.5
) -> list[dict]:
    """Volumized order blocks (Rupendra port). A block is REGISTERED the moment
    a volume-backed close breaks the running swing high/low: the origin candle
    (the bar before the breakout) body becomes the box. A block is 'broken'
    once a wick crosses through its far edge (Pine's 'Wick' invalidation).
    Near-duplicate origins from consecutive breakout bars (smooth ramps) are
    merged so real charts don't get box spam.
    Returns newest-last: [{'dir': 1|−1, 'top', 'bottom', 'bar', 'broken'}]"""
    import numpy as np

    hi = df["High"].values
    lo = df["Low"].values
    op = df["Open"].values
    cl = df["Close"].values
    vol = df["Volume"].values
    vol_sma = (
        df["vol_sma"].values
        if "vol_sma" in df.columns
        else np.full(len(df), vol.mean() if len(vol) else 1.0)
    )
    atr = df["atr"].values if "atr" in df.columns else None
    n = len(df)

    def _atr_at(i):
        return atr[i] if atr is not None and atr[i] > 0 else cl[i] * 0.02

    blocks: list[dict] = []
    for i in range(swing_len + 2, n):
        w = slice(i - swing_len, i)
        volx = vol[i] / (vol_sma[i] if vol_sma[i] > 0 else 1.0)
        new_dir = 0
        if cl[i] > hi[w].max() and volx > 1.3:
            new_dir = 1
        elif cl[i] < lo[w].min() and volx > 1.3:
            new_dir = -1
        if new_dir:
            b = i - 1
            top, bot = float(max(op[b], cl[b])), float(min(op[b], cl[b]))
            if 0 < top - bot <= max_atr_mult * _atr_at(i):
                last_same = next(
                    (x for x in reversed(blocks) if x["dir"] == new_dir), None
                )
                dup = (
                    last_same is not None
                    and i - last_same["bar"] <= 3
                    and abs(last_same["top"] - top) < 0.5 * (top - bot)
                )
                if not dup:
                    blocks.append(
                        {
                            "dir": new_dir,
                            "top": top,
                            "bottom": bot,
                            "bar": int(b),
                            "broken": False,
                        }
                    )
        # wick invalidation for every live block (Pine 'Wick' method)
        for x in blocks:
            if x["broken"]:
                continue
            if x["dir"] == 1 and lo[i] < x["bottom"]:
                x["broken"] = True
            elif x["dir"] == -1 and hi[i] > x["top"]:
                x["broken"] = True
        if len(blocks) > 60:
            blocks = blocks[-60:]
    return blocks[-max_blocks:]


def detect_reversal_pivots(
    df, k: int = 3, atr_mult: float = 2.5, max_pivots: int = 6
) -> list[dict]:
    """Reversal pivots (trade_w_samet port, faithful): track the running
    extreme; whenever price reverses ≥ atr_mult×ATR away from it — however
    many bars that takes — the extreme bar becomes a confirmed pivot zone and
    tracking flips to the other side."""
    hi = df["High"].values
    lo = df["Low"].values
    cl = df["Close"].values
    atr10 = df["atr10"].values if "atr10" in df.columns else None
    n = len(df)

    def _atr_at(i):
        return atr10[i] if atr10 is not None and atr10[i] > 0 else cl[i] * 0.02

    pivots: list[dict] = []
    run_hi = run_lo = None
    run_hi_bar = run_lo_bar = 0
    swing = 1  # 1 = tracking for a high pivot
    for i in range(1, n):
        a = _atr_at(i)
        if run_hi is None:
            run_hi, run_lo = hi[i], lo[i]
            run_hi_bar = run_lo_bar = i
            continue
        if swing == 1:
            if hi[i] > run_hi:
                run_hi, run_hi_bar = hi[i], i
            if run_hi - lo[i] >= atr_mult * a:
                pivots.append(
                    {
                        "dir": -1,
                        "price": float(run_hi),
                        "bar": int(run_hi_bar),
                        "confirmed_bar": int(i),
                    }
                )
                swing = -1
                run_lo, run_lo_bar = lo[i], i
        else:
            if lo[i] < run_lo:
                run_lo, run_lo_bar = lo[i], i
            if hi[i] - run_lo >= atr_mult * a:
                pivots.append(
                    {
                        "dir": 1,
                        "price": float(run_lo),
                        "bar": int(run_lo_bar),
                        "confirmed_bar": int(i),
                    }
                )
                swing = 1
                run_hi, run_hi_bar = hi[i], i
    return pivots[-max_pivots:]


def auto_sr_levels(
    df, lookback: int = 250, max_levels: int = 6, atr_mult: float = 1.5
) -> dict:
    """Auto support/resistance from swing pivots: a pivot high/low must reverse
    ≥ atr_mult×ATR before it's confirmed (Zone-Risk-Pro style). Returns
    {'support': [(price, bars_ago), ...], 'resistance': [...]} nearest-first,
    only levels touched recently enough to still matter."""
    hi = df["High"].values
    lo = df["Low"].values
    close = df["Close"].values
    atr = df["atr"].values if "atr" in df.columns else None
    n = len(df)
    start = max(10, n - lookback)
    atr_at = lambda i: atr[i] if atr is not None and atr[i] > 0 else close[i] * 0.02

    sup, res = [], []
    k = 5
    for i in range(start + k, n - k):
        w = 2 * atr_at(i)
        if hi[i] == hi[i - k : i + k + 1].max():
            # confirmed only if price fell away by ≥ atr_mult×ATR after
            if hi[i] - lo[i : i + k + 1].min() >= atr_mult * w * 0.5:
                res.append((float(hi[i]), n - 1 - i))
        if lo[i] == lo[i - k : i + k + 1].min():
            if hi[i : i + k + 1].max() - lo[i] >= atr_mult * w * 0.5:
                sup.append((float(lo[i]), n - 1 - i))

    # dedupe near-identical levels (within 0.25×ATR), keep the most recent
    def _dedupe(levels):
        levels.sort(key=lambda x: x[1])  # most recent first
        out = []
        for px, age in levels:
            if all(abs(px - p) > 0.25 * atr_at(n - 1) for p, _ in out):
                out.append((px, age))
        return out[:max_levels]

    return {"support": _dedupe(sup), "resistance": _dedupe(res)}


def build_drawing_plan(
    df, method: str, signal: dict | None, rules: dict | None = None
) -> dict:
    """Everything the renderer should draw, in TradingView-primitive form."""
    from plugins.trading_strategy import METHODS

    prof = METHODS[method]
    mkt = None
    try:
        from plugins.trading_strategy import detect_market

        mkt = detect_market(str(df.attrs.get("symbol", "")))
    except Exception:
        pass
    last = df.iloc[-1]
    px = float(last["Close"])
    atr = float(last["atr"]) or px * 0.02

    plan = {
        "symbol": str(df.attrs.get("symbol", "?")),
        "method": method,
        "interval": prof["interval"],
        "market": (mkt or {}).get("market", "?"),
        "session": (mkt or {}).get("session", "?"),
        "price": px,
        "atr": atr,
        "sr": auto_sr_levels(df),
        "order_blocks": detect_order_blocks(df),
        "pivots": detect_reversal_pivots(df),
        "fvgs": detect_fvg(df),
        "sweeps": detect_liquidity_sweeps(df),
        "hlines": [],
        "zones": [],
        "tags": [],
        "position": None,
        "signal_text": "",
        "signal_dir": 0,
    }

    if signal is None and rules is not None:
        signal = _signal_safe(df, rules, prof)
    if signal:
        plan["signal_text"] = (
            f"{signal['direction']} (score {signal['score']}{signal.get('ml_note', '')})"
        )
        plan["signal_dir"] = (
            1
            if signal["direction"] == "LONG"
            else -1
            if signal["direction"] == "SHORT"
            else 0
        )
        if plan["signal_dir"] != 0:
            d = plan["signal_dir"]
            stop = px - d * prof["atr_stop"] * atr
            risk = abs(px - stop) or 1e-9
            tgs = [px + d * rr * risk for rr in prof["targets_r"]]
            plan["position"] = {
                "dir": d,
                "entry": px,
                "stop": stop,
                "targets": tgs,
                "rr": prof["targets_r"],
                "risk": risk,
                "entry_zone": (
                    px - prof["atr_entry"] * atr,
                    px + prof["atr_entry"] * atr,
                ),
            }
    return plan


def _signal_safe(df, rules, prof) -> dict | None:
    try:
        from plugins.trading_strategy import _signal

        return _signal(df, rules, prof)
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Renderer — PIL candlesticks
# ══════════════════════════════════════════════════════════════════════════════


def render_chart(
    df,
    plan: dict,
    bars: int = 140,
    width: int = 1400,
    height: int = 780,
    out_path: Path | None = None,
) -> Path:
    """Render the plan onto the last `bars` candles and save a PNG."""
    from PIL import Image, ImageDraw

    d2 = df.tail(bars)
    n = len(d2)
    # ── layout
    M_L, M_R, M_T, M_B = 12, 96, 92, 44  # margins; right rail = price axis
    plot_w = width - M_L - M_R
    plot_h = height - M_T - M_B
    vol_h = int(plot_h * 0.14)
    price_h = plot_h - vol_h

    lo = float(d2["Low"].min())
    hi = float(d2["High"].max())
    # include ALL plan levels in the vertical range so nothing is clipped
    extra = [plan["price"]]
    for p, _ in plan["sr"]["support"] + plan["sr"]["resistance"]:
        extra.append(p)
    pos = plan.get("position")
    if pos:
        extra += [pos["entry"], pos["stop"]] + list(pos["targets"])
    pad = (hi - lo) * 0.06 or plan["atr"]
    lo = min([lo] + extra) - pad
    hi = max([hi] + extra) + pad

    def y_of(price: float) -> int:
        return int(M_T + (hi - price) / (hi - lo) * price_h)

    def x_of(i: int) -> int:  # i = 0..n-1 within the window
        return int(M_L + (i + 0.5) / n * plot_w)

    img = Image.new("RGB", (width, height), BG)
    dr = ImageDraw.Draw(img)

    # ── header band
    hdr = (
        f"{plan['symbol']}  ·  {plan['method'].upper()}  ·  "
        f"{plan['interval']}  ·  {plan['market']} ({plan['session']})"
    )
    dr.text((M_L + 4, 18), hdr, fill=TEXT, font=_font(22))
    sig = plan.get("signal_text") or ""
    if sig:
        col = (
            BULL
            if plan.get("signal_dir", 0) > 0
            else BEAR
            if plan.get("signal_dir", 0) < 0
            else TEXT_DIM
        )
        dr.text((M_L + 4, 48), f"Signal: {sig}", fill=col, font=_font(18))
    stamp = datetime.now().strftime("%d %b %Y %H:%M")
    dr.text((width - M_R - 220, 22), stamp, fill=TEXT_DIM, font=_font(15))
    # zone caption line under the signal
    n_ob_fresh = sum(1 for ob in plan.get("order_blocks", []) if not ob["broken"])
    n_pv = len(plan.get("pivots", []))
    n_fvg = sum(1 for g in plan.get("fvgs", []) if not g["filled"])
    n_sw = len(plan.get("sweeps", []))
    if n_ob_fresh or n_pv or n_fvg or n_sw:
        dr.text(
            (M_L + 4, 72),
            f"Order blocks: {n_ob_fresh} fresh · Pivots: {n_pv} · "
            f"FVGs: {n_fvg} open · Sweeps: {n_sw}",
            fill=TEXT_DIM,
            font=_font(15),
        )

    # ── horizontal price grid + right-axis labels
    steps = 8
    for s in range(steps + 1):
        p = lo + (hi - lo) * s / steps
        yy = y_of(p)
        dr.line([(M_L, yy), (width - M_R, yy)], fill=GRID, width=1)
        dr.text((width - M_R + 8, yy - 8), f"{p:,.2f}", fill=TEXT_DIM, font=_font(14))

    # ── S/R zones (freshness-faded translucent bands)
    for kind in ("support", "resistance"):
        col = SR_DEMAND if kind == "support" else SR_SUPPLY
        for px_l, age in plan["sr"][kind][:4]:
            fade = max(0.12, 0.45 - age / 400.0)
            band = plan["atr"] * 0.35
            y0, y1 = y_of(px_l + band), y_of(px_l - band)
            dr.rectangle([M_L, y0, width - M_R, y1], fill=_with_alpha(col, fade * 0.35))
            dr.line(
                [(M_L, y_of(px_l)), (width - M_R, y_of(px_l))],
                fill=_with_alpha(col, fade),
                width=1,
            )

    # ── structural zone boxes: order blocks & reversal pivots.
    #    Boxes span from their origin bar to the right edge (like TradingView
    #    boxes), drawn behind candles; fresh ones solid-fill, broken/mitigated
    #    ones outline-only with a faded fill.
    d2_reset = d2.reset_index(drop=True)
    idx0 = len(df) - n  # window start in the full frame

    def _zone_rect(top, bottom, left_bar, fresh, col):
        """left_bar = absolute bar index in the full df."""
        li = left_bar - idx0
        if li < 0:
            li = 0
        x0 = M_L + li / n * plot_w
        y0, y1 = y_of(top), y_of(bottom)
        if y1 - y0 < 2:
            y0, y1 = y0 - 1, y1 + 1
        if fresh:
            dr.rectangle(
                [x0, y0, width - M_R, y1],
                fill=_with_alpha(col, 0.18),
                outline=_with_alpha(col, 0.75),
                width=1,
            )
        else:
            dr.rectangle(
                [x0, y0, width - M_R, y1],
                fill=_with_alpha(col, 0.05),
                outline=_with_alpha(col, 0.30),
                width=1,
            )
        return x0

    for ob in plan.get("order_blocks", [])[-4:]:
        col = SR_DEMAND if ob["dir"] == 1 else SR_SUPPLY
        _zone_rect(ob["top"], ob["bottom"], ob["bar"], fresh=not ob["broken"], col=col)

    for pv in plan.get("pivots", [])[-4:]:
        col = SR_DEMAND if pv["dir"] == 1 else SR_SUPPLY
        band = plan["atr"] * 0.25
        _zone_rect(
            pv["price"] + band, pv["price"] - band, pv["bar"], fresh=True, col=col
        )

    # ── fair value gaps: thin imbalance boxes from their middle bar rightward
    for g in plan.get("fvgs", [])[-5:]:
        col = FVG_BULL if g["dir"] == 1 else FVG_BEAR
        li = max(0, g["bar"] - idx0)
        x0 = M_L + li / n * plot_w
        y0, y1 = y_of(g["top"]), y_of(g["bottom"])
        if y1 - y0 < 2:
            y0, y1 = y0 - 1, y1 + 1
        if g["filled"]:
            dr.rectangle(
                [x0, y0, width - M_R, y1],
                fill=_with_alpha(col, 0.04),
                outline=_with_alpha(col, 0.28),
                width=1,
            )
        else:
            dr.rectangle(
                [x0, y0, width - M_R, y1],
                fill=_with_alpha(col, 0.20),
                outline=_with_alpha(col, 0.60),
                width=1,
            )

    # ── liquidity sweep markers: gold ✕ at the wick extreme + dotted level
    for s in plan.get("sweeps", [])[-4:]:
        li = s["bar"] - idx0
        if li < 0 or li >= n:
            continue
        x = x_of(li)
        y = y_of(s["wick"])
        r = 5
        dr.line([(x - r, y - r), (x + r, y + r)], fill=SWEEP_COL, width=2)
        dr.line([(x - r, y + r), (x + r, y - r)], fill=SWEEP_COL, width=2)
        # dotted line from the swept pivot to the sweep bar
        xp = x_of(max(0, s["pivot_bar"] - idx0))
        for xx in range(int(xp), int(x), 8):
            dr.line(
                [(xx, y_of(s["level"])), (min(xx + 4, x), y_of(s["level"]))],
                fill=_with_alpha(SWEEP_COL, 0.55),
                width=1,
            )
        # swept-price tick label
        tag = "Sweep ↑" if s["kind"] == 1 else "Sweep ↓"
        dr.text((x + 8, y - 22), tag, fill=SWEEP_COL, font=_font(13))

    # ── position tool FIRST (boxes behind candles, like TV's position tool)
    if pos:
        d = pos["dir"]
        e, s0 = pos["entry"], pos["stop"]
        t1, t2, t3 = (list(pos["targets"]) + [None, None, None])[:3]
        # risk box (entry↔stop)
        dr.rectangle(
            [M_L, y_of(max(e, s0)), width - M_R, y_of(min(e, s0))],
            fill=_with_alpha(BEAR, 0.16),
        )
        # reward box (entry↔TP3)
        if t3:
            dr.rectangle(
                [M_L, y_of(max(e, t3)), width - M_R, y_of(min(e, t3))],
                fill=_with_alpha(BULL, 0.12),
            )
        # staged reward fills: TP1 third, TP2 second third
        for tg, frac in ((t1, 0.12), (t2, 0.12)):
            if tg:
                dr.rectangle(
                    [M_L, y_of(max(e, tg)), width - M_R, y_of(min(e, tg))],
                    fill=_with_alpha(BULL, frac),
                )
        # entry-zone band
        ez0, ez1 = pos["entry_zone"]
        dr.rectangle(
            [M_L, y_of(max(ez0, ez1)), width - M_R, y_of(min(ez0, ez1))],
            fill=_with_alpha(ACCENT, 0.10),
        )

    # ── volume histogram
    vmax = float(d2["Volume"].max()) or 1.0
    for i in range(n):
        v = float(d2["Volume"].iloc[i]) / vmax
        x = x_of(i)
        bh = int(v * (vol_h - 6))
        up = d2["Close"].iloc[i] >= d2["Open"].iloc[i]
        dr.rectangle(
            [x - 3, M_T + price_h + vol_h - bh, x + 3, M_T + price_h + vol_h],
            fill=_with_alpha(BULL if up else BEAR, 0.35),
        )

    # ── candles
    body_w = max(2, int(plot_w / n * 0.62))
    for i in range(n):
        o = float(d2["Open"].iloc[i])
        c = float(d2["Close"].iloc[i])
        h = float(d2["High"].iloc[i])
        l = float(d2["Low"].iloc[i])
        x = x_of(i)
        up = c >= o
        col = BULL if up else BEAR
        dr.line([(x, y_of(h)), (x, y_of(l))], fill=col, width=1)
        y_o, y_c = y_of(o), y_of(c)
        top, bot = min(y_o, y_c), max(y_o, y_c)
        if bot - top < 1:
            bot = top + 1
        dr.rectangle(
            [x - body_w // 2, top, x + body_w // 2, bot], fill=col if not up else col
        )

    # ── EMAs + trail (over candles)
    def _plot_series(col_name, color, w=2, dashed=False):
        pts = [
            (x_of(i), y_of(float(v)))
            for i, v in enumerate(d2[col_name])
            if v == v and abs(float(v)) > 0
        ]  # skip NaNs
        if dashed:
            for a, b in zip(pts[:-1], pts[1:]):
                if (a[0] // 6) % 2 == 0:
                    dr.line([a, b], fill=color, width=w)
        else:
            dr.line(pts, fill=color, width=w, joint="curve")

    _plot_series("ema_f", CYAN, 2)
    _plot_series("ema_m", PINK, 2)
    _plot_series("ema_s", PURPLE, 2)
    if "ema200" in d2.columns:
        _plot_series("ema200", TEXT_DIM, 1, dashed=True)
    if "trail" in d2.columns:
        _plot_series("trail", ORANGE, 2, dashed=True)

    # ── position levels: lines + right-edge tags (drawn over everything)
    def _hline(price, color, w=2, dashed=False, tag=None, tag_col=None):
        yy = y_of(price)
        if dashed:
            for x0 in range(M_L, width - M_R, 10):
                dr.line([(x0, yy), (min(x0 + 5, width - M_R), yy)], fill=color, width=w)
        else:
            dr.line([(M_L, yy), (width - M_R, yy)], fill=color, width=w)
        if tag:
            lbl = f"{tag} {price:,.2f}"
            tw = dr.textlength(lbl, font=_font(14))
            ty = max(8, min(height - 24, yy - 9))
            fillc = tag_col or color
            dr.rectangle([width - M_R + 2, ty - 2, width - 2, ty + 20], fill=fillc)
            dr.text((width - M_R + 6, ty), lbl, fill=BG, font=_font(14))

    if pos:
        d = pos["dir"]
        _hline(pos["entry"], ACCENT, 2, tag="ENTRY" if d == 1 else "ENTRY S")
        _hline(pos["stop"], BEAR, 2, dashed=True, tag="STOP")
        names = ["TP1", "TP2", "TP3"]
        for j, tg in enumerate(pos["targets"][:3]):
            if tg:
                _hline(tg, YELLOW, 2, dashed=(j > 0), tag=names[j])

    # ── last-price tag (always)
    lp = plan["price"]
    _hline(lp, TEXT_DIM, 1, dashed=True, tag="LST")

    # ── legend (two rows: indicators + zones)
    lg1 = [
        ("EMA fast", CYAN),
        ("EMA mid", PINK),
        ("EMA slow", PURPLE),
        ("EMA200", TEXT_DIM),
        ("Trail", ORANGE),
    ]
    x_lg = M_L + 6
    for name, col in lg1:
        dr.line([(x_lg, height - 40), (x_lg + 22, height - 40)], fill=col, width=3)
        dr.text((x_lg + 28, height - 48), name, fill=TEXT_DIM, font=_font(13))
        x_lg += 28 + dr.textlength(name, font=_font(13)) + 18
    # zone swatches: filled = fresh, outline = broken/mitigated
    sw = [
        ("OB fresh", SR_DEMAND, True),
        ("OB broken", SR_SUPPLY, False),
        ("Pivot", ORANGE, True),
        ("FVG", FVG_BULL, True),
        ("Sweep", SWEEP_COL, None),
    ]
    for name, col, filled in sw:
        y_sw = height - 18
        if filled is None:  # marker swatch: ✕
            dr.line([(x_lg + 2, y_sw - 6), (x_lg + 16, y_sw + 2)], fill=col, width=2)
            dr.line([(x_lg + 2, y_sw + 2), (x_lg + 16, y_sw - 6)], fill=col, width=2)
        elif filled:
            dr.rectangle(
                [x_lg, y_sw - 7, x_lg + 20, y_sw + 3],
                fill=_with_alpha(col, 0.30),
                outline=_with_alpha(col, 0.9),
                width=1,
            )
        else:
            dr.rectangle(
                [x_lg, y_sw - 7, x_lg + 20, y_sw + 3],
                fill=_with_alpha(col, 0.06),
                outline=_with_alpha(col, 0.45),
                width=1,
            )
        dr.text((x_lg + 26, y_sw - 8), name, fill=TEXT_DIM, font=_font(13))
        x_lg += 26 + dr.textlength(name, font=_font(13)) + 18

    out = (
        Path(out_path)
        if out_path
        else _CHART_DIR / f"chart_{plan['symbol'].replace('/', '_').replace(':', '_')}"
        f"_{plan['method']}.png"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG")
    _publish(out)
    return out


def _publish(path: Path) -> list[str]:
    """Copy the chart into the dashboard's uploads dir so its Files tab
    serves it (phone/desktop). Returns destination paths."""
    copied = []
    seen = set()
    for udir in _uploads_dirs():
        if str(udir) in seen:
            continue
        seen.add(str(udir))
        try:
            udir.mkdir(parents=True, exist_ok=True)
            dst = udir / path.name
            shutil.copyfile(path, dst)
            copied.append(str(dst))
        except Exception:
            continue
    return copied
