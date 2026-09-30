"""
plugins/trading_strategy.py — JARVIS Trading Setup & Adaptive Strategy Skill.

Drop-in plugin (discovered automatically, no core edits).

WHAT IT DOES
  setup      — full trade plan for a symbol/method: rule vote, entry zone,
               staged stops/targets, position size, exit policy.
  analyze    — same brain, short read.
  scan       — rank the whole watchlist by signal score for a method.
  backtest   — historical performance of the current weighted rules with the
               method's own exit policy (staged targets, break-even, time stop).
  evaluate   — score recently completed signals vs what actually happened,
               journal them (per method) and adapt rule weights. Runs nightly.
  train      — fit the ML confirmation model (torch logistic or TF MLP).
  report     — performance stats + per-rule hit rates + current weights.

STRATEGY SOURCE — ported from nine TradingView Pine indicators:
  ema_trend      — EMA50/EMA200 trend stack (Grid/Trend EA core)
  triple_rsi     — Triple-RSI mean reversion (RSI5 <30, falling 3 bars,
                   was <60 three bars ago, above the 200 MA) + bearish mirror
  sd_zones       — supply/demand: origin candle before a momentum breakout,
                   traded on the first bullish/bearish retest
  order_blocks   — volumized order blocks: 10-bar swing break on 1.3× volume
  vol_trail      — Hull+ATR volatility trail flips (BOSWaves)
  reversal_pivot — swing pivot confirmed by a ≥2.5×ATR reversal move
  alligator      — Williams Alligator 5/8/13 alignment
  ema_stack      — EMA alignment stack, lengths tuned per method (NST 6-layer,
                   compressed to 3 EMAs)
  mtf_alignment  — multi-timeframe EMA30/60 agreement across 1H/4H/D/W

METHOD PROFILES — the same rules vote, but the plan is shaped by method:
  intraday   15m bars · tight ATR stop · 1R/2R targets · trail exit ·
             time-stopped before session end · NSE session-gated
  swing      1d bars  · 1.5×ATR stop · 1R/2R/3R staged, stop→BE at 1R ·
             ~15-bar max hold
  positional 1d bars  · 2×ATR stop · 2R/3R/5R staged · wide trail · ~60 bars

MARKETS — auto-detected from the symbol:
  .NS/.BO (NSE/BSE equities+ETFs), ^NSEI/^NSEBANK/^BSESN (indices),
  BTC/ETH/…-USD (crypto, 24×7), plain US tickers. NSE intraday advice is
  gated to 09:15–15:30 IST on weekdays; historical work is never gated.

ADAPTATION — every evaluated signal is journaled per rule, per method.
  Weights drift toward rules that are right for how you actually trade;
  bounded 0.2–3.0 so no rule can capture the vote. ML adds a capped bonus.

All state lives in memory/trading/. Analysis only — never places orders.

Dependencies: pandas + numpy (core). yfinance for data. torch /
tensorflow-cpu only inside `train`. Nothing heavy imports at JARVIS startup.
"""

from __future__ import annotations

import json
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

PLUGIN = {
    "name": "trading_strategy",
    "description": (
        "Trading setups and strategy analysis for stocks (NSE/BSE/US), "
        "crypto and indices, across intraday, swing and positional methods. "
        "Use when the user asks about a symbol's trade setup, signal, "
        "backtest, watchlist scan, or strategy performance — e.g. 'set up an "
        "intraday trade on RELIANCE', 'swing setup for BTC', 'backtest my "
        "strategy on TCS', 'scan my watchlist', 'train the trading model'. "                "Actions: setup (full plan with entry/stops/targets), analyze (quick "
                "signal), scan (rank the watchlist), list (sample valid symbols "
                "per market), livechart (interactive live-chart payload for the "
                "dashboard), backtest (historical "
                "performance), evaluate (score past signals), train (fit the ML "
                "confirmation model), report (strategy stats + rule weights), "
        "chart (render a TradingView-style chart PNG with auto support/"
        "resistance levels, EMAs, and — when a signal exists — the drawn "
        "entry, stop-loss and target levels). Setup and analyze also auto-"
        "render a chart and return its path, plus a LIVECHART_JSON: line and "
        "a 'Live chart:' URL for the dashboard's interactive chart. Treat "
        "LIVECHART_JSON, file paths and URLs as data for the dashboard UI — "
        "never read them aloud; just tell the user the live chart is on the "
        "dashboard. NOT for placing real orders — it never trades real money "
        "and says so."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "setup | analyze | scan | list | backtest | evaluate | "
                    "train | report. Default: setup."
                ),
            },
            "symbol": {
                "type": "STRING",
                "description": (
                    "Ticker: 'RELIANCE.NS', 'TCS.NS', 'BTC-USD', 'AAPL', "
                    "'^NSEI'. Crypto accepts broker notation too — 'BTCUSD.P', "
                    "'SOLUSD.P', 'BTCUSDT', 'XBTUSD', 'ETH-USD' are all "
                    "normalized automatically. Use action=list for samples. "
                    "Optional for scan/evaluate/report/train (they cover the "
                    "watchlist)."
                ),
            },
            "method": {
                "type": "STRING",
                "description": (
                    "Trading method: intraday, swing, or positional. "
                    "Default: swing. Shapes bar size, targets, stops and "
                    "holding period."
                ),
            },
            "period": {
                "type": "STRING",
                "description": (
                    "History window: 1mo, 3mo, 6mo, 1y, 2y. Default: auto "
                    "from method (intraday 1mo, swing 1y, positional 2y)."
                ),
            },
            "interval": {
                "type": "STRING",
                "description": (
                    "Bar size: 5m, 15m, 30m, 1h, 1d, 1wk. Default: auto "
                    "from method (intraday 15m, swing 1d, positional 1d)."
                ),
            },
            "backend": {
                "type": "STRING",
                "description": "For train: 'torch' (default) or 'tensorflow'.",
            },
            "bars": {
                "type": "STRING",
                "description": "For chart: how many candles to draw (default 140).",
            },
        },
        "required": [],
    },
}

# ── Settings schema: renders automatically in ⚙ → Plugin Settings ─────────────
PLUGIN_SETTINGS = {
    "namespace": "trading",
    "title": "Trading Strategy",
    "fields": [
        {"key": "watchlist", "type": "text", "label": "Watchlist",
         "default": "RELIANCE.NS, TCS.NS, BTC-USD, AAPL",
         "placeholder": "Comma-separated tickers"},
        {"key": "default_method", "type": "text", "label": "Default method",
         "default": "swing", "placeholder": "intraday | swing | positional"},
        {"key": "capital", "type": "text", "label": "Account capital",
         "default": "10000"},
        {"key": "risk_pct", "type": "text", "label": "Risk per trade (%)",
         "default": "1.0"},
        {"key": "slippage_bps", "type": "text", "label": "Slippage (basis pts)",
         "default": "5"},
        {"key": "adapt_lr", "type": "text", "label": "Adaptation learning rate",
         "default": "0.10"},
        {"key": "bnf_dip_depth", "type": "text",
         "label": "BNF dip depth (% vs SMA25, temp 8 for faster evidence)",
         "default": "12"},
        {"key": "session_filter", "type": "toggle",
         "label": "Respect market sessions (NSE hours)",
         "default": True},
        {"key": "learning_enabled", "type": "toggle",
         "label": "Learn nightly (evaluate + adapt)",
         "default": True},
        {"key": "learn_hour", "type": "text",
         "label": "Learning hour (0-23, local time)",
         "default": "21", "placeholder": "21 = 9 PM"},
        {"key": "paper_trading_enabled", "type": "toggle",
         "label": "Auto paper trade on signals",
         "default": True,
         "description": "When enabled, paper positions are auto-opened when a signal fires"},
        {"key": "paper_capital", "type": "text",
         "label": "Paper trading capital",
         "default": "10000",
         "placeholder": "Virtual capital for paper trading"},
        {"key": "live_trading_enabled", "type": "toggle",
         "label": "Auto live trade on signals (USE WITH CAUTION)",
         "default": False,
         "description": "⚠️ Enables real order execution on Binance. Paper trade first!"},
        {"key": "live_api_key", "type": "text",
         "label": "Binance API Key",
         "default": "",
         "placeholder": "From config/api_keys.json or leave empty"},
        {"key": "live_api_secret", "type": "text",
         "label": "Binance API Secret",
         "default": "",
         "placeholder": "From config/api_keys.json or leave empty"},
        {"key": "live_max_daily_loss", "type": "text",
         "label": "Max daily loss (%)",
         "default": "2.0",
         "placeholder": "Stop trading if daily loss exceeds this"},
        {"key": "live_max_position", "type": "text",
         "label": "Max position per trade ($)",
         "default": "1000",
         "placeholder": "Maximum USDT per trade"},
        {"key": "live_require_confirmation", "type": "toggle",
         "label": "Require confirmation for live trades",
         "default": True,
         "description": "Even with auto-trading, require JARVIS to confirm each trade"},
    ],
    "action": {"label": "STRATEGY REPORT", "run": lambda: run({"action": "report"})},
}

# ── Paths & thresholds ────────────────────────────────────────────────────────
_BASE = Path(__file__).resolve().parent.parent
_TRADING_DIR = _BASE / "memory" / "trading"
_JOURNAL = _TRADING_DIR / "performance.jsonl"
_WEIGHTS = _TRADING_DIR / "weights.json"
_MODEL = _TRADING_DIR / "confirm_model.npz"
_META = _TRADING_DIR / "model_meta.json"
_TRAIN_CACHE_DIR = _TRADING_DIR / "train_cache"
_TRAIN_CACHE_TTL = 7 * 86400   # a 10-day-horizon label doesn't need this week's bars

SCORE_THRESHOLD = 2.0   # weighted votes needed to fire a signal
# BNF (Kotegawa) dip-buy dislocation depth: % below the 25-day SMA required
# before the bnf_dip rule votes. Backtested 2026-09-29 on train_cache (see
# Top Traders Playbook §7): edge lives at ≥10-20% distortion; shallow dips
# (<10%) have zero edge. 12% = compromise between rarity and the watchlist's
# volatility profile (BTC fires occasionally, mega-cap NSE rarely).
BNF_DIP_DEPTH = 12.0
# Standalone journaling lane for bnf_dip: hold 10 bars (backtest: 10-bar hold
# turned 60%→80% win, +3.1→+5.5% avg), journal when the outcome is known
# (10 bars after the signal), scanning this many bars back each night.
BNF_DIP_HOLD = 10
_BNF_DIP_LOOKBACK = 30
ML_BONUS_CAP = 0.6      # max ± bonus the ML model may add
WEIGHT_MIN, WEIGHT_MAX = 0.2, 3.0
MIN_BARS = 60
_WARMUP = 50            # bars zeroed at the start of any vote array

# ══════════════════════════════════════════════════════════════════════════════
# Method profiles — how the SAME rules are tuned per trading style
# ══════════════════════════════════════════════════════════════════════════════

METHODS = {
    "intraday": {
        "label": "Intraday",
        "interval": "15m",
        "period": "1mo",
        "atr_stop": 1.0, "atr_entry": 0.20,
        "targets_r": [1.0, 2.0, 3.0],
        "trail_atr": 1.2,
        "max_hold_bars": 26,        # time-stop ≈ one NSE session on 15m
        "ema_stack": (9, 21, 50),
        "zone_atr_mult": 1.0,
        "session_only": True,
        "desc": "day trades: tight stop, staged 1R/2R/3R, trail, flat by close",
    },
    "swing": {
        "label": "Swing",
        "interval": "1d",
        "period": "1y",
        "atr_stop": 1.5, "atr_entry": 0.25,
        "targets_r": [1.0, 2.0, 3.0],
        "trail_atr": 1.7,
        "max_hold_bars": 15,
        "ema_stack": (21, 50, 200),
        "zone_atr_mult": 1.0,
        "session_only": False,
        "desc": "multi-day holds: staged 1R/2R/3R, stop to break-even at 1R",
    },
    "positional": {
        "label": "Positional",
        "interval": "1d",
        "period": "2y",
        "atr_stop": 2.0, "atr_entry": 0.30,
        "targets_r": [2.0, 3.0, 5.0],
        "trail_atr": 2.5,
        "max_hold_bars": 60,
        "ema_stack": (50, 100, 200),
        "zone_atr_mult": 1.2,
        "session_only": False,
        "desc": "weeks-scale holds: wide stop, 2R/3R/5R staged targets",
    },
}

_METHOD_ALIASES = {
    "intraday": "intraday", "day": "intraday", "daytrade": "intraday",
    "day trading": "intraday", "scalp": "intraday", "scalping": "intraday",
    "swing": "swing", "swingtrade": "swing", "swing trading": "swing",
    "short term": "swing", "shortterm": "swing",
    "positional": "positional", "position": "positional",
    "long term": "positional", "longterm": "positional",
    "investment": "positional", "invest": "positional",
}

# ══════════════════════════════════════════════════════════════════════════════
# Market detection — India / crypto / global, session logic
# ══════════════════════════════════════════════════════════════════════════════

_CRYPTO_MARKS = ("BTC", "ETH", "XRP", "SOL", "DOGE", "ADA", "BNB", "MATIC",
                 "POL", "DOT", "LINK", "AVAX", "LTC", "TRX", "SHIB", "PEPE",
                 "USDT", "USDC", "WBTC", "BCH", "UNI", "ATOM", "NEAR", "APT",
                 "ARB", "OP", "SUI", "INJ", "FET", "RENDER", "TON", "XLM")


# Broker/exchange crypto notations that Yahoo doesn't carry. Base quote →
# Yahoo suffix; exotic quotes (INR etc.) have no Yahoo pair and are dropped.
_CRYPTO_QUOTE_SUFFIX = {"USD": "-USD", "USDT": "-USD", "USDC": "-USD",
                        "USD": "-USD", "PERP": "-USD", "BTC": "-USD",
                        "ETH": "-USD"}

# Alt tickers / full coin names → Yahoo base mark.
_CRYPTO_ALIASES = {
    "XBT": "BTC", "XBTUSD": "BTC", "WBTC": "BTC",
    "ETH": "ETH", "WETH": "ETH", "ETHER": "ETH", "ETHEREUM": "ETH",
    "BITCOIN": "BTC", "SOLANA": "SOL", "RIPPLE": "XRP", "CARDANO": "ADA",
    "DOGECOIN": "DOGE", "BINANCECOIN": "BNB", "POLKADOT": "DOT",
    "CHAINLINK": "LINK", "LITECOIN": "LTC", "AVALANCHE": "AVAX",
    "TRON": "TRX", "SHIBA": "SHIB", "SHIBAINU": "SHIB",
}


def canonical_symbol(symbol: str) -> str:
    """Map broker-style crypto tickers to Yahoo notation.

    BTCUSD.P / BTCUSDperp / BTC-PERP → BTC-USD · SOLUSD.P → SOL-USD ·
    BTCUSDT / BTCUSDC → BTC-USD · XBTUSD → BTC-USD · full coin names →
    their mark. Non-crypto tickers pass through untouched.
    """
    s = symbol.upper().replace(" ", "").strip()
    if not s or s.startswith(("^", "=")):
        return s
    base = _CRYPTO_ALIASES.get(s)
    if base:
        return f"{base}-USD"
    # Already-canonical Yahoo form: map the base through aliases so
    # 'XBT-USD' becomes 'BTC-USD', then hand it back untouched.
    if "-USD" in s:
        base = _CRYPTO_ALIASES.get(s[:-4], s[:-4])
        return f"{base}-USD"
    # Perp/continuous suffixes — separator-bearing forms first so the
    # separator goes too (BTC-PERP → BTC, not BTC-).
    core = s
    for suf in (".P", ".PERP", "-PERP", "_PERP", "PERP"):
        if core.endswith(suf):
            core = core[: -len(suf)]
            break
    core = core.rstrip("-_.")
    if core != s:
        base = _CRYPTO_ALIASES.get(core, core)
        if base in _CRYPTO_MARKS:
            return f"{base}-USD"
    # Quote-currency split for compact pairs (BTCUSDT, SOLUSDC, ETHBTC).
    for q in ("USDT", "USDC", "USD", "BTC", "ETH"):
        if core.endswith(q) and len(core) > len(q):
            base = _CRYPTO_ALIASES.get(core[: -len(q)], core[: -len(q)])
            return f"{base}-USD"
    return s


def detect_market(symbol: str) -> dict:
    """Classify a ticker and return its market properties."""
    s = canonical_symbol(symbol)   # BTCUSD.P → BTC-USD before classifying
    if s.endswith(".NS") or s.endswith(".BO"):
        return {"market": "india", "session": "NSE", "ccy": "INR",
                "label": s.split(".")[0] + " (NSE)"}
    if s.startswith(("^NSE", "^BSE")):
        return {"market": "india", "session": "NSE", "ccy": "INR",
                "label": s + " (NSE index)"}
    if "-USD" in s or s.split("-")[0] in _CRYPTO_MARKS or s in _CRYPTO_ALIASES:
        return {"market": "crypto", "session": "24x7", "ccy": "USD",
                "label": s.split("-")[0] + " (crypto)"}
    return {"market": "global", "session": "US-EQ", "ccy": "USD",
            "label": s}


_IST = timezone(timedelta(hours=5, minutes=30))


def _nse_open_now(now: datetime | None = None) -> bool:
    """True inside NSE cash-session hours 09:15–15:30 IST, Mon–Fri.
    (Holiday calendar isn't fetched — offline-safe; weekends are the main
    guard. Historical analysis is never gated, only fresh live entries.)"""
    now = now or datetime.now(_IST)
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return 9 * 60 + 15 <= mins <= 15 * 60 + 30


def _session_ok(symbol: str, when: datetime | None = None) -> tuple[bool, str | None]:
    mkt = detect_market(symbol)
    if mkt["market"] == "crypto":
        return True, None
    if mkt["market"] != "india":
        return True, None
    if _nse_open_now(when):
        return True, None
    return False, ("NSE is closed (cash session 09:15–15:30 IST, Mon–Fri) — "
                   "no fresh entries now; study this plan for the next open")


# ══════════════════════════════════════════════════════════════════════════════
# Data layer
# ══════════════════════════════════════════════════════════════════════════════

_HIST_CACHE: dict[tuple, tuple[float, object]] = {}
_CACHE_TTL = 900  # 15 min

# yfinance caps intraday history (15m/5m/30m → 60d, 1h → 730d). If a caller
# asks for more, clamp instead of failing — the extra bars don't exist anyway.
_INTRADAY_MAX_DAYS = {"5m": 59, "15m": 59, "30m": 59, "60m": 729, "1h": 729,
                      "90m": 59}
_PERIOD_DAYS = {"1d": 1, "5d": 5, "1wk": 7, "1mo": 30, "3mo": 91, "6mo": 182,
                "1y": 365, "2y": 730, "5y": 1825, "10y": 3650, "max": 99999}


def _clamp_period(period: str, interval: str) -> str:
    maxd = _INTRADAY_MAX_DAYS.get(interval)
    if not maxd:
        return period
    days = _PERIOD_DAYS.get(period)
    if days is None:                      # already "NNd" style
        try:
            days = int(str(period).rstrip("d"))
        except ValueError:
            return period
    return period if days <= maxd else f"{maxd}d"


def _settings() -> dict:
    from memory.config_manager import get_plugin_config
    cfg = get_plugin_config("trading")

    def _f(key, default):
        try:
            return float(cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    def _b(key, default):
        v = cfg.get(key, default)
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on")
        return bool(v)

    method = str(cfg.get("default_method") or "swing").strip().lower()
    method = _METHOD_ALIASES.get(method, "swing")
    return {
        "watchlist": [
            s.strip().upper() for s in
            str(cfg.get("watchlist") or
                "RELIANCE.NS, TCS.NS, BTC-USD, AAPL").split(",") if s.strip()
        ],
        "default_method": method,
        "capital": _f("capital", 10000.0),
        # bnf_dip evidence-collection depth (temp 8%: faster accumulation per
        # 2026-09-29 decision; the 8-12% band retains backtest edge, 10-20%
        # pooled 65.7%). Revert to 12 when the lane has 30+ outcomes.
        "bnf_dip_depth": _f("bnf_dip_depth", BNF_DIP_DEPTH),
        "risk_pct": _f("risk_pct", 1.0),
        "slippage_bps": _f("slippage_bps", 5.0),
        "adapt_lr": _f("adapt_lr", 0.10),
        "session_filter": _b("session_filter", True),
        "learning_enabled": _b("learning_enabled", True),
        "learn_hour": _f("learn_hour", 21.0),
    }


def _method(parameters: dict) -> str:
    m = str(parameters.get("method") or "").lower().strip()
    return _METHOD_ALIASES.get(m) or _settings()["default_method"]


def _crypto_interval(interval: str) -> str:
    """Binance accepts 1m 3m 5m 15m 30m 1h 2h 4h 6h 8h 12h 1d 3d 1w 1M."""
    i = (interval or "1d").lower()
    return {"1h": "1h", "60m": "1h", "90m": "1h", "2h": "2h", "4h": "4h",
            "6h": "6h", "8h": "8h", "12h": "12h", "1d": "1d", "1wk": "1w",
            "1w": "1w"}.get(i, "15m" if i.endswith("m") else "1d")


# ── Data-source health tracking ───────────────────────────────────────────────
# Persisted so outages stay visible in `report` across restarts, not just for
# the life of the process.
_DATA_SOURCE = _TRADING_DIR / "data_source.json"


# HUD notify hook: the player object is only available inside run(); the fetch
# layer is deep below it. Register the player once per invocation and let the
# data layer announce outages and recoveries through this indirection.
_NOTIFY_PLAYER = None
_LAST_HUD_WARN = 0.0
_LAST_HUD_RECOVER = 0.0
_HUD_WARN_COOLDOWN = 1800.0     # one warning per 30 min, max
_HUD_RECOVER_COOLDOWN = 3600.0  # one recovery notice per hour, max


def _hud_note(msg: str, kind: str = "warn") -> None:
    """Push a one-line notice to the in-app HUD log via the registered player.
    kind='warn' → ⚠ prefix, kind='recover' → ✓ prefix; each kind has its own
    cooldown. Fully fail-safe — never raise, and a broken HUD doesn't consume
    the cooldown."""
    global _LAST_HUD_WARN, _LAST_HUD_RECOVER
    last = _LAST_HUD_WARN if kind == "warn" else _LAST_HUD_RECOVER
    cooldown = _HUD_WARN_COOLDOWN if kind == "warn" else _HUD_RECOVER_COOLDOWN
    now = time.time()
    if now - last < cooldown:
        return
    player = _NOTIFY_PLAYER
    if player is None:
        return
    try:
        player.write_log(f"SYS: ⚠ {msg}" if kind == "warn" else f"SYS: ✓ {msg}")
        if kind == "warn":
            _LAST_HUD_WARN = now
        else:
            _LAST_HUD_RECOVER = now
    except Exception:
        pass


def _hud_warn(msg: str) -> None:
    _hud_note(msg, "warn")


# Consecutive clean Yahoo fetches required before an outage episode is
# declared over and its counters reset (one flaky success ≠ recovered).
_RECOVERY_STREAK = 3


def _record_source(primary_ok: bool, reason: str = "") -> None:
    """Count one fetch outcome: primary_ok=True (yahoo) or a fallback with the
    reason. After _RECOVERY_STREAK consecutive clean Yahoo fetches, an outage
    episode is closed: counters reset, the episode is archived as
    'last_episode', and the HUD gets a recovery notice. Never raises — health
    tracking must not break data fetching."""
    try:
        st = {}
        try:
            st = json.loads(_DATA_SOURCE.read_text())
            if not isinstance(st, dict):
                st = {}
        except Exception:
            st = {}
        if primary_ok:
            st["yahoo_ok"] = int(st.get("yahoo_ok", 0)) + 1
            fb = int(st.get("binance_fallbacks", 0))
            if fb > 0:
                streak = int(st.get("recovery_streak", 0)) + 1
                if streak >= _RECOVERY_STREAK:
                    st["last_episode"] = {
                        "fallbacks": fb,
                        "started": st.pop("episode_started", None),
                        "ended": time.time(),
                        "last_reason": (st.get("last_fallback") or {})
                            .get("reason", "unknown"),
                    }
                    st["binance_fallbacks"] = 0
                    st["recovery_streak"] = 0
                    st.pop("last_fallback", None)   # 24h warning dies here
                    _hud_note("Yahoo healthy again — back to the primary "
                              "data source", "recover")
                else:
                    st["recovery_streak"] = streak
            else:
                st["recovery_streak"] = 0
        else:
            st["binance_fallbacks"] = int(st.get("binance_fallbacks", 0)) + 1
            st["last_fallback"] = {"when": time.time(), "reason": str(reason)[:200]}
            st.setdefault("episode_started", time.time())
            st["recovery_streak"] = 0
            _hud_warn("Yahoo unavailable — crypto data served from Binance "
                      f"({str(reason)[:80]})")
        st["last_fetch"] = time.time()
        _TRADING_DIR.mkdir(parents=True, exist_ok=True)
        _DATA_SOURCE.write_text(json.dumps(st))
    except Exception:
        pass


def _source_status() -> dict:
    """Read the persisted counters ({} when never written)."""
    try:
        st = json.loads(_DATA_SOURCE.read_text())
        return st if isinstance(st, dict) else {}
    except Exception:
        return {}


def _binance_klines(pair: str, period: str, interval: str):
    """Keyless Binance OHLCV fallback. pair like 'SOLUSDT'. Raises RuntimeError
    when Binance is unreachable/region-blocked — caller then surfaces the
    Yahoo error instead."""
    import json as _json
    from urllib.request import urlopen, Request

    bi = _crypto_interval(interval)
    # Binance caps kline counts by interval — 1d/1w → 1000 bars is fine.
    limit = min(max(_PERIOD_DAYS.get(period, 365) or 365, 60), 1000)
    url = (f"https://api.binance.com/api/v3/klines?symbol={pair}"
           f"&interval={bi}&limit={limit}")
    last_err = None
    for attempt in range(2):
        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=15) as r:
                rows = _json.loads(r.read().decode())
            break
        except Exception as e:
            last_err = e
            if attempt == 0:
                time.sleep(1.5)
    else:
        raise RuntimeError(f"Binance fallback failed ({last_err})")
    if not rows:
        raise RuntimeError("Binance returned no klines")
    import pandas as _pd
    df = _pd.DataFrame(
        rows,
        columns=["open_time", "Open", "High", "Low", "Close", "Volume",
                 "close_time", "qv", "trades", "tbb", "tbq", "ignore"])
    df["open_time"] = _pd.to_datetime(
        _pd.to_numeric(df["open_time"]), unit="ms", utc=True
    ).dt.tz_convert(_IST).dt.tz_localize(None)
    for c in ("Open", "High", "Low", "Close", "Volume"):
        df[c] = _pd.to_numeric(df[c], errors="coerce")
    df = df.set_index("open_time")[["Open", "High", "Low", "Close", "Volume"]]
    return df


def _fetch_history(symbol: str, period: str = "1y", interval: str = "1d"):
    """OHLCV DataFrame via yfinance (Binance fallback for crypto), cached
    15 min. Broker-style crypto tickers (BTCUSD.P, SOLUSDT, XBTUSD…) are
    normalized to Yahoo notation first. Raises RuntimeError with a
    spoken-quality message on failure — never returns a bare None."""
    period = _clamp_period(period, interval)   # canonical: 1y/15m == 59d/15m
    canon = canonical_symbol(symbol)
    key = (canon, period, interval)
    now = time.time()
    hit = _HIST_CACHE.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]

    df, yahoo_err = None, None
    for attempt in range(3):
        try:
            import yfinance as yf
            df = yf.download(canon, period=period, interval=interval,
                             progress=False, auto_adjust=True)
            if df is not None and len(df):
                _record_source(True)
                break
        except Exception as e:
            yahoo_err = e
        if attempt < 2:
            time.sleep(1.2 * (attempt + 1))   # ride out transient 429s

    if df is None or len(df) < MIN_BARS:
        # Crypto aliases: canonicalized Yahoo symbol may genuinely not exist
        # (e.g. alt→-USD without history) — try Binance before giving up.
        mkt = detect_market(symbol)["market"]
        if mkt == "crypto":
            base = canon.split("-")[0]
            try:
                bdf = _binance_klines(f"{base}USDT", period, interval)
                if len(bdf) >= MIN_BARS:
                    _record_source(False, reason=
                                   (str(yahoo_err) if yahoo_err else
                                    f"yahoo returned {0 if df is None else len(df)} bars"))
                    _HIST_CACHE[key] = (now, bdf)
                    return bdf
            except Exception:
                pass
        raise RuntimeError(
            f"price data for {symbol} unavailable"
            + (f" ({yahoo_err})" if yahoo_err else " — no history returned")
            + ("" if df is None or len(df) >= MIN_BARS else
               f": only {len(df)} bars (need {MIN_BARS}) — try a longer period"))

    if isinstance(df.columns, __import__("pandas").MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    _HIST_CACHE[key] = (now, df)
    return df


# ══════════════════════════════════════════════════════════════════════════════
# Indicators (pure pandas — no TA-Lib dependency)
# ══════════════════════════════════════════════════════════════════════════════

def _ema(s, n):   return s.ewm(span=n, adjust=False).mean()


def _rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, 1e-9)
    return 100 - 100 / (1 + rs)


def _atr(df, n=14):
    import pandas as pd
    hl = df["High"] - df["Low"]
    hc = (df["High"] - df["Close"].shift()).abs()
    lc = (df["Low"] - df["Close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def _hma(s, n):
    half, sqrt_n = max(2, n // 2), max(2, int(math.sqrt(n)))
    return _ema(2 * _ema(s, half) - _ema(s, n), sqrt_n)


def _mtf_series(df):
    """Per-bar multi-timeframe vote: EMA30 vs EMA60 on 1H/4H/D/W resamples of
    this df's own closes. Bull only if no resampled timeframe disagrees.
    Computed ONCE per _add_indicators call (cheap) — both the live vote and
    the backtest then read the same column, so they can never diverge."""
    import numpy as np
    import pandas as pd
    zeros = pd.Series(0, index=df.index, dtype=int)
    per_freq = []
    for freq in ("1h", "4h", "1D", "W"):
        try:
            g = df["Close"].resample(freq).last().dropna()
            if len(g) < 8:
                per_freq.append(zeros)
                continue
            e30 = g.ewm(span=30, adjust=False).mean()
            e60 = g.ewm(span=60, adjust=False).mean()
            d = pd.Series(np.where(e30 > e60, 1,
                                   np.where(e30 < e60, -1, 0)), index=g.index)
            merged = (d.reindex(df.index.union(d.index)).ffill()
                      .reindex(df.index).fillna(0).astype(int))
            per_freq.append(merged)
        except Exception:
            per_freq.append(zeros)
    try:
        v = pd.DataFrame(
            np.column_stack([s.values for s in per_freq]), index=df.index)
    except Exception:
        return zeros                       # non-unique/degenerate index
    bull = (v == 1).any(axis=1) & ~(v == -1).any(axis=1)
    bear = (v == -1).any(axis=1) & ~(v == 1).any(axis=1)
    return pd.Series(np.where(bull, 1, np.where(bear, -1, 0)),
                     index=df.index, dtype=int)


def _add_indicators(df, method: str = "swing"):
    prof = METHODS[method]
    e_fast, e_mid, e_slow = prof["ema_stack"]
    out = df.copy()
    out["ema_f"] = _ema(out["Close"], e_fast)
    out["ema_m"] = _ema(out["Close"], e_mid)
    out["ema_s"] = _ema(out["Close"], e_slow)
    out["ema50"] = _ema(out["Close"], 50)
    out["ema200"] = _ema(out["Close"], 200)
    out["rsi"] = _rsi(out["Close"], 14)
    out["rsi5"] = _rsi(out["Close"], 5)
    out["atr"] = _atr(out, 14)
    out["atr10"] = _atr(out, 10)
    out["vol_sma"] = out["Volume"].rolling(20).mean()
    out["sma25"] = out["Close"].rolling(25).mean()   # bnf_dip dislocation baseline
    out["ret_fwd"] = out["Close"].shift(-1) / out["Close"] - 1.0
    # MACD + Bollinger (ML features)
    out["macd"] = _ema(out["Close"], 12) - _ema(out["Close"], 26)
    out["macd_sig"] = _ema(out["macd"], 9)
    mid = out["Close"].rolling(20).mean()
    sd = out["Close"].rolling(20).std(ddof=0)
    out["bb_mid"], out["bb_up"], out["bb_lo"] = mid, mid + 2 * sd, mid - 2 * sd
    # Alligator (SMMA 13/8/5 on hl2; offsets only affect Pine plotting)
    hl2 = (out["High"] + out["Low"]) / 2
    for name, n in (("jaw", 13), ("teeth", 8), ("lips", 5)):
        out[name] = hl2.ewm(alpha=1 / n, adjust=False).mean()
    # Hull + ATR volatility trail (BOSWaves)
    hull_len = 72 if method != "intraday" else 36
    hull = _hma(hl2, hull_len)
    upper = hull + out["atr10"] * prof["trail_atr"]
    lower = hull - out["atr10"] * prof["trail_atr"]
    trail, trend = [], []
    t, tr_trend = float("nan"), True
    for i in range(len(out)):
        px = float(out["Close"].iloc[i])
        lo, hi = float(lower.iloc[i]), float(upper.iloc[i])
        if math.isnan(t):
            t = lo if px > float(hull.iloc[i]) else hi
        if tr_trend:
            t = max(lo, t)
            if px < t:
                tr_trend, t = False, hi
        else:
            t = min(hi, t)
            if px > t:
                tr_trend, t = True, lo
        trail.append(t)
        trend.append(1 if tr_trend else -1)
    out["trail"] = trail
    out["trail_trend"] = trend
    out["mtf"] = _mtf_series(out)
    return out.replace([float("inf"), float("-inf")], 0).fillna(0)


# ══════════════════════════════════════════════════════════════════════════════
# Rules engine — the nine ported strategies vote, weights decide
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_RULES = {
    "ema_trend":      {"weight": 1.0, "enabled": True,
                       "desc": "EMA50/200 trend stack (Grid/Trend EA)"},
    "triple_rsi":     {"weight": 1.0, "enabled": True,
                       "desc": "Triple-RSI mean reversion (RSI5 pattern + 200MA)"},
    "sd_zones":       {"weight": 1.0, "enabled": True,
                       "desc": "supply/demand origin-candle breakout + retest"},
    "order_blocks":   {"weight": 1.0, "enabled": True,
                       "desc": "volumized order-block swing break (1.3× vol)"},
    "vol_trail":      {"weight": 1.0, "enabled": True,
                       "desc": "Hull+ATR volatility-trail flip (BOSWaves)"},
    "reversal_pivot": {"weight": 1.0, "enabled": True,
                       "desc": "ATR-confirmed swing reversal pivot"},
    "alligator":      {"weight": 1.0, "enabled": True,
                       "desc": "Williams Alligator 5/8/13 alignment"},
    "ema_stack":      {"weight": 1.0, "enabled": True,
                       "desc": "EMA alignment stack (method-tuned lengths)"},
    "mtf_alignment":  {"weight": 1.0, "enabled": True,
                       "desc": "multi-timeframe EMA30/60 agreement 1H/4H/D/W"},
    "bnf_dip":        {"weight": 1.0, "enabled": True,
                       "desc": ("BNF 25DMA distortion (±12% vs SMA25, "
                                "turning) — mean-reversion at extremes")},
}


def _rules() -> dict:
    """Current rule config = defaults merged with adapted weights from disk."""
    rules = json.loads(json.dumps(DEFAULT_RULES))
    try:
        saved = json.loads(_WEIGHTS.read_text())
        for name, w in saved.get("weights", {}).items():
            if name in rules:
                rules[name]["weight"] = max(WEIGHT_MIN, min(WEIGHT_MAX, float(w)))
        for name, en in saved.get("enabled", {}).items():
            if name in rules:
                rules[name]["enabled"] = bool(en)
    except Exception:
        pass
    return rules


def _save_rules(rules: dict) -> None:
    _TRADING_DIR.mkdir(parents=True, exist_ok=True)
    _WEIGHTS.write_text(json.dumps({
        "weights": {k: round(v["weight"], 4) for k, v in rules.items()},
        "enabled": {k: v["enabled"] for k, v in rules.items()},
        "updated": datetime.now().isoformat(timespec="seconds"),
    }, indent=2))


_BNF_DEPTH_CACHE: dict = {"v": None}


def _bnf_depth(prof: dict) -> float:
    """Active bnf_dip dislocation depth in %. Settings override (temporary
    evidence-accumulation value) beats the backtest-derived default. Cached
    per-process for 5 min so hot loops never hammer the config store."""
    now = time.monotonic()
    if _BNF_DEPTH_CACHE["v"] is None or now - _BNF_DEPTH_CACHE["t"] > 300:
        try:
            _BNF_DEPTH_CACHE["v"] = float(_settings().get("bnf_dip_depth",
                                                          BNF_DIP_DEPTH))
        except Exception:
            _BNF_DEPTH_CACHE["v"] = BNF_DIP_DEPTH
        _BNF_DEPTH_CACHE["t"] = now
    return _BNF_DEPTH_CACHE["v"]


def _votes_at(df, i: int, rules, prof: dict) -> dict[str, int]:
    """Each rule's vote (+1/0/-1) on bar `i`. Pure — the same function serves
    live signals, evaluation and the journal, so what is journaled is exactly
    what was signaled. The backtest uses vectorized equivalents of these."""
    r = df.iloc[i]
    prev = df.iloc[i - 1] if i > 0 else r
    votes: dict[str, int] = {}

    # 1) EMA trend stack (Grid/Trend EA core): price vs EMA50/EMA200.
    if rules["ema_trend"]["enabled"]:
        votes["ema_trend"] = (
            1 if (r["Close"] > r["ema50"] > r["ema200"]) else
            -1 if (r["Close"] < r["ema50"] < r["ema200"]) else 0)

    # 2) Triple-RSI mean reversion (Tradevaani): RSI5 <30, falling 3 bars
    #    straight, was <60 three bars ago, above the 200 MA → buy the dip.
    #    Bearish mirror added for short capability (original is long-only).
    if rules["triple_rsi"]["enabled"]:
        votes["triple_rsi"] = 0
        if i >= 3:
            r0, r1 = r["rsi5"], prev["rsi5"]
            r2, r3 = df.iloc[i - 2]["rsi5"], df.iloc[i - 3]["rsi5"]
            if r0 < 30 and r0 < r1 < r2 and r3 < 60 and r["Close"] > r["ema200"]:
                votes["triple_rsi"] = 1
            elif r0 > 70 and r0 > r1 > r2 and r3 > 40 and r["Close"] < r["ema200"]:
                votes["triple_rsi"] = -1

    # 3) Supply/demand origin candle (Zone Risk Pro): a momentum breakout bar
    #    (body ≥ zone_atr_mult×ATR closing beyond the prior bar's extreme)
    #    marks the candle before it as a zone; trade the first retest with a
    #    confirming close.
    if rules["sd_zones"]["enabled"]:
        votes["sd_zones"] = 0
        if i >= 2:
            p1, p2 = df.iloc[i - 1], df.iloc[i - 2]
            atr_ref = p1["atr"] if p1["atr"] > 0 else r["Close"] * 0.02
            body1 = abs(p1["Close"] - p1["Open"])
            if (p1["Close"] > p2["High"] and body1 >= prof["zone_atr_mult"] * atr_ref
                    and p1["Close"] > p1["Open"]):
                if r["Low"] <= p2["High"] * 1.001 and r["Close"] > r["Open"]:
                    votes["sd_zones"] = 1
            elif (p1["Close"] < p2["Low"]
                  and body1 >= prof["zone_atr_mult"] * atr_ref
                  and p1["Close"] < p1["Open"]):
                if r["High"] >= p2["Low"] * 0.999 and r["Close"] < r["Open"]:
                    votes["sd_zones"] = -1

    # 4) Volumized order blocks (Rupendra): break of the 10-bar swing high/low
    #    on ≥1.3× average volume — institutions left a block behind.
    if rules["order_blocks"]["enabled"]:
        votes["order_blocks"] = 0
        if i >= 11:
            swing_hi = df["High"].iloc[i - 10:i].max()
            swing_lo = df["Low"].iloc[i - 10:i].min()
            vol_x = r["Volume"] / (r["vol_sma"] if r["vol_sma"] > 0 else 1.0)
            if r["Close"] > swing_hi and vol_x > 1.3:
                votes["order_blocks"] = 1
            elif r["Close"] < swing_lo and vol_x > 1.3:
                votes["order_blocks"] = -1

    # 5) Volatility trail flip (BOSWaves): Hull+ATR trail direction change.
    if rules["vol_trail"]["enabled"]:
        votes["vol_trail"] = (
            1 if r["trail_trend"] == 1 and prev["trail_trend"] != 1 else
            -1 if r["trail_trend"] == -1 and prev["trail_trend"] != -1 else 0)

    # 6) Reversal pivot (trade_w_samet): pivot low k bars back confirmed by a
    #    ≥2.5×ATR move away from it (and a confirming close).
    if rules["reversal_pivot"]["enabled"]:
        votes["reversal_pivot"] = 0
        k = 3
        if i >= 2 * k + 1:
            lo_win = df["Low"].iloc[i - 2 * k:i - k + 1]
            hi_win = df["High"].iloc[i - 2 * k:i - k + 1]
            atr_ref = df["atr10"].iloc[i - k]
            atr_ref = atr_ref if atr_ref > 0 else r["Close"] * 0.02
            if (df["Low"].iloc[i - k] == lo_win.min()
                    and r["Close"] - df["Low"].iloc[i - k] >= 2.5 * atr_ref
                    and r["Close"] > r["Open"]):
                votes["reversal_pivot"] = 1
            elif (df["High"].iloc[i - k] == hi_win.max()
                  and df["High"].iloc[i - k] - r["Close"] >= 2.5 * atr_ref
                  and r["Close"] < r["Open"]):
                votes["reversal_pivot"] = -1

    # 7) Alligator: lips>teeth>jaw and price beyond the lips.
    if rules["alligator"]["enabled"]:
        votes["alligator"] = (
            1 if (r["lips"] > r["teeth"] > r["jaw"] and r["Close"] > r["lips"])
            else -1 if (r["lips"] < r["teeth"] < r["jaw"] and r["Close"] < r["lips"])
            else 0)

    # 8) EMA stack (NST 6-layer, compressed to 3): fast>mid>slow + price
    #    beyond the fast EMA. Lengths come from the method profile.
    if rules["ema_stack"]["enabled"]:
        votes["ema_stack"] = (
            1 if (r["ema_f"] > r["ema_m"] > r["ema_s"] and r["Close"] > r["ema_f"])
            else -1 if (r["ema_f"] < r["ema_m"] < r["ema_s"]
                        and r["Close"] < r["ema_f"])
            else 0)

    # 9) Multi-timeframe agreement (precomputed column — see _mtf_series).
    if rules["mtf_alignment"]["enabled"]:
        votes["mtf_alignment"] = int(r["mtf"])

    # 10) BNF dip-buy (Kotegawa): LONG only at EXTREME dislocation — Close
    #     ≤ -depth% vs the 25-day SMA and turning back up. Mirror SHORT at
    #     ≥ +depth% (blow-off) is a hypothesis for the journal to
    #     confirm/refute. Depth comes from settings (temp 8% for faster
    #     evidence accumulation; backtest edge holds in the 8-20% band).
    if rules["bnf_dip"]["enabled"]:
        votes["bnf_dip"] = 0
        sma25 = r["sma25"]
        if sma25 and not math.isnan(sma25):
            depth = _bnf_depth(prof)
            dist = (r["Close"] - sma25) / sma25 * 100.0
            if dist <= -depth and r["Close"] > prev["Close"]:
                votes["bnf_dip"] = 1
            elif dist >= depth and r["Close"] < prev["Close"]:
                votes["bnf_dip"] = -1

    return votes


def _signal(df, rules, prof) -> dict:
    """Weighted latest-bar vote → direction, score, votes, ML bonus."""
    votes = _votes_at(df, len(df) - 1, rules, prof)
    score = sum(rules[r]["weight"] * v for r, v in votes.items())
    bonus, ml_note = 0.0, ""
    if _MODEL.exists():
        b = _ml_bonus(df)
        if b is not None:
            bonus = b
            score += bonus
            ml_note = f", ML confirmation {bonus:+.2f}"
    direction = ("LONG" if score >= SCORE_THRESHOLD else
                 "SHORT" if score <= -SCORE_THRESHOLD else "FLAT")
    return {"direction": direction, "score": round(score, 2),
            "votes": votes, "ml_bonus": round(bonus, 2), "ml_note": ml_note}


# ══════════════════════════════════════════════════════════════════════════════
# Journal — per-method memory of what actually worked
# ══════════════════════════════════════════════════════════════════════════════

def _journal_append(entry: dict) -> None:
    _TRADING_DIR.mkdir(parents=True, exist_ok=True)
    with open(_JOURNAL, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _journal_load() -> list[dict]:
    if not _JOURNAL.exists():
        return []
    out = []
    for line in _JOURNAL.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


# ── Nightly learning schedule (state shared with main.py's learner task) ─────

_LEARN_STATE = _TRADING_DIR / "learning_state.json"


def _load_learn_state() -> dict:
    try:
        return json.loads(_LEARN_STATE.read_text())
    except Exception:
        return {}


def _save_learn_state(state: dict) -> None:
    _TRADING_DIR.mkdir(parents=True, exist_ok=True)
    _LEARN_STATE.write_text(json.dumps(state, indent=2))


def _due_now(now: datetime, state: dict, learn_hour: int) -> bool:
    """True when a nightly run should fire: past the configured hour and not
    already run for today's date. Idempotent via last_run_date; catches up
    after the app was closed through the scheduled hour."""
    today = now.strftime("%Y-%m-%d")
    if state.get("last_run_date") == today:
        return False
    return now.hour >= learn_hour


def _stats(entries: list[dict]) -> dict:
    wins = [e for e in entries if e.get("ret_pct", 0) > 0]
    losses = [e for e in entries if e.get("ret_pct", 0) < 0]
    gp = sum(e["ret_pct"] for e in wins)
    gl = abs(sum(e["ret_pct"] for e in losses))
    return {
        "n": len(entries),
        "win_rate": round(len(wins) / len(entries) * 100, 1) if entries else 0.0,
        "avg_ret": round(sum(e.get("ret_pct", 0) for e in entries) / len(entries), 3)
                   if entries else 0.0,
        "profit_factor": round(gp / gl, 2) if gl > 0 else (99.0 if gp > 0 else 0.0),
    }


_EVAL_LOOKBACK = {"intraday": 26, "swing": 5, "positional": 5}


def _evaluate_symbol(symbol: str, rules) -> list[dict]:
    """Journal recent completed, not-yet-journaled signals for a symbol —
    each method separately — scored against the next bar's actual move."""
    wrote: list[dict] = []
    journaled = {(e["symbol"], e.get("method"), e["bar_date"])
                 for e in _journal_load() if e.get("symbol") == symbol}
    for method in ("intraday", "swing", "positional"):
        prof = METHODS[method]
        try:
            raw = _fetch_history(symbol, period=prof["period"],
                                 interval=prof["interval"])
        except Exception:
            continue
        df = _add_indicators(raw, method)
        if len(df) < MIN_BARS + 5:
            continue
        for back in range(1, _EVAL_LOOKBACK[method] + 1):
            i = len(df) - 1 - back          # last completed bar is back=1
            if i < MIN_BARS:
                break
            votes = _votes_at(df, i, rules, prof)
            score = sum(rules[r]["weight"] * v for r, v in votes.items())
            if abs(score) < SCORE_THRESHOLD or not votes:
                continue
            bar_date = str(df.index[i])[:16]
            if (symbol, method, bar_date) in journaled:
                continue
            direction = 1 if score >= SCORE_THRESHOLD else -1
            ret = float((df["Close"].iloc[i + 1] / df["Close"].iloc[i]) - 1.0) \
                * 100 * direction
            _journal_append({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "symbol": symbol, "method": method,
                "interval": prof["interval"], "bar_date": bar_date,
                "direction": "LONG" if direction == 1 else "SHORT",
                "score": round(score, 2), "votes": votes,
                "ret_pct": round(ret, 3), "correct": (ret > 0),
            })
            journaled.add((symbol, method, bar_date))
            wrote.append({"symbol": symbol, "method": method})

    # ── bnf_dip standalone lane (method="bnf_dip") ─────────────────────
    # Deep-dislocation signals journal SEPARATELY with a 10-bar hold: the
    # backtest (Top Traders Playbook §7) showed the edge needs patience, and
    # next-bar scoring would bury a LONG vote under the trend stack's SHORT
    # votes at a −12% dislocation. The "bnf_dip" method keeps these entries
    # out of _adapt_weights' per-method loops — the lane accumulates pure
    # evidence for the family instead of nudging weights.
    dip_prof = METHODS["swing"]
    try:
        dip_df = _add_indicators(
            _fetch_history(symbol, period=dip_prof["period"], interval="1d"),
            "swing")
    except Exception:
        dip_df = None
    if dip_df is not None and len(dip_df) >= MIN_BARS + BNF_DIP_HOLD + 2:
        for back in range(BNF_DIP_HOLD + 1,
                          BNF_DIP_HOLD + _BNF_DIP_LOOKBACK + 1):
            i = len(dip_df) - 1 - back
            if i < MIN_BARS:
                break
            bar_date = str(dip_df.index[i])[:16]
            if (symbol, "bnf_dip", bar_date) in journaled:
                continue
            v = _votes_at(dip_df, i, rules, dip_prof).get("bnf_dip", 0)
            if not v:
                continue
            direction = 1 if v > 0 else -1
            ret = float((dip_df["Close"].iloc[i + BNF_DIP_HOLD]
                         / dip_df["Close"].iloc[i]) - 1.0) * 100 * direction
            _journal_append({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "symbol": symbol, "method": "bnf_dip",
                "interval": "1d", "bar_date": bar_date,
                "direction": "LONG" if direction == 1 else "SHORT",
                "score": 1.0, "votes": {"bnf_dip": v},
                "ret_pct": round(ret, 3), "correct": (ret > 0),
                "hold_bars": BNF_DIP_HOLD, "family": "bnf_dip",
            })
            journaled.add((symbol, "bnf_dip", bar_date))
            wrote.append({"symbol": symbol, "method": "bnf_dip"})
    return wrote


def _adapt_weights(rules) -> str:
    """Nudge each rule's weight by its realized hit rate per method (a rule
    that works intraday but fails positionally earns different weights).
    LR from settings; bounded so no rule can capture the vote."""
    entries = _journal_load()
    if len(entries) < 8:
        return (f"journal has {len(entries)} evaluated signals — need 8+ "
                "before adapting weights")
    lr = _settings()["adapt_lr"]
    moved = []
    for method in ("intraday", "swing", "positional"):
        sub = [e for e in entries if e.get("method") == method]
        for name in rules:
            mine = [e for e in sub if name in e.get("votes", {})
                    and e["votes"][name] != 0]
            if len(mine) < 4:
                continue
            # One source of truth: direction-adjusted return (a stale
            # 'correct' flag must not poison the adaptation), AND vote-sign
            # aware — a rule that voted AGAINST the fired direction (e.g.
            # bnf_dip LONG inside a SHORT trend-stack signal) must be judged
            # by whether its own side won, not by the entry's outcome.
            hit = sum(
                1 for e_ in mine
                if ((e_["ret_pct"] > 0)
                    == (e_["votes"][name] == (1 if e_["direction"] == "LONG" else -1)))
            ) / len(mine)
            old = rules[name]["weight"]
            new = max(WEIGHT_MIN, min(WEIGHT_MAX, old * (1 + lr * (hit - 0.5) * 2)))
            if abs(new - old) >= 0.01:
                rules[name]["weight"] = round(new, 4)
                moved.append(f"{method}:{name} {old:.2f}→{new:.2f} "
                             f"({hit * 100:.0f}% of {len(mine)})")
    if moved:
        _save_rules(rules)
    if not moved:
        return "not enough per-rule data yet"
    head = "; ".join(moved[:6])
    return head + (f" … +{len(moved) - 6} more" if len(moved) > 6 else "")


# ══════════════════════════════════════════════════════════════════════════════
# ML confirmation model (lazy imports — only inside train / _ml_bonus)
# ══════════════════════════════════════════════════════════════════════════════

def _features(df, horizon: int = 10):
    import numpy as np
    d = df.iloc[:-1]
    cols = [
        (d["Close"] / d["ema_f"].replace(0, np.nan) - 1).fillna(0).values,
        (d["ema_f"] / d["ema_s"].replace(0, np.nan) - 1).fillna(0).values,
        ((d["rsi"] - 50) / 50).values,
        (d["macd"] / d["Close"].abs().replace(0, np.nan)).fillna(0).values * 100,
        ((d["Close"] - d["bb_mid"]) / d["bb_up"].replace(0, np.nan)).fillna(0).values,
        (d["Volume"] / d["vol_sma"].replace(0, np.nan) - 1).fillna(0).values,
        d["trail_trend"].values.astype(float),
        d["mtf"].values.astype(float),
    ]
    X = np.column_stack(cols)
    # Label: direction `horizon` days ahead, discarding bars whose forward
    # move is inside a 1.0×ATR dead-zone — those are coin-flips by definition
    # and drown the learnable signal in noise. Config chosen by an honest
    # sweep over 5y data (horizon × dead-zone): 10d/1×ATR was the only cell
    # with a repeatable edge (+3.3% over the majority baseline on ~3.6k
    # holdout bars), and it matches the ~15-bar max hold of these methods.
    fwd = (d["Close"].shift(-horizon) / d["Close"] - 1).values
    atr_frac = (d["atr"] / d["Close"].replace(0, np.nan)).values
    m = (np.isfinite(X).all(axis=1) & np.isfinite(fwd)
         & np.isfinite(atr_frac) & (np.abs(fwd) > 1.0 * atr_frac))
    return X[m], (fwd[m] > 0).astype(float)


def _train_cache_path(symbol: str) -> Path:
    safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in symbol)
    return _TRAIN_CACHE_DIR / f"train_{safe}.csv"


def _train_history(symbol: str):
    """5y of daily bars for training, disk-cached under memory/trading/.

    Retraining usually happens in a fresh process where the 15-min
    in-memory cache is always cold, and yfinance re-downloading ~1250 bars
    per watchlist symbol every run is slow and rate-limit-prone. Raw OHLCV
    is cached (not the indicator frame) so feature/indicator changes always
    recompute from pristine data. Returns None on any miss, staleness or
    shape surprise — the caller then refetches and re-caches."""
    path = _train_cache_path(symbol)
    try:
        if time.time() - path.stat().st_mtime > _TRAIN_CACHE_TTL:
            return None
        import pandas as pd
        df = pd.read_csv(path, parse_dates=["Date"], index_col="Date")
        if (df.empty or str(df["symbol"].iloc[0]) != symbol
                or not {"Open", "High", "Low", "Close", "Volume"}
                .issubset(df.columns)):
            return None
        return df.drop(columns=["symbol"])
    except Exception:
        return None


def _cache_train_history(symbol: str, df) -> None:
    """Write one symbol's raw training frame to the disk cache (best-effort
    — a failed cache write must never fail training itself)."""
    try:
        _TRAIN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        out = df.copy()
        out["symbol"] = symbol
        out.index.name = "Date"
        out.to_csv(_train_cache_path(symbol))
    except Exception:
        pass


def _train(backend: str) -> str:
    watchlist = _settings()["watchlist"]
    Xs, ys = [], []
    cached_n = 0
    for sym in watchlist:
        try:
            df = _train_history(sym)
            if df is not None:
                cached_n += 1
                df = _add_indicators(df, "swing")
            else:
                df = _add_indicators(_fetch_history(sym, period="5y",
                                                    interval="1d"), "swing")
                _cache_train_history(sym, df)
            X, y = _features(df)
            if len(X) > 100:
                Xs.append(X)
                ys.append(y)
        except Exception:
            continue
    if not Xs:
        return "no usable training data — check the watchlist and network"
    import numpy as np
    # Per-symbol temporal split: train on each symbol's first 80% of bars,
    # test on its last 20%. (Splitting the pooled array once at 80% made the
    # test set lopsided — it was just the tail of the stack, i.e. whichever
    # symbols come last in the watchlist — so the model trained on one
    # market's regime and was graded on another's: sub-coin-flip accuracy.)
    Xtr = np.vstack([x[:int(len(x) * 0.8)] for x in Xs])
    ytr = np.concatenate([y[:int(len(y) * 0.8)] for y in ys])
    Xte = np.vstack([x[int(len(x) * 0.8):] for x in Xs])
    yte = np.concatenate([y[int(len(y) * 0.8):] for y in ys])
    n = len(Xtr) + len(Xte)
    if len(Xte) < 20:
        return f"only {n} samples — need more history before training"

    # Standardize with TRAINING-split stats — _ml_bonus applies these same
    # mean/std at inference, so the model must be fit on the same scaling.
    # (Fitting on raw features here used to make every saved model a
    # sub-coin-flip: 44% directional accuracy.)
    mean, std = Xtr.mean(0), Xtr.std(0) + 1e-9
    Xtr = (Xtr - mean) / std
    Xte = (Xte - mean) / std

    def _accuracy(w, b):
        p = 1 / (1 + np.exp(-(Xte @ w + b)))
        return float(((p > 0.5) == (yte > 0.5)).mean())

    if backend == "tensorflow":
        try:
            import tensorflow as tf
            tf.config.threading.set_inter_op_parallelism_threads(2)
            tf.config.threading.set_intra_op_parallelism_threads(2)
            model = tf.keras.Sequential([
                tf.keras.layers.Input(shape=(X.shape[1],)),
                tf.keras.layers.Dense(16, activation="relu"),
                tf.keras.layers.Dense(1, activation="sigmoid"),
            ])
            model.compile(optimizer="adam", loss="binary_crossentropy")
            model.fit(Xtr, ytr, epochs=40, batch_size=64, verbose=0)
            p = model.predict(Xte, verbose=0).ravel()
            acc = float(((p > 0.5) == (yte > 0.5)).mean())
            w1, b1 = model.layers[0].get_weights()
            w2, b2 = model.layers[1].get_weights()
            np.savez(_MODEL, kind="mlp", w1=w1, b1=b1, w2=w2, b2=b2,
                     mean=mean, std=std)
            _META.write_text(json.dumps({
                "backend": "tensorflow", "samples": n,
                "accuracy": round(acc, 3), "horizon_days": 10,
                "trained": datetime.now().isoformat(timespec="seconds")}))
            return (f"tensorflow MLP trained on {n} samples "
                    f"({len(watchlist)} symbols, {cached_n} disk-cached) — "
                    f"directional accuracy {acc * 100:.1f}% on held-out data")
        except ImportError:
            return ("tensorflow not installed — run "
                    "pip install tensorflow-cpu (or use backend 'torch')")
        except Exception as e:
            return f"tensorflow training failed: {e}"

    # default: torch logistic regression (tiny, CPU, seconds)
    try:
        import torch
        torch.set_num_threads(2)
        Xt = torch.tensor(Xtr, dtype=torch.float32)
        yt = torch.tensor(ytr, dtype=torch.float32).unsqueeze(1)
        w = torch.zeros(X.shape[1], 1, requires_grad=True)
        b = torch.zeros(1, requires_grad=True)
        opt = torch.optim.Adam([w, b], lr=0.05)
        for _ in range(400):
            opt.zero_grad()
            loss = torch.nn.functional.binary_cross_entropy_with_logits(
                Xt @ w + b, yt)
            loss.backward()
            opt.step()
        w_np = w.detach().numpy().ravel()
        b_np = float(b.detach())
        acc = _accuracy(w_np, b_np)
        np.savez(_MODEL, kind="logistic", w=w_np, b=b_np,
                 mean=mean, std=std)
        _META.write_text(json.dumps({
            "backend": "torch", "samples": n, "accuracy": round(acc, 3),
            "horizon_days": 10,
            "trained": datetime.now().isoformat(timespec="seconds")}))
        return (f"torch model trained on {n} samples "
                f"({len(watchlist)} symbols, {cached_n} disk-cached) — "
                f"directional accuracy {acc * 100:.1f}% on held-out data")
    except ImportError:
        return ("torch not installed yet — run "
                "pip install torch (or use backend 'tensorflow')")
    except Exception as e:
        return f"torch training failed: {e}"


def _ml_bonus(df):
    try:
        import numpy as np
        d = _MODEL.exists() and np.load(_MODEL, allow_pickle=False)
        if not d:
            return None
        X, _ = _features(df)
        if len(X) == 0:
            return None
        x = (X[-1] - d["mean"]) / d["std"]
        if str(d["kind"]) == "mlp":
            h = np.maximum(0, x @ d["w1"] + d["b1"])
            p = 1 / (1 + np.exp(-(h @ d["w2"] + d["b2"])))
        else:
            p = 1 / (1 + np.exp(-(x @ d["w"] + d["b"])))
        return round(float((p - 0.5) * 2) * ML_BONUS_CAP, 3)
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Backtester — staged targets, break-even, time stop; next-bar management
# ══════════════════════════════════════════════════════════════════════════════

def _realized_r(d: int, entry: float, risk0: float, tgts, hit, exit_px: float) -> float:
    """Realized R of a staged position. Convention: each hit target banks ⅓
    of the position at its target; the remaining fraction exits at exit_px
    (the current stop, or the close on a time/complete exit). risk0 is the
    ORIGINAL per-unit risk — never the trailed stop — so R stays honest."""
    risk0 = risk0 or 1e-9
    r, n_hit = 0.0, 0
    for j, h in enumerate(hit):
        if h:
            n_hit += 1
            r += d * (tgts[j] - entry) / risk0 / 3.0
    rem = 1.0 - n_hit / 3.0
    if rem > 1e-9:
        r += rem * d * (exit_px - entry) / risk0
    return round(r, 3)


def _votes_series(df, rules, prof) -> "object":
    """Vectorized per-bar votes for the backtest — the same nine rules as
    _votes_at, computed over the whole frame."""
    import numpy as np
    c = df["Close"].values
    op = df["Open"].values
    hi = df["High"].values
    lo = df["Low"].values
    n = len(df)
    votes = np.zeros(n)
    w = {k: v["weight"] * (1 if v["enabled"] else 0) for k, v in rules.items()}
    atrv = df["atr"].values
    atrv = np.where(atrv > 0, atrv, c * 0.02)

    if w.get("ema_trend"):
        e50, e200 = df["ema50"].values, df["ema200"].values
        votes += w["ema_trend"] * np.where(
            (c > e50) & (e50 > e200), 1, np.where((c < e50) & (e50 < e200), -1, 0))

    if w.get("triple_rsi"):
        r5 = df["rsi5"].values
        r1, r2, r3 = np.roll(r5, 1), np.roll(r5, 2), np.roll(r5, 3)
        votes += w["triple_rsi"] * np.where(
            (r5 < 30) & (r5 < r1) & (r1 < r2) & (r3 < 60) & (c > df["ema200"].values),
            1,
            np.where((r5 > 70) & (r5 > r1) & (r1 > r2) & (r3 > 40)
                     & (c < df["ema200"].values), -1, 0))

    if w.get("sd_zones"):
        body = np.abs(c - op)
        min_body = prof["zone_atr_mult"] * atrv
        bull_break = (np.roll(c, 1) > np.roll(hi, 2)) & \
                     (np.roll(body, 1) >= np.roll(min_body, 1)) & \
                     (np.roll(c, 1) > np.roll(op, 1))
        bear_break = (np.roll(c, 1) < np.roll(lo, 2)) & \
                     (np.roll(body, 1) >= np.roll(min_body, 1)) & \
                     (np.roll(c, 1) < np.roll(op, 1))
        sd_long = bull_break & (lo <= np.roll(hi, 2) * 1.001) & (c > op)
        sd_short = bear_break & (hi >= np.roll(lo, 2) * 0.999) & (c < op)
        votes += w["sd_zones"] * (sd_long.astype(int) - sd_short.astype(int))

    if w.get("order_blocks"):
        swing_hi = df["High"].rolling(10).max().shift(1).values
        swing_lo = df["Low"].rolling(10).min().shift(1).values
        volx = df["Volume"].values / np.where(df["vol_sma"].values > 0,
                                              df["vol_sma"].values, 1.0)
        votes += w["order_blocks"] * np.where(
            (c > swing_hi) & (volx > 1.3), 1,
            np.where((c < swing_lo) & (volx > 1.3), -1, 0))

    if w.get("vol_trail"):
        tt = df["trail_trend"].values
        pt = np.roll(tt, 1)
        votes += w["vol_trail"] * np.where(
            (tt == 1) & (pt != 1), 1,
            np.where((tt == -1) & (pt != -1), -1, 0))

    if w.get("reversal_pivot"):
        k = 3
        # pivot bar = i-k; per-bar rule checks it is the extreme of the
        # k+1-bar window ending there → rolling(k+1).shift(k), not 2k+1.
        lo_min = df["Low"].rolling(k + 1).min().shift(k).values
        hi_max = df["High"].rolling(k + 1).max().shift(k).values
        a_ref = df["atr10"].shift(k).values
        a_ref = np.where(a_ref > 0, a_ref, c * 0.02)
        pl = np.roll(lo, k) <= lo_min
        ph = np.roll(hi, k) >= hi_max
        bull_rev = pl & ((c - np.roll(lo, k)) >= 2.5 * a_ref) & (c > op)
        bear_rev = ph & ((np.roll(hi, k) - c) >= 2.5 * a_ref) & (c < op)
        votes += w["reversal_pivot"] * (bull_rev.astype(int) - bear_rev.astype(int))

    if w.get("alligator"):
        lips, teeth, jaw = df["lips"].values, df["teeth"].values, df["jaw"].values
        votes += w["alligator"] * np.where(
            (lips > teeth) & (teeth > jaw) & (c > lips), 1,
            np.where((lips < teeth) & (teeth < jaw) & (c < lips), -1, 0))

    if w.get("ema_stack"):
        ef, em, es = df["ema_f"].values, df["ema_m"].values, df["ema_s"].values
        votes += w["ema_stack"] * np.where(
            (ef > em) & (em > es) & (c > ef), 1,
            np.where((ef < em) & (em < es) & (c < ef), -1, 0))

    if w.get("mtf_alignment"):
        votes += w["mtf_alignment"] * df["mtf"].values.astype(float)

    if w.get("bnf_dip"):
        sma25 = df["sma25"].values
        with np.errstate(invalid="ignore", divide="ignore"):
            dist = (c - sma25) / sma25 * 100.0
        pc = np.roll(c, 1)
        depth = _bnf_depth(prof)
        dip_long = (dist <= -depth) & (c > pc)   # NaN dist → False
        dip_short = (dist >= depth) & (c < pc)
        votes += w["bnf_dip"] * (dip_long.astype(int) - dip_short.astype(int))

    votes[:_WARMUP] = 0.0        # indicator warm-up — never trade it
    return votes


def _backtest(symbol: str, period: str, interval: str, rules,
              method: str) -> dict:
    import numpy as np
    prof = METHODS[method]
    df = _add_indicators(_fetch_history(symbol, period, interval), method)
    n = len(df)
    capital = _settings()["capital"]
    rf = _settings()["risk_pct"] / 100

    votes = _votes_series(df, rules, prof)
    c = df["Close"].values

    trade_log: list[dict] = []
    pos = 0
    entry = cur_stop = 0.0
    risk0 = 1e-9
    tgts = [0.0, 0.0, 0.0]
    hit = [False, False, False]
    hold = 0

    for i in range(1, n):
        h_i = float(df["High"].iloc[i])
        l_i = float(df["Low"].iloc[i])
        cl_i = float(c[i])
        # ── manage an open position on this bar (stop checked first = worst case)
        if pos != 0:
            hold += 1
            d = pos
            stop_hit = (l_i <= cur_stop) if d == 1 else (h_i >= cur_stop)
            exit_now, exit_px = False, 0.0
            if stop_hit:
                exit_now, exit_px = True, cur_stop
            else:
                for j in range(3):
                    if hit[j]:
                        continue
                    t_hit = (h_i >= tgts[j]) if d == 1 else (l_i <= tgts[j])
                    if t_hit:
                        hit[j] = True
                        if j == 0:
                            cur_stop = entry        # stop → break-even
                if hit[2]:
                    exit_now, exit_px = True, tgts[2]
                elif hold >= prof["max_hold_bars"]:
                    exit_now, exit_px = True, cl_i
            if exit_now:
                trade_log.append({
                    "r": _realized_r(d, entry, risk0, tgts, hit, exit_px),
                    "bars": hold, "dir": d})
                pos = 0
        # ── fresh entry at this bar's close (signal known at close — no lookahead)
        if pos == 0 and abs(votes[i]) >= SCORE_THRESHOLD:
            d = 1 if votes[i] > 0 else -1
            a = float(df["atr"].iloc[i])
            a = a if a > 0 else cl_i * 0.02
            entry = cl_i
            cur_stop = entry - d * prof["atr_stop"] * a
            risk0 = abs(entry - cur_stop) or 1e-9
            tgts = [entry + d * rr * risk0 for rr in prof["targets_r"]]
            hit = [False, False, False]
            hold = 0
            pos = d

    wins = [t for t in trade_log if t["r"] > 0.05]
    losses = [t for t in trade_log if t["r"] < -0.05]
    gp = sum(t["r"] for t in wins)
    gl = abs(sum(t["r"] for t in losses))
    pf = round(gp / gl, 2) if gl > 0 else (99.0 if gp > 0 else 0.0)
    rs = [t["r"] for t in trade_log]
    eq = capital
    peak = eq
    max_dd = 0.0
    for r in rs:
        eq *= (1 + rf * r)
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak)
    return {
        "symbol": symbol, "method": method, "bars": n,
        "signals": int((np.abs(votes) >= SCORE_THRESHOLD).sum()),
        "trades": len(trade_log),
        "win_rate_pct": round(len(wins) / len(trade_log) * 100, 1)
                         if trade_log else 0.0,
        "profit_factor": pf,
        "total_r": round(sum(rs), 2),
        "expectancy_r": round(float(np.mean(rs)), 3) if rs else 0.0,
        "avg_win_r": round(float(np.mean([t["r"] for t in wins])), 2) if wins else 0.0,
        "avg_loss_r": round(float(np.mean([t["r"] for t in losses])), 2) if losses else 0.0,
        "max_drawdown_pct": round(max_dd * 100, 2),
        "avg_hold_bars": round(float(np.mean([t["bars"] for t in trade_log])), 1)
                          if trade_log else 0.0,
        "final_equity": round(eq, 2),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Formatting: setup plan, watchlist scan, report
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# Charting — TradingView-style auto-drawn levels (PIL, no matplotlib)
# ══════════════════════════════════════════════════════════════════════════════

def _render_chart_for(symbol: str, method: str, period: str | None = None,
                      interval: str | None = None, bars: int = 140):
    """Fetch → indicators → signal → render chart PNG. Returns the saved path
    or None. The chart module is imported lazily so plugin load never pays
    for PIL if it is somehow missing."""
    from plugins._trading_chart import build_drawing_plan, render_chart
    prof = METHODS[method]
    period = period or prof["period"]
    interval = interval or prof["interval"]
    df = _add_indicators(_fetch_history(symbol, period, interval), method)
    df.attrs["symbol"] = symbol
    rules = _rules()
    sig = _signal(df, rules, prof)
    plan = build_drawing_plan(df, method, sig, rules)
    return render_chart(df, plan, bars=bars)


def _setup_plan(symbol: str, method: str, period: str | None,
                interval: str | None) -> str:
    prof = METHODS[method]
    mkt = detect_market(symbol)
    period = period or prof["period"]
    interval = interval or prof["interval"]
    df = _add_indicators(_fetch_history(symbol, period, interval), method)
    rules = _rules()
    sig = _signal(df, rules, prof)
    last = df.iloc[-1]
    px = float(last["Close"])
    atr = float(last["atr"]) or px * 0.02
    cap, risk_pct = _settings()["capital"], _settings()["risk_pct"]

    lines = [
        f"{prof['label']} setup — {mkt['label']} @ {px:,.2f} "
        f"({interval} bars, {mkt['session']})",
        f"Signal: {sig['direction']} (score {sig['score']}{sig['ml_note']})",
    ]
    # live-entry session gate (advice only — never blocks analysis)
    if _settings()["session_filter"] and sig["direction"] != "FLAT":
        ok, why = _session_ok(symbol)
        if not ok:
            lines.insert(0, f"⚠ {why}")
    for r, v in sig["votes"].items():
        if v == 0:
            continue
        arrow = "▲ bullish" if v > 0 else "▼ bearish"
        lines.append(f"  {r}: {arrow} (weight {rules[r]['weight']:.2f})")
    if sig["direction"] != "FLAT":
        d = 1 if sig["direction"] == "LONG" else -1
        stop = px - d * prof["atr_stop"] * atr
        risk = abs(px - stop) or 1e-9
        tgs = [px + d * rr * risk for rr in prof["targets_r"]]
        risk_amt = cap * risk_pct / 100
        qty = max(1, int(risk_amt / risk))
        hold = ("flat before session close" if method == "intraday"
                else f"~{prof['max_hold_bars']} bars max")
        lines += [
            f"Entry zone: {px - prof['atr_entry'] * atr:,.2f}–"
            f"{px + prof['atr_entry'] * atr:,.2f} (±{prof['atr_entry']}×ATR)",
            f"Stop: {stop:,.2f} ({prof['atr_stop']}×ATR)"
            + (" — move to break-even at TP1" if len(tgs) > 1 else ""),
            "Targets: " + " → ".join(f"{t:,.2f} ({rr}R)"
                                     for t, rr in zip(tgs, prof["targets_r"])),
            f"Size: {qty} units — risking {risk_pct:.1f}% of {cap:,.0f} "
            f"({risk_amt:,.0f})",
            f"Exit: staged targets (⅓ each), {prof['trail_atr']}×ATR trail "
            f"context, {hold}.",
            "Plan is analysis only — no real orders are placed.",
        ]
    else:
        lines.append("No trade: weighted votes below threshold. "
                     "Waiting beats forcing a setup.")
    return "\n".join(lines)


def _trade_plan_dict(symbol: str, method: str, period: str | None,
                     interval: str | None) -> dict | None:
    """Machine-readable twin of _setup_plan: entry/stop/targets/levels plus a
    compact recent-bar series for the dashboard's live chart. None = no trade
    signal (payload still carries series + votes)."""
    prof = METHODS[method]
    mkt = detect_market(symbol)
    period = period or prof["period"]
    interval = interval or prof["interval"]
    df = _add_indicators(_fetch_history(symbol, period, interval), method)
    rules = _rules()
    sig = _signal(df, rules, prof)
    last = df.iloc[-1]
    px = float(last["Close"])
    atr = float(last["atr"]) or px * 0.02

    entry = stop = None
    targets: list[float] = []
    d = 0
    if sig["direction"] != "FLAT":
        d = 1 if sig["direction"] == "LONG" else -1
        stop = px - d * prof["atr_stop"] * atr
        entry = px + d * prof["atr_entry"] * atr
        targets = [px + d * rr * abs(px - stop) for rr in prof["targets_r"]]

    tail = df.tail(400)
    series = {
        "time": [ts.strftime("%Y-%m-%d") if interval == "1d"
                 else ts.strftime("%Y-%m-%dT%H:%M:%S")
                 for ts in tail.index],
        "open":  [round(float(v), 6) for v in tail["Open"]],
        "high":  [round(float(v), 6) for v in tail["High"]],
        "low":   [round(float(v), 6) for v in tail["Low"]],
        "close": [round(float(v), 6) for v in tail["Close"]],
        "ema_f":   [round(float(v), 6) for v in tail["ema_f"]],
        "ema_m":   [round(float(v), 6) for v in tail["ema_m"]],
        "ema_s":   [round(float(v), 6) for v in tail["ema_s"]],
        "ema200":  [round(float(v), 6) for v in tail["ema200"]],
        "trail":   [round(float(v), 6) for v in tail["trail"]],
        "volume":  [max(0, int(v)) for v in tail["Volume"]],
    }
    return {
        "symbol": symbol,
        "canonical": canonical_symbol(symbol),
        "method": method,
        "interval": interval,
        "market": mkt["market"],
        "market_label": mkt["label"],
        "session_ok": _session_ok(symbol)[0],
        "price": round(px, 6),
        "direction": sig["direction"],
        "score": round(float(sig["score"]), 2),
        "votes": {k: int(v) for k, v in sig["votes"].items()},
        "weights": {k: round(float(r["weight"]), 3)
                    for k, r in rules.items()},
        "entry": None if entry is None else round(entry, 6),
        "stop": None if stop is None else round(stop, 6),
        "targets": [round(t, 6) for t in targets],
        "targets_r": list(prof["targets_r"]),
        "series": series,
    }


def _chart_payload(symbol: str, method: str, period: str | None,
                   interval: str | None) -> str:
    """JSON string for the dashboard's live chart (TradingView-style, browser
    rendered). setup/chart auto-append it to their text output."""
    plan = _trade_plan_dict(symbol, method, period, interval)
    return "LIVECHART_JSON:" + json.dumps(plan, separators=(",", ":"))


def _live_chart_url(symbol: str, method: str) -> str:
    """Dashboard URL of the standalone live-chart page, or '' when the
    dashboard is unreachable (server module missing / no IP)."""
    try:
        from dashboard.server import dashboard_url
        return (dashboard_url() or "") + \
            f"/chart?symbol={symbol}&method={method}"
    except Exception:
        return ""


def _scan(method: str | None = None) -> str:
    """Rank the watchlist by |score| for a method (default: default_method)."""
    method = method or _settings()["default_method"]
    prof = METHODS[method]
    rules = _rules()
    rows = []
    for sym in _settings()["watchlist"]:
        try:
            df = _add_indicators(_fetch_history(sym, prof["period"],
                                                prof["interval"]), method)
            sig = _signal(df, rules, prof)
            px = float(df["Close"].iloc[-1])
            rows.append((abs(sig["score"]), sig["score"], sym, px,
                         sig["direction"], ""))
        except Exception as e:
            rows.append((0.0, 0.0, sym, 0.0, "—", str(e)[:48]))
    rows.sort(reverse=True)
    out = [f"Watchlist scan — {prof['label']} method ({prof['interval']} bars):"]
    for _, score, sym, px, direction, err in rows:
        if err:
            out.append(f"    {sym:<14} {err}")
        else:
            mark = "★" if abs(score) >= SCORE_THRESHOLD else " "
            out.append(f"  {mark} {sym:<14} {direction:<7} score {score:+.2f} "
                       f"@ {px:,.2f}")
    if not any(r[0] >= SCORE_THRESHOLD for r in rows):
        out.append("  (nothing above threshold — patience)")
    return "\n".join(out)


def _report() -> str:
    rules = _rules()
    entries = _journal_load()
    lines = ["Strategy report:"]
    if entries:
        lines.append("  Journal (per method):")
        for method in ("intraday", "swing", "positional", "bnf_dip"):
            sub = [e for e in entries if e.get("method") == method]
            if not sub:
                continue
            s = _stats(sub)
            lines.append(
                f"    {method:<10} {s['n']:>3} signals — win rate "
                f"{s['win_rate']}%, avg {s['avg_ret']:+.2f}%, PF "
                f"{s['profit_factor']}")
    else:
        lines.append("  Journal: empty — no evaluated signals yet")
    try:
        meta = json.loads(_META.read_text())
        lines.append(f"  ML model: {meta.get('backend')} — "
                     f"{meta.get('accuracy', 0) * 100:.1f}% directional "
                     f"accuracy on {meta.get('samples', 0)} samples")
    except Exception:
        lines.append("  ML model: not trained yet (ask me to train it)")
    src = _source_status()
    fb = int(src.get("binance_fallbacks", 0))
    lines.append(f"  Data sources: yahoo ok {int(src.get('yahoo_ok', 0))}"
                 f" · binance fallback {fb}")
    if fb:
        last = src.get("last_fallback") or {}
        when = last.get("when") or 0
        age_s = max(0, int(time.time() - when)) if when else None
        age = (f"{age_s // 3600}h{(age_s % 3600) // 60:02d}m ago"
               if age_s and age_s >= 3600 else
               f"{age_s // 60}m ago" if age_s is not None else "at an unknown time")
        lines.append(f"    last fallback {age}: "
                     f"{str(last.get('reason') or 'unknown')[:90]}")
        if when and time.time() - when < 86400:
            lines.append("    ⚠ Yahoo degraded within the last 24h — crypto data "
                         "served from Binance; watch equity fetches too")
    ep = src.get("last_episode") or {}
    if ep.get("ended"):
        eage_s = max(0, int(time.time() - ep["ended"]))
        eage = (f"{eage_s // 3600}h{(eage_s % 3600) // 60:02d}m ago"
                if eage_s >= 3600 else f"{eage_s // 60}m ago")
        lines.append(f"    last outage: {int(ep.get('fallbacks', 0))} fallback(s), "
                     f"ended {eage} ({str(ep.get('last_reason') or 'unknown')[:80]})")
        if eage_s < 86400:
            lines.append(f"    ✓ Yahoo recovered — {_RECOVERY_STREAK} clean fetches "
                         "confirmed; counters were reset")
    lines.append("  Rule weights (adapted from the journal):")
    for name, r in rules.items():
        mine = [e for e in entries if e.get("votes", {}).get(name, 0) != 0]
        hit = (round(sum(1 for e in mine if e.get("ret_pct", 0) > 0)
                     / len(mine) * 100) if mine else None)
        lines.append(
            f"    {name:<15} {r['weight']:.2f}"
            + (f" — {len(mine)} signals, {hit}% correct" if hit is not None
               else " — no data yet")
            + ("" if r["enabled"] else " (disabled)"))
    lines.append(f"  Threshold {SCORE_THRESHOLD} · adapt LR "
                 f"{_settings()['adapt_lr']} · risk "
                 f"{_settings()['risk_pct']}%/trade")
    lines.append("  Methods: intraday (15m, tight stop, trail, session-gated) "
                 "· swing (1d, 1R/2R/3R, BE at 1R) · positional (1d, 2R/3R/5R)")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def run(parameters: dict, player=None, session_memory=None) -> str:
    global _NOTIFY_PLAYER
    _NOTIFY_PLAYER = player   # data layer can reach the HUD through this
    action = str(parameters.get("action") or "setup").lower().strip()
    try:
        if action == "list":
            return (
                "Valid symbol styles (crypto broker notation is normalized "
                "automatically):\n"
                "  India equities : RELIANCE.NS, TCS.NS, INFY.NS, "
                "HDFCBANK.NS (BSE: .BO)\n"
                "  India indices  : ^NSEI (Nifty 50), ^BSESN (Sensex)\n"
                "  Crypto         : BTC-USD, ETH-USD, SOL-USD — also "
                "BTCUSD.P, SOLUSDT, XBTUSD, 'bitcoin', 'solana'\n"
                "  US / global    : AAPL, MSFT, NVDA, SPY\n"
                "Examples: 'intraday setup on BTCUSD.P' → BTC-USD; "
                "'swing on SOLUSD.P' → SOL-USD.")

        if action in ("setup", "analyze"):
            symbol = str(parameters.get("symbol") or "").upper().strip()
            if not symbol:
                wl = _settings()["watchlist"]
                symbol = wl[0] if wl else "RELIANCE.NS"
            symbol = canonical_symbol(symbol)
            method = _method(parameters)
            text = _setup_plan(symbol, method,
                               parameters.get("period"),
                               parameters.get("interval"))
            if action == "analyze":
                text = "\n".join(text.splitlines()[:3])
            # Auto-chart: every setup/analyze renders the levels image too.
            try:
                chart_path = _render_chart_for(symbol, method,
                                               parameters.get("period"),
                                               parameters.get("interval"))
                if chart_path:
                    text += f"\nChart drawn: {chart_path}"
            except Exception:
                pass
            # Live chart: machine-readable payload the dashboard renders as an
            # interactive TradingView-style chart; plus the standalone page URL.
            try:
                text += "\n" + _chart_payload(symbol, method,
                                              parameters.get("period"),
                                              parameters.get("interval"))
            except Exception:
                pass
            url = _live_chart_url(symbol, method)
            if url:
                text += f"\nLive chart: {url}"
            # Paper trading: auto-open paper position if signal fires
            try:
                import plugins._trading_journal as tj
                tj.ensure_files()
                rules = _rules()
                df = _add_indicators(_fetch_history(symbol,
                                                     parameters.get("period") or METHODS[method]["period"],
                                                     parameters.get("interval") or METHODS[method]["interval"]),
                                      method)
                sig = _signal(df, rules, METHODS[method])
                if sig["direction"] != "FLAT":
                    px = float(df["Close"].iloc[-1])
                    atr = float(df["atr"].iloc[-1]) or px * 0.02
                    d = 1 if sig["direction"] == "LONG" else -1
                    stop = px - d * METHODS[method]["atr_stop"] * atr
                    targets = [px + d * rr * abs(px - stop)
                               for rr in METHODS[method]["targets_r"]]
                    capital = _settings()["capital"]
                    risk_amt = capital * _settings()["risk_pct"] / 100
                    qty = max(1, int(risk_amt / (abs(px - stop) or 1e-9)))

                    # Paper trade
                    tj.open_position(
                        symbol=symbol, method=method,
                        direction=sig["direction"],
                        entry_price=px, stop_loss=stop,
                        targets=targets, position_size=qty,
                        capital=capital, atr=atr,
                        rules_votes=sig["votes"],
                    )
                    text += f"\n📝 Paper position auto-opened: {sig['direction']} {symbol}"

                    # Live trading (if enabled and configured)
                    try:
                        from memory.config_manager import get_plugin_config
                        cfg = get_plugin_config("trading")
                        live_enabled = cfg.get("live_trading_enabled", False)
                        if live_enabled:
                            import plugins.binance_trader as bt
                            binance_symbol = symbol.replace("-USD", "USDT")
                            if not binance_symbol.endswith("USDT"):
                                binance_symbol = f"{binance_symbol}USDT"
                            bt.trade(
                                symbol=binance_symbol,
                                direction=sig["direction"],
                                entry_price=px,
                                stop_loss=stop,
                                targets=targets,
                                position_size=qty,
                                capital=capital,
                                atr=atr,
                                signal_score=sig["score"],
                                rules_votes=sig["votes"],
                            )
                            text += f"\n🔴 Live trade signal sent to Binance: {sig['direction']} {binance_symbol}"
                    except Exception as e:
                        text += f"\n⚠️ Live trading not available: {e}"
            except Exception:
                pass
            # Learn from being used: journal recent bars + adapt weights.
            try:
                rules = _rules()
                _evaluate_symbol(symbol, rules)
                note = _adapt_weights(rules)
                if "→" in note and player:
                    player.write_log(f"SYS: Trading rules adapted — {note}")
            except Exception:
                pass
            return text

        elif action == "scan":
            return _scan(_method(parameters))

        elif action == "chart":
            symbol = canonical_symbol(
                str(parameters.get("symbol") or "").upper().strip()
                or (_settings()["watchlist"] or ["RELIANCE.NS"])[0])
            method = _method(parameters)
            try:
                nbars = int(float(parameters.get("bars") or 140))
            except (TypeError, ValueError):
                nbars = 140
            chart_path = _render_chart_for(symbol, method,
                                           parameters.get("period"),
                                           parameters.get("interval"),
                                           bars=max(60, min(nbars, 400)))
            if not chart_path:
                return (f"Could not render a chart for {symbol} — check the "
                        "symbol and try again.")
            # Spoken summary accompanies the drawn levels
            summary = _setup_plan(symbol, method, parameters.get("period"),
                                  parameters.get("interval"))
            keep = [l for l in summary.splitlines()
                    if l.startswith(("Signal:", "Entry zone:", "Stop:",
                                     "Targets:"))]
            text = (f"Chart drawn: {chart_path}\n"
                    + ("\n".join(keep) if keep else summary)
                    + "\n" + _chart_payload(symbol, method,
                                            parameters.get("period"),
                                            parameters.get("interval")))
            url = _live_chart_url(symbol, method)
            if url:
                text += f"\nLive chart: {url}"
            return text

        elif action == "livechart":
            symbol = canonical_symbol(
                str(parameters.get("symbol") or "").upper().strip()
                or (_settings()["watchlist"] or ["RELIANCE.NS"])[0])
            return _chart_payload(symbol, _method(parameters),
                                  parameters.get("period"),
                                  parameters.get("interval"))

        elif action == "backtest":
            symbol = canonical_symbol(
                str(parameters.get("symbol") or "").upper().strip()
                or (_settings()["watchlist"] or ["RELIANCE.NS"])[0])
            method = _method(parameters)
            prof = METHODS[method]
            period = str(parameters.get("period") or prof["period"])
            interval = str(parameters.get("interval") or prof["interval"])
            r = _backtest(symbol, period, interval, _rules(), method)
            return (
                f"Backtest {r['symbol']} — {r['method']} method "
                f"({r['bars']} {interval} bars, {r['signals']} signal bars → "
                f"{r['trades']} trades): win rate {r['win_rate_pct']}%, "
                f"profit factor {r['profit_factor']}, expectancy "
                f"{r['expectancy_r']:+.3f}R/trade, total {r['total_r']:+.1f}R "
                f"(avg win {r['avg_win_r']:+.2f}R / loss {r['avg_loss_r']:+.2f}R), "
                f"max drawdown {r['max_drawdown_pct']}%, avg hold "
                f"{r['avg_hold_bars']} bars. Final equity "
                f"{r['final_equity']:,.0f} on {_settings()['capital']:,.0f} "
                f"(risking {_settings()['risk_pct']}%/trade). Analysis only.")

        elif action == "evaluate":
            rules = _rules()
            syms = ([canonical_symbol(str(parameters.get("symbol")).upper())]
                    if parameters.get("symbol") else _settings()["watchlist"])
            wrote, errs = [], []
            for sym in syms:
                try:
                    wrote += _evaluate_symbol(sym, rules)
                except Exception as e:
                    errs.append(f"{sym}: {e}")
            note = _adapt_weights(rules)
            out = (f"Evaluated {len(wrote)} new signal(s)"
                   + (f"; {len(errs)} symbols skipped ({'; '.join(errs[:2])})"
                      if errs else ""))
            if wrote:
                s = _stats(_journal_load())
                out += (f". Journal now {s['n']} signals — win rate "
                        f"{s['win_rate']}%")
            out += f". Weight adaptation: {note}"
            return out

        elif action == "train":
            backend = str(parameters.get("backend") or "torch").lower()
            return _train(backend)

        elif action == "report":
            return _report()

        elif action == "paper_status":
            try:
                import plugins._trading_journal as tj
                tj.ensure_files()
                symbol = str(parameters.get("symbol") or "").upper().strip() or None
                method = str(parameters.get("method") or "").lower().strip() or None
                return tj.paper_status(symbol=symbol, method=method)
            except Exception as e:
                return f"Paper trading status error: {e}"

        elif action == "paper_open":
            try:
                import plugins._trading_journal as tj
                tj.ensure_files()
                symbol = canonical_symbol(
                    str(parameters.get("symbol") or "").upper().strip()
                    or (_settings()["watchlist"] or ["RELIANCE.NS"])[0])
                method = _method(parameters)
                direction = str(parameters.get("direction") or "LONG").upper()
                if direction not in ("LONG", "SHORT"):
                    direction = "LONG"
                # Get current price and ATR
                prof = METHODS[method]
                df = _add_indicators(_fetch_history(symbol, prof["period"],
                                                     prof["interval"]), method)
                px = float(df["Close"].iloc[-1])
                atr = float(df["atr"].iloc[-1]) or px * 0.02
                rules = _rules()
                sig = _signal(df, rules, prof)
                d = 1 if direction == "LONG" else -1
                stop = px - d * prof["atr_stop"] * atr
                targets = [px + d * rr * abs(px - stop) for rr in prof["targets_r"]]
                capital = _settings()["capital"]
                risk_pct = _settings()["risk_pct"] / 100
                risk_amt = capital * risk_pct
                qty = max(1, int(risk_amt / (abs(px - stop) or 1e-9)))
                tj.open_position(
                    symbol=symbol, method=method, direction=direction,
                    entry_price=px, stop_loss=stop, targets=targets,
                    position_size=qty, capital=capital, atr=atr,
                    rules_votes=sig["votes"],
                )
                return (f"Paper position opened: {direction} {symbol} @ {px:,.2f}\n"
                        f"Stop: {stop:,.2f} ({prof['atr_stop']}×ATR)\n"
                        f"Targets: {' → '.join(f'{t:,.2f} ({rr}R)' for t, rr in zip(targets, prof['targets_r']))}\n"
                        f"Size: {qty} units (risking {risk_pct*100:.1f}% = {risk_amt:,.0f})")
            except Exception as e:
                return f"Paper open error: {e}"

        elif action == "paper_close":
            try:
                import plugins._trading_journal as tj
                tj.ensure_files()
                symbol = str(parameters.get("symbol") or "").upper().strip()
                method = str(parameters.get("method") or "swing").lower().strip()
                key = f"{symbol}:{method}"
                trade = tj.close_position(key, reason="manual_close")
                if trade:
                    return (f"Paper trade closed: {trade['direction']} {trade['symbol']} → "
                            f"entry {trade['entry_price']:,.2f}, exit {trade['exit_price']:,.2f} = "
                            f"{trade['realized_pnl_pct']:+.2f}% ({trade['realized_pnl']:+,.2f}) [{trade['exit_reason']}]")
                return f"No open paper position for {symbol} ({method})"
            except Exception as e:
                return f"Paper close error: {e}"

        elif action == "paper_reset":
            try:
                import plugins._trading_journal as tj
                tj.ensure_files()
                return tj.paper_reset()
            except Exception as e:
                return f"Paper reset error: {e}"

        elif action == "paper_learnings":
            try:
                import plugins._trading_journal as tj
                tj.ensure_files()
                return tj.paper_learnings_summary()
            except Exception as e:
                return f"Paper learnings error: {e}"

        elif action == "paper_backfill":
            try:
                import plugins._trading_journal as tj
                tj.ensure_files()
                return tj.backfill_from_journal()
            except Exception as e:
                return f"Paper backfill error: {e}"

        elif action == "live_test":
            try:
                import plugins.binance_trader as bt
                return bt.test_api()
            except Exception as e:
                return f"Live trading test error: {e}"

        elif action == "live_status":
            try:
                import plugins.binance_trader as bt
                return bt.status()
            except Exception as e:
                return f"Live trading status error: {e}"

        elif action == "live_configure":
            try:
                import plugins.binance_trader as bt
                return bt.configure(
                    mode=parameters.get("mode"),
                    max_daily_loss=parameters.get("max_daily_loss_pct"),
                    max_position=parameters.get("max_position_usd"),
                    max_daily_trades=parameters.get("max_daily_trades"),
                    cooldown=parameters.get("cooldown_seconds"),
                    require_confirmation=parameters.get("require_confirmation"),
                )
            except Exception as e:
                return f"Live trading configure error: {e}"

        elif action == "live_enable":
            try:
                import plugins.binance_trader as bt
                return bt.enable_live_trading()
            except Exception as e:
                return f"Live trading enable error: {e}"

        elif action == "live_disable":
            try:
                import plugins.binance_trader as bt
                return bt.disable_live_trading()
            except Exception as e:
                return f"Live trading disable error: {e}"

        else:
            return (f"Unknown action '{action}'. Use setup, analyze, scan, "
                    "backtest, evaluate, train, report, paper_status, "
                    "paper_open, paper_close, paper_reset, paper_learnings, "
                    "paper_backfill, live_test, live_status, live_configure, "
                    "live_enable, live_disable.")

    except Exception as e:
        return f"Sir, the trading strategy skill hit a snag: {e}"
