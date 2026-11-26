"""
core/binance_ws.py — Real-time Binance market data daemon.

Two transport modes:
  1. WebSocket (primary) — multiplexed stream for ticker + kline_1m/5m/15m
     across 5 symbols. Zero cost, no API key.
  2. HTTP poll (fallback) — REST /ticker/24hr + /klines every 15s when WS
     is unreachable (e.g. restricted networks, no IPv6).

Every tick / closed candle is persisted to SQLite
(knowledge/binance_market.db). Price alerts fire through Hermes send;
periodic summaries go the same route.

Public API (safe from any thread):
  latest_prices()  → {symbol: {...}}
  hourly_stats(sym) → {min,max,avg,change_pct,...}  or {}
  query_history(sym, interval, bars) → list[dict]
  get_alerts(sym), set_alert(sym,**kw), remove_alert(sym,key)
  status()         → {ws_up, uptime_s, symbols_tracking, latest, db}
  start(), stop()  — lifecycle
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

_HOME = Path.home()
_DB_PATH = Path(__file__).resolve().parent.parent / "knowledge" / "binance_market.db"
_ALERTS_FILE = _HOME / ".hermes" / "binance_alerts.json"

_SYMBOLS = ("btcusdt", "ethusdt", "solusdt", "paxgusdt", "xautusdt")
_STREAMS = "/".join(
    f"{s}@ticker/{s}@kline_1m/{s}@kline_3m/{s}@kline_5m/{s}@kline_15m"
    for s in _SYMBOLS
)
# Binance runs a dedicated public market-data deployment alongside the main
# API. On networks where api.binance.com / stream.binance.com are blocked or
# intermittent, the .vision hosts serve the same spot data. Both are tried in
# order; the working one stays preferred (no simulation, same real venue).
_STREAM_HOSTS = (
    "wss://stream.binance.com:9443/stream?streams=",
    "wss://data-stream.binance.vision/stream?streams=",
)
_STREAM_URL = _STREAM_HOSTS[0] + _STREAMS       # kept for compatibility
_REST_HOSTS = (
    "https://api.binance.com/api/v3",
    "https://data-api.binance.vision/api/v3",
)
_REST_BASE = _REST_HOSTS[0]                     # kept for compatibility
_rest_lock = threading.Lock()
_rest_host_idx = 0

_DEFAULT_ALERTS = {
    "btcusdt":  {"below": 66000, "above": 70000, "pct_1m": 1.5, "pct_5m": 3.0},
    "ethusdt":  {"below": 1850, "above": 2000, "pct_1m": 1.5, "pct_5m": 3.0},
    "solusdt":  {"below": 70,   "above": 85,   "pct_1m": 2.0, "pct_5m": 4.0},
    "paxgusdt": {"below": 3800, "above": 3950, "pct_1m": 0.5, "pct_5m": 1.0},
    "xautusdt": {"below": 3800, "above": 3950, "pct_1m": 0.5, "pct_5m": 1.0},
}

_RECONNECT_BASE = 5
_RECONNECT_MAX  = 300
_WS_CONNECT_TIMEOUT_S = float(os.environ.get("BINANCE_WS_CONNECT_TIMEOUT", "25"))
# Notification policy: price-threshold alerts ONLY. Periodic Market Summary and
# standalone price updates stay disabled; the data/alert pipeline keeps running.
# Set TELEGRAM_MARKET_SUMMARIES=1 to restore the old summary behaviour.
_SEND_MARKET_SUMMARIES = os.environ.get(
    "TELEGRAM_MARKET_SUMMARIES", "0").strip().lower() in {
        "1", "true", "on", "yes"}
_SUMMARY_INTERVAL_S = int(os.environ.get("BINANCE_SUMMARY_INTERVAL", "60"))
_ALERT_COOLDOWN_S   = int(os.environ.get("BINANCE_ALERT_COOLDOWN",  "300"))
_POLL_INTERVAL_S    = 15


# ── Event loop ────────────────────────────────────────────────────────────────
# The daemon needs a REAL running loop: a loop that is merely created never
# executes the task scheduled by start(), so every alert/summary/price stayed
# at its initial value. One daemon thread owns the loop for the process
# lifetime; all public calls hop onto it with run_coroutine_threadsafe().
_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
_loop_lock = threading.Lock()


def _run_loop_forever(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()


def _get_loop() -> asyncio.AbstractEventLoop:
    global _loop, _loop_thread
    with _loop_lock:
        if _loop is None or _loop.is_closed():
            _loop = asyncio.new_event_loop()
        if _loop_thread is None or not _loop_thread.is_alive():
            _loop_thread = threading.Thread(
                target=_run_loop_forever,
                args=(_loop,),
                name="binance-ws-loop",
                daemon=True,
            )
            _loop_thread.start()
        return _loop


def run_coro(coro, timeout: float = 15.0):
    """Run a coroutine on the daemon loop from any thread.

    Returns the coroutine's result when it completes within ``timeout``,
    otherwise a concurrent.futures.TimeoutError propagates (the caller keeps
    the work running on the loop). Called from the loop thread itself, the
    coroutine is scheduled and its Task returned instead of deadlocking."""
    loop = _get_loop()
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        return loop.create_task(coro)
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)


# ── SQLite store ──────────────────────────────────────────────────────────────
_db_conn: sqlite3.Connection | None = None
_db_lock = threading.RLock()
_db_init_lock = threading.Lock()


def _create_db() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tickers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, price REAL, change_24h REAL,
            high_24h REAL, low_24h REAL, volume REAL,
            ts INTEGER, created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS candles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, interval TEXT,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            taker_buy_volume REAL,
            ts INTEGER, closed INTEGER, created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    # Existing installs predate taker-buy volume (Binance kline field V /
    # REST index 9). Add it in place; never recreate the table.
    cur.execute("PRAGMA table_info(candles)")
    if "taker_buy_volume" not in {row[1] for row in cur.fetchall()}:
        cur.execute("ALTER TABLE candles ADD COLUMN taker_buy_volume REAL")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ticker_st ON tickers(symbol, ts)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_candle_sit ON candles(symbol, interval, ts)")
    # One row per (symbol, interval, candle-open-time). A WS kline update
    # arrives many times for the same candle and the HTTP fallback re-reads
    # the last few bars every cycle; without the unique index both paths
    # added duplicate history rows. Dedupe pre-existing rows once, then
    # INSERT OR REPLACE keeps the table idempotent.
    cur.execute(
        "DELETE FROM candles WHERE id NOT IN "
        "(SELECT MAX(id) FROM candles GROUP BY symbol, interval, ts)")
    # Older builds stored every in-progress kline update. Those rows are not
    # final history; drop them so the table is closed-candles-only.
    cur.execute("DELETE FROM candles WHERE closed=0")
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_candles "
        "ON candles(symbol, interval, ts)")
    conn.commit()
    return conn


def _open_db() -> sqlite3.Connection:
    """Lazy singleton connection. Double-checked under a lock: the daemon
    thread and a Jarvis action can both hit the first access simultaneously."""
    global _db_conn
    if _db_conn is None:
        with _db_init_lock:
            if _db_conn is None:
                _db_conn = _create_db()
    return _db_conn


def _prune_old(rows_limit: int = 500_000) -> None:
    try:
        conn = _open_db()
        with _db_lock:
            conn.execute(
                f"DELETE FROM tickers WHERE id NOT IN "
                f"(SELECT id FROM tickers ORDER BY ts DESC LIMIT {rows_limit})")
            conn.execute(
                f"DELETE FROM candles WHERE id NOT IN "
                f"(SELECT id FROM candles ORDER BY ts DESC LIMIT {rows_limit * 4})")
            conn.commit()
    except Exception:
        pass


# ── In-memory state ───────────────────────────────────────────────────────────
_state_lock = threading.RLock()
_latest_ticker: Dict[str, dict] = {}
_latest_candles: Dict[str, Dict[str, dict]] = {s: {} for s in _SYMBOLS}
_connect_time: float = 0.0
_ws_up: bool = False
_transport = "none"   # "ws" | "http" | "none"

# Closed-candle fan-out for local consumers (liquidity engine, future strategy
# workers). This is a SUBSCRIBER hook on the one daemon — not a second feed.
_closed_subscribers: list = []
_sub_lock = threading.Lock()
_notified_ts: Dict[tuple, int] = {}


def subscribe_closed_candles(callback) -> None:
    """Register callback(candle_dict) for each newly closed candle."""
    if not callable(callback):
        raise TypeError("callback must be callable")
    with _sub_lock:
        if callback not in _closed_subscribers:
            _closed_subscribers.append(callback)


def unsubscribe_closed_candles(callback) -> None:
    with _sub_lock:
        try:
            _closed_subscribers.remove(callback)
        except ValueError:
            pass


def _notify_closed(candle: dict) -> None:
    """Fan out one newly closed candle, exactly once per (symbol, interval, ts).

    The HTTP poll re-reads the last few bars every cycle, so without this the
    same closed candle would be delivered repeatedly and could re-fire alerts.
    """
    key = (candle.get("symbol"), candle.get("interval"))
    ts = int(candle.get("ts") or 0)
    with _sub_lock:
        if ts <= _notified_ts.get(key, -1):
            return
        _notified_ts[key] = ts
        subscribers = list(_closed_subscribers)
    for callback in subscribers:
        try:
            callback(dict(candle))
        except Exception as e:
            print(f"[BinanceWS] candle subscriber error: "
                  f"{type(e).__name__}: {e}", flush=True)


def _set_ticker(sym: str, data: dict) -> None:
    with _state_lock:
        _latest_ticker[sym] = data


def _set_candle(sym: str, interval: str, data: dict) -> None:
    with _state_lock:
        _latest_candles.setdefault(sym, {})[interval] = data


# ── Alerts persistence ────────────────────────────────────────────────────────
_alerts_lock = threading.RLock()
_pending_alerts: Dict[str, float] = {}


def _load_alerts() -> dict:
    with _alerts_lock:
        if _ALERTS_FILE.exists():
            try:
                return json.loads(_ALERTS_FILE.read_text())
            except Exception:
                pass
        _ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_ALERTS_FILE, "w") as f:
            json.dump(_DEFAULT_ALERTS, f, indent=2)
        # Deep copy: the per-symbol dicts inside _DEFAULT_ALERTS must never be
        # mutated by set_alert(), or the next process start inherits whatever
        # level was last set (and a fresh test would see another test's alert).
        return json.loads(json.dumps(_DEFAULT_ALERTS))


def _save_alerts(data: dict) -> None:
    with _alerts_lock:
        _ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _ALERTS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(_ALERTS_FILE)


_HERMES_BIN = shutil.which("hermes") or str(_HOME / ".local" / "bin" / "hermes")
# Fixed, minimal env — the GUI process must never hand Hermes its own
# environment (same rule as actions/hermes_task.py). PATH still has to reach
# the CLI and Hermes' bundled node.
_HERMES_ENV = {
    "PATH": os.pathsep.join([
        str(_HOME / ".local" / "bin"),
        str(_HOME / ".hermes" / "node" / "bin"),
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]),
    "HOME": str(_HOME),
}
_send_lock = threading.Lock()


def _telegram_chat_id() -> str:
    chat_id = os.environ.get("BINANCE_CHAT_ID", "").strip()
    if chat_id:
        return chat_id
    env_file = _HOME / ".hermes" / ".env"
    try:
        for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.strip().startswith("TELEGRAM_CHAT_ID="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _send_hermes_target(target: str, text: str) -> bool:
    """Blocking Hermes send to an explicit target (e.g. telegram:12345)."""
    try:
        proc = subprocess.run(
            [_HERMES_BIN, "send", "-t", target, text[:3500]],
            capture_output=True, text=True, timeout=15, env=_HERMES_ENV,
        )
        if proc.returncode != 0:
            print(f"[BinanceWS] Hermes send failed: "
                  f"{(proc.stderr or proc.stdout or '')[:140]}", flush=True)
            return False
        return True
    except FileNotFoundError:
        print("[BinanceWS] hermes CLI not found — alert dropped", flush=True)
        return False
    except subprocess.TimeoutExpired:
        print("[BinanceWS] Hermes send timed out", flush=True)
        return False
    except Exception as e:
        print(f"[BinanceWS] Hermes send error: {e}", flush=True)
        return False


def _send_via_hermes(text: str) -> bool:
    """Deliver one message to the configured chat. Blocking; use _queue_send()."""
    chat_id = _telegram_chat_id()
    if not chat_id:
        print("[BinanceWS] no TELEGRAM_CHAT_ID/BINANCE_CHAT_ID — alert dropped",
              flush=True)
        return False
    return _send_hermes_target(f"telegram:{chat_id}", text)


def _queue_send_to(target: str, text: str) -> None:
    def _worker() -> None:
        with _send_lock:
            _send_hermes_target(target, text)

    threading.Thread(target=_worker, name="binance-send", daemon=True).start()


def _queue_send(text: str) -> None:
    """Send off the event loop, serialized so two alerts can't interleave.

    subprocess.run() can block for up to 15 s; running it inline stalled the
    WS receive loop past its 10 s ping timeout and dropped the connection."""
    def _worker() -> None:
        with _send_lock:
            _send_via_hermes(text)

    threading.Thread(target=_worker, name="binance-send", daemon=True).start()


def send_hermes(target: str, text: str) -> bool:
    """Queue a message to an explicit Hermes target (non-blocking).

    Used by the deterministic /paper_* command replies so they go back to the
    same platform/chat that sent the command. No second Hermes integration:
    this is the same CLI, fixed env and lock as every other alert.
    """
    target = str(target or "telegram").strip() or "telegram"
    if target == "telegram":
        chat_id = _telegram_chat_id()
        if chat_id:
            target = f"telegram:{chat_id}"
    if ":" not in target and target != "telegram":
        return False
    _queue_send_to(target, str(text)[:3500])
    return True


def send_telegram(text: str) -> bool:
    """Queue a Telegram message through Hermes. Non-blocking.

    Returns True when a chat target is configured and the send was queued;
    the actual CLI result is logged by the worker. Never includes secrets.
    """
    if not _telegram_chat_id():
        return False
    _queue_send(str(text)[:3500])
    return True


def _fire_alert(lines: List[str]) -> None:
    if not lines:
        return
    _queue_send("⚡ *ALERT*\n" + "\n".join(lines))
    print(f"[BinanceWS] ⚡ ALERT queued ({len(lines)} line(s))", flush=True)


def _fire_summary(lines: List[str]) -> None:
    # Alerts-only policy: no Market Summary / standalone price Telegram sends.
    if not _SEND_MARKET_SUMMARIES:
        return
    _queue_send("📊 *Market Summary*\n" + "\n".join(lines))
    print("[BinanceWS] 📊 summary queued", flush=True)


def _is_cooldown(key: str) -> bool:
    with _alerts_lock:
        last = _pending_alerts.get(key)
        # No entry means "never fired" — not "fired at boot". Using 0.0 as the
        # default suppressed the first alert on machines with < 5 min uptime.
        return last is not None and (time.monotonic() - last) < _ALERT_COOLDOWN_S


def _mark_fired(key: str) -> None:
    with _alerts_lock:
        _pending_alerts[key] = time.monotonic()


# ── Public query API ──────────────────────────────────────────────────────────
def latest_prices() -> dict:
    with _state_lock:
        return dict(_latest_ticker)


def hourly_stats(symbol: str, hours: float = 1) -> dict:
    # Rows are stored under the stream symbol (BTCUSDT); callers ask for BTC.
    symbol = normalize_symbol(symbol).upper()
    conn = _open_db()
    window_s = max(0.1, float(hours)) * 3600
    since = int((datetime.now(timezone.utc).timestamp() - window_s) * 1000)
    with _db_lock:
        cur = conn.cursor()
        cur.execute(
            "SELECT price FROM tickers WHERE symbol=? AND ts>? ORDER BY ts",
            (symbol, since),
        )
        prices = [r[0] for r in cur.fetchall()]
    if not prices:
        return {}
    return {
        "symbol": symbol,
        "min": round(min(prices), 2),
        "max": round(max(prices), 2),
        "avg": round(sum(prices) / len(prices), 2),
        "first": round(prices[0], 2),
        "last": round(prices[-1], 2),
        "change_pct": round((prices[-1] - prices[0]) / prices[0] * 100, 4),
        "count": len(prices),
    }


def query_history(symbol: str, interval: str = "1m", bars: int = 50) -> list:
    symbol = normalize_symbol(symbol).upper()
    conn = _open_db()
    with _db_lock:
        cur = conn.cursor()
        cur.execute(
            "SELECT ts, open, high, low, close, volume FROM candles "
            "WHERE symbol=? AND interval=? ORDER BY ts DESC LIMIT ?",
            (symbol, interval, min(bars, 200)),
        )
        return [
            {"ts": r[0], "open": r[1], "high": r[2], "low": r[3],
             "close": r[4], "volume": r[5]}
            for r in cur.fetchall()
        ][::-1]


# Alert keys (and the daemon's feed) use the full stream symbol. Jarvis and
# the agent naturally say "BTC", so "BTC" must resolve to "btcusdt" — storing
# an alert under "btc" silently never matched _check_alerts("btcusdt", ...).
_SYMBOL_ALIASES = {
    "btc": "btcusdt",
    "eth": "ethusdt",
    "sol": "solusdt",
    "paxg": "paxgusdt",
    "xaut": "xautusdt",
}


def normalize_symbol(symbol: str) -> str:
    s = str(symbol or "").strip().lower()
    return _SYMBOL_ALIASES.get(s, s)


def get_alerts(symbol: str) -> dict:
    return _load_alerts().get(normalize_symbol(symbol), {})


def set_alert(symbol: str, **kwargs) -> dict:
    data = _load_alerts()
    sym = normalize_symbol(symbol)
    if sym not in data:
        data[sym] = dict(_DEFAULT_ALERTS.get(sym, {}))
    data[sym].update(kwargs)
    _save_alerts(data)
    return data[sym]


def remove_alert(symbol: str, key: str) -> dict:
    data = _load_alerts()
    sym = normalize_symbol(symbol)
    if sym in data and key in data[sym]:
        del data[sym][key]
        _save_alerts(data)
    return data.get(sym, {})


def database_path() -> Path:
    """The SQLite file this daemon writes (knowledge/binance_market.db)."""
    return _DB_PATH


_INTERVAL_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000}


def fetch_recent_closed(symbol: str, interval: str = "1m", limit: int = 200,
                        persist: bool = True) -> list:
    """Fetch recent CLOSED candles from Binance REST (real data only).

    Includes the exchange-reported taker-buy base volume (kline index 9) so
    downstream strategies can estimate taker-side pressure. Persisted rows are
    INSERT OR REPLACE (closed-only), never a reset.
    """
    sym = normalize_symbol(symbol).upper()
    if interval not in _INTERVAL_MS:
        raise ValueError(f"interval must be one of {sorted(_INTERVAL_MS)}")
    limit = max(1, min(int(limit), 1000))
    params = urllib.parse.urlencode(
        {"symbol": sym, "interval": interval, "limit": limit})
    rows = _rest_get(f"/klines?{params}", timeout=10)
    now_ms = int(time.time() * 1000)
    out = []
    for r in rows:
        close_ts = int(r[6])
        if close_ts > now_ms:
            continue
        tb = (float(r[9]) if len(r) > 9 and r[9] not in (None, "") else None)
        out.append({
            "symbol": sym,
            "interval": interval,
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
            "volume": float(r[5]),
            "taker_buy_volume": tb,
            "closed": True,
            "ts": int(r[0]),
            "close_ts": close_ts,
        })
    if persist and out:
        conn = _open_db()
        with _db_lock:
            for candle in out:
                conn.execute(
                    "INSERT OR REPLACE INTO candles"
                    " (symbol, interval, open, high, low, close, volume,"
                    "  taker_buy_volume, ts, closed)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (candle["symbol"], candle["interval"], candle["open"],
                     candle["high"], candle["low"], candle["close"],
                     candle["volume"], candle["taker_buy_volume"],
                     candle["ts"], 1),
                )
            conn.commit()
    return out


def latest_closed_candles(symbol: str, interval: str = "1m",
                          limit: int = 200) -> list:
    """Recent closed candles from the local store, chronological order."""
    sym = normalize_symbol(symbol).upper()
    limit = max(1, min(int(limit), 1000))
    conn = _open_db()
    with _db_lock:
        rows = conn.execute(
            "SELECT ts, open, high, low, close, volume, taker_buy_volume "
            "FROM candles WHERE symbol=? AND interval=? AND closed=1 "
            "ORDER BY ts DESC LIMIT ?",
            (sym, interval, limit),
        ).fetchall()
    span = _INTERVAL_MS.get(interval, 60_000)
    return [
        {"symbol": sym, "interval": interval, "ts": r[0],
         "open": r[1], "high": r[2], "low": r[3], "close": r[4],
         "volume": r[5], "taker_buy_volume": r[6],
         "close_ts": r[0] + span, "closed": True}
        for r in reversed(rows)
    ]


def status() -> dict:
    with _state_lock:
        latest = {s: dict(v) for s, v in _latest_ticker.items()}
        ws_up, transport = _ws_up, _transport
        uptime = round(time.monotonic() - _connect_time, 1) if _connect_time else 0
    db = _open_db()
    with _db_lock:
        try:
            cur = db.cursor()
            cur.execute("SELECT COUNT(*) FROM tickers")
            ticker_rows = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM candles")
            candle_rows = cur.fetchone()[0]
        except Exception:
            ticker_rows = candle_rows = 0
    last_ts = max((t.get("ts", 0) for t in latest.values()), default=0)
    age_s = None
    if last_ts:
        age_s = round(time.time() - last_ts / 1000, 1)
    return {
        # ws_up is WS-specific; `live` means any real feed (WS or REST) is up.
        "ws_up": ws_up,
        "live": transport in ("ws", "http") and age_s is not None,
        "transport": transport,
        "uptime_s": uptime,
        "last_tick_age_s": age_s,
        "symbols_tracking": list(_SYMBOLS),
        "latest": latest,
        "db": {"ticker_rows": ticker_rows, "candle_rows": candle_rows},
    }


# ── Alert checking ────────────────────────────────────────────────────────────
def _check_alerts(sym: str, ticker: dict, c1m: dict | None, c5m: dict | None) -> list:
    alerts_data = _load_alerts()
    cfg = alerts_data.get(sym, {})
    if not cfg:
        return []
    triggered = []
    price = ticker["price"]

    if "below" in cfg and price <= cfg["below"]:
        key = f"{sym}_below"
        if not _is_cooldown(key):
            triggered.append(
                f"🔴 *{sym.upper()}* dropped **below ${cfg['below']:,}** "
                f"→ `${price:,.2f}`"
            )
            _mark_fired(key)

    if "above" in cfg and price >= cfg["above"]:
        key = f"{sym}_above"
        if not _is_cooldown(key):
            triggered.append(
                f"🟢 *{sym.upper()}* broke **above ${cfg['above']:,}** "
                f"→ `${price:,.2f}`"
            )
            _mark_fired(key)

    for interval, candle in [("1m", c1m), ("5m", c5m)]:
        if not candle or not candle.get("closed"):
            continue
        pct_key = f"pct_{interval}"
        if pct_key not in cfg:
            continue
        o, c = candle["open"], candle["close"]
        pct_move = abs(c - o) / o * 100 if o else 0
        if pct_move >= cfg[pct_key]:
            key = f"{sym}_{interval}"
            if not _is_cooldown(key):
                direction = "🚀 UP" if c > o else "💥 DOWN"
                triggered.append(
                    f"{direction} *{sym.upper()}* {interval} candle: "
                    f"**{pct_move:.2f}%** (${o:,.2f} → ${c:,.2f})"
                )
                _mark_fired(key)

    return triggered


# ── WebSocket daemon ──────────────────────────────────────────────────────────
def _handle_ws_message(d: dict) -> None:
    """Apply one stream event. In-progress candles update memory only —
    the database stores CLOSED candles exclusively."""
    e = d.get("e")

    if e == "24hrTicker":
        sym = d.get("s", "").lower()
        if sym not in _SYMBOLS:
            return
        ticker = {
            "symbol": sym.upper(),
            "price": float(d["c"]),
            "change_24h": float(d["P"]),
            "high_24h": float(d["h"]),
            "low_24h": float(d["l"]),
            "volume": float(d["v"]),
            "ts": d["E"],
        }
        _set_ticker(sym, ticker)
        conn = _open_db()
        with _db_lock:
            conn.execute(
                "INSERT INTO tickers (symbol, price, change_24h,"
                " high_24h, low_24h, volume, ts)"
                " VALUES (?,?,?,?,?,?,?)",
                (ticker["symbol"], ticker["price"], ticker["change_24h"],
                 ticker["high_24h"], ticker["low_24h"], ticker["volume"],
                 ticker["ts"]),
            )
            conn.commit()
        with _state_lock:
            c1m = (_latest_candles.get(sym) or {}).get("1m")
            c5m = (_latest_candles.get(sym) or {}).get("5m")
        alerts = _check_alerts(sym, ticker, c1m, c5m)
        if alerts:
            _fire_alert(alerts)

    elif e == "kline":
        k = d.get("k", {})
        sym = k.get("s", "").lower()
        if sym not in _SYMBOLS:
            return
        interval = k.get("i", "")
        taker_buy = k.get("V")
        candle = {
            "symbol": sym.upper(),
            "interval": interval,
            "open": float(k["o"]),
            "high": float(k["h"]),
            "low": float(k["l"]),
            "close": float(k["c"]),
            "volume": float(k["v"]),
            # Binance kline field V = taker buy base-asset volume. This is an
            # exchange-reported taker-side estimate, not full order flow.
            "taker_buy_volume": float(taker_buy) if taker_buy is not None else None,
            "closed": k.get("x", False),
            "ts": k["t"],
            "close_ts": k.get("T"),
        }
        _set_candle(sym, interval, candle)
        if not candle["closed"]:
            return
        conn = _open_db()
        with _db_lock:
            conn.execute(
                "INSERT OR REPLACE INTO candles"
                " (symbol, interval, open, high, low, close, volume,"
                "  taker_buy_volume, ts, closed)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (candle["symbol"], candle["interval"],
                 candle["open"], candle["high"], candle["low"],
                 candle["close"], candle["volume"],
                 candle["taker_buy_volume"], candle["ts"], 1),
            )
            conn.commit()
        _notify_closed(candle)


async def _ws_loop(connected: asyncio.Event | None = None) -> None:
    global _ws_up, _connect_time, _transport
    delay = _RECONNECT_BASE
    announced = False
    host_idx = 0
    try:
        import websockets
    except ImportError:
        print("[BinanceWS] websockets not installed — HTTP poll only", flush=True)
        return

    while True:
        stream_url = _STREAM_HOSTS[host_idx] + _STREAMS
        try:
            async with websockets.connect(
                stream_url,
                ping_interval=20,
                ping_timeout=10,
                open_timeout=10,
                max_size=2**20,
            ) as ws:
                _ws_up = True
                _connect_time = time.monotonic()
                _transport = "ws"
                delay = _RECONNECT_BASE
                if connected is not None:
                    connected.set()
                print(f"[BinanceWS] 🟢 WebSocket connected "
                      f"({stream_url.split('/stream')[0].split('//')[1]})",
                      flush=True)
                if not announced:
                    announced = True
                    _fire_summary([f"🟢 Connected · tracking {', '.join(s.upper() for s in _SYMBOLS)}"])

                summary_next = time.monotonic() + _SUMMARY_INTERVAL_S
                prune_next = time.monotonic() + 3600

                async for raw in ws:
                    now_mono = time.monotonic()

                    if now_mono >= summary_next:
                        summary_next = now_mono + _SUMMARY_INTERVAL_S
                        await _emit_summary()
                    if now_mono >= prune_next:
                        prune_next = now_mono + 3600
                        _prune_old()

                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    if "data" not in msg:
                        continue
                    try:
                        _handle_ws_message(msg["data"])
                    except Exception as e:
                        # A single malformed frame must not drop a healthy
                        # stream and force a host rotation/reconnect.
                        print(f"[BinanceWS] message error: "
                              f"{type(e).__name__}: {str(e)[:120]}", flush=True)
                        continue

        except asyncio.CancelledError:
            print("[BinanceWS] cancelled", flush=True)
            raise
        except Exception as e:
            host_idx = (host_idx + 1) % len(_STREAM_HOSTS)
            print(f"[BinanceWS] WS error: {type(e).__name__}: {str(e)[:120]} "
                  f"— next host, retry in {delay}s", flush=True)
            _ws_up = False
            _transport = "none"
            await asyncio.sleep(delay)
            delay = min(delay * 2, _RECONNECT_MAX)


# ── HTTP polling fallback ─────────────────────────────────────────────────────
async def _poll_loop() -> None:
    global _connect_time, _transport
    print("[BinanceWS] 📡 HTTP poll mode (WS unavailable)", flush=True)
    connected = False
    summary_next = time.monotonic() + _SUMMARY_INTERVAL_S
    prune_next = time.monotonic() + 3600
    next_poll = 0.0

    while True:
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise

        now_mono = time.monotonic()
        if now_mono >= summary_next:
            summary_next = now_mono + _SUMMARY_INTERVAL_S
            await _emit_summary()
        if now_mono >= prune_next:
            prune_next = now_mono + 3600
            _prune_old()

        # One REST cycle every _POLL_INTERVAL_S, not every second. The old
        # sleep(1) re-fetched the whole market + 15 klines every second, which
        # is well past Binance's request-weight budget.
        if now_mono < next_poll:
            continue
        next_poll = now_mono + _POLL_INTERVAL_S
        try:
            ok = await _do_poll_cycle()
        except Exception as e:
            print(f"[BinanceWS] poll error: {e}", flush=True)
            continue
        if ok and not connected:
            connected = True
            _connect_time = time.monotonic()
            _transport = "http"
            print("[BinanceWS] 📡 HTTP poll connected", flush=True)


def _rest_get(path: str, timeout: float = 10):
    """GET a REST path, trying the main API then the market-data mirror.

    The working host becomes the preferred one for subsequent calls, so a
    blocked main API costs one timeout, not one per request."""
    global _rest_host_idx
    with _rest_lock:
        start = _rest_host_idx
    errors = []
    for offset in range(len(_REST_HOSTS)):
        idx = (start + offset) % len(_REST_HOSTS)
        base = _REST_HOSTS[idx]
        try:
            req = urllib.request.Request(
                f"{base}{path}",
                headers={"User-Agent": "JarvisBinanceAgent/1.0"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode())
            with _rest_lock:
                _rest_host_idx = idx
            return payload
        except Exception as e:
            errors.append(f"{base}: {type(e).__name__}: {str(e)[:80]}")
    raise RuntimeError("; ".join(errors))


async def _do_poll_cycle() -> bool:
    """Fetch tracked tickers + recent CLOSED candles via REST, persist, alert.

    Returns True when at least the ticker batch succeeded."""
    conn = _open_db()
    now_ms = int(time.time() * 1000)

    # Only the five tracked symbols, one request. Unfiltered /ticker/24hr
    # returns every symbol on the exchange (weight 40) and was called every
    # second by the old loop.
    query = urllib.parse.urlencode({
        "symbols": json.dumps([s.upper() for s in _SYMBOLS], separators=(",", ":"))
    })
    try:
        data = _rest_get(f"/ticker/24hr?{query}", timeout=10)
    except Exception as e:
        print(f"[BinanceWS] ticker fetch failed: {e}", flush=True)
        return False

    if isinstance(data, dict):        # a single-symbol response is an object
        data = [data]

    for item in data:
        sym = item.get("symbol", "").lower()
        if sym not in _SYMBOLS:
            continue
        ticker = {
            "symbol": sym.upper(),
            "price": float(item.get("lastPrice", 0)),
            "change_24h": float(item.get("priceChangePercent", 0)),
            "high_24h": float(item.get("highPrice", 0)),
            "low_24h": float(item.get("lowPrice", 0)),
            "volume": float(item.get("volume", 0)),
            "ts": int(time.time() * 1000),
        }
        _set_ticker(sym, ticker)
        with _db_lock:
            conn.execute(
                "INSERT INTO tickers (symbol, price, change_24h,"
                " high_24h, low_24h, volume, ts)"
                " VALUES (?,?,?,?,?,?,?)",
                (ticker["symbol"], ticker["price"],
                 ticker["change_24h"], ticker["high_24h"],
                 ticker["low_24h"], ticker["volume"],
                 ticker["ts"]),
            )
        with _state_lock:
            c1m = (_latest_candles.get(sym) or {}).get("1m")
            c5m = (_latest_candles.get(sym) or {}).get("5m")
        alerts = _check_alerts(sym, ticker, c1m, c5m)
        if alerts:
            _fire_alert(alerts)

    # Candles: keep only bars whose close time has passed. The last row of a
    # /klines series is the in-progress candle; storing it as final made the
    # stored close lag reality and re-wrote it every cycle.
    for sym in _SYMBOLS:
        for interval in ("1m", "3m", "5m", "15m"):
            try:
                params = urllib.parse.urlencode(
                    {"symbol": sym.upper(), "interval": interval, "limit": 5})
                rows = _rest_get(f"/klines?{params}", timeout=8)
                closed_rows = [r for r in rows if int(r[6]) <= now_ms]
                if not closed_rows:
                    continue
                last = closed_rows[-1]
                taker_buy = (float(last[9]) if len(last) > 9
                             and last[9] not in (None, "") else None)
                candle = {
                    "symbol": sym.upper(),
                    "interval": interval,
                    "open": float(last[1]),
                    "high": float(last[2]),
                    "low": float(last[3]),
                    "close": float(last[4]),
                    "volume": float(last[5]),
                    "taker_buy_volume": taker_buy,
                    "closed": True,
                    "ts": int(last[0]),
                    "close_ts": int(last[6]),
                }
                _set_candle(sym, interval, candle)
                delivered = []
                with _db_lock:
                    for krow in closed_rows:
                        tb = (float(krow[9]) if len(krow) > 9
                              and krow[9] not in (None, "") else None)
                        conn.execute(
                            "INSERT OR REPLACE INTO candles"
                            " (symbol, interval, open, high, low, close, volume,"
                            "  taker_buy_volume, ts, closed)"
                            " VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (sym.upper(), interval,
                             float(krow[1]), float(krow[2]), float(krow[3]),
                             float(krow[4]), float(krow[5]), tb,
                             int(krow[0]), 1),
                        )
                        delivered.append({
                            "symbol": sym.upper(), "interval": interval,
                            "open": float(krow[1]), "high": float(krow[2]),
                            "low": float(krow[3]), "close": float(krow[4]),
                            "volume": float(krow[5]),
                            "taker_buy_volume": tb,
                            "closed": True, "ts": int(krow[0]),
                            "close_ts": int(krow[6]),
                        })
                # Ascending order: a reconnect may have missed more than one
                # bar, and the engine must see them in sequence.
                for c in delivered:
                    _notify_closed(c)
            except Exception as e:
                print(f"[BinanceWS] candle fetch {sym}/{interval}: {e}", flush=True)

    with _db_lock:
        conn.commit()
    return True


async def _emit_summary() -> None:
    """Emit a concise one-line-per-symbol alert — no 24h range, no 1h stats."""
    # Alerts-only policy: skip generation entirely when summaries are disabled.
    # The ticker/candle/alert pipeline is untouched.
    if not _SEND_MARKET_SUMMARIES:
        return
    with _state_lock:
        snapshots = dict(_latest_ticker)
    if not snapshots:
        print("[BinanceWS] no prices yet — summary skipped", flush=True)
        return
    lines = []
    for sym in _SYMBOLS:
        t = snapshots.get(sym)
        if not t:
            continue
        emoji = "🟢" if t["change_24h"] >= 0 else "🔴"
        line = f"{emoji} *{t['symbol']}* `${t['price']:,.2f}` ({t['change_24h']:+.2f}%)"
        lines.append(line)
    _fire_summary(lines)


# ── Lifecycle ─────────────────────────────────────────────────────────────────
_daemon_future: concurrent.futures.Future | None = None
_daemon_lock = threading.Lock()


def start() -> None:
    """Start the daemon on its dedicated loop thread. Idempotent."""
    global _daemon_future
    with _daemon_lock:
        if _daemon_future is not None and not _daemon_future.done():
            return
        loop = _get_loop()
        _daemon_future = asyncio.run_coroutine_threadsafe(_run_daemon(), loop)
        print("[BinanceWS] daemon starting", flush=True)


def stop() -> None:
    global _daemon_future, _ws_up, _transport
    with _daemon_lock:
        if _daemon_future is not None:
            _daemon_future.cancel()
            _daemon_future = None
        _ws_up = False
        _transport = "none"
        print("[BinanceWS] daemon stopped", flush=True)


def emit_summary() -> bool:
    """Queue an immediate summary without blocking the caller.

    Returns False when the alerts-only policy has summaries disabled, so
    callers (e.g. the Jarvis summary action) can answer honestly.
    """
    if not _SEND_MARKET_SUMMARIES:
        return False
    try:
        asyncio.run_coroutine_threadsafe(_emit_summary(), _get_loop())
        return True
    except Exception as e:
        print(f"[BinanceWS] summary queue failed: {e}", flush=True)
        return False


async def _run_daemon() -> None:
    """WS first; fall back to HTTP polling when it cannot connect.

    No simulation: when the venue is unreachable, status() honestly reports
    transport=none instead of inventing prices Jarvis might repeat as real.
    """
    try:
        import websockets  # noqa: F401 — availability check
    except ImportError:
        print("[BinanceWS] websockets unavailable — using HTTP poll", flush=True)
        await _poll_loop()
        return

    connected = asyncio.Event()
    ws_task = asyncio.create_task(_ws_loop(connected))
    try:
        try:
            # Only the CONNECTION gets a deadline. Wrapping the whole infinite
            # _ws_loop() in wait_for (the old code) cancelled a healthy stream
            # after 20 s and silently degraded every session to HTTP polling.
            await asyncio.wait_for(connected.wait(), timeout=_WS_CONNECT_TIMEOUT_S)
        except asyncio.TimeoutError:
            print(f"[BinanceWS] WS did not connect in {_WS_CONNECT_TIMEOUT_S:g}s "
                  "— HTTP poll fallback", flush=True)
            ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await ws_task
            await _poll_loop()
            return
        await ws_task
    finally:
        if not ws_task.done():
            ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await ws_task


# ── CLI (Jarvis action + Hermes skills share this entry point) ───────────────
def _cli(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m core.binance_ws",
        description="Binance market data: daemon, status, prices, stats, alerts.",
    )
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("status", help="connection + database state (JSON)")
    sub.add_parser("prices", help="latest ticker per symbol (JSON)")
    q = sub.add_parser("query", help="recent stats + candles (JSON)")
    q.add_argument("symbol", nargs="?", default="BTC")
    q.add_argument("--hours", type=float, default=1)
    q.add_argument("--interval", default="1m", choices=["1m", "5m", "15m"])
    q.add_argument("--bars", type=int, default=20)
    sub.add_parser("summary", help="push one summary through Hermes now")
    sub.add_parser("run", help="run the daemon in the foreground (Ctrl+C stops)")
    args = parser.parse_args(argv)

    if args.cmd in (None, "status"):
        print(json.dumps(status(), indent=2))
        return 0
    if args.cmd == "prices":
        print(json.dumps(latest_prices(), indent=2))
        return 0
    if args.cmd == "query":
        print(json.dumps({
            "stats": hourly_stats(args.symbol, args.hours),
            "candles": query_history(args.symbol, args.interval, args.bars),
        }, indent=2))
        return 0
    if args.cmd == "summary":
        emit_summary()
        print("summary queued")
        return 0
    if args.cmd == "run":
        start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            stop()
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
