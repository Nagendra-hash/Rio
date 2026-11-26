"""
core/trading_monitor.py — continuous background trading monitor.

This module is the orchestration/observability layer the RIO trading stack was
missing. It owns NO market feed and NO second strategy engine:

  * market data:   the EXISTING Binance daemon (core/binance_ws.py) — websocket
                   kline/ticker streams with REST fallback and its own reconnect
                   logic. This module only subscribes to closed candles.
  * Setup A:       the EXISTING liquidity sweep state machine
                   (core/liquidity_sweep.py), consumed through its signal-event
                   hook. It is never re-implemented here.
  * trade execution: the EXISTING paper engine (core/paper_trading.py) when it
                   accepts the signal. This module never places an order and
                   has no broker/live path.

What this module adds:

  1. A persistent process-level monitor that keeps running after Hermes/RIO
     answers a request — started once from main.py, not per user message.
  2. Setup B (short continuation) evaluated only on candle CLOSE.
  3. A complete trade plan (entry, SL, T1, T2, quantity, risk, margin,
     leverage, R:R) computed from the live price and the user's existing
     paper-trading settings.
  4. Verbosity-gated, deduplicated Telegram alerts (entry, lifecycle levels,
     connectivity loss/recovery).
  5. Signal lifecycle tracking (entry → SL / T1 / T2) from real candle highs
     and lows.
  6. Health reporting that never claims ALERTS ACTIVE while the feed is down.
  7. SQLite persistence + restart recovery (last processed candle, active
     signals, Setup B armed state, lifecycle).

Honesty rules encoded here (not just documented):
  * Prices and volume come from Binance only. Nothing is synthesised.
  * Delta is the exchange-reported taker-buy estimate from the kline feed
    (see core/liquidity_sweep.py). If it is missing, the condition is
    UNAVAILABLE and no signal is produced.
  * Gold is monitored on the exchange's own tokenised-gold instrument
    (PAXGUSDT). A crypto BTC price is never substituted for gold, and no
    XAUUSD spot price is fabricated because this feed cannot supply one.
  * No SSL/BSL zone or Setup B trigger is invented. Missing configuration is
    reported as "not configured", never guessed.
  * Live trading is never enabled by this module.
"""
from __future__ import annotations

import copy
import json
import math
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Callable, Optional

from core import binance_ws as bw

UTC = timezone.utc

_INTERVAL_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000}

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config" / "trading_monitor.json"
PAPER_CONFIG_PATH = BASE_DIR / "config" / "paper_trading.json"

VERBOSITY = ("quiet", "normal", "verbose")

LONG = "LONG"
SHORT = "SHORT"

# Lifecycle states for a confirmed monitored signal.
SIGNAL_OPEN = "OPEN"
SIGNAL_T1 = "TARGET_1_HIT"
SIGNAL_T2 = "TARGET_2_HIT"
SIGNAL_SL = "STOP_LOSS_HIT"
SIGNAL_CLOSED = "CLOSED"

DEFAULT_CONFIG = {
    "version": 1,
    "enabled": True,
    "poll_interval_s": 5,
    "stale_after_s": 180,
    "verbosity": "normal",
    "symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "PAXGUSDT"],
    "alerts": {
        "enabled": True,
        "entry": True,
        "stages": False,
        "lifecycle": True,
        "connectivity": True,
    },
    "setup_a": {
        "enabled": True,
        "source": "liquidity_engine",
        "targets": {"t1_r": 1.5, "t2_r": 2.5},
        "min_rr": 1.5,
        "max_risk_pct": 2.0,
    },
    "setup_b": {
        "enabled": True,
        "timeframe": "5m",
        "min_rr": 1.5,
        "targets": {"t1_r": 2.0, "t2_r": 3.0},
        "symbols": {
            "BTCUSDT": {"enabled": True, "trigger": 64200.0, "stop_loss": 64500.0,
                        "target_1": 63500.0, "target_2": 63000.0},
            "ETHUSDT": {"enabled": True, "trigger": 1800.0, "stop_loss": 1825.0,
                        "target_1": 1770.0, "target_2": 1750.0},
            "SOLUSDT": {"enabled": False, "trigger": None, "stop_loss": None,
                        "target_1": None, "target_2": None},
            "PAXGUSDT": {"enabled": False, "trigger": None, "stop_loss": None,
                         "target_1": None, "target_2": None},
        },
    },
    "lifecycle": {"enabled": True, "defer_to_paper": True},
}


# ── helpers ──────────────────────────────────────────────────────────────────
def utc_iso(ms: Optional[int]) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(int(ms) / 1000, tz=UTC).isoformat()


def _num(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _round(value, digits=8):
    return round(float(value), digits) if value is not None else None


def _floor_down(value: float, precision: int) -> float:
    factor = 10 ** int(precision)
    return math.floor(float(value) * factor) / factor


def _pair(symbol) -> str:
    return bw.normalize_symbol(str(symbol or "")).upper()


def _deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _load_json(path: Path, default: dict) -> dict:
    loaded = {}
    try:
        if path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        loaded = {}
    if not isinstance(loaded, dict):
        loaded = {}
    return _deep_merge(default, loaded)


def _trade_mode(paper: dict) -> str:
    """The only execution mode this monitor can ever be in.

    Live trading is deliberately unreachable: this module has no broker, no
    order call, and no live switch. When the paper engine is active the signal
    is labelled PAPER; otherwise it is informational only.
    """
    if paper.get("enabled"):
        return "PAPER"
    return "ALERT-ONLY"


# ── monitor ──────────────────────────────────────────────────────────────────
class TradingMonitor:
    """Process-level continuous monitor.

    One instance per process (see get_monitor()). Safe to call from the
    Binance daemon's candle-dispatch thread, from the liquidity engine's
    signal thread, and from the dashboard/watchdog thread.
    """

    def __init__(
        self,
        config_path: Optional[str | Path] = None,
        db_path: Optional[str | Path] = None,
        *,
        alert_sender: Optional[Callable[[str], bool]] = None,
        feed_provider: Optional[Callable[[], dict]] = None,
        price_provider: Optional[Callable[[str], Optional[dict]]] = None,
        engine=None,
        paper_config_path: Optional[str | Path] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.config_path = Path(config_path or DEFAULT_CONFIG_PATH)
        self.db_path = Path(db_path or bw.database_path())
        self.paper_config_path = Path(paper_config_path or PAPER_CONFIG_PATH)
        self._alert_sender = alert_sender or bw.send_telegram
        self._feed_provider = feed_provider or bw.status
        self._price_provider = price_provider or self._default_price
        self._engine = engine
        self._clock = clock or time.time

        self._lock = threading.RLock()
        self._config = _load_json(self.config_path, DEFAULT_CONFIG)
        self._apply_config()

        self._started = False
        self._stop_event = threading.Event()
        self._watchdog: Optional[threading.Thread] = None
        self._on_candle_cb = self._on_candle
        self._on_signal_cb = self.on_strategy_signal

        self._last_candle: dict = {}
        self._last_market_ts: int = 0
        self._feed_state_cached = {"live": False, "transport": "none",
                                   "stale": True, "last_tick_age_s": None}
        self._connectivity = "DISCONNECTED"
        self._ever_connected = False
        self._errors = 0
        self._candles: dict[tuple, list] = {}

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_schema()
        self._load_recovery_state()

    # ── config ───────────────────────────────────────────────────────────
    def _apply_config(self) -> None:
        cfg = self._config
        self._symbols = [_pair(s) for s in (cfg.get("symbols") or [])
                         if str(s or "").strip()]
        verbosity = str(cfg.get("verbosity") or "normal").strip().lower()
        self._verbosity = verbosity if verbosity in VERBOSITY else "normal"
        self._poll_interval = max(1.0, _num(cfg.get("poll_interval_s"), 5.0))
        self._stale_after_s = max(30.0, _num(cfg.get("stale_after_s"), 180.0))
        self._alerts_cfg = dict(cfg.get("alerts") or {})
        self._setup_a = dict(cfg.get("setup_a") or {})
        self._setup_b = dict(cfg.get("setup_b") or {})
        self._lifecycle_cfg = dict(cfg.get("lifecycle") or {})

    def _alert_enabled(self, kind: str) -> bool:
        if not bool(self._alerts_cfg.get("enabled", True)):
            return False
        if kind == "entry":
            return bool(self._alerts_cfg.get("entry", True))
        if kind == "lifecycle":
            return bool(self._alerts_cfg.get("lifecycle", True))
        if kind == "connectivity":
            return bool(self._alerts_cfg.get("connectivity", True))
        if kind == "stage":
            return (bool(self._alerts_cfg.get("stages", False))
                    or self._verbosity == "verbose")
        return False

    def _paper_config(self) -> dict:
        try:
            from core.paper_trading import DEFAULT_CONFIG as _pc_default
            default = copy.deepcopy(_pc_default)
        except Exception:
            default = {}
        return _load_json(self.paper_config_path, default)

    # ── persistence ──────────────────────────────────────────────────────
    def _init_schema(self) -> None:
        self._conn.executescript("""
        CREATE TABLE IF NOT EXISTS monitor_signals (
            signal_id TEXT PRIMARY KEY,
            setup TEXT, direction TEXT, symbol TEXT, timeframe TEXT,
            state TEXT, entry REAL, stop_loss REAL, target_1 REAL, target_2 REAL,
            risk_per_unit REAL, reward_per_unit REAL, rr REAL, rr_t2 REAL,
            quantity REAL, risk_amount REAL, capital_required REAL,
            notional REAL,
            leverage REAL, margin REAL, fee_estimate REAL, slippage_estimate REAL,
            execution TEXT, trigger_json TEXT, payload_json TEXT,
            created_ts INTEGER, created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS monitor_symbols (
            setup_id TEXT PRIMARY KEY, setup TEXT, symbol TEXT, direction TEXT,
            realm TEXT, armed INTEGER, updated_ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS monitor_alert_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, alert_type TEXT, dedupe_key TEXT,
            message TEXT, delivered INTEGER DEFAULT 0, ts INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(symbol, alert_type, dedupe_key)
        );
        CREATE TABLE IF NOT EXISTS monitor_lifecycle (
            signal_id TEXT PRIMARY KEY,
            setup TEXT, symbol TEXT, direction TEXT,
            entry REAL, stop_loss REAL, target_1 REAL, target_2 REAL,
            quantity REAL, status TEXT, last_price REAL, last_ts INTEGER,
            hit_t1 INTEGER DEFAULT 0, hit_t2 INTEGER DEFAULT 0,
            hit_sl INTEGER DEFAULT 0, realized_pnl REAL,
            created_ts INTEGER, updated_ts INTEGER,
            UNIQUE(signal_id)
        );
        CREATE TABLE IF NOT EXISTS monitor_setup_b_seen (
            symbol TEXT, interval TEXT, ts INTEGER,
            UNIQUE(symbol, interval, ts)
        );
        CREATE TABLE IF NOT EXISTS monitor_state (
            key TEXT PRIMARY KEY, value TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_mon_sig_symbol
            ON monitor_signals(symbol, created_ts);
        CREATE INDEX IF NOT EXISTS idx_mon_life_status
            ON monitor_lifecycle(status);
        """)
        self._conn.commit()

    def _load_recovery_state(self) -> None:
        row = self._conn.execute(
            "SELECT value FROM monitor_state WHERE key='last_candle'").fetchone()
        if row and row[0]:
            try:
                self._last_candle = json.loads(row[0])
            except ValueError:
                self._last_candle = {}
        row = self._conn.execute(
            "SELECT value FROM monitor_state WHERE key='error_count'").fetchone()
        if row and row[0]:
            self._errors = int(_num(row[0], 0) or 0)
        self._seed_candles_from_db()

    def _seed_candles_from_db(self) -> None:
        """Restore recent closed-candle history from the shared daemon DB.

        Restart recovery must not lose the trend/relative-volume window, and
        must not reprocess candles that were already handled. `_setup_b_seen`
        is the authoritative duplicate guard; this only restores context.
        """
        intervals = {self._setup_b_timeframe(), "1m", "3m", "5m"}
        for sym in self._symbols:
            for interval in sorted(intervals):
                try:
                    rows = self._conn.execute(
                        "SELECT ts, open, high, low, close, volume,"
                        " taker_buy_volume FROM candles"
                        " WHERE symbol=? AND interval=? AND closed=1"
                        " ORDER BY ts DESC LIMIT 60",
                        (sym, interval)).fetchall()
                except sqlite3.Error:
                    rows = []
                if not rows:
                    continue
                self._candles[(sym, interval)] = [{
                    "ts": r[0], "open": r[1], "high": r[2], "low": r[3],
                    "close": r[4], "volume": r[5], "taker_buy_volume": r[6],
                } for r in reversed(rows)]

    def _set_state(self, key: str, value) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO monitor_state (key, value) VALUES (?,?)",
            (key, json.dumps(value) if not isinstance(value, str) else value))
        self._conn.commit()

    # ── prices / feed ────────────────────────────────────────────────────
    def _default_price(self, symbol: str) -> Optional[dict]:
        tick = bw.latest_prices().get(str(symbol).lower(), {})
        price = _num(tick.get("price"))
        if price is None or price <= 0:
            return None
        return {"price": price, "ts": tick.get("ts"), "source": "binance_ticker",
                "change_24h": _num(tick.get("change_24h")),
                "volume": _num(tick.get("volume"))}

    def _quote(self, symbol: str) -> Optional[dict]:
        try:
            raw = self._price_provider(symbol)
        except Exception:
            return None
        if not isinstance(raw, dict):
            return None
        price = _num(raw.get("price"))
        if price is None or price <= 0:
            return None
        return {"price": price, "ts": raw.get("ts"),
                "source": str(raw.get("source") or "ticker"),
                "change_24h": _num(raw.get("change_24h"))}

    def _feed_state(self) -> dict:
        try:
            raw = self._feed_provider() or {}
        except Exception as exc:
            self._errors += 1
            return {"live": False, "transport": "none", "stale": True,
                    "last_tick_age_s": None,
                    "reason": f"status_error:{type(exc).__name__}"}
        transport = str(raw.get("transport") or "none")
        live = bool(raw.get("live")) and transport in ("ws", "http")
        age = _num(raw.get("last_tick_age_s"))
        stale = age is None or age > self._stale_after_s
        latest_ts = 0
        for tick in (raw.get("latest") or {}).values():
            latest_ts = max(latest_ts, int(_num(tick.get("ts"), 0) or 0))
        if latest_ts:
            self._last_market_ts = max(self._last_market_ts, latest_ts)
        return {"live": live, "transport": transport,
                "last_tick_age_s": age, "stale": stale,
                "last_market_ts": latest_ts or None,
                "reason": str(raw.get("reason") or "")}

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._stop_event.clear()
        if self._engine is None:
            try:
                from core.liquidity_sweep import get_engine
                self._engine = get_engine()
            except Exception as exc:
                print(f"[Monitor] liquidity engine unavailable: {exc}", flush=True)
        if self._engine is not None:
            try:
                self._engine.subscribe_signals(self._on_signal_cb)
            except Exception as exc:
                print(f"[Monitor] signal subscribe failed: {exc}", flush=True)
        bw.subscribe_closed_candles(self._on_candle_cb)
        self._watchdog = threading.Thread(
            target=self._watchdog_loop, name="trading-monitor", daemon=True)
        self._watchdog.start()
        print("[Monitor] continuous trading monitor started", flush=True)
        # Start notice only in verbose mode: the default policy is one detailed
        # entry alert plus important status updates, not per-launch chatter.
        if self._verbosity == "verbose":
            self._send("SYSTEM", "MONITOR_STARTED",
                       f"{self._today()}:{self._now_ms()}",
                       "RIO TRADING MONITOR STARTED\n\n"
                       f"Symbols: {', '.join(self._symbols) or 'none'}\n"
                       f"Setup A: {'ON' if self._setup_a.get('enabled') else 'OFF'}\n"
                       f"Setup B: {'ON' if self._setup_b.get('enabled') else 'OFF'}\n"
                       "Market data: existing Binance daemon (websocket)\n"
                       "Execution: alerts only — no real order path")

    def stop(self) -> None:
        try:
            bw.unsubscribe_closed_candles(self._on_candle_cb)
        except Exception:
            pass
        if self._engine is not None:
            try:
                self._engine.unsubscribe_signals(self._on_signal_cb)
            except Exception:
                pass
        self._stop_event.set()
        with self._lock:
            self._started = False
        print("[Monitor] continuous trading monitor stopped", flush=True)

    def close(self) -> None:
        self.stop()
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    def _watchdog_loop(self) -> None:
        while not self._stop_event.wait(self._poll_interval):
            try:
                self._watchdog_tick()
            except Exception as exc:
                self._errors += 1
                print(f"[Monitor] watchdog error: {type(exc).__name__}: {exc}",
                      flush=True)

    def _watchdog_tick(self) -> None:
        feed = self._feed_state()
        self._feed_state_cached = feed
        state = self._connectivity_state(feed)
        previous = self._connectivity
        if state != previous:
            self._connectivity = state
            if state == "CONNECTED":
                self._ever_connected = True
            if state == "RECONNECTED":
                self._ever_connected = True
                if self._alert_enabled("connectivity"):
                    self._send("SYSTEM", "MONITOR_RECOVERED",
                               f"recovered:{self._last_market_ts}",
                               "✅ RIO TRADING MONITOR RECOVERED\n\n"
                               f"Market data: {feed.get('transport')} live\n"
                               f"Symbols: {', '.join(self._symbols)}\n"
                               "Monitoring resumed on closed candles.")
            elif previous in ("CONNECTED", "RECONNECTED"):
                if self._alert_enabled("connectivity"):
                    self._send("SYSTEM", "MONITOR_DISCONNECTED",
                               f"down:{int(self._clock())}",
                               "⚠️ RIO TRADING MONITOR — MARKET DATA DISCONNECTED\n\n"
                               f"Transport: {feed.get('transport') or 'none'}\n"
                               "No setup can be verified while the feed is down.\n"
                               "No alert is being claimed as active.")
        self._set_state("error_count", str(self._errors))

    def _connectivity_state(self, feed: dict) -> str:
        live = bool(feed.get("live")) and not feed.get("stale")
        if live:
            # A cold start is not a "recovery" — only an observed loss counts.
            if self._ever_connected and self._connectivity in ("DISCONNECTED", "STALE"):
                return "RECONNECTED"
            return "CONNECTED"
        if feed.get("live") and feed.get("stale"):
            return "STALE"
        return "DISCONNECTED"

    # ── candle pipeline ──────────────────────────────────────────────────
    def _on_candle(self, candle: dict) -> None:
        try:
            self.on_closed_candle(candle)
        except Exception as exc:
            self._errors += 1
            print(f"[Monitor] candle error: {type(exc).__name__}: {exc}",
                  flush=True)

    def on_closed_candle(self, candle: dict) -> None:
        if not isinstance(candle, dict) or not candle.get("closed"):
            return
        symbol = _pair(candle.get("symbol"))
        interval = str(candle.get("interval") or "")
        if interval not in _INTERVAL_MS:
            return
        ts = int(_num(candle.get("ts"), 0) or 0)
        if ts <= 0:
            return
        key = (symbol, interval)
        with self._lock:
            hist = self._candles.setdefault(key, [])
            if hist and ts <= hist[-1].get("ts", -1):
                return
            hist.append({
                "ts": ts, "interval": interval,
                "open": _num(candle.get("open")),
                "high": _num(candle.get("high")), "low": _num(candle.get("low")),
                "close": _num(candle.get("close")),
                "volume": _num(candle.get("volume")),
                "taker_buy_volume": _num(candle.get("taker_buy_volume")),
            })
            if len(hist) > 120:
                del hist[:-120]
            self._last_candle = {
                "symbol": symbol, "interval": interval, "ts": ts,
                "time": utc_iso(ts), "close": _num(candle.get("close")),
            }
            self._set_state("last_candle", json.dumps(self._last_candle))
            self._last_market_ts = max(self._last_market_ts, ts)
            current = hist[-1]

        if self._setup_b.get("enabled") and interval == self._setup_b_timeframe():
            self._evaluate_setup_b(symbol, interval, ts, current)
        if self._lifecycle_cfg.get("enabled", True):
            self._track_lifecycle(symbol, current)

    # ── Setup A (existing liquidity engine signals) ──────────────────────
    def on_strategy_signal(self, event: dict) -> None:
        """Called by the liquidity engine when a setup changes.

        Only LONG_SIGNAL_READY produces a trade plan. Everything else is a
        stage observation, forwarded only in verbose mode.
        """
        if not isinstance(event, dict):
            return
        try:
            kind = str(event.get("event") or "")
            if kind == "SETUP_CLOSED":
                self._on_setup_closed(event)
                return
            if kind != "LONG_SIGNAL_READY":
                return
            if not self._setup_a.get("enabled", True):
                return
            self._confirm_setup_a(event)
        except Exception as exc:
            self._errors += 1
            print(f"[Monitor] signal error: {type(exc).__name__}: {exc}", flush=True)

    def _confirm_setup_a(self, event: dict) -> None:
        setup_id = str(event.get("signal_id") or "")
        symbol = _pair(event.get("symbol"))
        if not setup_id or not symbol:
            return
        risk = event.get("risk") or {}
        rejection = event.get("rejection") or {}
        volume = event.get("volume") or {}
        delta = event.get("delta") or {}
        confirmation = event.get("confirmation") or {}

        if not risk.get("ok"):
            return
        if not (volume.get("passed") is True and delta.get("available") is True
                and delta.get("passed") is True and confirmation.get("passed") is True
                and rejection.get("close_above_zone") is True):
            return

        quote = self._quote(symbol)
        live_price = _num((quote or {}).get("price"))
        stop = _num(risk.get("stop_loss"))
        zone_low = _num((event.get("zone_low")))
        zone_high = _num((event.get("zone_high")))
        candle_close = _num(event.get("price"))
        if stop is None:
            return
        # Actual entry comes from the confirming candle (real close). The live
        # tick may be used only when the close is unavailable.
        entry = candle_close or live_price
        if entry is None or entry <= stop:
            return
        risk_per_unit = entry - stop
        if risk_per_unit <= 0:
            return
        targets = self._setup_a.get("targets") or {}
        t1_r = _num(targets.get("t1_r"), 1.5) or 1.5
        t2_r = _num(targets.get("t2_r"), 2.5) or 2.5
        t1 = entry + t1_r * risk_per_unit
        t2 = entry + t2_r * risk_per_unit

        plan = self._build_plan(
            symbol=symbol, direction=LONG, setup="A", timeframe=str(event.get("interval") or ""),
            entry=entry, stop=stop, t1=t1, t2=t2,
            min_rr=_num(self._setup_a.get("min_rr"), 1.5),
            trigger=self._setup_a_trigger(event, volume, delta, rejection, confirmation),
            signal_id=setup_id)
        if not plan:
            return
        self._record_signal(plan)
        self._open_lifecycle(plan)
        mode = plan["execution"]
        if self._alert_enabled("entry"):
            self._send(symbol, "TRADE_SIGNAL", setup_id,
                       self._entry_alert_text(plan, mode))

    def _setup_a_trigger(self, event, volume, delta, rejection, confirmation) -> dict:
        return {
            "ssl_zone": [event.get("zone_low"), event.get("zone_high")],
            "sweep_low": (event.get("sweep") or {}).get("price"),
            "sweep_depth_pct": (event.get("sweep") or {}).get("depth_pct"),
            "rejection_patterns": rejection.get("patterns") or [],
            "rejection_close": rejection.get("close"),
            "volume_multiplier": volume.get("multiplier"),
            "volume_threshold": volume.get("threshold"),
            "volume_avg": volume.get("avg_volume"),
            "delta": delta.get("delta"),
            "delta_imbalance": delta.get("imbalance"),
            "delta_flip": delta.get("flip"),
            "delta_source": "binance_taker_buy_volume (exchange-reported estimate)",
            "next_candle_close": confirmation.get("close"),
            "next_candle_confirmed": confirmation.get("passed"),
        }

    def _on_setup_closed(self, event: dict) -> None:
        setup_id = str(event.get("signal_id") or "")
        symbol = _pair(event.get("symbol"))
        if not setup_id:
            return
        if not self._alert_enabled("lifecycle"):
            return
        outcome = str(event.get("outcome") or "")
        reason = str(event.get("reason") or "")
        if outcome in ("invalidated", "expired"):
            self._send(symbol, "SETUP_INVALIDATED", f"{setup_id}:{outcome}:{reason}",
                       f"{symbol} — SETUP {outcome.upper()}\n\n"
                       f"Reason: {reason}\n"
                       f"Timestamp: {utc_iso(event.get('ts'))}\n\n"
                       "No entry signal was generated from this setup.")

    # ── Setup B (short continuation, candle-close only) ──────────────────
    def _setup_b_timeframe(self) -> str:
        tf = str(self._setup_b.get("timeframe") or "5m")
        return tf if tf in _INTERVAL_MS else "5m"

    def _setup_b_rule(self, symbol: str) -> Optional[dict]:
        rules = (self._setup_b.get("symbols") or {})
        rule = rules.get(symbol)
        if not isinstance(rule, dict) or not rule.get("enabled"):
            return None
        trigger = _num(rule.get("trigger"))
        stop = _num(rule.get("stop_loss"))
        t1 = _num(rule.get("target_1"))
        t2 = _num(rule.get("target_2"))
        if None in (trigger, stop, t1, t2):
            return None      # not configured — never invent levels
        if not (stop > trigger > t1 >= t2):
            return None
        return {"trigger": trigger, "stop_loss": stop,
                "target_1": t1, "target_2": t2}

    def _evaluate_setup_b(self, symbol: str, interval: str, ts: int,
                          candle: dict) -> None:
        rule = self._setup_b_rule(symbol)
        if not rule:
            return
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO monitor_setup_b_seen (symbol, interval, ts)"
            " VALUES (?,?,?)", (symbol, interval, ts))
        self._conn.commit()
        if cur.rowcount == 0:
            return           # this candle was already evaluated (restart-safe)

        close = _num(candle.get("close"))
        if close is None:
            return
        armed = self._armed(symbol)
        if close > rule["trigger"]:
            if not armed:
                self._set_armed(symbol, True)
            return
        if not armed:
            return           # was already below the trigger; do not re-fire
        if close >= rule["trigger"]:
            return

        rule_id = f"sb-{symbol}-{interval}-{ts}"
        plan = self._build_plan(
            symbol=symbol, direction=SHORT, setup="B", timeframe=interval,
            entry=close, stop=rule["stop_loss"],
            t1=rule["target_1"], t2=rule["target_2"],
            min_rr=_num(self._setup_b.get("min_rr"), 1.5),
            trigger={
                "rule": "short_continuation",
                "trigger_level": rule["trigger"],
                "interval": interval,
                "candle_close": close,
                "candle_close_confirmed": True,
                "candle_ts": ts,
                "candle_time": utc_iso(ts),
                "note": "requires a closed candle below the trigger; a live "
                        "tick below the level does not qualify",
            },
            signal_id=rule_id)
        if not plan:
            return
        self._set_armed(symbol, False)
        self._record_signal(plan)
        self._open_lifecycle(plan)
        if self._alert_enabled("entry"):
            self._send(symbol, "TRADE_SIGNAL", rule_id,
                       self._entry_alert_text(plan, plan["execution"]))

    def _armed(self, symbol: str) -> bool:
        row = self._conn.execute(
            "SELECT armed FROM monitor_symbols WHERE setup_id=?",
            (f"setupB:{symbol}",)).fetchone()
        # Starts DISARMED: a level that is already breached when monitoring
        # begins did not produce an observed breakdown, so it must not fire.
        # The symbol is armed only once a candle closes back above the trigger.
        return bool(row[0]) if row else False

    def _set_armed(self, symbol: str, armed: bool) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO monitor_symbols"
            " (setup_id, setup, symbol, direction, realm, armed, updated_ts)"
            " VALUES (?,?,?,?,?,?,?)",
            (f"setupB:{symbol}", "B", symbol, SHORT, "rearm",
             int(bool(armed)), self._now_ms()))
        self._conn.commit()

    # ── trade plan / position sizing ─────────────────────────────────────
    def _build_plan(self, *, symbol, direction, setup, timeframe, entry, stop,
                    t1, t2, min_rr, trigger, signal_id) -> Optional[dict]:
        entry = _num(entry)
        stop = _num(stop)
        t1 = _num(t1)
        t2 = _num(t2)
        if None in (entry, stop, t1, t2) or entry <= 0 or stop <= 0:
            return None
        if direction == LONG and not (stop < entry < t1 <= t2):
            return None
        if direction == SHORT and not (stop > entry > t1 >= t2):
            return None
        risk_per_unit = abs(entry - stop)
        if risk_per_unit <= 0:
            return None
        reward1 = abs(t1 - entry)
        reward2 = abs(t2 - entry)
        rr = reward1 / risk_per_unit
        rr_t2 = reward2 / risk_per_unit
        # T1/T2 for Setup A are DERIVED from configured R multiples, so an exact
        # 1.5R target can compute as 1.4999999999999982 in binary floating point.
        # Compare with a small relative tolerance rather than rejecting a valid
        # plan on representation error.
        if min_rr is not None and rr < float(min_rr) * (1.0 - 1e-9):
            return None      # reject below the user's minimum R:R

        sizing = self._size(symbol, entry, stop, risk_per_unit)
        if sizing is None:
            return None

        plan = {
            "signal_id": signal_id,
            "setup": setup,
            "direction": direction,
            "symbol": symbol,
            "timeframe": timeframe,
            "state": "ENTRY_CONFIRMED",
            "entry": _round(entry),
            "stop_loss": _round(stop),
            "target_1": _round(t1),
            "target_2": _round(t2),
            "risk_per_unit": _round(risk_per_unit),
            "reward_per_unit": _round(reward1),
            "rr": _round(rr, 4),
            "rr_t2": _round(rr_t2, 4),
            "trigger": trigger,
            "created_ts": self._now_ms(),
        }
        plan.update(sizing)
        return plan

    def _size(self, symbol, entry, stop, risk_per_unit) -> Optional[dict]:
        paper = self._paper_config()
        equity = self._equity(paper)
        if equity is None or equity <= 0:
            return None
        risk_pct = _num(paper.get("default_risk_percent"), 1.0) or 1.0
        leverage = max(1.0, min(20.0, _num(paper.get("leverage"), 1.0) or 1.0))
        fee_pct = max(0.0, _num(paper.get("simulated_fee_percent"), 0.1) or 0.1)
        slip_pct = max(0.0, _num(paper.get("slippage_percent"), 0.05) or 0.0)
        rules = (paper.get("symbol_rules") or {}).get(symbol) or {}
        precision = int(_num(rules.get("quantity_precision"), 6) or 6)
        min_qty = _num(rules.get("min_quantity"), 0.0) or 0.0
        min_notional = _num(paper.get("min_notional"), 0.0) or 0.0

        risk_amount = equity * risk_pct / 100.0
        qty = risk_amount / risk_per_unit
        # Caps are notional-based: leverage never enlarges the risk budget.
        pos_cap = (equity * (_num(paper.get("maximum_position_percent"), 25.0) or 25.0)
                   / 100.0 / entry)
        qty = min(qty, pos_cap)
        qty = _floor_down(qty, precision)
        if qty <= 0:
            return None
        notional = qty * entry
        if min_qty and qty < min_qty:
            return None
        if min_notional and notional < min_notional:
            return None
        margin = notional / leverage
        fee = notional * fee_pct / 100.0
        slip = notional * slip_pct / 100.0
        actual_risk = qty * risk_per_unit
        return {
            "quantity": _round(qty, max(precision, 8)),
            "risk_amount": _round(actual_risk, 4),
            "capital_required": _round(notional + fee + slip, 4),
            "notional": _round(notional, 4),
            "leverage": _round(leverage, 4),
            "margin": _round(margin, 4),
            "fee_estimate": _round(fee, 4),
            "slippage_estimate": _round(slip, 4),
            "risk_pct_of_capital": _round(actual_risk / equity * 100.0, 4),
            "equity": _round(equity, 4),
            "execution": self._execution_mode(symbol),
        }

    def _equity(self, paper: dict) -> Optional[float]:
        try:
            from core import paper_trading as pt
            engine = pt.get_engine()
            status = engine.status()
            equity = _num((status.get("account") or {}).get("equity"))
            if equity is not None and equity > 0:
                return equity
        except Exception:
            pass
        return _num(paper.get("initial_balance"), 10000.0) or 10000.0

    def _execution_mode(self, symbol: str) -> str:
        paper = self._paper_config()
        mode = _trade_mode(paper)
        if mode == "PAPER":
            allowed = [str(s).upper() for s in (paper.get("allowed_symbols") or [])]
            if symbol not in allowed:
                return "ALERT-ONLY"
        return mode

    def _record_signal(self, plan: dict) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO monitor_signals"
            " (signal_id, setup, direction, symbol, timeframe, state, entry,"
            "  stop_loss, target_1, target_2, risk_per_unit, reward_per_unit,"
            "  rr, rr_t2, quantity, risk_amount, capital_required, notional,"
            "  leverage, margin, fee_estimate, slippage_estimate, execution,"
            "  trigger_json, payload_json, created_ts, updated_at)"
            " VALUES (" + ",".join(["?"] * 26) + ",datetime('now'))",
            (plan["signal_id"], plan["setup"], plan["direction"], plan["symbol"],
             plan["timeframe"], plan["state"], plan["entry"], plan["stop_loss"],
             plan["target_1"], plan["target_2"], plan["risk_per_unit"],
             plan["reward_per_unit"], plan["rr"], plan["rr_t2"],
             plan.get("quantity"), plan.get("risk_amount"),
             plan.get("capital_required"), plan.get("notional"),
             plan.get("leverage"), plan.get("margin"),
             plan.get("fee_estimate"), plan.get("slippage_estimate"),
             plan.get("execution"), json.dumps(plan.get("trigger") or {}),
             json.dumps(plan, default=str), plan["created_ts"]))
        self._conn.commit()

    # ── lifecycle tracking ───────────────────────────────────────────────
    def _open_lifecycle(self, plan: dict) -> None:
        if not self._lifecycle_cfg.get("enabled", True):
            return
        self._conn.execute(
            "INSERT OR REPLACE INTO monitor_lifecycle"
            " (signal_id, setup, symbol, direction, entry, stop_loss,"
            "  target_1, target_2, quantity, status, last_price, last_ts,"
            "  hit_t1, hit_t2, hit_sl, realized_pnl, created_ts, updated_ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,0,0,NULL,?,?)",
            (plan["signal_id"], plan["setup"], plan["symbol"], plan["direction"],
             plan["entry"], plan["stop_loss"], plan["target_1"],
             plan["target_2"], plan.get("quantity"), SIGNAL_OPEN,
             plan["entry"], plan["created_ts"], plan["created_ts"],
             plan["created_ts"]))
        self._conn.commit()

    def _track_lifecycle(self, symbol: str, candle: dict) -> None:
        high = _num(candle.get("high"))
        low = _num(candle.get("low"))
        close = _num(candle.get("close"))
        if None in (high, low, close):
            return
        rows = self._conn.execute(
            "SELECT l.signal_id, l.setup, l.direction, l.entry, l.stop_loss,"
            " l.target_1, l.target_2, l.quantity, l.hit_t1, l.hit_t2, l.hit_sl,"
            " s.execution"
            " FROM monitor_lifecycle l"
            " JOIN monitor_signals s ON s.signal_id = l.signal_id"
            " WHERE l.symbol=? AND l.status IN (?,?)",
            (symbol, SIGNAL_OPEN, SIGNAL_T1)).fetchall()
        for (sid, setup, direction, entry, stop, t1, t2, qty, hit_t1, hit_t2,
             hit_sl, execution) in rows:
            entry = _num(entry)
            stop = _num(stop)
            t1 = _num(t1)
            t2 = _num(t2)
            qty = _num(qty, 0.0) or 0.0
            if None in (entry, stop, t1, t2):
                continue
            status = SIGNAL_OPEN if not hit_t1 else SIGNAL_T1
            pnl = None
            # The paper engine announces its own exits for the trades it owns;
            # the monitor must not repeat that message (targets still reported).
            notify = not self._lifecycle_deferred(setup, direction, execution)
            if direction == LONG:
                if low <= stop:
                    status = SIGNAL_SL
                    pnl = (stop - entry) * qty
                    self._lifecycle_hit(symbol, sid, "STOP_LOSS", SIGNAL_SL,
                                        stop, candle, pnl, notify=notify)
                    continue
                if not hit_t2 and high >= t2:
                    status = SIGNAL_T2
                    pnl = (t2 - entry) * qty
                    self._lifecycle_hit(symbol, sid, "TARGET_2", SIGNAL_T2,
                                        t2, candle, pnl)
                    continue
                if not hit_t1 and high >= t1:
                    status = SIGNAL_T1
                    self._lifecycle_hit(symbol, sid, "TARGET_1", SIGNAL_T1,
                                        t1, candle, None)
            else:
                if high >= stop:
                    status = SIGNAL_SL
                    pnl = (entry - stop) * qty
                    self._lifecycle_hit(symbol, sid, "STOP_LOSS", SIGNAL_SL,
                                        stop, candle, pnl, notify=notify)
                    continue
                if not hit_t2 and low <= t2:
                    status = SIGNAL_T2
                    pnl = (entry - t2) * qty
                    self._lifecycle_hit(symbol, sid, "TARGET_2", SIGNAL_T2,
                                        t2, candle, pnl)
                    continue
                if not hit_t1 and low <= t1:
                    status = SIGNAL_T1
                    self._lifecycle_hit(symbol, sid, "TARGET_1", SIGNAL_T1,
                                        t1, candle, None)
            self._conn.execute(
                "UPDATE monitor_lifecycle SET last_price=?, last_ts=?,"
                " status=?, updated_ts=? WHERE signal_id=?",
                (close, int(_num(candle.get("ts"), 0) or 0), status,
                 self._now_ms(), sid))
        self._conn.commit()

    def _lifecycle_deferred(self, setup, direction, execution) -> bool:
        """True when the paper engine owns this trade's exit notification.

        The paper engine keeps its own position ledger and sends its own
        "TRADE CLOSED" message. Sending a second stop-loss message for the same
        trade would be a duplicate, so the monitor suppresses only that one
        message. Targets are still reported because the paper engine uses a
        single take-profit and does not announce T1 at all.
        """
        if not self._lifecycle_cfg.get("defer_to_paper", True):
            return False
        return (str(execution or "") == "PAPER"
                and str(setup) == "A" and str(direction) == LONG)

    def _lifecycle_hit(self, symbol, signal_id, level, status, price, candle,
                       pnl, *, notify: bool = True) -> None:
        columns = {"TARGET_1": ("hit_t1",), "TARGET_2": ("hit_t1", "hit_t2"),
                   "STOP_LOSS": ("hit_sl",)}[level]
        marks = ", ".join(f"{c}=1" for c in columns)
        self._conn.execute(
            f"UPDATE monitor_lifecycle SET {marks}, status=?, last_price=?,"
            " last_ts=?, realized_pnl=COALESCE(?, realized_pnl), updated_ts=?"
            " WHERE signal_id=?",
            (status, _num(price), int(_num(candle.get("ts"), 0) or 0),
             _round(pnl, 4) if pnl is not None else None, self._now_ms(),
             signal_id))
        self._conn.commit()
        if not notify or not self._alert_enabled("lifecycle"):
            return
        label = {"TARGET_1": "TARGET 1 REACHED", "TARGET_2": "TARGET 2 REACHED",
                 "STOP_LOSS": "STOP-LOSS REACHED"}[level]
        icon = "🎯" if level != "STOP_LOSS" else "🛑"
        text = (f"{icon} RIO TRADING SIGNAL — {label}\n\n"
                f"Symbol: {symbol}\n"
                f"Signal: {signal_id}\n"
                f"Price: {_num(price):,.6f}\n"
                f"Timeframe: {candle.get('interval') or '—'}\n"
                f"Timestamp: {utc_iso(_num(candle.get('ts'), 0))}\n")
        if pnl is not None:
            text += f"Realized P&L (simulated): {pnl:+,.4f}\n"
        text += "\nNo real order was placed."
        self._send(symbol, level, f"{signal_id}:{level}", text)

    # ── alert text ───────────────────────────────────────────────────────
    def _entry_alert_text(self, plan: dict, mode: str) -> str:
        trig = plan.get("trigger") or {}
        side = "BUY" if plan["direction"] == LONG else "SELL"
        setup_name = ("SSL Sweep Reversal" if plan["setup"] == "A"
                      else "Short Continuation")
        lines = [
            f"🚨 RIO TRADING SIGNAL — {side}",
            "",
            f"Symbol: {plan['symbol']}",
            f"Setup: {setup_name}",
            f"Timeframe: {plan['timeframe'] or '—'}",
            "",
            f"Entry: {plan['entry']:,.6f}",
            f"Stop Loss: {plan['stop_loss']:,.6f}",
            f"Target 1: {plan['target_1']:,.6f}",
            f"Target 2: {plan['target_2']:,.6f}",
            "",
            f"Risk/Reward: 1:{plan['rr']:.2f} (T1) · 1:{plan['rr_t2']:.2f} (T2)",
        ]
        if plan["setup"] == "A":
            patterns = ", ".join(trig.get("rejection_patterns") or []) or "—"
            vol = trig.get("volume_multiplier")
            delta = trig.get("delta")
            lines += [
                f"Volume: {vol:.2f}x average" if vol is not None else "Volume: —",
                f"Delta: {'positive' if (delta or 0) > 0 else '—'} "
                f"({delta:+,.2f} taker estimate)" if delta is not None else "Delta: —",
                f"Rejection: {patterns}",
                f"Next Candle: {'Confirmed' if trig.get('next_candle_confirmed') else '—'}",
            ]
        else:
            lines += [
                f"Trigger: {trig.get('interval')} close "
                f"{trig.get('candle_close', 0):,.6f} below "
                f"{trig.get('trigger_level', 0):,.6f}",
                f"Candle Close: Confirmed",
            ]
        lines += [
            "",
            f"Risk Amount: {plan.get('risk_amount', 0):,.4f}",
            f"Quantity: {plan.get('quantity', 0):,.8f}",
            f"Notional: {plan.get('notional', 0):,.4f}",
            f"Leverage: {plan.get('leverage', 1):.2f}x",
            f"Estimated Margin: {plan.get('margin', 0):,.4f}",
            f"Fees + Slippage: {(plan.get('fee_estimate') or 0) + (plan.get('slippage_estimate') or 0):,.4f}",
            "",
            "Signal Status: CONFIRMED",
            f"Trading Mode: {mode}",
            "No real order was placed." if mode != "PAPER"
            else "Paper engine may simulate this entry.",
        ]
        return "\n".join(lines)

    def _send(self, symbol: str, alert_type: str, dedupe_key: str,
              text: str) -> bool:
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO monitor_alert_log"
            " (symbol, alert_type, dedupe_key, message, delivered, ts)"
            " VALUES (?,?,?,?,0,?)",
            (symbol, alert_type, dedupe_key, text, self._now_ms()))
        self._conn.commit()
        if cur.rowcount == 0:
            return False
        delivered = False
        try:
            delivered = bool(self._alert_sender(text))
        except Exception as exc:
            self._errors += 1
            print(f"[Monitor] alert send failed: {type(exc).__name__}: {exc}",
                  flush=True)
        self._conn.execute(
            "UPDATE monitor_alert_log SET delivered=? WHERE symbol=?"
            " AND alert_type=? AND dedupe_key=?",
            (int(delivered), symbol, alert_type, dedupe_key))
        self._conn.commit()
        return delivered

    # ── symbol state / health ────────────────────────────────────────────
    def _today(self) -> str:
        return datetime.fromtimestamp(self._clock(), tz=UTC).strftime("%Y-%m-%d")

    def _trend(self, symbol: str, interval: str) -> str:
        hist = self._candles.get((symbol, interval)) or []
        closes = [c["close"] for c in hist[-20:] if c.get("close") is not None]
        if len(closes) < 5:
            return "UNKNOWN"
        ema = closes[0]
        alpha = 2.0 / (len(closes) + 1)
        for value in closes[1:]:
            ema = alpha * value + (1 - alpha) * ema
        diff_pct = (closes[-1] - ema) / ema * 100 if ema else 0.0
        if diff_pct > 0.05:
            return "UP"
        if diff_pct < -0.05:
            return "DOWN"
        return "FLAT"

    def _symbol_snapshot(self, symbol: str) -> dict:
        engine_snap = {}
        if self._engine is not None:
            try:
                engine_snap = self._engine.get_status(symbol) or {}
            except Exception:
                engine_snap = {}
        feed = self._feed_state_cached
        quote = self._quote(symbol) or {}
        # Prefer the engine's signal timeframe, but a symbol with no history on
        # that interval (or no engine setup at all) must fall back to one that
        # has candles — otherwise the snapshot reports empty volume/delta.
        interval = str(engine_snap.get("timeframe") or "")
        candidates = [interval, self._setup_b_timeframe(), "3m", "1m", "5m"]
        hist = []
        for candidate in candidates:
            if not candidate:
                continue
            hist = self._candles.get((symbol, candidate)) or []
            if hist:
                interval = candidate
                break
        if not interval:
            interval = self._setup_b_timeframe()
        candle_close = _num((hist[-1] or {}).get("close")) if hist else None
        price = _num(quote.get("price")) or candle_close
        bsl = None
        if len(hist) > 2:
            bsl = max(c["high"] for c in hist[:-1] if c.get("high") is not None) \
                if any(c.get("high") is not None for c in hist[:-1]) else None
        volume = _num((hist[-1] or {}).get("volume")) if hist else None
        avg_volume = None
        if len(hist) > 6:
            prior = [c["volume"] for c in hist[-6:-1] if c.get("volume") is not None]
            avg_volume = mean(prior) if prior else None
        rel_volume = (volume / avg_volume) if volume and avg_volume else None

        # Delta: prefer the engine's gate value (it has the flip/threshold
        # logic). When the engine has no active setup, derive the SAME
        # exchange-reported taker estimate from the monitor's own closed
        # candle, so a live reading is shown instead of a bare "none".
        delta = None
        delta_available = None
        engine_delta = engine_snap.get("delta") or {}
        if engine_delta:
            delta = engine_delta.get("delta")
            delta_available = engine_delta.get("available")
        elif hist:
            last = hist[-1]
            taker = last.get("taker_buy_volume")
            vol = last.get("volume")
            if taker is not None and vol is not None and vol > 0:
                delta = round(max(0.0, taker) - max(0.0, vol - taker), 8)
                delta_available = True
            else:
                delta_available = False
        connected = bool(feed.get("live")) and not feed.get("stale")
        if not self._started:
            # The worker itself is not running, so nothing is being evaluated
            # regardless of what the feed says. Never report MONITORING here.
            monitoring = "STOPPED"
        elif not connected:
            monitoring = "DISCONNECTED"
        else:
            monitoring = "MONITORING"
        return {
            "price": _round(price),
            "change_24h": _num(quote.get("change_24h")),
            "trend": self._trend(symbol, interval),
            "ssl_zone": (engine_snap.get("zone") or {}),
            "bsl_level": _round(bsl),
            "sweep_zone": ((engine_snap.get("zone") or {}).get("low")),
            "volume": _round(volume),
            "avg_volume": _round(avg_volume),
            "rel_volume": _round(rel_volume, 4),
            "delta": delta,
            "delta_available": delta_available,
            "candle_timeframe": interval,
            "liquidity_sweep_state": engine_snap.get("state") or "IDLE",
            "rejection_state": ("DETECTED" if engine_snap.get("rejection") else "NONE"),
            "entry_confirmation_state": (
                "CONFIRMED" if (engine_snap.get("confirmation") or {}).get("passed")
                else "PENDING" if engine_snap.get("confirmation") else "NONE"),
            "monitoring_status": monitoring,
            "setup_a_configured": bool((engine_snap.get("zone") or {}).get("low")),
            "setup_b_configured": bool(self._setup_b_rule(symbol)),
        }

    def health(self) -> dict:
        with self._lock:
            feed = self._feed_state()
            self._feed_state_cached = feed
        connected = bool(feed.get("live")) and not feed.get("stale")
        if not self._started:
            monitor_state = "STOPPED"
        elif connected:
            monitor_state = "RUNNING"
        elif feed.get("live") and feed.get("stale"):
            monitor_state = "STALE"
        else:
            monitor_state = "DISCONNECTED"
        symbols = {}
        for sym in self._symbols:
            snap = self._symbol_snapshot(sym)
            symbols[sym] = snap["monitoring_status"]
        today = self._today()
        day_start_ms = int(datetime.strptime(today, "%Y-%m-%d")
                           .replace(tzinfo=UTC).timestamp() * 1000)
        confirmed_today = self._conn.execute(
            "SELECT COUNT(*) FROM monitor_signals WHERE created_ts>=?",
            (day_start_ms,)).fetchone()[0]
        alerts_today = self._conn.execute(
            "SELECT COUNT(*) FROM monitor_alert_log WHERE delivered=1"
            " AND ts>=?", (day_start_ms,)).fetchone()[0]
        active_signals = self._conn.execute(
            "SELECT COUNT(*) FROM monitor_lifecycle WHERE status IN (?,?)",
            (SIGNAL_OPEN, SIGNAL_T1)).fetchone()[0]
        last_candle_ms = int(_num((self._last_candle or {}).get("ts"), 0) or 0)
        return {
            "trading_monitor": monitor_state,
            "market_data": ("CONNECTED" if connected
                            else "STALE" if feed.get("live") else "DISCONNECTED"),
            "transport": feed.get("transport") or "none",
            "symbols": symbols,
            "active_setups": active_signals,
            "confirmed_signals_today": int(confirmed_today),
            "alerts_sent_today": int(alerts_today),
            "last_market_update": utc_iso(self._last_market_ts) or None,
            "last_candle_processed": utc_iso(last_candle_ms) or None,
            "monitor_errors": self._errors,
            "verbosity": self._verbosity,
            "setup_a": bool(self._setup_a.get("enabled", True)),
            "setup_b": bool(self._setup_b.get("enabled", True)),
            "execution_mode": _trade_mode(self._paper_config()),
            "symbols_configured": list(self._symbols),
        }

    def state(self) -> dict:
        with self._lock:
            feed = self._feed_state()
            self._feed_state_cached = feed
            symbols = {sym: self._symbol_snapshot(sym) for sym in self._symbols}
        return {"health": self.health(), "market_data": feed,
                "symbols": symbols, "updated_at": utc_iso(self._now_ms())}

    def signals(self, limit: int = 50) -> list:
        limit = max(1, min(int(limit), 200))
        rows = self._conn.execute(
            "SELECT signal_id, setup, direction, symbol, timeframe, entry,"
            " stop_loss, target_1, target_2, rr, rr_t2, quantity, risk_amount,"
            " notional, leverage, margin, execution, created_at"
            " FROM monitor_signals ORDER BY created_ts DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            out.append({
                "signal_id": r[0], "setup": r[1], "direction": r[2],
                "symbol": r[3], "timeframe": r[4], "entry": r[5],
                "stop_loss": r[6], "target_1": r[7], "target_2": r[8],
                "rr": r[9], "rr_t2": r[10], "quantity": r[11],
                "risk_amount": r[12], "notional": r[13], "leverage": r[14],
                "margin": r[15], "execution": r[16], "created_at": r[17],
            })
        return out

    def lifecycle(self, limit: int = 50) -> list:
        limit = max(1, min(int(limit), 200))
        rows = self._conn.execute(
            "SELECT signal_id, setup, symbol, direction, entry, stop_loss,"
            " target_1, target_2, quantity, status, last_price, hit_t1, hit_t2,"
            " hit_sl, realized_pnl, updated_ts FROM monitor_lifecycle"
            " ORDER BY updated_ts DESC LIMIT ?", (limit,)).fetchall()
        return [{
            "signal_id": r[0], "setup": r[1], "symbol": r[2], "direction": r[3],
            "entry": r[4], "stop_loss": r[5], "target_1": r[6],
            "target_2": r[7], "quantity": r[8], "status": r[9],
            "last_price": r[10], "hit_t1": bool(r[11]), "hit_t2": bool(r[12]),
            "hit_sl": bool(r[13]), "realized_pnl": r[14],
            "updated_at": utc_iso(r[15]),
        } for r in rows]


# ── singleton ────────────────────────────────────────────────────────────────
_monitor: Optional[TradingMonitor] = None
_monitor_lock = threading.Lock()


def get_monitor() -> TradingMonitor:
    global _monitor
    with _monitor_lock:
        if _monitor is None:
            _monitor = TradingMonitor()
        return _monitor


def reset_monitor() -> None:
    global _monitor
    with _monitor_lock:
        if _monitor is not None:
            try:
                _monitor.close()
            except Exception:
                pass
        _monitor = None


__all__ = [
    "TradingMonitor", "get_monitor", "reset_monitor", "DEFAULT_CONFIG",
    "DEFAULT_CONFIG_PATH", "SIGNAL_OPEN", "SIGNAL_T1", "SIGNAL_T2", "SIGNAL_SL",
]
