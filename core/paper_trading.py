"""
core/paper_trading.py — RIO autonomous PAPER trading engine.

PAPER ONLY. This module has no broker import, no Binance order endpoint and no
real-money path. It consumes the existing liquidity engine's LONG_SIGNAL_READY
event and the existing Binance daemon's closed-candle hook.

Plumbing:

    Binance (existing daemon)
        -> closed candles -> LiquiditySweepEngine (existing strategy)
        -> LONG_SIGNAL_READY / SETUP_CLOSED events
        -> PaperTradingEngine (this module)
        -> SQLite virtual account + Hermes notifications

Execution is virtual: the entry uses the first real ticker quote available
after confirmation (never an earlier candle close), with configured slippage
and fees. SL/TP exits use real 1-minute candle highs/lows; if both are inside
the same candle the stop is assumed first (per the brief). No price is ever
invented.
"""
from __future__ import annotations

import copy
import json
import math
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from core import binance_ws as bw

UTC = timezone.utc
DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "paper_trading.json"
)
_REQUIRED = (
    "enabled", "initial_balance", "quote_currency", "default_risk_percent",
    "maximum_open_positions", "maximum_daily_trades",
    "maximum_daily_loss_percent", "maximum_total_exposure_percent",
    "maximum_position_percent", "simulated_fee_percent", "slippage_model",
    "stop_loss_buffer", "default_reward_risk_ratio", "allowed_symbols",
    "position_size_mode", "min_notional", "symbol_rules",
    "execution_price_max_age_s", "stale_after_s", "data_unreliable_action",
    "daily_report_enabled", "daily_report_hour_utc",
)

DEFAULT_CONFIG = {
    "version": 1,
    "enabled": True,
    "initial_balance": 10000.0,
    "quote_currency": "USDT",
    "default_risk_percent": 1.0,
    "maximum_open_positions": 2,
    "maximum_daily_trades": 5,
    "maximum_daily_loss_percent": 3.0,
    "maximum_total_exposure_percent": 50.0,
    "maximum_position_percent": 25.0,
    "simulated_fee_percent": 0.1,
    "slippage_model": {"type": "percent", "value": 0.05},
    "slippage_percent": 0.05,
    "stop_loss_buffer": 0.0,
    "default_reward_risk_ratio": 2.0,
    "quantity_mode": "AUTO",
    "fixed_quantity": 0.01,
    "leverage": 1.0,
    "allowed_symbols": ["BTCUSDT", "ETHUSDT"],
    "position_size_mode": "risk_percent",
    "min_notional": 10.0,
    "symbol_rules": {
        "BTCUSDT": {"min_quantity": 0.00001, "quantity_precision": 5},
        "ETHUSDT": {"min_quantity": 0.0001, "quantity_precision": 4},
    },
    "execution_price_max_age_s": 10.0,
    "stale_after_s": 180.0,
    "data_unreliable_action": "suspend",
    "daily_report_enabled": True,
    "daily_report_hour_utc": 0,
}


class PaperConfigError(ValueError):
    """Invalid or incomplete paper-trading configuration."""


# Public setting registry used by the Telegram/voice commands. Values are
# persisted in config/paper_trading.json; every change is audited in SQLite.
SETTING_SPECS = {
    "starting_capital": {
        "label": "Starting virtual capital", "config_key": "initial_balance",
        "kind": "float", "default": 10000.0, "min": 100.0,
        "max": 100_000_000.0, "suffix": " USDT",
        "applies": "New paper accounts only; current balance/equity/P&L "
                   "are not changed."},
    "quantity_mode": {
        "label": "Quantity mode", "config_key": "quantity_mode",
        "kind": "enum", "choices": ["AUTO", "FIXED"], "default": "AUTO",
        "min": None, "max": None, "suffix": "",
        "applies": "New trades only."},
    "fixed_quantity": {
        "label": "Fixed quantity", "config_key": "fixed_quantity",
        "kind": "float", "default": 0.01, "min": 0.00000001,
        "max": 1_000_000.0, "suffix": "",
        "applies": "New trades only (FIXED mode)."},
    "leverage": {
        "label": "Simulated leverage", "config_key": "leverage",
        "kind": "float", "default": 1.0, "min": 1.0, "max": 20.0,
        "suffix": "x",
        "applies": "New trades only; existing positions keep their leverage."},
    "risk_percent": {
        "label": "Risk per trade", "config_key": "default_risk_percent",
        "kind": "float", "default": 1.0, "min": 0.1, "max": 10.0,
        "suffix": "%", "applies": "New trades only."},
    "reward_risk": {
        "label": "Risk/reward", "config_key": "default_reward_risk_ratio",
        "kind": "float", "default": 2.0, "min": 0.1, "max": 10.0,
        "suffix": " (1:x)", "applies": "New trades only."},
    "max_daily_trades": {
        "label": "Maximum trades/day", "config_key": "maximum_daily_trades",
        "kind": "int", "default": 5, "min": 1, "max": 100, "suffix": "",
        "applies": "New entries only."},
    "max_open_positions": {
        "label": "Maximum open positions",
        "config_key": "maximum_open_positions", "kind": "int",
        "default": 2, "min": 1, "max": 20, "suffix": "",
        "applies": "New entries only."},
    "max_daily_loss_percent": {
        "label": "Maximum daily loss", "config_key": "maximum_daily_loss_percent",
        "kind": "float", "default": 3.0, "min": 0.1, "max": 100.0,
        "suffix": "%", "applies": "New entries only."},
    "max_exposure_percent": {
        "label": "Maximum exposure",
        "config_key": "maximum_total_exposure_percent", "kind": "float",
        "default": 50.0, "min": 1.0, "max": 100.0, "suffix": "%",
        "applies": "New entries only."},
    "fee_percent": {
        "label": "Simulated fee", "config_key": "simulated_fee_percent",
        "kind": "float", "default": 0.1, "min": 0.0, "max": 5.0,
        "suffix": "%", "applies": "New trades only; existing positions keep "
                                  "their original fee."},
    "slippage_percent": {
        "label": "Simulated slippage", "config_key": "slippage_percent",
        "kind": "float", "default": 0.05, "min": 0.0, "max": 5.0,
        "suffix": "%", "applies": "New trades only; existing positions keep "
                                  "their original slippage."},
}

SETTING_ALIASES = {
    "capital": "starting_capital", "balance": "starting_capital",
    "quantity": "fixed_quantity", "qty": "fixed_quantity",
    "rr": "reward_risk", "risk_reward": "reward_risk",
    "risk": "risk_percent", "max_trades": "max_daily_trades",
    "max_positions": "max_open_positions", "daily_loss": "max_daily_loss_percent",
    "max_exposure": "max_exposure_percent", "exposure": "max_exposure_percent",
    "fee": "fee_percent", "slippage": "slippage_percent",
}


def _parse_settings_value(key: str, raw) -> object:
    spec = SETTING_SPECS.get(key)
    if not spec:
        raise ValueError(f"unknown setting {key!r}")
    if spec["kind"] == "enum":
        value = str(raw).strip().upper()
        if value not in spec["choices"]:
            raise ValueError(
                f"{spec['label']} must be one of {', '.join(spec['choices'])}")
        return value
    if key == "reward_risk":
        text = str(raw).strip().lower()
        if ":" in text:
            parts = text.split(":")
            if len(parts) != 2:
                raise ValueError("risk/reward must look like 1:2 or 2")
            text = parts[1]
        try:
            value = float(text)
        except ValueError:
            raise ValueError("risk/reward must look like 1:2 or 2") from None
    elif spec["kind"] == "int":
        try:
            number = float(str(raw).strip())
        except ValueError:
            raise ValueError(f"{spec['label']} must be a whole number") from None
        if not number.is_integer():
            raise ValueError(f"{spec['label']} must be a whole number")
        value = int(number)
    else:
        try:
            value = float(str(raw).strip())
        except ValueError:
            raise ValueError(f"{spec['label']} must be a number") from None
    if not math.isfinite(float(value)):
        raise ValueError(f"{spec['label']} must be a finite number")
    lo, hi = spec.get("min"), spec.get("max")
    if lo is not None and value < lo:
        raise ValueError(f"{spec['label']} must be ≥ {format_setting_value(key, lo)}")
    if hi is not None and value > hi:
        raise ValueError(f"{spec['label']} must be ≤ {format_setting_value(key, hi)}")
    return value


def format_setting_value(key: str, value) -> str:
    spec = SETTING_SPECS.get(key) or {}
    if key == "quantity_mode":
        return str(value)
    if key == "starting_capital":
        return f"{float(value):,.2f} USDT"
    if key in ("risk_percent", "max_daily_loss_percent",
               "fee_percent", "slippage_percent"):
        return f"{float(value):g}%"
    if key == "max_exposure_percent":
        return f"{float(value):g}%"
    if key == "leverage":
        return f"{float(value):g}x"
    if key == "reward_risk":
        return f"1:{float(value):g}"
    if spec.get("kind") == "int":
        return str(int(value))
    return f"{float(value):g}"


# ── helpers ─────────────────────────────────────────────────────────────────
def utc_iso(ms: Optional[int]) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(int(ms) / 1000, tz=UTC).isoformat()


def _num(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _round_down(value: float, precision: int) -> float:
    factor = 10 ** int(precision)
    return math.floor(float(value) * factor) / factor


def _load_config(path: Path) -> dict:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n",
                       encoding="utf-8")
        tmp.replace(path)
        print(f"[Paper] created default config at {path}", flush=True)
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PaperConfigError(f"cannot read {path}: {exc}") from exc
    missing = [k for k in _REQUIRED if k not in config]
    if missing:
        raise PaperConfigError(
            "missing required paper-trading values: " + ", ".join(missing))
    # Migration for configs created before the Telegram settings feature.
    # An older config keeps the exact slippage it had (derived from the model).
    if "slippage_percent" not in config:
        model = config.get("slippage_model") or {}
        value = _num(model.get("value"), 0.0) or 0.0
        if model.get("type") == "fixed_bps":
            value = value / 100.0
        elif model.get("type") != "percent":
            value = 0.0
        config["slippage_percent"] = value
    for key, default in (("quantity_mode", "AUTO"),
                         ("fixed_quantity", 0.01),
                         ("leverage", 1.0)):
        config.setdefault(key, default)
    _validate_config(config)
    return config


def _validate_config(c: dict) -> None:
    def _positive(key, allow_zero=False):
        v = _num(c.get(key))
        if v is None or (v < 0 if allow_zero else v <= 0):
            raise PaperConfigError(f"{key} must be "
                                   f"{'>= 0' if allow_zero else '> 0'}")

    if not isinstance(c.get("enabled"), bool):
        raise PaperConfigError("enabled must be a boolean")
    _positive("initial_balance")
    if not isinstance(c.get("quote_currency"), str) or not c["quote_currency"].strip():
        raise PaperConfigError("quote_currency must be a non-empty string")
    _positive("default_risk_percent")
    if float(c["default_risk_percent"]) > 100:
        raise PaperConfigError("default_risk_percent must be <= 100")
    if not isinstance(c.get("maximum_open_positions"), int) or c["maximum_open_positions"] < 1:
        raise PaperConfigError("maximum_open_positions must be an integer >= 1")
    if not isinstance(c.get("maximum_daily_trades"), int) or c["maximum_daily_trades"] < 0:
        raise PaperConfigError("maximum_daily_trades must be an integer >= 0")
    _positive("maximum_daily_loss_percent")
    _positive("maximum_total_exposure_percent")
    if float(c["maximum_total_exposure_percent"]) > 100:
        raise PaperConfigError("maximum_total_exposure_percent must be <= 100")
    _positive("maximum_position_percent")
    if float(c["maximum_position_percent"]) > 100:
        raise PaperConfigError("maximum_position_percent must be <= 100")
    _positive("simulated_fee_percent", allow_zero=True)
    model = c.get("slippage_model")
    if not isinstance(model, dict) or model.get("type") not in (
            "fixed_bps", "percent", "none"):
        raise PaperConfigError(
            "slippage_model.type must be fixed_bps, percent or none")
    if model.get("type") != "none" and _num(model.get("value"), -1) < 0:
        raise PaperConfigError("slippage_model.value must be >= 0")
    _positive("stop_loss_buffer", allow_zero=True)
    _positive("default_reward_risk_ratio")
    symbols = c.get("allowed_symbols")
    if not isinstance(symbols, list) or not symbols:
        raise PaperConfigError("allowed_symbols must be a non-empty list")
    if c.get("position_size_mode") != "risk_percent":
        raise PaperConfigError(
            "position_size_mode: only 'risk_percent' is supported")
    if str(c.get("quantity_mode") or "AUTO").upper() not in ("AUTO", "FIXED"):
        raise PaperConfigError("quantity_mode must be AUTO or FIXED")
    _positive("fixed_quantity")
    leverage = _num(c.get("leverage"))
    if leverage is None or not 1.0 <= leverage <= 20.0:
        raise PaperConfigError("leverage must be between 1 and 20")
    slippage = _num(c.get("slippage_percent"))
    if slippage is None or not 0.0 <= slippage <= 5.0:
        raise PaperConfigError("slippage_percent must be between 0 and 5")
    _positive("min_notional")
    rules = c.get("symbol_rules")
    if not isinstance(rules, dict):
        raise PaperConfigError("symbol_rules must be an object")
    for sym in symbols:
        rule = rules.get(str(sym).upper())
        if not isinstance(rule, dict):
            raise PaperConfigError(f"symbol_rules missing for {sym}")
        if _num(rule.get("min_quantity"), 0) <= 0:
            raise PaperConfigError(f"{sym} min_quantity must be > 0")
        if not isinstance(rule.get("quantity_precision"), int) or rule["quantity_precision"] < 0:
            raise PaperConfigError(f"{sym} quantity_precision must be an integer >= 0")
    _positive("execution_price_max_age_s")
    _positive("stale_after_s")
    if c.get("data_unreliable_action") not in ("suspend", "close"):
        raise PaperConfigError(
            "data_unreliable_action must be 'suspend' or 'close'")
    if not isinstance(c.get("daily_report_enabled"), bool):
        raise PaperConfigError("daily_report_enabled must be a boolean")
    if not isinstance(c.get("daily_report_hour_utc"), int) or not 0 <= c["daily_report_hour_utc"] <= 23:
        raise PaperConfigError("daily_report_hour_utc must be 0..23")


def _save_config(path: Path, config: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


# ── engine ──────────────────────────────────────────────────────────────────
class PaperTradingEngine:
    """One virtual account, one strategy feed, no real orders."""

    def __init__(
        self,
        config_path: Optional[str | Path] = None,
        db_path: Optional[str | Path] = None,
        *,
        alert_sender: Optional[Callable[[str], bool]] = None,
        price_provider: Optional[Callable[[str], Optional[dict]]] = None,
        feed_provider: Optional[Callable[[], dict]] = None,
        strategy_provider: Optional[Callable[[str], dict]] = None,
        liquidity_engine=None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.config_path = Path(config_path or DEFAULT_CONFIG_PATH)
        self.db_path = Path(db_path or bw.database_path())
        self._config = _load_config(self.config_path)
        self._alert_sender = alert_sender or bw.send_telegram
        self._price_provider = price_provider or self._default_price
        self._feed_provider = feed_provider or bw.status
        self._strategy_provider = strategy_provider
        self._clock = clock or time.time
        self._liquidity_engine = liquidity_engine
        self._lock = threading.RLock()
        self._started = False
        self._accepting = bool(self._config.get("enabled"))
        self._subscribed = False
        self._last_candle: dict | None = None
        self._stop_event = threading.Event()
        self._daily_thread: Optional[threading.Thread] = None
        self._daily_last_key = ""
        self._on_signal_cb = self.on_signal
        self._on_candle_cb = self.on_closed_candle

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_schema()
        self._ensure_account()

    # ── db / account ────────────────────────────────────────────────────
    def _init_schema(self) -> None:
        self._conn.executescript("""
        CREATE TABLE IF NOT EXISTS paper_accounts (
            account_id TEXT PRIMARY KEY,
            status TEXT DEFAULT 'active',
            initial_balance REAL,
            cash REAL,
            realized_pnl REAL DEFAULT 0,
            peak_equity REAL,
            max_drawdown REAL DEFAULT 0,
            last_equity REAL,
            quote_currency TEXT,
            created_at TEXT,
            updated_at TEXT,
            archived_at TEXT
        );
        CREATE TABLE IF NOT EXISTS paper_orders (
            order_id TEXT PRIMARY KEY,
            account_id TEXT, signal_id TEXT, symbol TEXT, side TEXT,
            order_type TEXT, quantity REAL, requested_price REAL,
            fill_price REAL, slippage REAL, fee REAL, status TEXT, reason TEXT,
            quote_ts INTEGER, price_source TEXT,
            created_at TEXT, filled_at TEXT,
            UNIQUE(account_id, signal_id, side)
        );
        CREATE TABLE IF NOT EXISTS paper_positions (
            position_id TEXT PRIMARY KEY,
            account_id TEXT, signal_id TEXT UNIQUE, symbol TEXT, direction TEXT,
            quantity REAL, entry_price REAL, entry_fee REAL, entry_slippage REAL,
            stop_loss REAL, take_profit REAL, opened_at TEXT, opened_ts INTEGER,
            status TEXT DEFAULT 'OPEN', closed_at TEXT, exit_price REAL,
            exit_fee REAL, exit_slippage REAL, exit_reason TEXT,
            gross_pnl REAL, net_pnl REAL, fees_total REAL, risk_amount REAL,
            rr REAL, strategy_interval TEXT, last_price REAL, last_price_ts INTEGER,
            unrealized_pnl REAL, ambiguous_candle INTEGER DEFAULT 0,
            entry_quote_ts INTEGER, entry_price_source TEXT,
            exit_price_source TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS paper_trades (
            trade_id TEXT PRIMARY KEY, position_id TEXT UNIQUE,
            account_id TEXT, signal_id TEXT, symbol TEXT, direction TEXT,
            quantity REAL, entry_price REAL, exit_price REAL,
            gross_pnl REAL, entry_fee REAL, exit_fee REAL, fees_total REAL,
            net_pnl REAL, entry_slippage REAL, exit_slippage REAL,
            entry_at TEXT, exit_at TEXT, duration_s REAL, exit_reason TEXT,
            exit_price_source TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS paper_transactions (
            tx_id TEXT PRIMARY KEY, account_id TEXT, type TEXT,
            amount REAL, balance_after REAL, position_id TEXT, detail TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS paper_daily_stats (
            account_id TEXT, day TEXT, trades INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0,
            gross_profit REAL DEFAULT 0, gross_loss REAL DEFAULT 0,
            fees REAL DEFAULT 0, net_pnl REAL DEFAULT 0,
            equity_start REAL, equity_end REAL, peak_equity REAL,
            max_drawdown REAL DEFAULT 0,
            PRIMARY KEY(account_id, day)
        );
        CREATE TABLE IF NOT EXISTS paper_execution_events (
            event_id TEXT PRIMARY KEY, account_id TEXT, signal_id TEXT,
            event_type TEXT, detail_json TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS paper_strategy_signals (
            signal_id TEXT PRIMARY KEY, account_id TEXT, symbol TEXT,
            interval TEXT, state TEXT, payload_json TEXT,
            processed INTEGER DEFAULT 0, outcome TEXT, reason TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS paper_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT, kind TEXT, dedupe_key TEXT, message TEXT,
            delivered INTEGER DEFAULT 0, created_at TEXT,
            UNIQUE(account_id, kind, dedupe_key)
        );
        CREATE TABLE IF NOT EXISTS paper_settings_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT, setting TEXT, old_value TEXT, new_value TEXT,
            actor TEXT, source TEXT, created_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_paper_pos_account
            ON paper_positions(account_id, status);
        CREATE INDEX IF NOT EXISTS idx_paper_trade_account
            ON paper_trades(account_id, exit_at);
        CREATE INDEX IF NOT EXISTS idx_paper_daily_account
            ON paper_daily_stats(account_id, day);
        CREATE INDEX IF NOT EXISTS idx_paper_settings_history
            ON paper_settings_history(account_id, created_at);
        """)
        # In-place migration for accounts created before the settings feature.
        # Existing rows keep NULL for these columns: no historical trade is
        # rewritten and no open position is altered.
        self._migrate_columns("paper_positions", {
            "notional": "REAL", "margin": "REAL", "leverage": "REAL",
            "entry_settings_json": "TEXT", "quantity_mode": "TEXT",
            "risk_at_entry_pct": "REAL", "fee_at_entry_pct": "REAL",
            "slippage_at_entry_pct": "REAL", "liquidation_price": "REAL",
        })
        self._migrate_columns("paper_accounts", {
            "starting_capital": "REAL",
        })
        self._migrate_columns("paper_orders", {
            "leverage": "REAL", "margin": "REAL", "settings_json": "TEXT",
        })
        self._migrate_columns("paper_trades", {
            "leverage": "REAL", "settings_json": "TEXT",
        })
        self._conn.execute(
            "UPDATE paper_accounts SET starting_capital=initial_balance"
            " WHERE starting_capital IS NULL")
        self._conn.commit()

    def _migrate_columns(self, table: str, columns: dict) -> None:
        existing = {row[1] for row in
                    self._conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in existing:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def _ensure_account(self) -> None:
        row = self._conn.execute(
            "SELECT account_id, initial_balance, cash, realized_pnl,"
            " peak_equity, max_drawdown, quote_currency"
            " FROM paper_accounts WHERE status='active'"
            " ORDER BY created_at DESC LIMIT 1").fetchone()
        if row:
            self._account_id = row[0]
            return
        account_id = f"paper-{uuid.uuid4().hex[:12]}"
        now = utc_iso(self._now_ms())
        initial = float(self._config["initial_balance"])
        self._conn.execute(
            "INSERT INTO paper_accounts"
            " (account_id, status, initial_balance, starting_capital, cash,"
            "  realized_pnl, peak_equity, max_drawdown, last_equity,"
            "  quote_currency, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (account_id, "active", initial, initial, initial, 0.0, initial,
             0.0, initial, self._config["quote_currency"], now, now),
        )
        self._conn.execute(
            "INSERT INTO paper_transactions"
            " (tx_id, account_id, type, amount, balance_after, position_id,"
            "  detail, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (f"tx-{uuid.uuid4().hex[:12]}", account_id, "INITIAL_DEPOSIT",
             initial, initial, "", "paper account opening balance", now),
        )
        self._conn.commit()
        self._account_id = account_id

    def _account(self) -> dict:
        row = self._conn.execute(
            "SELECT * FROM paper_accounts WHERE account_id=?",
            (self._account_id,)).fetchone()
        if not row:
            raise RuntimeError("paper account missing")
        cols = [d[0] for d in self._conn.execute(
            "SELECT * FROM paper_accounts LIMIT 0").description]
        return dict(zip(cols, row))

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    def _day(self, ms: Optional[int] = None) -> str:
        ms = self._now_ms() if ms is None else int(ms)
        return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d")

    def _default_price(self, symbol: str) -> Optional[dict]:
        tick = bw.latest_prices().get(str(symbol).lower(), {})
        price = _num(tick.get("price"))
        if price is None:
            return None
        return {"price": price, "ts": tick.get("ts"),
                "source": "binance_ticker"}

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
                "source": str(raw.get("source") or "ticker")}

    def _feed(self) -> dict:
        try:
            raw = self._feed_provider() or {}
        except Exception as exc:
            return {"live": False, "transport": "none", "stale": True,
                    "last_tick_age_s": None,
                    "reason": f"status_error:{type(exc).__name__}"}
        transport = str(raw.get("transport") or "none")
        live = bool(raw.get("live")) and transport in ("ws", "http")
        age = raw.get("last_tick_age_s")
        try:
            age = float(age) if age is not None else None
        except (TypeError, ValueError):
            age = None
        stale = age is None or age > float(self._config["stale_after_s"])
        return {"live": live, "transport": transport,
                "last_tick_age_s": age, "stale": stale,
                "reason": str(raw.get("reason") or "")}

    def _slippage_percent(self) -> float:
        value = _num(self._config.get("slippage_percent"))
        if value is not None:
            return max(0.0, float(value))
        model = self._config.get("slippage_model") or {}
        value = _num(model.get("value"), 0.0) or 0.0
        if model.get("type") == "fixed_bps":
            return value / 100.0
        if model.get("type") == "percent":
            return value
        return 0.0

    def _slippage_fraction(self) -> float:
        return self._slippage_percent() / 100.0

    def _fee_percent(self) -> float:
        return max(0.0, float(self._config["simulated_fee_percent"]))

    def _fee_fraction(self) -> float:
        return self._fee_percent() / 100.0

    def _leverage(self) -> float:
        return max(1.0, min(20.0, _num(self._config.get("leverage"), 1.0) or 1.0))

    def _quantity_mode(self) -> str:
        return str(self._config.get("quantity_mode") or "AUTO").upper()

    def _fixed_quantity(self) -> float:
        return _num(self._config.get("fixed_quantity"), 0.0) or 0.0

    # ── marks / equity ─────────────────────────────────────────────────
    def _open_positions(self, conn=None) -> list:
        conn = conn or self._conn
        rows = conn.execute(
            "SELECT * FROM paper_positions WHERE account_id=? AND status='OPEN'"
            " ORDER BY opened_ts", (self._account_id,)).fetchall()
        cols = [d[0] for d in conn.execute(
            "SELECT * FROM paper_positions LIMIT 0").description]
        return [dict(zip(cols, r)) for r in rows]

    def _position_marks(self, positions: list) -> dict:
        marks = {}
        for pos in positions:
            quote = self._quote(pos["symbol"])
            if quote:
                marks[pos["position_id"]] = {
                    "price": quote["price"], "source": quote["source"],
                    "ts": quote.get("ts")}
            else:
                last = _num(pos.get("last_price"))
                marks[pos["position_id"]] = {
                    "price": last if last is not None else _num(pos["entry_price"]),
                    "source": "last_known" if last is not None else "entry_price",
                    "ts": pos.get("last_price_ts")}
        return marks

    def _equity_state(self, conn=None) -> dict:
        conn = conn or self._conn
        account = self._account()
        positions = self._open_positions(conn)
        marks = self._position_marks(positions)
        exposure = 0.0
        unrealized = 0.0
        for pos in positions:
            mark = marks[pos["position_id"]]["price"]
            exposure += pos["quantity"] * mark
            unrealized += (mark - pos["entry_price"]) * pos["quantity"]
        cash = _num(account["cash"], 0.0)
        # Cash already excludes posted margin, so equity = cash + position
        # value is correct at any leverage without double-counting margin.
        equity = cash + exposure
        used_margin = sum(_num(p.get("margin"), 0.0) or 0.0 for p in positions)
        return {"cash": cash, "exposure": exposure, "equity": equity,
                "unrealized": unrealized, "used_margin": used_margin,
                "positions": positions, "marks": marks}

    def _update_account_marks(self, conn, state: dict) -> None:
        peak = max(_num(self._account().get("peak_equity"), 0.0), state["equity"])
        dd = max(_num(self._account().get("max_drawdown"), 0.0),
                 peak - state["equity"])
        conn.execute(
            "UPDATE paper_accounts SET peak_equity=?, max_drawdown=?,"
            " last_equity=?, updated_at=? WHERE account_id=?",
            (peak, dd, state["equity"], utc_iso(self._now_ms()),
             self._account_id))

    def _daily_row(self, conn, day: str) -> dict | None:
        row = conn.execute(
            "SELECT * FROM paper_daily_stats WHERE account_id=? AND day=?",
            (self._account_id, day)).fetchone()
        if not row:
            return None
        cols = [d[0] for d in conn.execute(
            "SELECT * FROM paper_daily_stats LIMIT 0").description]
        return dict(zip(cols, row))

    def _ensure_daily(self, conn, day: str, equity: float) -> dict:
        conn.execute(
            "INSERT OR IGNORE INTO paper_daily_stats"
            " (account_id, day, equity_start, equity_end, peak_equity,"
            "  max_drawdown) VALUES (?,?,?,?,?,0)",
            (self._account_id, day, equity, equity, equity))
        row = self._daily_row(conn, day)
        if row is None:
            # Another connection (a second RIO instance or a test harness) may
            # hold the row; read again after the implicit commit.
            conn.commit()
            row = self._daily_row(conn, day)
        return row or {}

    def _update_daily_mark(self, conn, equity: float) -> None:
        day = self._day()
        row = self._ensure_daily(conn, day, equity)
        peak = max(_num(row.get("peak_equity"), equity), equity)
        dd = max(_num(row.get("max_drawdown"), 0.0), peak - equity)
        conn.execute(
            "UPDATE paper_daily_stats SET equity_end=?, peak_equity=?,"
            " max_drawdown=? WHERE account_id=? AND day=?",
            (equity, peak, dd, self._account_id, day))

    # ── settings (Telegram/voice) ───────────────────────────────────────
    def settings_snapshot(self) -> dict:
        """Every public setting as currently persisted."""
        out = {}
        for key, spec in SETTING_SPECS.items():
            config_key = spec["config_key"]
            raw = self._config.get(config_key)
            if key == "quantity_mode":
                value = str(raw or "AUTO").upper()
            elif spec["kind"] == "int":
                value = int(_num(raw, spec["default"]))
            else:
                value = float(_num(raw, spec["default"]))
            out[key] = value
        return out

    def get_setting(self, key: str) -> dict:
        key = SETTING_ALIASES.get(str(key).strip().lower(),
                                  str(key).strip().lower())
        spec = SETTING_SPECS.get(key)
        if spec is None:
            raise ValueError(f"unknown setting {key!r}")
        with self._lock:
            return {
                "key": key, "label": spec["label"],
                "value": self.settings_snapshot()[key],
                "default": spec["default"],
                "min": spec.get("min"), "max": spec.get("max"),
                "choices": spec.get("choices"),
                "applies": spec.get("applies", ""),
            }

    def update_setting(self, key: str, raw_value, *,
                       actor: str = "telegram", source: str = "command") -> dict:
        """Validate, persist and audit one setting. Raises ValueError."""
        resolved = SETTING_ALIASES.get(str(key).strip().lower(),
                                       str(key).strip().lower())
        spec = SETTING_SPECS.get(resolved)
        if spec is None:
            known = ", ".join(sorted(SETTING_SPECS))
            raise ValueError(f"unknown setting {key!r}. Available: {known}")
        value = _parse_settings_value(resolved, raw_value)
        config_key = spec["config_key"]
        with self._lock:
            current = self.settings_snapshot()[resolved]
            if current == value:
                return {"key": resolved, "changed": False,
                        "old": current, "new": value, "label": spec["label"]}
            previous_raw = self._config.get(config_key)
            self._config[config_key] = value
            if resolved == "slippage_percent":
                # Keep the legacy model field consistent for older readers.
                self._config["slippage_model"] = {
                    "type": "percent", "value": value}
            try:
                _validate_config(self._config)
            except PaperConfigError as exc:
                self._config[config_key] = previous_raw
                if resolved == "slippage_percent":
                    self._config["slippage_model"] = {
                        "type": "percent", "value": current}
                raise ValueError(str(exc)) from None
            try:
                _save_config(self.config_path, self._config)
            except OSError as exc:
                self._config[config_key] = previous_raw
                raise ValueError(f"could not persist setting: {exc}") from None
            if resolved == "starting_capital":
                # Metadata only: the configured opening capital is updated for
                # display and future accounts. Current cash/equity/P&L are
                # deliberately untouched.
                try:
                    self._conn.execute(
                        "UPDATE paper_accounts SET starting_capital=?,"
                        " updated_at=? WHERE account_id=?",
                        (value, utc_iso(self._now_ms()), self._account_id))
                    self._conn.commit()
                except sqlite3.Error as exc:
                    print(f"[Paper] starting capital update failed: {exc}",
                          flush=True)
            self._audit_setting(resolved, current, value, actor, source)
        return {"key": resolved, "changed": True, "old": current,
                "new": value, "label": spec["label"]}

    def _audit_setting(self, key: str, old, new, actor: str, source: str) -> None:
        try:
            self._conn.execute(
                "INSERT INTO paper_settings_history"
                " (account_id, setting, old_value, new_value, actor, source,"
                "  created_at) VALUES (?,?,?,?,?,?,?)",
                (self._account_id, key,
                 json.dumps(old, default=str), json.dumps(new, default=str),
                 str(actor or "unknown")[:120], str(source or "")[:40],
                 utc_iso(self._now_ms())),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            print(f"[Paper] settings audit failed: {exc}", flush=True)

    def settings_history(self, limit: int = 20) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT setting, old_value, new_value, actor, source, created_at"
                " FROM paper_settings_history WHERE account_id=?"
                " ORDER BY id DESC LIMIT ?",
                (self._account_id, max(1, min(int(limit), 200)))).fetchall()
        out = []
        for key, old, new, actor, source, created in rows:
            try:
                old_v, new_v = json.loads(old), json.loads(new)
            except (TypeError, ValueError):
                old_v, new_v = old, new
            out.append({"setting": key, "old": old_v, "new": new_v,
                        "actor": actor, "source": source, "at": created})
        return out

    def republish_config(self) -> None:
        """Rewrite config/paper_trading.json from the in-memory config."""
        with self._lock:
            _save_config(self.config_path, self._config)

    def reset_settings(self, *, actor: str = "telegram",
                       source: str = "command") -> dict:
        """Restore every public setting to its default (audited)."""
        changed = {}
        with self._lock:
            backup = dict(self._config)
            for key, spec in SETTING_SPECS.items():
                self._config[spec["config_key"]] = spec["default"]
            self._config["slippage_model"] = {
                "type": "percent", "value": SETTING_SPECS["slippage_percent"]["default"]}
            try:
                _validate_config(self._config)
                _save_config(self.config_path, self._config)
            except (PaperConfigError, OSError) as exc:
                self._config.clear()
                self._config.update(backup)
                raise ValueError(f"could not reset settings: {exc}") from None
            for key in SETTING_SPECS:
                before = backup.get(SETTING_SPECS[key]["config_key"])
                after = self._config.get(SETTING_SPECS[key]["config_key"])
                if before != after:
                    changed[key] = {"old": before, "new": after}
                    self._audit_setting(key, before, after, actor, source)
        return {"changed": changed, "settings": self.settings_snapshot()}

    # ── event validation / sizing ──────────────────────────────────────
    def _weekday(self) -> str:
        return self._day()

    def _signal_reject_reasons(self, event: dict) -> list:
        reasons = []
        symbol = str(event.get("symbol") or "").upper()
        if event.get("event") != "LONG_SIGNAL_READY":
            reasons.append("not_long_signal_ready")
        if str(event.get("state") or "") != "LONG_SIGNAL_READY":
            reasons.append("state_not_long_signal_ready")
        if symbol not in [str(s).upper() for s in self._config["allowed_symbols"]]:
            reasons.append("symbol_not_allowed")
        risk = event.get("risk") or {}
        if not risk.get("ok"):
            reasons.append("strategy_risk_gate_failed")
        rejection = event.get("rejection") or {}
        volume = event.get("volume") or {}
        delta = event.get("delta") or {}
        confirmation = event.get("confirmation") or {}
        if not rejection or not rejection.get("close_above_zone"):
            reasons.append("missing_rejection_confirmation")
        if not volume or volume.get("passed") is not True:
            reasons.append("volume_not_confirmed")
        if not delta or delta.get("available") is not True or delta.get("passed") is not True:
            reasons.append("positive_delta_not_confirmed")
        if not confirmation or confirmation.get("passed") is not True:
            reasons.append("next_candle_not_confirmed")
        return reasons

    def _daily_guard(self, conn, equity: float) -> tuple[bool, str, dict]:
        day = self._day()
        row = self._ensure_daily(conn, day, equity)
        # This guard runs before the entry transaction. Close the implicit
        # transaction opened by INSERT OR IGNORE; otherwise BEGIN IMMEDIATE in
        # _open_position fails with "cannot start a transaction within a
        # transaction". Callers inside an explicit transaction do not use this
        # guard.
        self._conn.commit()
        if int(row.get("trades") or 0) >= int(self._config["maximum_daily_trades"]):
            return False, "maximum_daily_trades", row
        limit = float(self._config["maximum_daily_loss_percent"]) / 100.0
        start = _num(row.get("equity_start"), equity) or equity
        open_today = [
            p for p in self._open_positions(conn)
            if str(p.get("opened_at") or "").startswith(day)
        ]
        marks = self._position_marks(open_today)
        unreal_today = sum(
            (marks[p["position_id"]]["price"] - p["entry_price"]) * p["quantity"]
            for p in open_today)
        day_loss = _num(row.get("net_pnl"), 0.0) + unreal_today
        if start > 0 and day_loss <= -limit * start:
            return False, "maximum_daily_loss", row
        return True, "", row

    def _position_size(self, event: dict, equity: float, cash: float,
                       exposure: float, entry: float, stop: float,
                       symbol: str) -> tuple[Optional[float], str]:
        """Quantity from AUTO (risk-based) or FIXED mode, after every cap.

        Leverage lowers the simulated margin requirement, never the loss
        between entry and stop: the risk-based quantity is still derived from
        the full price distance.
        """
        risk_amount = equity * float(self._config["default_risk_percent"]) / 100.0
        risk_per_unit = abs(entry - stop)
        if risk_per_unit <= 0 or risk_amount <= 0:
            return None, "invalid_risk_geometry"
        leverage = self._leverage()
        fee = self._fee_fraction()
        mode = self._quantity_mode()
        if mode == "FIXED":
            qty = self._fixed_quantity()
            if qty <= 0:
                return None, "fixed_quantity_invalid"
            if qty * risk_per_unit > risk_amount + 1e-9:
                return None, "fixed_quantity_exceeds_risk"
        else:
            qty = risk_amount / risk_per_unit

        # Caps are identical in both modes; a fixed quantity is never increased,
        # and an oversized fixed quantity is rejected rather than trimmed.
        # Position/exposure caps use NOTIONAL, so leverage cannot bypass them.
        margin_per_unit = entry / leverage
        cash_cap = cash / (margin_per_unit + entry * fee) if cash > 0 else 0.0
        pos_cap = (equity * float(self._config["maximum_position_percent"]) / 100.0
                   / entry)
        remaining_exposure = max(
            0.0,
            equity * float(self._config["maximum_total_exposure_percent"]) / 100.0
            - exposure)
        port_cap = remaining_exposure / entry
        cap = min(cash_cap, pos_cap, port_cap)
        if mode == "FIXED" and qty > cap:
            return None, "fixed_quantity_exceeds_limits"
        qty = min(qty, cap)
        precision = int(self._config["symbol_rules"][symbol]["quantity_precision"])
        qty = _round_down(qty, precision)
        min_qty = float(self._config["symbol_rules"][symbol]["min_quantity"])
        if qty < min_qty:
            return None, "below_min_quantity"
        if qty * entry < float(self._config["min_notional"]):
            return None, "below_min_notional"
        # Liquidation check: with leverage, the margin must survive the move to
        # the stop. (Simulated liquidation, never a real one.)
        margin = qty * margin_per_unit
        if (entry - stop) * qty >= margin:
            return None, "stop_beyond_liquidation"
        return qty, ""

    def _risk_levels(self, event: dict, entry: float) -> tuple[Optional[float], Optional[float], str]:
        risk = event.get("risk") or {}
        stop = _num(risk.get("stop_loss"))
        if stop is None or stop >= entry:
            return None, None, "strategy_stop_unavailable"
        buffer_pct = float(self._config["stop_loss_buffer"]) / 100.0
        stop = stop - entry * buffer_pct
        if stop <= 0 or stop >= entry:
            return None, None, "invalid_stop_loss"
        tp = entry + (entry - stop) * float(
            self._config["default_reward_risk_ratio"])
        return stop, tp, ""

    # ── notification / audit ────────────────────────────────────────────
    def _notify(self, kind: str, dedupe_key: str, text: str) -> bool:
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO paper_notifications"
            " (account_id, kind, dedupe_key, message, delivered, created_at)"
            " VALUES (?,?,?,?,0,?)",
            (self._account_id, kind, dedupe_key, text,
             utc_iso(self._now_ms())),
        )
        self._conn.commit()
        if cur.rowcount == 0:
            return False
        delivered = False
        try:
            delivered = bool(self._alert_sender(text))
        except Exception as exc:
            print(f"[Paper] notification failed: "
                  f"{type(exc).__name__}: {exc}", flush=True)
        self._conn.execute(
            "UPDATE paper_notifications SET delivered=? WHERE account_id=?"
            " AND kind=? AND dedupe_key=?",
            (int(delivered), self._account_id, kind, dedupe_key))
        self._conn.commit()
        return True

    def _event(self, event_type: str, signal_id: str, detail: dict) -> None:
        self._conn.execute(
            "INSERT INTO paper_execution_events"
            " (event_id, account_id, signal_id, event_type, detail_json,"
            "  created_at) VALUES (?,?,?,?,?,?)",
            (f"pe-{uuid.uuid4().hex[:12]}", self._account_id, signal_id,
             event_type, json.dumps(detail, default=str),
             utc_iso(self._now_ms())),
        )

    def _record_signal(self, event: dict, outcome: str, reason: str) -> None:
        signal_id = str(event.get("signal_id") or "")
        if not signal_id:
            return
        self._conn.execute(
            "UPDATE paper_strategy_signals SET processed=1, outcome=?,"
            " reason=? WHERE signal_id=?",
            (outcome, reason, signal_id))
        self._conn.commit()

    # ── signal handling ────────────────────────────────────────────────
    def on_signal(self, event: dict) -> Optional[dict]:
        if not isinstance(event, dict):
            return None
        if event.get("event") == "SETUP_CLOSED":
            return self._on_setup_closed(event)
        signal_id = str(event.get("signal_id") or "").strip()
        if not signal_id:
            return None
        with self._lock:
            # One row per strategy signal; a replay cannot open a second trade.
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO paper_strategy_signals"
                " (signal_id, account_id, symbol, interval, state, payload_json,"
                "  processed, created_at) VALUES (?,?,?,?,?,?,0,?)",
                (signal_id, self._account_id,
                 str(event.get("symbol") or "").upper(),
                 str(event.get("interval") or ""),
                 str(event.get("state") or ""),
                 json.dumps(event, default=str), utc_iso(self._now_ms())),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                self._event("SIGNAL_DUPLICATE", signal_id,
                            {"reason": "signal_id already processed"})
                self._conn.commit()
                return None

            reasons = self._signal_reject_reasons(event)
            if reasons:
                self._record_signal(event, "rejected", ",".join(reasons))
                self._event("SIGNAL_REJECTED", signal_id,
                            {"reasons": reasons})
                self._conn.commit()
                return {"opened": False, "reasons": reasons}

            if not self._accepting:
                self._record_signal(event, "rejected", "paper_trading_stopped")
                self._event("SIGNAL_REJECTED", signal_id,
                            {"reasons": ["paper_trading_stopped"]})
                self._conn.commit()
                return {"opened": False, "reasons": ["paper_trading_stopped"]}

            feed = self._feed()
            if not feed["live"]:
                return self._reject_locked(event, "binance_disconnected")
            if feed["stale"]:
                return self._reject_locked(event, "stale_market_data")

            symbol = str(event["symbol"]).upper()
            quote = self._quote(symbol)
            now = self._now_ms()
            max_age = float(self._config["execution_price_max_age_s"])
            if not quote:
                return self._reject_locked(event, "execution_price_unavailable")
            qts = quote.get("ts")
            if qts is None or (now - int(qts)) / 1000.0 > max_age:
                return self._reject_locked(event, "stale_execution_price")

            state = self._equity_state()
            daily_ok, daily_reason, _ = self._daily_guard(self._conn, state["equity"])
            if not daily_ok:
                return self._reject_locked(event, daily_reason)
            if len(state["positions"]) >= int(self._config["maximum_open_positions"]):
                return self._reject_locked(event, "maximum_open_positions")

            slip = self._slippage_fraction()
            entry = quote["price"] * (1.0 + slip)
            stop, tp, reason = self._risk_levels(event, entry)
            if not stop or not tp:
                return self._reject_locked(event, reason or "risk_levels_unavailable")
            qty, reason = self._position_size(
                event, state["equity"], state["cash"], state["exposure"],
                entry, stop, symbol)
            if not qty:
                return self._reject_locked(event, reason)

            fee = qty * entry * self._fee_fraction()
            notional = qty * entry
            leverage = self._leverage()
            margin = notional / leverage
            if state["cash"] < margin + fee:
                return self._reject_locked(event, "insufficient_margin")

            result = self._open_position(event, symbol, qty, quote["price"],
                                         entry, stop, tp, fee, notional,
                                         margin, leverage, quote, slip)
            if not result:
                return {"opened": False, "reasons": ["database_transaction_failed"]}
            return result

    def _reject_locked(self, event: dict, reason: str) -> dict:
        signal_id = str(event.get("signal_id") or "")
        self._record_signal(event, "rejected", reason)
        self._event("SIGNAL_REJECTED", signal_id, {"reasons": [reason]})
        self._conn.commit()
        return {"opened": False, "reasons": [reason]}

    def _open_position(self, event, symbol, qty, reference, entry, stop, tp,
                       fee, notional, margin, leverage, quote,
                       slip) -> Optional[dict]:
        signal_id = str(event["signal_id"])
        position_id = f"pp-{uuid.uuid4().hex[:12]}"
        order_id = f"po-{uuid.uuid4().hex[:12]}"
        now_ms = self._now_ms()
        now = utc_iso(now_ms)
        risk = event.get("risk") or {}
        snapshot = self.settings_snapshot()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            # Re-read the account inside the transaction.
            account = self._account()
            cash = _num(account["cash"], 0.0)
            if cash < margin + fee:
                self._conn.rollback()
                self._record_signal(event, "rejected", "insufficient_margin")
                return None
            self._conn.execute(
                "INSERT INTO paper_orders"
                " (order_id, account_id, signal_id, symbol, side, order_type,"
                "  quantity, requested_price, fill_price, slippage, fee,"
                "  status, reason, quote_ts, price_source, created_at, filled_at,"
                "  leverage, margin, settings_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (order_id, self._account_id, signal_id, symbol, "BUY",
                 "MARKET_PAPER", qty, reference, entry,
                 round(entry - reference, 10), fee, "FILLED", "",
                 quote.get("ts"), quote.get("source"), now, now,
                 leverage, margin, json.dumps(snapshot, default=str)),
            )
            self._conn.execute(
                "INSERT INTO paper_positions"
                " (position_id, account_id, signal_id, symbol, direction,"
                "  quantity, entry_price, entry_fee, entry_slippage, stop_loss,"
                "  take_profit, opened_at, opened_ts, status, risk_amount, rr,"
                "  strategy_interval, last_price, last_price_ts, unrealized_pnl,"
                "  entry_quote_ts, entry_price_source, updated_at, notional,"
                "  margin, leverage, entry_settings_json, quantity_mode,"
                "  risk_at_entry_pct, fee_at_entry_pct, slippage_at_entry_pct,"
                "  liquidation_price)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (position_id, self._account_id, signal_id, symbol, "LONG",
                 qty, entry, fee, round(entry - reference, 10), stop, tp,
                 now, now_ms, "OPEN", _num(risk.get("risk_amount"), 0.0),
                 float(self._config["default_reward_risk_ratio"]),
                 str(event.get("interval") or ""), entry, quote.get("ts"), 0.0,
                 quote.get("ts"), quote.get("source"), now, notional,
                 margin, leverage, json.dumps(snapshot, default=str),
                 self._quantity_mode(),
                 float(self._config["default_risk_percent"]),
                 self._fee_percent(), self._slippage_percent(),
                 entry * (1.0 - 1.0 / leverage)),
            )
            new_cash = cash - margin - fee
            self._conn.execute(
                "UPDATE paper_accounts SET cash=?, updated_at=?"
                " WHERE account_id=?",
                (new_cash, now, self._account_id))
            self._conn.execute(
                "INSERT INTO paper_transactions"
                " (tx_id, account_id, type, amount, balance_after,"
                "  position_id, detail, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (f"tx-{uuid.uuid4().hex[:12]}", self._account_id, "ENTRY",
                 -(margin + fee), new_cash, position_id,
                 f"BUY {qty} {symbol} @ {entry} "
                 f"(notional {notional:.2f}, margin {margin:.2f}, "
                 f"leverage {leverage:g}x)", now))
            day = self._day(now_ms)
            self._ensure_daily(self._conn, day, self._equity_state()["equity"])
            self._conn.execute(
                "UPDATE paper_daily_stats SET trades=trades+1"
                " WHERE account_id=? AND day=?", (self._account_id, day))
            self._update_account_marks(self._conn, self._equity_state())
            self._update_daily_mark(self._conn, self._equity_state()["equity"])
            self._record_signal(event, "opened", "")
            self._event("PAPER_ORDER_FILLED", signal_id, {
                "order_id": order_id, "position_id": position_id,
                "fill_price": entry, "reference_price": reference,
                "slippage": round(entry - reference, 10), "fee": fee,
                "quantity": qty, "stop_loss": stop, "take_profit": tp,
                "price_source": quote.get("source")})
            self._conn.commit()
        except Exception as exc:
            try:
                self._conn.rollback()
            except Exception:
                pass
            self._record_signal(event, "rejected", "database_transaction_failed")
            self._event("ENTRY_FAILED", signal_id,
                        {"error": f"{type(exc).__name__}: {exc}"})
            self._conn.commit()
            print(f"[Paper] entry transaction failed: "
                  f"{type(exc).__name__}: {exc}", flush=True)
            return None
        self._notify_trade_opened(position_id)
        return {"opened": True, "position_id": position_id,
                "order_id": order_id, "entry": entry, "quantity": qty,
                "stop_loss": stop, "take_profit": tp, "fee": fee}

    # ── position management ─────────────────────────────────────────────
    def on_closed_candle(self, candle: dict) -> None:
        if not isinstance(candle, dict) or not candle.get("closed"):
            return
        if candle.get("interval") != "1m":
            return
        symbol = str(candle.get("symbol") or "").upper()
        if symbol not in [str(s).upper() for s in self._config["allowed_symbols"]]:
            return
        with self._lock:
            self._last_candle = {
                "symbol": symbol, "interval": "1m", "ts": candle.get("ts"),
                "time": utc_iso(candle.get("ts")), "close": candle.get("close"),
                "high": candle.get("high"), "low": candle.get("low"),
            }
            positions = [p for p in self._open_positions()
                         if p["symbol"] == symbol]
            if not positions:
                self._update_daily_mark(
                    self._conn, self._equity_state()["equity"])
                self._conn.commit()
                return
            feed = self._feed()
            if (not feed["live"] or feed["stale"]) and \
                    self._config["data_unreliable_action"] == "close":
                for pos in positions:
                    self._close_position(
                        pos, _num(candle.get("close")), "DATA_UNRELIABLE",
                        candle.get("ts"), price_source="candle_close",
                        ambiguous=False)
                return
            for pos in positions:
                self._manage_position(pos, candle)
            self._update_account_marks(self._conn, self._equity_state())
            self._update_daily_mark(self._conn, self._equity_state()["equity"])
            self._conn.commit()

    def _manage_position(self, pos: dict, candle: dict) -> None:
        high = _num(candle.get("high"), 0.0)
        low = _num(candle.get("low"), 0.0)
        close = _num(candle.get("close"), 0.0)
        sl = _num(pos.get("stop_loss"))
        tp = _num(pos.get("take_profit"))
        liq = _num(pos.get("liquidation_price"))
        self._conn.execute(
            "UPDATE paper_positions SET last_price=?, last_price_ts=?,"
            " unrealized_pnl=?, updated_at=? WHERE position_id=?",
            (close, candle.get("ts"),
             (close - pos["entry_price"]) * pos["quantity"],
             utc_iso(self._now_ms()), pos["position_id"]))
        # Close the implicit transaction before _close_position issues
        # BEGIN IMMEDIATE (SQLite does not nest transactions).
        self._conn.commit()
        if not sl or not tp:
            return
        sl_hit = low <= sl
        tp_hit = high >= tp
        # Simulated liquidation: at leverage L the posted margin is exhausted at
        # entry*(1 - 1/L). If that comes before the stop, the position is closed
        # there (a paper liquidation, never a real one).
        if liq and low <= liq and liq < sl:
            self._close_position(pos, liq, "LIQUIDATION", candle.get("ts"),
                                 price_source="leverage_liquidation",
                                 ambiguous=False)
            return
        if sl_hit and tp_hit:
            # Conservative: within one candle we cannot know the sequence;
            # assume the stop was hit first. Actual trade-level sequencing is
            # not available from this feed. The close transaction records the
            # ambiguous flag.
            open_price = _num(candle.get("open"), sl)
            reference = min(sl, open_price) if open_price else sl
            self._close_position(pos, reference, "STOP_LOSS", candle.get("ts"),
                                 price_source="candle_low_stop_first",
                                 ambiguous=True)
            return
        if sl_hit:
            open_price = _num(candle.get("open"), sl)
            reference = min(sl, open_price) if open_price else sl
            self._close_position(pos, reference, "STOP_LOSS", candle.get("ts"),
                                 price_source="candle_low", ambiguous=False)
        elif tp_hit:
            self._close_position(pos, tp, "TAKE_PROFIT", candle.get("ts"),
                                 price_source="candle_high", ambiguous=False)

    def _close_position(self, pos: dict, reference, reason: str, ts,
                        *, price_source: str, ambiguous: bool) -> Optional[dict]:
        reference = _num(reference)
        if reference is None or reference <= 0:
            self._event("EXIT_SKIPPED", pos.get("signal_id", ""),
                        {"reason": "invalid_exit_price", "exit_reason": reason})
            self._conn.commit()
            return None
        # The position keeps the fee/slippage captured at entry: a later
        # settings change must not rewrite an open trade's economics.
        if pos.get("slippage_at_entry_pct") is not None:
            slip = float(pos["slippage_at_entry_pct"]) / 100.0
        else:
            slip = self._slippage_fraction()
        fee_pct = (float(pos["fee_at_entry_pct"]) / 100.0
                   if pos.get("fee_at_entry_pct") is not None
                   else self._fee_fraction())
        exit_price = reference * (1.0 - slip)
        qty = float(pos["quantity"])
        entry = float(pos["entry_price"])
        entry_fee = float(pos["entry_fee"] or 0.0)
        exit_fee = exit_price * qty * fee_pct
        gross = (exit_price - entry) * qty
        net = gross - entry_fee - exit_fee
        exit_ts = int(ts or self._now_ms())
        exit_at = utc_iso(exit_ts)
        duration = max(0.0, (exit_ts - int(pos["opened_ts"])) / 1000.0)
        trade_id = f"pt-{uuid.uuid4().hex[:12]}"
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            closed = self._conn.execute(
                "UPDATE paper_positions SET status='CLOSED', closed_at=?,"
                " exit_price=?, exit_fee=?, exit_slippage=?, exit_reason=?,"
                " gross_pnl=?, net_pnl=?, fees_total=?, unrealized_pnl=0,"
                " exit_price_source=?, ambiguous_candle=?, updated_at=?"
                " WHERE position_id=? AND status='OPEN'",
                (exit_at, exit_price, exit_fee, round(reference - exit_price, 10),
                 reason, gross, net, entry_fee + exit_fee, price_source,
                 int(ambiguous), exit_at, pos["position_id"]))
            if closed.rowcount == 0:
                self._conn.rollback()
                return None
            self._conn.execute(
                "INSERT INTO paper_trades"
                " (trade_id, position_id, account_id, signal_id, symbol,"
                "  direction, quantity, entry_price, exit_price, gross_pnl,"
                "  entry_fee, exit_fee, fees_total, net_pnl, entry_slippage,"
                "  exit_slippage, entry_at, exit_at, duration_s, exit_reason,"
                "  exit_price_source, created_at, leverage, settings_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (trade_id, pos["position_id"], self._account_id,
                 pos.get("signal_id", ""), pos["symbol"], pos["direction"],
                 qty, entry, exit_price, gross, entry_fee, exit_fee,
                 entry_fee + exit_fee, net, pos.get("entry_slippage") or 0.0,
                 round(reference - exit_price, 10), pos["opened_at"], exit_at,
                 duration, reason, price_source, exit_at,
                 pos.get("leverage"),
                 pos.get("entry_settings_json")))
            account = self._account()
            # Return the margin posted at entry, then settle the trade.
            released = _num(pos.get("margin"), None)
            if released is None:
                released = entry * qty
            cash = (_num(account["cash"], 0.0) + released
                    + (exit_price - entry) * qty - exit_fee)
            realized = _num(account["realized_pnl"], 0.0) + net
            self._conn.execute(
                "UPDATE paper_accounts SET cash=?, realized_pnl=?, updated_at=?"
                " WHERE account_id=?",
                (cash, realized, exit_at, self._account_id))
            self._conn.execute(
                "INSERT INTO paper_transactions"
                " (tx_id, account_id, type, amount, balance_after, position_id,"
                "  detail, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (f"tx-{uuid.uuid4().hex[:12]}", self._account_id, "EXIT",
                 released + (exit_price - entry) * qty - exit_fee, cash,
                 pos["position_id"],
                 f"SELL {qty} {pos['symbol']} @ {exit_price} ({reason}); "
                 f"margin released {released:.2f}", exit_at))
            day = self._day(exit_ts)
            self._ensure_daily(self._conn, day, self._equity_state()["equity"])
            win = 1 if net > 0 else 0
            loss = 1 if net < 0 else 0
            self._conn.execute(
                "UPDATE paper_daily_stats SET wins=wins+?, losses=losses+?,"
                " gross_profit=gross_profit+?, gross_loss=gross_loss+?,"
                " fees=fees+?, net_pnl=net_pnl+?, equity_end=?"
                " WHERE account_id=? AND day=?",
                (win, loss, max(gross, 0.0), min(gross, 0.0),
                 entry_fee + exit_fee, net, self._equity_state()["equity"],
                 self._account_id, day))
            self._event("PAPER_POSITION_CLOSED", pos.get("signal_id", ""), {
                "trade_id": trade_id, "position_id": pos["position_id"],
                "reason": reason, "reference_price": reference,
                "exit_price": exit_price, "gross_pnl": gross, "net_pnl": net,
                "fees": entry_fee + exit_fee, "price_source": price_source,
                "ambiguous_candle": ambiguous})
            self._update_account_marks(self._conn, self._equity_state())
            self._update_daily_mark(self._conn, self._equity_state()["equity"])
            self._conn.commit()
        except Exception as exc:
            try:
                self._conn.rollback()
            except Exception:
                pass
            self._event("EXIT_FAILED", pos.get("signal_id", ""),
                        {"error": f"{type(exc).__name__}: {exc}",
                         "reason": reason})
            self._conn.commit()
            print(f"[Paper] exit transaction failed: "
                  f"{type(exc).__name__}: {exc}", flush=True)
            return None
        self._notify_trade_closed(pos, exit_price, gross, entry_fee + exit_fee,
                                  net, reason)
        return {"closed": True, "trade_id": trade_id, "exit_price": exit_price,
                "gross_pnl": gross, "net_pnl": net, "reason": reason}

    def _on_setup_closed(self, event: dict) -> Optional[dict]:
        signal_id = str(event.get("signal_id") or "")
        if not signal_id:
            return None
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM paper_positions WHERE account_id=? AND"
                " signal_id=? AND status='OPEN'",
                (self._account_id, signal_id)).fetchall()
            if not rows:
                return None
            cols = [d[0] for d in self._conn.execute(
                "SELECT * FROM paper_positions LIMIT 0").description]
            pos = dict(zip(cols, rows[0]))
            price = _num(event.get("price"))
            source = "strategy_candle_close"
            if price is None or price <= 0:
                price = _num(pos.get("last_price")) or _num(pos.get("entry_price"))
                source = "last_known"
            reason = ("STRATEGY_EXPIRED"
                      if str(event.get("outcome")) == "expired"
                      else "STRATEGY_INVALIDATION")
            return self._close_position(pos, price, reason, event.get("ts"),
                                        price_source=source, ambiguous=False)

    # ── notifications ──────────────────────────────────────────────────
    def _notify_trade_opened(self, position_id: str) -> None:
        row = self._conn.execute(
            "SELECT * FROM paper_positions WHERE position_id=?",
            (position_id,)).fetchone()
        if not row:
            return
        cols = [d[0] for d in self._conn.execute(
            "SELECT * FROM paper_positions LIMIT 0").description]
        p = dict(zip(cols, row))
        cash = self._account()["cash"]
        text = (
            "RIO PAPER TRADE OPENED\n\n"
            f"Symbol: {p['symbol']}\n"
            f"Direction: {p['direction']}\n"
            f"Entry: {p['entry_price']:,.6f}\n"
            f"Stop-loss: {p['stop_loss']:,.6f}\n"
            f"Take-profit: {p['take_profit']:,.6f}\n"
            f"Quantity: {p['quantity']:,.8f}\n"
            f"Risk: {p['risk_amount']:,.2f} USDT\n"
            f"Risk/reward: 1:{p['rr']:.2f}\n"
            f"Virtual balance: {cash:,.2f} USDT\n"
            f"Mode: PAPER ONLY"
        )
        self._notify("TRADE_OPENED", p["signal_id"], text)

    def _notify_trade_closed(self, pos, exit_price, gross, fees, net,
                             reason) -> None:
        cash = self._account()["cash"]
        equity = self._equity_state()["equity"]
        text = (
            "RIO PAPER TRADE CLOSED\n\n"
            f"Symbol: {pos['symbol']}\n"
            f"Exit reason: {reason}\n"
            f"Entry: {pos['entry_price']:,.6f}\n"
            f"Exit: {exit_price:,.6f}\n"
            f"Gross P&L: {gross:+,.2f} USDT\n"
            f"Fees: {fees:,.2f} USDT\n"
            f"Net P&L: {net:+,.2f} USDT\n"
            f"Updated virtual balance: {cash:,.2f} USDT\n"
            f"Equity: {equity:,.2f} USDT\n"
            f"Mode: PAPER ONLY"
        )
        self._notify("TRADE_CLOSED", f"{pos['signal_id']}:{reason}", text)

    # ── public status / reports ─────────────────────────────────────────
    def status(self) -> dict:
        with self._lock:
            state = self._equity_state()
            account = self._account()
            positions = []
            for p in state["positions"]:
                mark = state["marks"][p["position_id"]]
                positions.append({
                    "position_id": p["position_id"],
                    "symbol": p["symbol"],
                    "direction": p["direction"],
                    "entry_price": p["entry_price"],
                    "current_price": mark["price"],
                    "price_source": mark["source"],
                    "quantity": p["quantity"],
                    "stop_loss": p["stop_loss"],
                    "take_profit": p["take_profit"],
                    "unrealized_pnl": round(
                        (mark["price"] - p["entry_price"]) * p["quantity"], 8),
                    "risk_amount": p.get("risk_amount"),
                    "rr": p.get("rr"),
                    "notional": p.get("notional"),
                    "margin": p.get("margin"),
                    "leverage": p.get("leverage"),
                    "quantity_mode": p.get("quantity_mode"),
                    "liquidation_price": p.get("liquidation_price"),
                    "opened_at": p["opened_at"],
                    "duration_s": max(0, self._now_ms() - int(p["opened_ts"])) / 1000.0,
                    "signal_id": p.get("signal_id"),
                })
            performance = self._performance_locked()
            daily = self._daily_row(self._conn, self._day()) or {}
            feed = self._feed()
            last_candle = copy.deepcopy(self._last_candle)
            monitoring = {
                "started": self._started,
                "accepting_entries": self._accepting,
                "enabled": bool(self._config.get("enabled")),
                "allowed_symbols": list(self._config["allowed_symbols"]),
                "last_candle": last_candle,
            }
            used_margin = sum(
                _num(p.get("margin"), 0.0) or 0.0 for p in state["positions"])
            account_view = {
                "account_id": self._account_id,
                # starting_capital is the configured opening value; balance and
                # equity are the live paper values and are never overwritten by
                # a settings change.
                "starting_capital": _num(
                    account.get("starting_capital"),
                    account["initial_balance"]),
                "initial_balance": account["initial_balance"],
                "balance": state["cash"],
                "available_balance": state["cash"],
                "equity": state["equity"],
                "realized_pnl": account["realized_pnl"],
                "unrealized_pnl": state["unrealized"],
                "quote_currency": account["quote_currency"],
                "peak_equity": account["peak_equity"],
                "max_drawdown": account["max_drawdown"],
                "used_margin": round(used_margin, 8),
                "exposure": round(state["exposure"], 8),
                "status": account["status"],
            }
        strategy = {}
        for sym in self._config["allowed_symbols"]:
            try:
                strategy[sym] = self._strategy_snapshot(sym)
            except Exception as exc:
                strategy[sym] = {"ok": False,
                                 "error": f"{type(exc).__name__}: {exc}"}
        return {
            "ok": True,
            "paper_only": True,
            "account": account_view,
            "positions": positions,
            "performance": performance,
            "daily": daily,
            "strategy": strategy,
            "feed": feed,
            "monitoring": monitoring,
            "last_update": utc_iso(self._now_ms()),
            "settings": self.settings_snapshot(),
            "config": {
                "enabled": bool(self._config.get("enabled")),
                "quantity_mode": self._quantity_mode(),
                "fixed_quantity": self._fixed_quantity(),
                "leverage": self._leverage(),
                "default_risk_percent": self._config["default_risk_percent"],
                "maximum_open_positions": self._config["maximum_open_positions"],
                "maximum_daily_trades": self._config["maximum_daily_trades"],
                "maximum_daily_loss_percent": self._config["maximum_daily_loss_percent"],
                "maximum_total_exposure_percent": self._config["maximum_total_exposure_percent"],
                "simulated_fee_percent": self._fee_percent(),
                "slippage_percent": self._slippage_percent(),
                "slippage_model": copy.deepcopy(self._config["slippage_model"]),
                "default_reward_risk_ratio": self._config["default_reward_risk_ratio"],
            },
        }

    def _strategy_snapshot(self, symbol: str) -> dict:
        if self._strategy_provider is not None:
            return self._strategy_provider(symbol)
        # Prefer the exact engine this paper account subscribed to. In the
        # production singleton path this is also core.liquidity_sweep.get_engine().
        if self._liquidity_engine is not None:
            return self._liquidity_engine.get_status(symbol)
        from core.liquidity_sweep import get_engine
        return get_engine().get_status(symbol)

    def _performance_locked(self) -> dict:
        rows = self._conn.execute(
            "SELECT gross_pnl, net_pnl, fees_total FROM paper_trades"
            " WHERE account_id=?", (self._account_id,)).fetchall()
        trades = len(rows)
        wins = sum(1 for r in rows if (r[1] or 0) > 0)
        losses = sum(1 for r in rows if (r[1] or 0) < 0)
        gross_profit = sum(max(r[0] or 0.0, 0.0) for r in rows)
        gross_loss = sum(min(r[0] or 0.0, 0.0) for r in rows)
        fees = sum(r[2] or 0.0 for r in rows)
        net = sum(r[1] or 0.0 for r in rows)
        profit_factor = (gross_profit / abs(gross_loss)) if gross_loss else (
            None if not gross_profit else float("inf"))
        account = self._account()
        daily = []
        for r in self._conn.execute(
                "SELECT day, trades, wins, losses, net_pnl, fees,"
                " max_drawdown FROM paper_daily_stats"
                " WHERE account_id=? ORDER BY day DESC LIMIT 14",
                (self._account_id,)).fetchall():
            daily.append({"day": r[0], "trades": r[1], "wins": r[2],
                          "losses": r[3], "net_pnl": r[4], "fees": r[5],
                          "max_drawdown": r[6]})
        return {
            "total_trades": trades, "wins": wins, "losses": losses,
            "win_rate": round(wins / trades * 100, 2) if trades else 0.0,
            "gross_profit": round(gross_profit, 8),
            "gross_loss": round(gross_loss, 8),
            "fees": round(fees, 8), "net_pnl": round(net, 8),
            "profit_factor": (round(profit_factor, 4)
                              if profit_factor is not None
                              and math.isfinite(profit_factor) else None),
            "max_drawdown": round(_num(account.get("max_drawdown"), 0.0), 8),
            "daily": daily,
        }

    def performance(self) -> dict:
        with self._lock:
            return self._performance_locked()

    def positions(self) -> list:
        return self.status()["positions"]

    def history(self, limit: int = 50) -> list:
        limit = max(1, min(int(limit), 500))
        with self._lock:
            rows = self._conn.execute(
                "SELECT trade_id, signal_id, symbol, direction, quantity,"
                " entry_price, exit_price, gross_pnl, entry_fee, exit_fee,"
                " fees_total, net_pnl, entry_at, exit_at, duration_s,"
                " exit_reason FROM paper_trades WHERE account_id=?"
                " ORDER BY exit_at DESC LIMIT ?",
                (self._account_id, limit)).fetchall()
        return [{
            "trade_id": r[0], "signal_id": r[1], "symbol": r[2],
            "direction": r[3], "quantity": r[4], "entry_price": r[5],
            "exit_price": r[6], "gross_pnl": r[7], "entry_fee": r[8],
            "exit_fee": r[9], "fees": r[10], "net_pnl": r[11],
            "entry_at": r[12], "exit_at": r[13], "duration_s": r[14],
            "exit_reason": r[15],
        } for r in rows]

    def daily_report(self, day: Optional[str] = None) -> str:
        day = day or self._day()
        with self._lock:
            row = self._daily_row(self._conn, day) or {}
            perf = self._performance_locked()
            account = self._account()
            open_positions = len(self._open_positions())
            equity = self._equity_state()["equity"]
        return (
            "RIO DAILY PAPER TRADING REPORT\n\n"
            f"Day (UTC): {day}\n"
            f"Total trades: {row.get('trades', 0)}\n"
            f"Winning trades: {row.get('wins', 0)}\n"
            f"Losing trades: {row.get('losses', 0)}\n"
            f"Win rate: {((row.get('wins', 0) / row.get('trades', 1) * 100) if row.get('trades') else 0):.2f}%\n"
            f"Gross profit: {row.get('gross_profit', 0):+,.2f} USDT\n"
            f"Gross loss: {row.get('gross_loss', 0):+,.2f} USDT\n"
            f"Total fees: {row.get('fees', 0):,.2f} USDT\n"
            f"Net P&L: {row.get('net_pnl', 0):+,.2f} USDT\n"
            f"Maximum drawdown: {row.get('max_drawdown', 0):,.2f} USDT\n"
            f"Current equity: {equity:,.2f} USDT\n"
            f"Open positions: {open_positions}\n"
            f"All-time net P&L: {perf.get('net_pnl', 0):+,.2f} USDT\n"
            f"Mode: PAPER ONLY"
        )

    # ── commands / lifecycle ────────────────────────────────────────────
    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._stop_event.clear()
            if self._liquidity_engine is None:
                from core.liquidity_sweep import get_engine
                self._liquidity_engine = get_engine()
            self._liquidity_engine.subscribe_signals(self._on_signal_cb)
            bw.subscribe_closed_candles(self._on_candle_cb)
            self._subscribed = True
            self._accepting = bool(self._config.get("enabled"))
        if self._config.get("daily_report_enabled"):
            self._daily_thread = threading.Thread(
                target=self._daily_loop, name="paper-daily", daemon=True)
            self._daily_thread.start()
        if self._accepting:
            self._notify_started()
        print("[Paper] engine started", flush=True)

    def stop(self) -> None:
        if self._subscribed:
            bw.unsubscribe_closed_candles(self._on_candle_cb)
            if self._liquidity_engine is not None:
                self._liquidity_engine.unsubscribe_signals(self._on_signal_cb)
            self._subscribed = False
        self._stop_event.set()
        with self._lock:
            self._started = False
            self._accepting = False
        print("[Paper] engine stopped", flush=True)

    def close(self) -> None:
        self.stop()
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    def start_trading(self) -> None:
        with self._lock:
            self._config["enabled"] = True
            _save_config(self.config_path, self._config)
            self._accepting = True
        if not self._started:
            self.start()
        print("[Paper] paper trading enabled", flush=True)

    def stop_trading(self) -> None:
        with self._lock:
            self._config["enabled"] = False
            _save_config(self.config_path, self._config)
            self._accepting = False
        print("[Paper] paper trading stopped (open positions still managed)",
              flush=True)

    def reset(self, confirm: bool = False) -> dict:
        if not confirm:
            raise ValueError(
                "paper reset requires explicit confirmation (confirm=True)")
        with self._lock:
            for pos in self._open_positions():
                quote = self._quote(pos["symbol"])
                price = quote["price"] if quote else _num(pos.get("last_price"))
                source = quote["source"] if quote else "last_known"
                if price is None:
                    price = _num(pos.get("entry_price"))
                    source = "entry_price"
                self._close_position(pos, price, "RESET",
                                     self._now_ms(), price_source=source,
                                     ambiguous=False)
            now = utc_iso(self._now_ms())
            self._conn.execute(
                "UPDATE paper_accounts SET status='archived', archived_at=?,"
                " updated_at=? WHERE account_id=?",
                (now, now, self._account_id))
            self._conn.commit()
            old = self._account_id
            self._ensure_account()
        self._notify("ACCOUNT_RESET",
                     f"reset:{self._account_id}",
                     "RIO PAPER ACCOUNT RESET\n\n"
                     f"Previous account archived: {old}\n"
                     f"New account: {self._account_id}\n"
                     f"Opening balance: {self._config['initial_balance']:,.2f} "
                     f"{self._config['quote_currency']}\n"
                     "Mode: PAPER ONLY")
        return {"ok": True, "archived_account": old,
                "account_id": self._account_id}

    def _notify_started(self) -> None:
        account = self._account()
        # One notification per engine start (a restart is a new monitoring
        # session, not a duplicate of the previous launch).
        self._notify(
            "START",
            f"started:{self._account_id}:{int(self._clock())}",
            "RIO PAPER TRADING STARTED\n\n"
            f"Symbols: {', '.join(self._config['allowed_symbols'])}\n"
            "Strategy: Liquidity Sweep Reversal\n"
            "Monitoring: 1-minute candles\n"
            f"Account: {account['cash']:,.2f} "
            f"{self._config['quote_currency']} virtual\n"
            "Mode: PAPER ONLY")

    def _daily_loop(self) -> None:
        while not self._stop_event.wait(60):
            if not self._config.get("daily_report_enabled"):
                continue
            now = datetime.fromtimestamp(self._clock(), tz=UTC)
            if now.hour < int(self._config["daily_report_hour_utc"]):
                continue
            # Report the previous completed UTC day.
            day = (datetime.fromtimestamp(
                self._clock() - 86400, tz=UTC)).strftime("%Y-%m-%d")
            if day == self._daily_last_key:
                continue
            try:
                text = self.daily_report(day)
                self._notify("DAILY", day, text)
                self._daily_last_key = day
            except Exception as exc:
                print(f"[Paper] daily report failed: "
                      f"{type(exc).__name__}: {exc}", flush=True)


# ── human/voice formatters (action + Telegram commands share these) ────────
def _money(value, currency="USDT") -> str:
    try:
        return f"{float(value):,.2f} {currency}"
    except (TypeError, ValueError):
        return f"— {currency}"


def _price(value) -> str:
    try:
        return f"{float(value):,.6f}"
    except (TypeError, ValueError):
        return "—"


def format_status(engine: "PaperTradingEngine") -> str:
    s = engine.status()
    a = s["account"]
    feed = s["feed"]
    mon = s["monitoring"]
    daily = s.get("daily") or {}
    lines = [
        "RIO PAPER TRADING STATUS",
        "Mode: PAPER ONLY — no real orders",
        f"Binance: {feed.get('transport', 'none')} · "
        f"{'LIVE' if feed.get('live') else 'DISCONNECTED'}"
        + (" · STALE" if feed.get("stale") else ""),
        f"Monitoring: {'started' if mon.get('started') else 'not started'} · "
        f"entries {'ON' if mon.get('accepting_entries') else 'OFF'}",
        f"Symbols: {', '.join(mon.get('allowed_symbols') or [])}",
        "",
        f"Initial balance: {_money(a.get('initial_balance'), a.get('quote_currency'))}",
        f"Balance (cash): {_money(a.get('balance'), a.get('quote_currency'))}",
        f"Equity: {_money(a.get('equity'), a.get('quote_currency'))}",
        f"Available: {_money(a.get('available_balance'), a.get('quote_currency'))}",
        f"Realized P&L: {_money(a.get('realized_pnl'), a.get('quote_currency'))}",
        f"Unrealized P&L: {_money(a.get('unrealized_pnl'), a.get('quote_currency'))}",
        f"Open positions: {len(s.get('positions') or [])}",
        "Strategy: " + (", ".join(
            f"{sym} {snap.get('state', '—')}"
            for sym, snap in (s.get("strategy") or {}).items()) or "—"),
        "",
        f"Today: {daily.get('trades', 0)} trade(s) · "
        f"net {_money(daily.get('net_pnl'), a.get('quote_currency'))}",
        f"Last update (UTC): {s.get('last_update') or '—'}",
    ]
    return "\n".join(lines)


def format_balance(engine: "PaperTradingEngine") -> str:
    s = engine.status()
    a = s["account"]
    cur = a.get("quote_currency", "USDT")
    return "\n".join([
        "RIO PAPER BALANCE",
        f"Initial: {_money(a.get('initial_balance'), cur)}",
        f"Cash balance: {_money(a.get('balance'), cur)}",
        f"Equity: {_money(a.get('equity'), cur)}",
        f"Available: {_money(a.get('available_balance'), cur)}",
        f"Realized P&L: {_money(a.get('realized_pnl'), cur)}",
        f"Unrealized P&L: {_money(a.get('unrealized_pnl'), cur)}",
        f"Max drawdown: {_money(a.get('max_drawdown'), cur)}",
        "Mode: PAPER ONLY",
    ])


def format_positions(engine: "PaperTradingEngine") -> str:
    s = engine.status()
    positions = s.get("positions") or []
    if not positions:
        return "No open paper positions.\nMode: PAPER ONLY"
    cur = s["account"].get("quote_currency", "USDT")
    lines = ["RIO OPEN PAPER POSITIONS"]
    for p in positions:
        lines.append(
            f"- {p['symbol']} {p['direction']} · qty {p['quantity']:,.8f} · "
            f"entry {_price(p['entry_price'])} · current {_price(p['current_price'])} · "
            f"SL {_price(p['stop_loss'])} · TP {_price(p['take_profit'])} · "
            f"uPnL {_money(p['unrealized_pnl'], cur)} · "
            f"{p['duration_s']/60:.1f}m")
    lines.append("Mode: PAPER ONLY")
    return "\n".join(lines)


def format_history(engine: "PaperTradingEngine", limit: int = 20) -> str:
    rows = engine.history(limit)
    if not rows:
        return "No closed paper trades yet.\nMode: PAPER ONLY"
    cur = engine.status()["account"].get("quote_currency", "USDT")
    lines = ["RIO PAPER TRADE HISTORY"]
    for r in rows[:limit]:
        lines.append(
            f"- {r['exit_at']} {r['symbol']} {r['direction']} · "
            f"entry {_price(r['entry_price'])} → exit {_price(r['exit_price'])} · "
            f"net {_money(r['net_pnl'], cur)} · {r['exit_reason']}")
    lines.append("Mode: PAPER ONLY")
    return "\n".join(lines)


def format_performance(engine: "PaperTradingEngine") -> str:
    p = engine.performance()
    cur = engine.status()["account"].get("quote_currency", "USDT")
    pf = p.get("profit_factor")
    return "\n".join([
        "RIO PAPER PERFORMANCE",
        f"Total trades: {p['total_trades']}",
        f"Wins / losses: {p['wins']} / {p['losses']}",
        f"Win rate: {p['win_rate']:.2f}%",
        f"Profit factor: {pf if pf is not None else 'n/a'}",
        f"Gross profit: {_money(p['gross_profit'], cur)}",
        f"Gross loss: {_money(p['gross_loss'], cur)}",
        f"Fees: {_money(p['fees'], cur)}",
        f"Net P&L: {_money(p['net_pnl'], cur)}",
        f"Max drawdown: {_money(p['max_drawdown'], cur)}",
        "Daily: " + (", ".join(
            f"{d['day']} {_money(d['net_pnl'], cur)}"
            for d in (p.get("daily") or [])[:5]) or "no closed days yet"),
        "Mode: PAPER ONLY",
    ])


def format_report(engine: "PaperTradingEngine") -> str:
    return engine.daily_report()


# ── singleton ───────────────────────────────────────────────────────────────
_engine: Optional[PaperTradingEngine] = None
_engine_lock = threading.Lock()


def get_engine() -> PaperTradingEngine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = PaperTradingEngine()
        return _engine


def reset_engine() -> None:
    global _engine
    with _engine_lock:
        if _engine is not None:
            try:
                _engine.close()
            except Exception:
                pass
        _engine = None


__all__ = [
    "PaperTradingEngine", "PaperConfigError", "get_engine", "reset_engine",
    "DEFAULT_CONFIG", "DEFAULT_CONFIG_PATH",
]
