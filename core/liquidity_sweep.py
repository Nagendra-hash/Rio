"""
core/liquidity_sweep.py — Liquidity Sweep + Reversal Confirmation Engine.

Consumes the EXISTING Binance daemon (core/binance_ws.py) through its
closed-candle subscription hook. It does not run a second daemon or stream;
one bounded REST warmup for recent history goes through the daemon's
fetch_recent_closed() helper. It never synthesises prices/candles/volume/
delta and has NO order path.

State machine (never skipped):

    SWEEPING_LIQUIDITY
        -> REJECTION_DETECTED
        -> VOLUME_CONFIRMED
        -> DELTA_CONFIRMED
        -> CONFIRMATION_PENDING
        -> CONFIRMED_REVERSAL
        -> LONG_SIGNAL_READY

A sweep alone, a rejection alone, or a percentage move is never a buy signal.
LONG_SIGNAL_READY is informational only and requires every gate below.

Data notes:
  * Price/candles/volume come from Binance.
  * "Delta" is estimated from Binance's exchange-reported taker-buy base
    volume (kline field V / REST kline index 9):
        buy  = taker_buy_volume
        sell = volume - taker_buy_volume
        delta = buy - sell
    This is a taker-side ESTIMATE, not complete exchange-wide order flow and
    not institutional order flow. If it is missing, delta is UNAVAILABLE.
  * All stored/displayed timestamps are UTC. Internal candle timestamps are
    UTC epoch milliseconds.
  * Every alert is deduplicated in SQLite before Hermes is called.
"""
from __future__ import annotations

import copy
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Callable, Optional

from core import binance_ws as bw

UTC = timezone.utc
INTERVAL_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000}

# Exact state names required by the brief.
SWEEPING = "SWEEPING_LIQUIDITY"
REJECTION = "REJECTION_DETECTED"
VOLUME = "VOLUME_CONFIRMED"
DELTA = "DELTA_CONFIRMED"
CONFIRMATION_PENDING = "CONFIRMATION_PENDING"
CONFIRMED = "CONFIRMED_REVERSAL"
LONG = "LONG_SIGNAL_READY"

STATES = (SWEEPING, REJECTION, VOLUME, DELTA, CONFIRMATION_PENDING,
          CONFIRMED, LONG)

_ALLOWED = {
    "NONE": {SWEEPING},
    SWEEPING: {REJECTION},
    REJECTION: {VOLUME},
    VOLUME: {DELTA},
    DELTA: {CONFIRMATION_PENDING},
    CONFIRMATION_PENDING: {CONFIRMED},
    CONFIRMED: {LONG},
    LONG: set(),
}

DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "liquidity_sweep.json"
)

_PROFILE_DEFAULTS = {
    "enabled": True,
    "zone_low": None,
    "zone_high": None,
    "watch_levels": [],
    "breakout_threshold": None,
    "invalidation_level": None,
    "target_price": None,
    "volume_multiplier": None,
    "signal_interval": None,
    "alert_pct_1m": None,
    "alerts_enabled": True,
}

DEFAULT_CONFIG = {
    "version": 1,
    "globals": {
        "intervals": ["1m", "3m", "5m"],
        "signal_interval": "3m",
        "swing_lookback": 20,
        "volume_lookback": 20,
        "volume_min_history": 5,
        "volume_multiplier": 2.0,
        "delta_required": True,
        "delta_lookback": 3,
        "require_delta_flip": True,
        "min_delta_imbalance": 0.0,
        "rejection": {
            "min_body_ratio": 0.12,
            "max_body_ratio": 0.62,
            "min_lower_wick_ratio": 0.30,
            "wick_body_multiple": 1.5,
            "close_upper_ratio": 0.60,
        },
        "confirmation_candles": 1,
        "require_higher_high": False,
        "require_higher_close": True,
        "signal_valid_candles": 4,
        "expiry_candles": 12,
        "stale_after_s": 180,
        "min_sweep_depth_pct": 0.0,
        "max_sweep_depth_pct": 3.0,
        "risk": {
            "stop_buffer_pct": 0.20,
            "max_risk_pct": 2.0,
            "min_rr": 1.5,
            "default_rr": 2.0,
            "account_size": None,
            "risk_per_trade_pct": 1.0,
        },
        "alerts": {
            "enabled": True,
            "states": [SWEEPING, CONFIRMED, LONG],
            "pct_1m": 0.8,
            "breakout_interval": "3m",
            "breakout_volume_multiplier": 2.0,
            "invalidation": False,
        },
        "auto_zone": False,
        "zone_width": 50.0,
    },
    "profiles": {
        "BTCUSDT": {
            **_PROFILE_DEFAULTS,
            "zone_low": 65000.0, "zone_high": 65050.0,
            "watch_levels": [64800.0, 64500.0, 64000.0],
            "breakout_threshold": 65200.0,
        },
        "ETHUSDT": {
            **_PROFILE_DEFAULTS,
            "zone_low": 1800.0, "zone_high": 1805.0,
            "watch_levels": [1780.0, 1750.0],
            "breakout_threshold": 1820.0,
        },
    },
}

_ASSET_NAMES = {
    "BTCUSDT": "Bitcoin", "ETHUSDT": "Ethereum", "SOLUSDT": "Solana",
    "PAXGUSDT": "PAX Gold", "XAUTUSDT": "Tether Gold",
}


# ── small helpers ────────────────────────────────────────────────────────────
def utc_iso(ms: Optional[int]) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(int(ms) / 1000, tz=UTC).isoformat()


def _pair(symbol) -> str:
    """BTC / btc / BTCUSDT -> BTCUSDT (uppercase canonical pair)."""
    return bw.normalize_symbol(str(symbol or "")).upper()


def _asset(symbol: str) -> str:
    return _ASSET_NAMES.get(symbol, symbol)


def _deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _num(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _round(value, digits=6):
    return round(float(value), digits) if value is not None else None


def _load_config(path: Path) -> dict:
    loaded = {}
    try:
        if path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        loaded = {}
    base = copy.deepcopy(DEFAULT_CONFIG)
    config = {
        "version": loaded.get("version", base["version"]),
        "globals": _deep_merge(base["globals"], loaded.get("globals") or {}),
        "profiles": {},
    }
    source = loaded.get("profiles") or base["profiles"]
    for symbol, profile in source.items():
        sym = _pair(symbol)
        config["profiles"][sym] = _deep_merge(
            _PROFILE_DEFAULTS, profile or {})
    if not config["profiles"]:
        config["profiles"] = copy.deepcopy(base["profiles"])
    return config


def _save_config(path: Path, config: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _normalize_candle(raw: dict) -> Optional[dict]:
    if not raw:
        return None
    interval = str(raw.get("interval") or "")
    if interval not in INTERVAL_MS:
        return None
    ts = int(raw.get("ts") or 0)
    if ts <= 0:
        return None
    symbol = _pair(raw.get("symbol"))
    if not symbol:
        return None
    taker = raw.get("taker_buy_volume")
    try:
        taker = float(taker) if taker is not None else None
    except (TypeError, ValueError):
        taker = None
    close_ts = raw.get("close_ts")
    if close_ts is None:
        close_ts = ts + INTERVAL_MS[interval]
    return {
        "symbol": symbol,
        "interval": interval,
        "ts": ts,
        "close_ts": int(close_ts),
        "open": float(raw["open"]),
        "high": float(raw["high"]),
        "low": float(raw["low"]),
        "close": float(raw["close"]),
        "volume": float(raw.get("volume") or 0.0),
        "taker_buy_volume": taker,
        "closed": bool(raw.get("closed", True)),
    }


@dataclass
class Setup:
    setup_id: str
    symbol: str
    interval: str
    state: str
    created_ts: int
    updated_ts: int
    zone_low: float
    zone_high: float
    watch_levels: list
    invalidation_level: Optional[float]
    sweep: dict
    rejection: Optional[dict] = None
    volume: Optional[dict] = None
    delta: Optional[dict] = None
    confirmation: Optional[dict] = None
    risk: Optional[dict] = None
    candle_count: int = 0
    confirmation_attempts: int = 0
    confirmation_start_ts: Optional[int] = None
    signal_ts: Optional[int] = None
    signal_candle_count: int = 0
    signal_blocked: str = ""
    warnings: list = field(default_factory=list)

    def payload(self) -> dict:
        return asdict(self)


# ── engine ───────────────────────────────────────────────────────────────────
class LiquiditySweepEngine:
    """One engine per process; one active setup per symbol at a time."""

    def __init__(
        self,
        config_path: Optional[str | Path] = None,
        db_path: Optional[str | Path] = None,
        *,
        alert_sender: Optional[Callable[[str], bool]] = None,
        live_provider: Optional[Callable[[], dict]] = None,
        data_provider: Optional[Callable] = None,
        price_provider: Optional[Callable[[str], Optional[float]]] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.config_path = Path(config_path or DEFAULT_CONFIG_PATH)
        self.db_path = Path(db_path or bw.database_path())
        self._alert_sender = alert_sender or bw.send_telegram
        self._live_provider = live_provider or bw.status
        self._data_provider = data_provider or bw.fetch_recent_closed
        self._price_provider = price_provider or self._default_price
        self._clock = clock or time.time

        self._lock = threading.RLock()
        self._config = _load_config(self.config_path)
        if not self.config_path.exists():
            _save_config(self.config_path, self._config)
        self._apply_config()

        self._candles: dict[tuple, list] = {}
        self._seen: dict[tuple, int] = {}
        self._setups: dict[str, Setup] = {}
        self._observations: dict[str, dict] = {}
        self._started = False
        self._stop_event = threading.Event()
        self._warm_thread: Optional[threading.Thread] = None
        # Local consumers (paper trading) subscribe to strategy events. This
        # is a callback on the one engine — not a second strategy engine.
        self._signal_listeners: list = []

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        # The daemon writes ticks on another connection; wait instead of
        # failing with "database is locked" during a burst.
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_schema()
        self._load_history_from_db()
        self._load_active_setups()

    # ── config ───────────────────────────────────────────────────────────
    def _apply_config(self) -> None:
        g = self._config["globals"]
        self._intervals = [i for i in g.get("intervals", ["1m", "3m", "5m"])
                           if i in INTERVAL_MS]
        if not self._intervals:
            self._intervals = ["3m"]
        self._signal_interval = str(g.get("signal_interval") or "3m")
        self._swing_lookback = int(g.get("swing_lookback") or 20)
        self._volume_lookback = int(g.get("volume_lookback") or 20)
        self._volume_min_history = int(g.get("volume_min_history") or 5)
        self._volume_multiplier = _num(g.get("volume_multiplier"), 2.0)
        self._delta_required = bool(g.get("delta_required", True))
        self._delta_lookback = int(g.get("delta_lookback") or 3)
        self._require_delta_flip = bool(g.get("require_delta_flip", True))
        self._min_delta_imbalance = _num(g.get("min_delta_imbalance"), 0.0)
        self._rejection_cfg = dict(g.get("rejection") or {})
        self._confirmation_candles = int(g.get("confirmation_candles") or 1)
        self._require_higher_high = bool(g.get("require_higher_high", False))
        self._require_higher_close = bool(g.get("require_higher_close", True))
        self._signal_valid_candles = int(g.get("signal_valid_candles") or 4)
        self._expiry_candles = int(g.get("expiry_candles") or 12)
        self._stale_after_s = float(g.get("stale_after_s") or 180)
        self._min_depth = _num(g.get("min_sweep_depth_pct"), 0.0)
        self._max_depth = _num(g.get("max_sweep_depth_pct"), 3.0)
        self._risk_cfg = dict(g.get("risk") or {})
        self._alerts_cfg = dict(g.get("alerts") or {})
        self._profiles = {
            _pair(sym): _deep_merge(_PROFILE_DEFAULTS, cfg or {})
            for sym, cfg in (self._config.get("profiles") or {}).items()
        }

    def _profile(self, symbol: str) -> Optional[dict]:
        return self._profiles.get(_pair(symbol))

    def _effective_volume_multiplier(self, profile: dict) -> float:
        # Brief: configurable between 2x and 3x.
        value = _num(profile.get("volume_multiplier"), self._volume_multiplier)
        return max(2.0, min(3.0, float(value if value is not None else 2.0)))

    def _pct_1m(self, profile: dict) -> float:
        value = _num(profile.get("alert_pct_1m"),
                     _num(self._alerts_cfg.get("pct_1m"), 0.8))
        return float(value if value is not None else 0.8)

    def _breakout_interval(self, profile: dict) -> str:
        return str(profile.get("signal_interval")
                   or self._alerts_cfg.get("breakout_interval")
                   or self._signal_interval)

    def _alerts_enabled(self, profile: dict) -> bool:
        return bool(self._alerts_cfg.get("enabled", True)) and bool(
            profile.get("alerts_enabled", True))

    # ── DB ───────────────────────────────────────────────────────────────
    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        cur.execute("PRAGMA table_info(candles)")
        cols = {row[1] for row in cur.fetchall()}
        if cols and "taker_buy_volume" not in cols:
            cur.execute("ALTER TABLE candles ADD COLUMN taker_buy_volume REAL")
        # The daemon owns this table; creating it IF NOT EXISTS keeps the engine
        # usable standalone and lets restart recovery read its own history.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS candles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT, interval TEXT,
                open REAL, high REAL, low REAL, close REAL, volume REAL,
                taker_buy_volume REAL,
                ts INTEGER, closed INTEGER,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_candles "
                    "ON candles(symbol, interval, ts)")
        cur.executescript("""
        CREATE TABLE IF NOT EXISTS liquidity_sweeps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            setup_id TEXT, symbol TEXT, interval TEXT,
            sweep_ts INTEGER, sweep_price REAL, sweep_close REAL,
            prev_swing_low REAL, depth_pct REAL, depth_abs REAL,
            below_zone INTEGER, recovery_price REAL,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(symbol, interval, sweep_ts)
        );
        CREATE TABLE IF NOT EXISTS rejection_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            setup_id TEXT, symbol TEXT, interval TEXT, candle_ts INTEGER,
            open REAL, high REAL, low REAL, close REAL,
            patterns TEXT, body_ratio REAL, lower_wick_ratio REAL,
            close_above_zone INTEGER, created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(setup_id, candle_ts)
        );
        CREATE TABLE IF NOT EXISTS volume_confirmations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            setup_id TEXT, symbol TEXT, interval TEXT, candle_ts INTEGER,
            volume REAL, avg_volume REAL, multiplier REAL, lookback INTEGER,
            available INTEGER, passed INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(setup_id, candle_ts)
        );
        CREATE TABLE IF NOT EXISTS delta_confirmations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            setup_id TEXT, symbol TEXT, interval TEXT, candle_ts INTEGER,
            buy_volume REAL, sell_volume REAL, delta REAL, imbalance REAL,
            prev_delta REAL, flip INTEGER, increasing INTEGER,
            available INTEGER, passed INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(setup_id, candle_ts)
        );
        CREATE TABLE IF NOT EXISTS confirmation_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            setup_id TEXT, symbol TEXT, interval TEXT, candle_ts INTEGER,
            close REAL, higher_close INTEGER, higher_high INTEGER,
            above_zone INTEGER, passed INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(setup_id, candle_ts)
        );
        CREATE TABLE IF NOT EXISTS setup_transitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            setup_id TEXT, symbol TEXT, interval TEXT,
            from_state TEXT, to_state TEXT, ts INTEGER, detail TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(setup_id, to_state)
        );
        CREATE TABLE IF NOT EXISTS reversal_signals (
            setup_id TEXT PRIMARY KEY, symbol TEXT, interval TEXT,
            status TEXT, state TEXT, entry_low REAL, entry_high REAL,
            entry_mid REAL, stop_loss REAL, target REAL,
            risk_per_unit REAL, risk_pct REAL, rr REAL,
            position_size REAL, risk_amount REAL,
            blocked_reason TEXT, payload_json TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS setup_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            setup_id TEXT, symbol TEXT, interval TEXT,
            outcome TEXT, reason TEXT, ts INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(setup_id, outcome)
        );
        CREATE TABLE IF NOT EXISTS alert_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, alert_type TEXT, dedupe_key TEXT,
            message TEXT, delivered INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(symbol, alert_type, dedupe_key)
        );
        CREATE TABLE IF NOT EXISTS active_setups (
            symbol TEXT PRIMARY KEY, setup_id TEXT, state TEXT,
            payload_json TEXT, updated_ts INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_sweep_symbol_ts
            ON liquidity_sweeps(symbol, sweep_ts);
        CREATE INDEX IF NOT EXISTS idx_signal_symbol
            ON reversal_signals(symbol, updated_at);
        CREATE INDEX IF NOT EXISTS idx_outcome_symbol
            ON setup_outcomes(symbol, ts);
        """)
        self._conn.commit()

    def _load_history_from_db(self) -> None:
        limit = max(self._swing_lookback, self._volume_lookback,
                    self._delta_lookback) + 10
        for symbol, profile in self._profiles.items():
            if not profile.get("enabled"):
                continue
            for interval in self._intervals:
                try:
                    rows = self._conn.execute(
                        "SELECT ts, open, high, low, close, volume,"
                        " taker_buy_volume FROM candles "
                        "WHERE symbol=? AND interval=? AND closed=1 "
                        "ORDER BY ts DESC LIMIT ?",
                        (symbol, interval, limit),
                    ).fetchall()
                except sqlite3.Error:
                    rows = []
                candles = [_normalize_candle({
                    "symbol": symbol, "interval": interval, "ts": r[0],
                    "open": r[1], "high": r[2], "low": r[3], "close": r[4],
                    "volume": r[5], "taker_buy_volume": r[6],
                    "close_ts": r[0] + INTERVAL_MS[interval], "closed": True,
                }) for r in reversed(rows)]
                candles = [c for c in candles if c]
                if candles:
                    self._candles[(symbol, interval)] = candles
                    self._seen[(symbol, interval)] = candles[-1]["ts"]

    def _load_active_setups(self) -> None:
        try:
            rows = self._conn.execute(
                "SELECT payload_json FROM active_setups").fetchall()
        except sqlite3.Error:
            return
        for (payload,) in rows:
            try:
                setup = Setup(**json.loads(payload))
                if setup.symbol in self._profiles:
                    self._setups[setup.symbol] = setup
            except Exception as exc:
                print(f"[Liquidity] active setup restore failed: {exc}",
                      flush=True)

    def _save_active(self, setup: Setup) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO active_setups"
            " (symbol, setup_id, state, payload_json, updated_ts)"
            " VALUES (?,?,?,?,?)",
            (setup.symbol, setup.setup_id, setup.state,
             json.dumps(setup.payload()), int(setup.updated_ts or 0)),
        )
        self._conn.commit()

    def _remove_active(self, symbol: str) -> None:
        self._conn.execute("DELETE FROM active_setups WHERE symbol=?", (symbol,))
        self._conn.commit()

    # ── signal listeners (paper trading) ─────────────────────────────────
    def subscribe_signals(self, callback) -> None:
        """Register callback(event_dict) for LONG_SIGNAL_READY / SETUP_CLOSED."""
        if not callable(callback):
            raise TypeError("callback must be callable")
        with self._lock:
            if callback not in self._signal_listeners:
                self._signal_listeners.append(callback)

    def unsubscribe_signals(self, callback) -> None:
        with self._lock:
            try:
                self._signal_listeners.remove(callback)
            except ValueError:
                pass

    def _emit_signal(self, event_type: str, setup: Setup, ts: int,
                     *, price=None, outcome: str = "",
                     reason: str = "") -> None:
        payload = {
            "event": event_type,
            "signal_id": setup.setup_id,
            "setup_id": setup.setup_id,
            "symbol": setup.symbol,
            "interval": setup.interval,
            "state": setup.state,
            "ts": int(ts),
            "timestamp": utc_iso(ts),
            "price": price,
            "zone_low": setup.zone_low,
            "zone_high": setup.zone_high,
            "invalidation_level": setup.invalidation_level,
            "sweep": copy.deepcopy(setup.sweep),
            "rejection": copy.deepcopy(setup.rejection),
            "volume": copy.deepcopy(setup.volume),
            "delta": copy.deepcopy(setup.delta),
            "confirmation": copy.deepcopy(setup.confirmation),
            "risk": copy.deepcopy(setup.risk),
            "outcome": outcome,
            "reason": reason,
        }
        with self._lock:
            listeners = list(self._signal_listeners)
        for callback in listeners:
            try:
                callback(dict(payload))
            except Exception as exc:
                print(f"[Liquidity] signal listener error: "
                      f"{type(exc).__name__}: {exc}", flush=True)

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self, warmup: bool = True) -> None:
        """Subscribe to the existing daemon and optionally warm up history."""
        with self._lock:
            if self._started:
                return
            self._started = True
            self._stop_event.clear()
        bw.subscribe_closed_candles(self._on_candle)
        if warmup:
            self._warm_thread = threading.Thread(
                target=self._warmup, name="liquidity-warmup", daemon=True)
            self._warm_thread.start()
        print("[Liquidity] engine started", flush=True)

    def stop(self) -> None:
        bw.unsubscribe_closed_candles(self._on_candle)
        self._stop_event.set()
        with self._lock:
            self._started = False
        print("[Liquidity] engine stopped", flush=True)

    def close(self) -> None:
        self.stop()
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    def _warmup(self) -> None:
        limit = max(self._swing_lookback, self._volume_lookback,
                    self._delta_lookback) + 10
        for symbol, profile in list(self._profiles.items()):
            if self._stop_event.is_set():
                break
            if not profile.get("enabled"):
                continue
            for interval in self._intervals:
                if self._stop_event.is_set():
                    break
                try:
                    candles = self._data_provider(
                        symbol, interval, limit=limit) or []
                except Exception as exc:
                    print(f"[Liquidity] warmup {symbol}/{interval}: "
                          f"{type(exc).__name__}: {str(exc)[:120]}", flush=True)
                    continue
                self._seed_history(symbol, interval, candles)
        if self._config["globals"].get("auto_zone"):
            for symbol in list(self._profiles):
                self.auto_set_zone(symbol, persist=True)

    def _seed_history(self, symbol: str, interval: str, candles) -> None:
        normalized = []
        for raw in candles or []:
            candle = _normalize_candle(raw)
            if candle and candle["symbol"] == symbol and candle["interval"] == interval:
                normalized.append(candle)
        if not normalized:
            return
        normalized.sort(key=lambda c: c["ts"])
        with self._lock:
            key = (symbol, interval)
            existing = {c["ts"]: c for c in self._candles.get(key, [])}
            for candle in normalized:
                existing[candle["ts"]] = candle
            merged = [existing[ts] for ts in sorted(existing)]
            maxlen = max(self._swing_lookback, self._volume_lookback,
                         self._delta_lookback) + 15
            self._candles[key] = merged[-maxlen:]
            self._seen[key] = max(self._seen.get(key, 0), merged[-1]["ts"])

    def _on_candle(self, candle: dict) -> None:
        try:
            self.process_closed_candle(candle)
        except Exception as exc:
            print(f"[Liquidity] candle error: {type(exc).__name__}: {exc}",
                  flush=True)

    # ── processing ───────────────────────────────────────────────────────
    def process_closed_candle(self, raw: dict):
        candle = _normalize_candle(raw)
        if not candle or not candle["closed"]:
            return None
        symbol = candle["symbol"]
        with self._lock:
            profile = self._profiles.get(symbol)
            if not profile or not profile.get("enabled"):
                return None
            key = (symbol, candle["interval"])
            if candle["ts"] <= self._seen.get(key, -1):
                return self._snapshot_locked(symbol, profile)
            # Idempotent closed-only persistence (shared with the daemon).
            self._conn.execute(
                "INSERT OR REPLACE INTO candles"
                " (symbol, interval, open, high, low, close, volume,"
                "  taker_buy_volume, ts, closed)"
                " VALUES (?,?,?,?,?,?,?,?,?,1)",
                (symbol, candle["interval"], candle["open"], candle["high"],
                 candle["low"], candle["close"], candle["volume"],
                 candle["taker_buy_volume"], candle["ts"]),
            )
            self._conn.commit()
            hist = self._candles.setdefault(key, [])
            prev = hist[-1] if hist else None
            hist.append(candle)
            maxlen = max(self._swing_lookback, self._volume_lookback,
                         self._delta_lookback) + 15
            if len(hist) > maxlen:
                del hist[:-maxlen]
            self._seen[key] = candle["ts"]
            self._observe(symbol, candle, profile)
            self._maybe_fast_alerts(candle, hist, prev, profile)

            setup = self._setups.get(symbol)
            if setup is None:
                self._maybe_start(symbol, candle, hist, prev, profile)
            elif setup.interval == candle["interval"]:
                self._advance(setup, candle, hist, prev, profile)
            elif setup.state == SWEEPING and setup.sweep:
                # A different timeframe can deepen the observed sweep, but it
                # never drives rejection/volume/delta (comparable intervals).
                if candle["low"] < setup.sweep.get("sweep_price", candle["low"]):
                    setup.sweep["sweep_price"] = candle["low"]
                    setup.sweep["depth_pct"] = self._depth_pct(
                        setup.sweep.get("prev_swing_low"), candle["low"])
                    self._save_active(setup)
            return self._snapshot_locked(symbol, profile)

    def _observe(self, symbol: str, candle: dict, profile: dict) -> None:
        zone_low = _num(profile.get("zone_low"))
        zone_high = _num(profile.get("zone_high"))
        obs = self._observations.setdefault(symbol, {})
        obs.update({
            "price": candle["close"],
            "last_ts": candle["ts"],
            "last_interval": candle["interval"],
            "last_update": utc_iso(candle["ts"]),
            "below_zone": (zone_low is not None
                           and candle["close"] < zone_low),
            "zone_entered": (zone_low is not None and zone_high is not None
                             and candle["low"] <= zone_high
                             and candle["high"] >= zone_low),
        })
        if zone_low:
            obs["distance_to_zone_pct"] = round(
                (candle["close"] - zone_low) / zone_low * 100, 3)

    def _maybe_start(self, symbol: str, candle: dict, hist: list,
                     prev: Optional[dict], profile: dict) -> None:
        zone_low = _num(profile.get("zone_low"))
        zone_high = _num(profile.get("zone_high"))
        if zone_low is None or zone_high is None or zone_low >= zone_high:
            return
        before = hist[:-1]
        if len(before) < min(5, self._swing_lookback):
            return
        swing_low = min(c["low"] for c in before[-self._swing_lookback:])
        low = candle["low"]
        # Sweep = break the recent swing low while trading into or below the
        # configured SSL zone. (A close below the zone is tracked separately;
        # it is not required for the sweep itself.)
        if not (low < swing_low and low <= zone_high):
            return
        depth = self._depth_pct(swing_low, low)
        if depth is None or depth < self._min_depth or depth > self._max_depth:
            return
        setup_id = f"ls-{symbol}-{candle['ts']}-{uuid.uuid4().hex[:8]}"
        buffer = (max(0.0, _num(self._risk_cfg.get("stop_buffer_pct"), 0.2))
                  / 100.0 * low)
        invalidation = profile.get("invalidation_level")
        invalidation = (_num(invalidation)
                        if invalidation is not None else low - buffer)
        sweep = {
            "ts": candle["ts"],
            "time": utc_iso(candle["ts"]),
            "price": low,
            "close": candle["close"],
            "prev_swing_low": swing_low,
            "depth_pct": round(depth, 4),
            "depth_abs": round(swing_low - low, 8),
            "below_zone": candle["close"] < zone_low,
            "recovery_price": candle["close"],
            "recovered_pct": round((candle["close"] - low) / low * 100, 4)
            if low else 0.0,
        }
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO liquidity_sweeps"
            " (setup_id, symbol, interval, sweep_ts, sweep_price, sweep_close,"
            "  prev_swing_low, depth_pct, depth_abs, below_zone, recovery_price)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (setup_id, symbol, candle["interval"], candle["ts"], low,
             candle["close"], swing_low, sweep["depth_pct"], sweep["depth_abs"],
             int(sweep["below_zone"]), candle["close"]),
        )
        self._conn.commit()
        if cur.rowcount == 0:
            return
        setup = Setup(
            setup_id=setup_id, symbol=symbol, interval=candle["interval"],
            state="NONE", created_ts=candle["ts"], updated_ts=candle["ts"],
            zone_low=zone_low, zone_high=zone_high,
            watch_levels=list(profile.get("watch_levels") or []),
            invalidation_level=invalidation, sweep=sweep,
        )
        self._setups[symbol] = setup
        self._transition(setup, SWEEPING, candle["ts"], "sweep")
        self._alert_sweep(setup, candle)
        self._save_active(setup)
        # A sweep candle can itself be a rejection (large lower wick closing
        # back inside/above the zone), but it can never reach a buy signal on
        # this candle: confirmation requires the NEXT candle.
        self._advance(setup, candle, hist, prev, profile, initial=True)

    def _advance(self, setup: Setup, candle: dict, hist: list,
                 prev: Optional[dict], profile: dict,
                 initial: bool = False) -> None:
        if initial:
            setup.candle_count = max(setup.candle_count, 1)
        else:
            setup.candle_count += 1
        setup.updated_ts = candle["ts"]
        setup.sweep["recovery_price"] = max(
            _num(setup.sweep.get("recovery_price"), candle["close"]),
            candle["close"])
        sweep_low = _num(setup.sweep.get("price"))
        if sweep_low:
            setup.sweep["recovered_pct"] = round(
                (setup.sweep["recovery_price"] - sweep_low) / sweep_low * 100, 4)

        # Invalidation uses the actual traded low (conservative).
        if (setup.invalidation_level is not None
                and candle["low"] <= setup.invalidation_level
                and candle["ts"] != setup.created_ts):
            self._invalidate(setup, "price_broke_invalidation", candle)
            return

        if (setup.state != LONG
                and setup.candle_count > self._expiry_candles):
            self._expire(setup, "max_candles", candle)
            return

        state = setup.state
        if state == SWEEPING:
            rejection = self._detect_rejection(candle, prev, profile)
            if rejection:
                setup.rejection = rejection
                self._insert_rejection(setup, rejection)
                if self._transition(setup, REJECTION, candle["ts"],
                                    "rejection:" + ",".join(rejection["patterns"])):
                    self._try_volume_delta(setup, candle, hist, profile)
            else:
                self._save_active(setup)
        elif state == REJECTION:
            self._try_volume_delta(setup, candle, hist, profile)
        elif state == VOLUME:
            self._try_delta(setup, candle, hist, profile)
            if setup.state == DELTA:
                if self._transition(setup, CONFIRMATION_PENDING, candle["ts"],
                                    "delta confirmed"):
                    setup.confirmation_start_ts = candle["ts"]
                    self._save_active(setup)
        elif state == DELTA:
            if self._transition(setup, CONFIRMATION_PENDING, candle["ts"],
                                "delta confirmed"):
                setup.confirmation_start_ts = candle["ts"]
                self._save_active(setup)
        elif state == CONFIRMATION_PENDING:
            if (setup.confirmation_start_ts is None
                    or candle["ts"] > setup.confirmation_start_ts):
                self._check_confirmation(setup, candle, profile)
        elif state == CONFIRMED:
            # Risk may have been blocked by a temporary disconnect/staleness;
            # re-evaluate on each fresh candle while the setup is still valid.
            self._try_risk(setup, candle, profile)
        elif state == LONG:
            if (setup.signal_candle_count
                    and setup.candle_count - setup.signal_candle_count
                    > self._signal_valid_candles):
                self._expire(setup, "signal_expired", candle)

    # ── patterns / confirmation data ─────────────────────────────────────
    def _depth_pct(self, swing_low, sweep_low) -> Optional[float]:
        swing_low = _num(swing_low)
        sweep_low = _num(sweep_low)
        if not swing_low or sweep_low is None or swing_low <= 0:
            return None
        return (swing_low - sweep_low) / swing_low * 100

    def _detect_rejection(self, candle: dict, prev: Optional[dict],
                          profile: dict) -> Optional[dict]:
        zone_low = _num(profile.get("zone_low"))
        if zone_low is None:
            return None
        o, h, l, c = (candle["open"], candle["high"], candle["low"],
                      candle["close"])
        rng = h - l
        body = abs(c - o)
        if rng <= 0 or c <= o:
            return None                      # must be bullish
        if c < zone_low:
            return None                      # must close back in/above zone
        cfg = self._rejection_cfg
        min_body = _num(cfg.get("min_body_ratio"), 0.12)
        max_body = _num(cfg.get("max_body_ratio"), 0.62)
        min_wick = _num(cfg.get("min_lower_wick_ratio"), 0.30)
        wick_mult = _num(cfg.get("wick_body_multiple"), 1.5)
        close_upper = _num(cfg.get("close_upper_ratio"), 0.60)

        body_ratio = body / rng
        lower_wick = min(o, c) - l
        lower_ratio = lower_wick / rng
        upper_wick = h - max(o, c)
        close_pos = (c - l) / rng

        patterns = []
        if (body_ratio <= max_body and lower_ratio >= min_wick
                and lower_wick >= wick_mult * max(body, 1e-12)
                and close_pos >= close_upper):
            patterns.append("HAMMER")
        if (body_ratio <= max_body and lower_wick >= 2.0 * max(body, 1e-12)
                and close_pos >= close_upper):
            patterns.append("PIN_BAR")
        if (prev and prev["close"] < prev["open"]
                and c >= prev["open"] and o <= prev["close"]
                and body > abs(prev["close"] - prev["open"])):
            patterns.append("BULLISH_ENGULFING")
        if (body_ratio >= min_body and lower_ratio >= min_wick
                and close_pos >= close_upper):
            patterns.append("STRONG_REJECTION")
        if not patterns:
            return None
        return {
            "ts": candle["ts"],
            "time": utc_iso(candle["ts"]),
            "open": o, "high": h, "low": l, "close": c,
            "patterns": patterns,
            "body_ratio": round(body_ratio, 4),
            "lower_wick_ratio": round(lower_ratio, 4),
            "upper_wick": round(upper_wick, 8),
            "close_above_zone": c >= zone_low,
            "zone_low": zone_low,
        }

    def _insert_rejection(self, setup: Setup, rejection: dict) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO rejection_events"
            " (setup_id, symbol, interval, candle_ts, open, high, low, close,"
            "  patterns, body_ratio, lower_wick_ratio, close_above_zone)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (setup.setup_id, setup.symbol, setup.interval,
             rejection["ts"], rejection["open"], rejection["high"],
             rejection["low"], rejection["close"],
             json.dumps(rejection["patterns"]), rejection["body_ratio"],
             rejection["lower_wick_ratio"],
             int(rejection["close_above_zone"])),
        )
        self._conn.commit()

    def _volume_state(self, candle: dict, hist: list, profile: dict) -> dict:
        before = hist[:-1][-self._volume_lookback:]
        volumes = [c["volume"] for c in before if c.get("volume") is not None]
        threshold = self._effective_volume_multiplier(profile)
        if len(volumes) < self._volume_min_history:
            return {
                "ts": candle["ts"], "time": utc_iso(candle["ts"]),
                "volume": candle["volume"], "avg_volume": None,
                "multiplier": None, "lookback": len(volumes),
                "available": False, "passed": False, "threshold": threshold,
                "reason": "insufficient_history",
            }
        avg = mean(volumes)
        multiplier = (candle["volume"] / avg) if avg > 0 else None
        passed = multiplier is not None and multiplier >= threshold
        return {
            "ts": candle["ts"], "time": utc_iso(candle["ts"]),
            "volume": candle["volume"], "avg_volume": round(avg, 8),
            "multiplier": round(multiplier, 4) if multiplier is not None else None,
            "lookback": len(volumes), "available": True, "passed": passed,
            "threshold": threshold, "reason": "" if passed else "below_threshold",
        }

    def _delta_state(self, candle: dict, hist: list, profile: dict) -> dict:
        taker = candle.get("taker_buy_volume")
        volume = candle.get("volume")
        threshold = _num(self._min_delta_imbalance, 0.0)
        if taker is None or volume is None or volume <= 0:
            return {
                "ts": candle["ts"], "time": utc_iso(candle["ts"]),
                "buy_volume": None, "sell_volume": None, "delta": None,
                "imbalance": None, "prev_delta": None, "flip": False,
                "increasing": False, "available": False, "passed": False,
                "threshold": threshold, "reason": "trade_data_unavailable",
            }
        buy = max(0.0, float(taker))
        sell = max(0.0, float(volume) - buy)
        delta = buy - sell
        imbalance = delta / float(volume)
        before = hist[:-1][-self._delta_lookback:]
        prev_delta = None
        for c in reversed(before):
            if c.get("taker_buy_volume") is None:
                continue
            b = float(c["taker_buy_volume"])
            s = max(0.0, float(c.get("volume") or 0) - b)
            prev_delta = b - s
            break
        flip = bool(delta > 0 and prev_delta is not None and prev_delta <= 0)
        increasing = bool(prev_delta is not None and delta > prev_delta)
        passed = bool(
            delta > 0 and imbalance >= threshold
            and (flip if self._require_delta_flip else True))
        reason = ""
        if delta <= 0:
            reason = "negative_delta"
        elif imbalance < threshold:
            reason = "imbalance_below_threshold"
        elif self._require_delta_flip and not flip:
            reason = "no_positive_flip"
        return {
            "ts": candle["ts"], "time": utc_iso(candle["ts"]),
            "buy_volume": round(buy, 8), "sell_volume": round(sell, 8),
            "delta": round(delta, 8), "imbalance": round(imbalance, 6),
            "prev_delta": round(prev_delta, 8) if prev_delta is not None else None,
            "flip": flip, "increasing": increasing,
            "available": True, "passed": passed,
            "threshold": threshold, "reason": reason,
        }

    def _try_volume_delta(self, setup: Setup, candle: dict, hist: list,
                          profile: dict) -> None:
        vol = self._volume_state(candle, hist, profile)
        setup.volume = vol
        self._conn.execute(
            "INSERT OR IGNORE INTO volume_confirmations"
            " (setup_id, symbol, interval, candle_ts, volume, avg_volume,"
            "  multiplier, lookback, available, passed)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (setup.setup_id, setup.symbol, setup.interval, candle["ts"],
             vol["volume"], vol["avg_volume"], vol["multiplier"],
             vol["lookback"], int(vol["available"]), int(vol["passed"])),
        )
        self._conn.commit()
        if not vol["passed"]:
            self._save_active(setup)
            return
        if setup.state == REJECTION:
            if not self._transition(setup, VOLUME, candle["ts"],
                                    f"volume {vol['multiplier']}x"):
                return
        self._try_delta(setup, candle, hist, profile)

    def _try_delta(self, setup: Setup, candle: dict, hist: list,
                   profile: dict) -> None:
        delta = self._delta_state(candle, hist, profile)
        setup.delta = delta
        self._conn.execute(
            "INSERT OR IGNORE INTO delta_confirmations"
            " (setup_id, symbol, interval, candle_ts, buy_volume, sell_volume,"
            "  delta, imbalance, prev_delta, flip, increasing, available, passed)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (setup.setup_id, setup.symbol, setup.interval, candle["ts"],
             delta["buy_volume"], delta["sell_volume"], delta["delta"],
             delta["imbalance"], delta["prev_delta"], int(delta["flip"]),
             int(delta["increasing"]), int(delta["available"]),
             int(delta["passed"])),
        )
        self._conn.commit()
        advance = delta["passed"]
        if not advance and not delta["available"] and not self._delta_required:
            advance = True
            setup.warnings.append(
                "delta UNAVAILABLE — strict delta disabled by configuration")
        if not advance:
            self._save_active(setup)
            return
        if setup.state == VOLUME:
            if not self._transition(
                    setup, DELTA, candle["ts"],
                    "delta " + ("PASS" if delta["passed"] else "UNAVAILABLE")):
                return
        if setup.state == DELTA:
            if self._transition(setup, CONFIRMATION_PENDING, candle["ts"],
                                "awaiting next candle"):
                setup.confirmation_start_ts = candle["ts"]
                self._save_active(setup)

    def _check_confirmation(self, setup: Setup, candle: dict,
                            profile: dict) -> None:
        rejection = setup.rejection or {}
        rej_close = _num(rejection.get("close"))
        rej_high = _num(rejection.get("high"))
        higher_close = (candle["close"] > rej_close
                        if rej_close is not None else False)
        higher_high = (candle["high"] > rej_high
                       if rej_high is not None else True)
        above_zone = candle["close"] >= setup.zone_low
        passed = bool(
            (higher_close if self._require_higher_close else True)
            and (higher_high if self._require_higher_high else True)
            and above_zone)
        confirmation = {
            "ts": candle["ts"], "time": utc_iso(candle["ts"]),
            "close": candle["close"], "higher_close": higher_close,
            "higher_high": higher_high, "above_zone": above_zone,
            "passed": passed,
        }
        setup.confirmation = confirmation
        self._conn.execute(
            "INSERT OR IGNORE INTO confirmation_events"
            " (setup_id, symbol, interval, candle_ts, close, higher_close,"
            "  higher_high, above_zone, passed)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (setup.setup_id, setup.symbol, setup.interval, candle["ts"],
             candle["close"], int(higher_close), int(higher_high),
             int(above_zone), int(passed)),
        )
        self._conn.commit()
        if passed:
            if self._transition(setup, CONFIRMED, candle["ts"],
                                "next candle confirmed"):
                self._try_risk(setup, candle, profile)
            return
        setup.confirmation_attempts += 1
        if setup.confirmation_attempts >= self._confirmation_candles:
            self._invalidate(setup, "no_follow_through", candle)
        else:
            self._save_active(setup)

    # ── risk ─────────────────────────────────────────────────────────────
    def _feed_state(self) -> dict:
        try:
            raw = self._live_provider() or {}
        except Exception as exc:
            return {"live": False, "transport": "none",
                    "last_tick_age_s": None, "stale": True,
                    "reason": f"status_error:{type(exc).__name__}"}
        transport = str(raw.get("transport") or "none")
        live = bool(raw.get("live")) and transport in ("ws", "http")
        age = raw.get("last_tick_age_s")
        try:
            age_val = float(age) if age is not None else None
        except (TypeError, ValueError):
            age_val = None
        stale = age_val is None or age_val > self._stale_after_s
        return {"live": live, "transport": transport,
                "last_tick_age_s": age_val, "stale": stale, "reason": ""}

    def _compute_risk(self, setup: Setup, candle: dict, profile: dict) -> dict:
        rejection = setup.rejection or {}
        sweep_low = _num(setup.sweep.get("price"))
        zone_low = _num(setup.zone_low)
        zone_high = _num(setup.zone_high)
        rej_close = _num(rejection.get("close"))
        if None in (sweep_low, zone_low, zone_high, rej_close):
            return {"ok": False, "reason": "stop_unavailable"}
        entry_low = zone_low
        entry_high = max(zone_high, rej_close)
        if entry_high <= entry_low:
            entry_high = entry_low + max(1e-8, entry_low * 0.0005)
        entry_mid = (entry_low + entry_high) / 2.0
        buffer_pct = max(0.0, _num(self._risk_cfg.get("stop_buffer_pct"), 0.2))
        stop = min(sweep_low, zone_low) - buffer_pct / 100.0 * max(sweep_low, 1e-9)
        if stop >= entry_low:
            return {"ok": False, "reason": "stop_unavailable",
                    "entry_low": round(entry_low, 8),
                    "entry_high": round(entry_high, 8),
                    "entry_mid": round(entry_mid, 8),
                    "stop_loss": round(stop, 8)}
        risk_per_unit = entry_mid - stop
        target = _num(profile.get("target_price"))
        if target is None or target <= entry_mid:
            target = _num(profile.get("breakout_threshold"))
        if target is None or target <= entry_mid:
            default_rr = _num(self._risk_cfg.get("default_rr"), 2.0)
            target = entry_mid + default_rr * risk_per_unit
        reward = target - entry_mid
        rr = reward / risk_per_unit if risk_per_unit > 0 else 0.0
        risk_pct = risk_per_unit / entry_mid * 100.0 if entry_mid > 0 else 999.0
        max_risk = _num(self._risk_cfg.get("max_risk_pct"), 2.0)
        min_rr = _num(self._risk_cfg.get("min_rr"), 1.5)
        reasons = []
        if max_risk is not None and risk_pct > max_risk:
            reasons.append("risk_pct_above_max")
        if min_rr is not None and rr < min_rr:
            reasons.append("risk_reward_below_min")
        account = _num(self._risk_cfg.get("account_size"))
        risk_pct_trade = _num(self._risk_cfg.get("risk_per_trade_pct"), 1.0)
        position_size = None
        risk_amount = None
        potential_profit = None
        if account and account > 0 and risk_per_unit > 0:
            risk_amount = account * (risk_pct_trade or 0.0) / 100.0
            position_size = risk_amount / risk_per_unit
            potential_profit = position_size * reward
        return {
            "ok": not reasons,
            "reason": ",".join(reasons),
            "entry_low": round(entry_low, 8),
            "entry_high": round(entry_high, 8),
            "entry_mid": round(entry_mid, 8),
            "entry_zone": [round(entry_low, 8), round(entry_high, 8)],
            "stop_loss": round(stop, 8),
            "risk_per_unit": round(risk_per_unit, 8),
            "risk_pct": round(risk_pct, 4),
            "target": round(target, 8),
            "reward_per_unit": round(reward, 8),
            "rr": round(rr, 4),
            "position_size": _round(position_size, 8),
            "risk_amount": _round(risk_amount, 4),
            "potential_profit": _round(potential_profit, 4),
            "max_risk_pct": max_risk,
            "min_rr": min_rr,
        }

    def _try_risk(self, setup: Setup, candle: dict, profile: dict) -> None:
        risk = self._compute_risk(setup, candle, profile)
        feed = self._feed_state()
        now_ms = self._now_ms()
        close_ts = int(candle.get("close_ts") or candle["ts"])
        candle_age = max(0.0, (now_ms - close_ts) / 1000.0)
        reason = ""
        if not feed["live"]:
            reason = "binance_disconnected"
        elif feed["stale"] or candle_age > self._stale_after_s:
            reason = "stale_market_data"
        elif not setup.rejection or not setup.volume or not setup.delta \
                or not setup.confirmation:
            reason = "confirmation_data_incomplete"
        elif not risk.get("ok"):
            reason = risk.get("reason") or "risk_limit"
        if reason:
            risk["ok"] = False
        risk.update({"feed": feed, "candle_age_s": round(candle_age, 2),
                     "blocked_reason": reason})
        setup.risk = risk
        if reason:
            setup.signal_blocked = reason
            self._upsert_signal(setup, "blocked", reason)
            self._alert_reversal_blocked(setup, reason)
            self._save_active(setup)
            return
        setup.signal_blocked = ""
        if setup.state == CONFIRMED:
            if self._transition(setup, LONG, candle["ts"], "all gates passed"):
                setup.signal_ts = candle["ts"]
                setup.signal_candle_count = setup.candle_count
                self._upsert_signal(setup, "ready", "")
                self._alert_reversal(setup)
                self._save_active(setup)
                self._emit_signal("LONG_SIGNAL_READY", setup, candle["ts"],
                                  price=candle["close"])
        else:
            self._upsert_signal(setup, "ready", "")
            self._save_active(setup)

    def _upsert_signal(self, setup: Setup, status: str, reason: str) -> None:
        risk = setup.risk or {}
        self._conn.execute(
            "INSERT OR REPLACE INTO reversal_signals"
            " (setup_id, symbol, interval, status, state, entry_low, entry_high,"
            "  entry_mid, stop_loss, target, risk_per_unit, risk_pct, rr,"
            "  position_size, risk_amount, blocked_reason, payload_json,"
            "  updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
            (setup.setup_id, setup.symbol, setup.interval, status, setup.state,
             risk.get("entry_low"), risk.get("entry_high"),
             risk.get("entry_mid"), risk.get("stop_loss"), risk.get("target"),
             risk.get("risk_per_unit"), risk.get("risk_pct"), risk.get("rr"),
             risk.get("position_size"), risk.get("risk_amount"), reason,
             json.dumps(setup.payload())),
        )
        self._conn.commit()

    # ── transitions / outcomes / alerts ─────────────────────────────────
    def _transition(self, setup: Setup, to_state: str, ts: int,
                    detail: str) -> bool:
        if to_state not in _ALLOWED.get(setup.state, set()):
            print(f"[Liquidity] illegal transition {setup.symbol} "
                  f"{setup.state} -> {to_state}", flush=True)
            return False
        from_state = setup.state
        setup.state = to_state
        setup.updated_ts = ts
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO setup_transitions"
            " (setup_id, symbol, interval, from_state, to_state, ts, detail)"
            " VALUES (?,?,?,?,?,?,?)",
            (setup.setup_id, setup.symbol, setup.interval, from_state,
             to_state, ts, detail),
        )
        self._conn.commit()
        self._save_active(setup)
        return cur.rowcount > 0

    def _invalidate(self, setup: Setup, reason: str, candle: dict) -> None:
        self._close_setup(setup, "invalidated", reason, candle.get("ts"),
                          price=candle.get("close"))

    def _expire(self, setup: Setup, reason: str, candle: dict) -> None:
        self._close_setup(setup, "expired", reason, candle.get("ts"),
                          price=candle.get("close"))

    def _close_setup(self, setup: Setup, outcome: str, reason: str,
                     ts: Optional[int], price=None) -> None:
        ts = int(ts or self._now_ms())
        self._conn.execute(
            "INSERT OR IGNORE INTO setup_outcomes"
            " (setup_id, symbol, interval, outcome, reason, ts)"
            " VALUES (?,?,?,?,?,?)",
            (setup.setup_id, setup.symbol, setup.interval, outcome, reason, ts),
        )
        self._conn.commit()
        self._setups.pop(setup.symbol, None)
        self._remove_active(setup.symbol)
        if self._alerts_cfg.get("invalidation"):
            self._alert_once(
                setup.symbol, "INVALIDATED", setup.setup_id,
                f"{setup.symbol} — SETUP {outcome.upper()}\n"
                f"Timeframe: {setup.interval}\n"
                f"Reason: {reason}\n"
                f"Timestamp: {utc_iso(ts)}\n\nNo entry signal.",
            )
        self._emit_signal("SETUP_CLOSED", setup, ts, price=price,
                          outcome=outcome, reason=reason)

    def _alert_once(self, symbol: str, alert_type: str, dedupe_key: str,
                    text: str) -> bool:
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO alert_log"
            " (symbol, alert_type, dedupe_key, message, delivered)"
            " VALUES (?,?,?,?,0)",
            (symbol, alert_type, dedupe_key, text),
        )
        self._conn.commit()
        if cur.rowcount == 0:
            return False
        profile = self._profile(symbol) or {}
        delivered = False
        if self._alerts_enabled(profile):
            try:
                delivered = bool(self._alert_sender(text))
            except Exception as exc:
                print(f"[Liquidity] alert send failed: "
                      f"{type(exc).__name__}: {exc}", flush=True)
        self._conn.execute(
            "UPDATE alert_log SET delivered=? WHERE symbol=? AND alert_type=?"
            " AND dedupe_key=?",
            (int(delivered), symbol, alert_type, dedupe_key),
        )
        self._conn.commit()
        return True

    def _alert_sweep(self, setup: Setup, candle: dict) -> None:
        if SWEEPING not in (self._alerts_cfg.get("states") or []):
            return
        sweep = setup.sweep
        price = self._current_price(setup.symbol, candle)
        text = (
            f"{setup.symbol} — LIQUIDITY SWEEP DETECTED\n"
            f"Timeframe: {setup.interval}\n"
            f"Price: {price:,.2f}\n"
            f"Sweep low: {sweep['price']:,.2f}\n"
            f"Previous swing low: {sweep['prev_swing_low']:,.2f}\n"
            f"Depth: {sweep['depth_pct']:.2f}%\n"
            f"State: {SWEEPING}\n"
            f"Timestamp: {sweep['time']}\n\n"
            f"No entry signal. No paper trade opened. Waiting for rejection."
        )
        self._alert_once(setup.symbol, "SWEEP", setup.setup_id, text)

    def _alert_reversal(self, setup: Setup) -> None:
        if LONG not in (self._alerts_cfg.get("states") or []):
            return
        risk = setup.risk or {}
        vol = setup.volume or {}
        delta = setup.delta or {}
        rej = setup.rejection or {}
        text = (
            f"{setup.symbol} — REVERSAL CONFIRMATION\n"
            f"Timeframe: {setup.interval}\n"
            f"Rejection: PASS ({', '.join(rej.get('patterns') or [])})\n"
            f"Volume: PASS ({vol.get('multiplier', 0):.1f}x average)\n"
            f"Buying pressure: PASS (delta {delta.get('delta', 0):+,.2f}; "
            f"taker estimate)\n"
            f"Next candle confirmation: PASS\n"
            f"State: {LONG}\n"
            f"Entry zone: {risk.get('entry_low', 0):,.2f}–"
            f"{risk.get('entry_high', 0):,.2f}\n"
            f"Stop-loss: {risk.get('stop_loss', 0):,.2f}\n"
            f"Target: {risk.get('target', 0):,.2f} · R:R "
            f"{risk.get('rr', 0):.2f}\n"
            f"Timestamp: {utc_iso(setup.signal_ts or setup.updated_ts)}\n\n"
            f"Long signal ready. The PAPER engine may execute this "
            f"automatically. NO real order was placed."
        )
        self._alert_once(setup.symbol, "REVERSAL", setup.setup_id, text)

    def _alert_reversal_blocked(self, setup: Setup, reason: str) -> None:
        if CONFIRMED not in (self._alerts_cfg.get("states") or []):
            return
        risk = setup.risk or {}
        text = (
            f"{setup.symbol} — REVERSAL CONFIRMED · RISK BLOCKED\n"
            f"Timeframe: {setup.interval}\n"
            f"Rejection: PASS\n"
            f"Volume: PASS ({((setup.volume or {}).get('multiplier') or 0):.1f}x)\n"
            f"Buying pressure: PASS\n"
            f"Next candle: PASS\n"
            f"State: {CONFIRMED}\n"
            f"Blocked: {reason}\n"
            f"Timestamp: {utc_iso(setup.updated_ts)}\n\n"
            f"No long signal. No order was placed."
        )
        self._alert_once(setup.symbol, "REVERSAL_BLOCKED", setup.setup_id, text)

    def _maybe_fast_alerts(self, candle: dict, hist: list,
                           prev: Optional[dict], profile: dict) -> None:
        # Percentage movement alert (never a reversal confirmation).
        if candle["interval"] == "1m":
            pct = (candle["close"] - candle["open"]) / candle["open"] * 100 \
                if candle["open"] else 0.0
            if pct >= self._pct_1m(profile):
                text = (
                    f"{candle['symbol']} — MOVE ALERT\n"
                    f"1m closed bullish candle: {pct:+.2f}%\n"
                    f"Close: {candle['close']:,.2f}\n"
                    f"Timestamp: {utc_iso(candle['ts'])}\n\n"
                    f"Movement alert only — NOT a reversal confirmation."
                )
                self._alert_once(candle["symbol"], "PCT_1M",
                                 f"1m:{candle['ts']}", text)
        # Breakout alert on the configured interval with volume + pressure.
        if candle["interval"] != self._breakout_interval(profile):
            return
        threshold = _num(profile.get("breakout_threshold"))
        if threshold is None or candle["close"] <= threshold:
            return
        if candle["close"] <= candle["open"]:
            return
        volume = self._volume_state(candle, hist, profile)
        delta = self._delta_state(candle, hist, profile)
        multiplier = _num(self._alerts_cfg.get("breakout_volume_multiplier"), 2.0)
        if not volume["passed"] or (volume["multiplier"] or 0) < multiplier:
            return
        if not delta["available"] or not delta["passed"]:
            return                      # no invented buying pressure
        text = (
            f"{candle['symbol']} — BREAKOUT ALERT\n"
            f"Timeframe: {candle['interval']} closed candle\n"
            f"Close: {candle['close']:,.2f} above {threshold:,.2f}\n"
            f"Volume: {volume['multiplier']:.1f}x average\n"
            f"Buying pressure: positive (taker estimate)\n"
            f"Timestamp: {utc_iso(candle['ts'])}\n\n"
            f"Breakout alert only — NOT a reversal confirmation. "
            f"No order was placed."
        )
        self._alert_once(candle["symbol"], "BREAKOUT",
                         f"{candle['interval']}:{candle['ts']}", text)

    # ── public query API ─────────────────────────────────────────────────
    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    def _default_price(self, symbol: str) -> Optional[float]:
        try:
            tick = bw.latest_prices().get(_pair(symbol).lower(), {})
            return _num(tick.get("price"))
        except Exception:
            return None

    def _current_price(self, symbol: str, candle: Optional[dict] = None):
        price = self._price_provider(symbol)
        if price is None:
            price = _num((candle or {}).get("close"))
        if price is None:
            hist = self._candles.get((symbol, self._signal_interval)) or []
            price = _num(hist[-1]["close"]) if hist else None
        return price

    def get_status(self, symbol) -> dict:
        with self._lock:
            sym = _pair(symbol)
            profile = self._profiles.get(sym)
            if not profile:
                return {"ok": False, "symbol": sym,
                        "error": "no configured liquidity profile"}
            return self._snapshot_locked(sym, profile)

    def _snapshot_locked(self, symbol: str, profile: dict) -> dict:
        setup = self._setups.get(symbol)
        feed = self._feed_state()
        interval = (setup.interval if setup
                    else (profile.get("signal_interval") or self._signal_interval))
        hist = self._candles.get((symbol, interval)) or []
        last = hist[-1] if hist else None
        price = self._current_price(symbol, last)
        obs = self._observations.get(symbol, {})
        state = setup.state if setup else (
            "WATCHING_ZONE" if obs.get("zone_entered") else "IDLE")
        stale = bool(feed.get("stale"))
        disconnected = not feed.get("live")
        no_data = last is None
        if disconnected or stale or no_data:
            color = "GREY"
        elif setup and setup.state == LONG:
            color = "GREEN"
        elif setup and setup.state == SWEEPING:
            color = "RED"
        elif setup and setup.state in (REJECTION, VOLUME, DELTA,
                                       CONFIRMATION_PENDING, CONFIRMED):
            color = "YELLOW"
        else:
            color = "GREY"

        conditions = self._conditions_locked(setup, profile, feed, stale)
        below_zone = (price is not None
                      and _num(profile.get("zone_low")) is not None
                      and price < _num(profile.get("zone_low")))
        snapshot = {
            "ok": True,
            "symbol": symbol,
            "asset": _asset(symbol),
            "timeframe": interval,
            "configured_intervals": list(self._intervals),
            "price": round(price, 8) if price is not None else None,
            "price_source": ("binance_ticker" if self._price_provider(symbol)
                             is not None else "last_closed_candle"),
            "zone": {"low": profile.get("zone_low"),
                     "high": profile.get("zone_high")},
            "watch_levels": list(profile.get("watch_levels") or []),
            "breakout_threshold": profile.get("breakout_threshold"),
            "state": state,
            "state_color": color,
            "signal_ready": bool(setup and setup.state == LONG),
            "signal": setup.risk if setup and setup.state == LONG else None,
            "signal_blocked": setup.signal_blocked if setup else "",
            "sweep": (setup.sweep if setup else None),
            "rejection": (setup.rejection if setup else None),
            "volume": (setup.volume if setup else None),
            "delta": (setup.delta if setup else None),
            "confirmation": (setup.confirmation if setup else None),
            "risk": setup.risk if setup else None,
            "invalidation_level": setup.invalidation_level if setup else None,
            "below_zone": bool(below_zone),
            "zone_entered": bool(obs.get("zone_entered")),
            "distance_to_zone_pct": obs.get("distance_to_zone_pct"),
            "conditions": conditions,
            "candles_processed": len(hist),
            "history_by_interval": {
                i: len(self._candles.get((symbol, i), []))
                for i in self._intervals
            },
            "last_candle": ({
                "ts": last["ts"], "time": utc_iso(last["ts"]),
                "open": last["open"], "high": last["high"],
                "low": last["low"], "close": last["close"],
                "volume": last["volume"],
                "taker_buy_volume": last["taker_buy_volume"],
            } if last else None),
            "last_update": utc_iso(last["ts"]) if last else (
                obs.get("last_update") or ""),
            "feed": feed,
            "disconnected": disconnected,
            "stale": stale,
            "warnings": list(setup.warnings) if setup else [],
            "profile": {
                "enabled": profile.get("enabled"),
                "alerts_enabled": profile.get("alerts_enabled"),
                "volume_multiplier": self._effective_volume_multiplier(profile),
                "delta_required": self._delta_required,
                "pct_1m": self._pct_1m(profile),
            },
            "advice": self._advice(symbol, setup, profile, feed, stale, price),
        }
        return snapshot

    def _conditions_locked(self, setup: Optional[Setup], profile: dict,
                           feed: dict, stale: bool) -> list:
        conditions = []
        if not feed.get("live"):
            conditions.append({"name": "Binance feed", "status": "FAIL",
                               "detail": "disconnected"})
        elif stale:
            conditions.append({"name": "Binance feed", "status": "FAIL",
                               "detail": "stale market data"})
        else:
            conditions.append({"name": "Binance feed", "status": "PASS",
                               "detail": feed.get("transport", "")})
        if not setup:
            conditions += [
                {"name": "Liquidity sweep", "status": "PENDING",
                 "detail": "no sweep below the zone"},
                {"name": "Rejection candle", "status": "PENDING", "detail": ""},
                {"name": "Volume climax", "status": "PENDING", "detail": ""},
                {"name": "Buying pressure", "status": "PENDING", "detail": ""},
                {"name": "Next candle", "status": "PENDING", "detail": ""},
                {"name": "Risk", "status": "PENDING", "detail": ""},
            ]
            return conditions
        conditions.append({"name": "Liquidity sweep", "status": "PASS",
                           "detail": f"low {setup.sweep.get('price')}"})
        rejection = setup.rejection
        conditions.append({
            "name": "Rejection candle",
            "status": "PASS" if rejection else (
                "PENDING" if setup.state == SWEEPING else "FAIL"),
            "detail": ",".join(rejection.get("patterns") or [])
            if rejection else "",
        })
        vol = setup.volume or {}
        conditions.append({
            "name": "Volume climax",
            "status": ("UNAVAILABLE" if vol.get("available") is False
                       else "PASS" if vol.get("passed") else
                       "FAIL" if vol else "PENDING"),
            "detail": (f"{vol.get('multiplier')}x (min "
                       f"{vol.get('threshold')}x; lookback {vol.get('lookback')})"
                       if vol else ""),
        })
        delta = setup.delta or {}
        conditions.append({
            "name": "Buying pressure / delta",
            "status": ("UNAVAILABLE" if delta.get("available") is False
                       else "PASS" if delta.get("passed") else
                       "FAIL" if delta else "PENDING"),
            "detail": (f"delta {delta.get('delta')} "
                       f"imbalance {delta.get('imbalance')} (taker estimate)"
                       if delta.get("available") else
                       "taker-side data unavailable"),
        })
        conf = setup.confirmation or {}
        conditions.append({
            "name": "Next candle confirmation",
            "status": ("PASS" if conf.get("passed") else
                       "FAIL" if conf else "PENDING"),
            "detail": (f"close {conf.get('close')}" if conf else ""),
        })
        risk = setup.risk or {}
        conditions.append({
            "name": "Risk gate",
            "status": ("PASS" if risk.get("ok") and not setup.signal_blocked
                       else "FAIL" if setup.signal_blocked else "PENDING"),
            "detail": setup.signal_blocked or (
                f"R:R {risk.get('rr')}" if risk else ""),
        })
        return conditions

    def _advice(self, symbol: str, setup: Optional[Setup], profile: dict,
                feed: dict, stale: bool, price) -> str:
        asset = _asset(symbol) if symbol else "This market"
        if not feed.get("live"):
            return (f"{asset} is not being monitored right now: Binance is "
                    f"disconnected. No signal and no invented data.")
        if stale:
            return (f"{asset} data is stale; no signal is produced from stale "
                    f"market data.")
        if not setup:
            zone_low = _num(profile.get("zone_low"))
            zone_high = _num(profile.get("zone_high"))
            if zone_low is None or zone_high is None:
                return (f"{asset} has no SSL zone configured, so no sweep can "
                        f"be detected for it. Levels are never invented — set a "
                        f"zone (or auto-zone) to begin monitoring.")
            zone = f"{zone_low:,.2f}–{zone_high:,.2f}"
            if self._observations.get(symbol, {}).get("zone_entered"):
                return (f"{asset} is inside the configured SSL zone ({zone}). "
                        f"No sweep below the recent swing low yet. No long "
                        f"signal. Waiting.")
            return (f"{asset} is being monitored for a liquidity sweep below "
                    f"the {zone} zone. No sweep detected yet. No long signal.")
        state = setup.state
        if state == SWEEPING:
            return (f"{asset} is currently in the liquidity sweep monitoring "
                    f"stage. Price has swept liquidity below the configured "
                    f"support zone. Waiting for a bullish rejection candle, "
                    f"volume confirmation and positive buying pressure. "
                    f"No long signal.")
        if state == REJECTION:
            return (f"{asset}: a rejection candle was detected after the "
                    f"sweep. Volume confirmation is still pending. "
                    f"No long signal.")
        if state == VOLUME:
            return (f"{asset}: volume confirmation passed "
                    f"({((setup.volume or {}).get('multiplier') or 0):.1f}x). "
                    f"Waiting for positive buying pressure / delta "
                    f"confirmation. No long signal.")
        if state in (DELTA, CONFIRMATION_PENDING):
            return (f"{asset}: buying pressure confirmed. Waiting for the "
                    f"next closed candle to confirm higher price action. "
                    f"No long signal yet.")
        if state == CONFIRMED:
            return (f"{asset}: reversal confirmed, but the risk gate is "
                    f"blocked ({setup.signal_blocked or 'pending'}). "
                    f"No long signal.")
        if state == LONG:
            risk = setup.risk or {}
            return (f"{asset} long signal ready (informational only): entry "
                    f"{risk.get('entry_low')}–{risk.get('entry_high')}, stop "
                    f"{risk.get('stop_loss')}, target {risk.get('target')}, "
                    f"R:R {risk.get('rr')}. No order was placed. Awaiting "
                    f"your authorization.")
        return f"{asset}: no long signal."

    def voice_answer(self, symbol) -> str:
        return self.get_status(symbol).get("advice", "No data.")

    def get_conditions(self, symbol) -> list:
        return self.get_status(symbol).get("conditions", [])

    def get_history(self, symbol, limit: int = 20) -> list:
        sym = _pair(symbol)
        limit = max(1, min(int(limit), 200))
        with self._lock:
            rows = []
            for r in self._conn.execute(
                    "SELECT setup_id, interval, sweep_ts, sweep_price,"
                    " prev_swing_low, depth_pct, below_zone, recovery_price"
                    " FROM liquidity_sweeps WHERE symbol=? "
                    "ORDER BY sweep_ts DESC LIMIT ?", (sym, limit)).fetchall():
                rows.append({
                    "kind": "sweep", "setup_id": r[0], "interval": r[1],
                    "ts": r[2], "time": utc_iso(r[2]), "sweep_price": r[3],
                    "prev_swing_low": r[4], "depth_pct": r[5],
                    "below_zone": bool(r[6]), "recovery_price": r[7],
                })
            for r in self._conn.execute(
                    "SELECT setup_id, interval, status, state, entry_low,"
                    " entry_high, stop_loss, target, rr, blocked_reason,"
                    " updated_at FROM reversal_signals WHERE symbol=? "
                    "ORDER BY updated_at DESC LIMIT ?", (sym, limit)).fetchall():
                rows.append({
                    "kind": "signal", "setup_id": r[0], "interval": r[1],
                    "status": r[2], "state": r[3], "entry_low": r[4],
                    "entry_high": r[5], "stop_loss": r[6], "target": r[7],
                    "rr": r[8], "blocked_reason": r[9], "time": r[10],
                })
            for r in self._conn.execute(
                    "SELECT setup_id, interval, outcome, reason, ts"
                    " FROM setup_outcomes WHERE symbol=? ORDER BY ts DESC"
                    " LIMIT ?", (sym, limit)).fetchall():
                rows.append({
                    "kind": "outcome", "setup_id": r[0], "interval": r[1],
                    "outcome": r[2], "reason": r[3], "ts": r[4],
                    "time": utc_iso(r[4]),
                })
        rows.sort(key=lambda x: x.get("ts") or 0, reverse=True)
        return rows[:limit]

    def set_zone(self, symbol, low, high) -> dict:
        sym = _pair(symbol)
        low, high = _num(low), _num(high)
        if low is None or high is None or low <= 0 or high <= 0 or low >= high:
            raise ValueError("zone requires 0 < low < high")
        with self._lock:
            profile = self._profiles.get(sym)
            if profile is None:
                raise ValueError(f"no profile for {sym}")
            active = self._setups.get(sym)
            if active:
                self._close_setup(active, "invalidated", "zone_updated",
                                  self._now_ms())
            profile["zone_low"] = float(low)
            profile["zone_high"] = float(high)
            self._config["profiles"][sym]["zone_low"] = float(low)
            self._config["profiles"][sym]["zone_high"] = float(high)
            _save_config(self.config_path, self._config)
            return self._snapshot_locked(sym, profile)

    def set_alerts(self, symbol, enabled: bool) -> dict:
        sym = _pair(symbol)
        with self._lock:
            profile = self._profiles.get(sym)
            if profile is None:
                raise ValueError(f"no profile for {sym}")
            profile["alerts_enabled"] = bool(enabled)
            self._config["profiles"][sym]["alerts_enabled"] = bool(enabled)
            _save_config(self.config_path, self._config)
            return self._snapshot_locked(sym, profile)

    def auto_set_zone(self, symbol, persist: bool = True) -> dict:
        sym = _pair(symbol)
        with self._lock:
            profile = self._profiles.get(sym)
            if profile is None:
                raise ValueError(f"no profile for {sym}")
            hist = self._candles.get((sym, self._signal_interval)) or []
            before = hist[-(self._swing_lookback + 1):-1] or hist
            if not before:
                raise ValueError("no closed candles to derive a zone")
            swing_low = min(c["low"] for c in before)
            width = max(0.01, _num(self._config["globals"].get("zone_width"), 50.0))
            low, high = swing_low, swing_low + width
            profile["zone_low"] = low
            profile["zone_high"] = high
            if persist:
                self._config["profiles"][sym]["zone_low"] = low
                self._config["profiles"][sym]["zone_high"] = high
                _save_config(self.config_path, self._config)
            return self._snapshot_locked(sym, profile)

    def dashboard_state(self) -> dict:
        with self._lock:
            feed = self._feed_state()
            return {
                "binance": feed,
                "updated_at": utc_iso(self._now_ms()),
                "symbols": {
                    sym: self._snapshot_locked(sym, profile)
                    for sym, profile in self._profiles.items()
                    if profile.get("enabled")
                },
            }


# ── singleton for Jarvis actions / dashboard ─────────────────────────────────
_engine: Optional[LiquiditySweepEngine] = None
_engine_lock = threading.Lock()


def get_engine() -> LiquiditySweepEngine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = LiquiditySweepEngine()
        return _engine


def reset_engine() -> None:
    """Test/teardown helper: close and forget the singleton."""
    global _engine
    with _engine_lock:
        if _engine is not None:
            try:
                _engine.close()
            except Exception:
                pass
        _engine = None


__all__ = [
    "LiquiditySweepEngine", "Setup", "get_engine", "reset_engine",
    "SWEEPING", "REJECTION", "VOLUME", "DELTA", "CONFIRMATION_PENDING",
    "CONFIRMED", "LONG", "STATES", "DEFAULT_CONFIG", "DEFAULT_CONFIG_PATH",
]
