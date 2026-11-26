"""
dashboard/trading_api.py — Phase 9 Trading Center + Mission Center backend.

Everything here is read-through over the EXISTING trading stack
(orchestrator/agents/trading: MarketData / analysis / signal / risk /
PaperBroker / TradingAgent) and the EXISTING autonomy engine
(orchestrator/autonomy: TaskStore / Scheduler / AutonomyEngine). No market
data, signal or price is ever synthesised:

    * every quote/bar/chain/news answer is carried with its Provenance
      label (delayed / live / unavailable) straight from data.DataResult;
    * a fetch that fails returns {ok:false, provenance:"unavailable"} —
      the UI shows DISCONNECTED, never a placeholder price;
    * the simulated adapter is NEVER selected here (prefer_sim is not
      exposed on any route);
    * broker status is honest: paper is the default; Delta live is shown as
      CONNECTED only when the separate live switch, credentials and HALT
      check all allow it. The switch is never enabled by this API on its own.

Endpoints (bearer auth, same _auth as every /api route):

    GET  /api/rio/trading/connection   data provider + broker + kill-switch
    GET  /api/rio/trading/overview     quotes for indices + watchlist
    GET  /api/rio/trading/watchlist    real watchlist store
    POST /api/rio/trading/watchlist    {action: add|remove, symbol}
    GET  /api/rio/trading/chart        real OHLC bars for the SVG chart
    GET  /api/rio/trading/charts       stored chart PNGs on disk
    GET  /api/rio/trading/chart-file   whitelisted PNG delivery
    GET  /api/rio/trading/signals      signal checkpoints (history)
    POST /api/rio/trading/analyze      run the REAL TradingAgent pipeline
    GET  /api/rio/trading/options      served options chain + greeks
    GET  /api/rio/trading/news         served news (or honest unavailable)
    GET  /api/rio/trading/positions    paper positions (open + closed)
    GET  /api/rio/trading/orders       paper order journal + live order journal
    GET  /api/rio/trading/risk         safety config + daily state + live switch
    GET  /api/rio/trading/live/status  separate live-execution switch status
    POST /api/rio/trading/live/probe   read-only signed Delta account probe
    POST /api/rio/trading/live/enable  {confirm:true, environment, phrase} ARM
    POST /api/rio/trading/live/disable unconditional disarm
    POST /api/rio/trading/approve      {confirm:true, task_id, phrase} manual order
    GET  /api/rio/trading/autotrade    Auto Trade state (default STOPPED)
    POST /api/rio/trading/autotrade/start   {confirm: true} REQUIRED
    POST /api/rio/trading/autotrade/stop    unconditional stop
    POST /api/rio/trading/autotrade/emergency  HALT sentinel + autonomy stop

    GET  /api/rio/missions             missions + capabilities (honest)
    POST /api/rio/missions/start       real scheduler.run_task in background
    POST /api/rio/missions/pause       real store.update(enabled=false)
    POST /api/rio/missions/resume      real store.update(enabled=true)

Auto Trade rules (spec): default STOPPED (no file → stopped), never
enabled automatically (only an explicit confirm:true POST flips it),
START requires confirmation, RUNNING when enabled, plus STOP and
EMERGENCY STOP. The emergency path (a) disables Auto Trade, (b) trips the
vibe-trading kill-switch sentinel ~/.vibe-trading/live/HALT (the real
filesystem-enforced kill switch, SPEC Consent §4), (c) trips the
autonomy engine's emergency stop — the SAME engine call behind the real
/api/autonomy/stop endpoint, which the UI button also calls directly.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse

from dashboard.rio_api import BASE_DIR, _age_s, _tail_jsonl

TRADING_DIR = BASE_DIR / "memory" / "trading"
WATCHLIST_FILE = TRADING_DIR / "watchlist.json"
SIGNALS_JOURNAL = TRADING_DIR / "signals.jsonl"
AUTOTRADE_FILE = TRADING_DIR / "auto_trade.json"
AUTOTRADE_AUDIT = TRADING_DIR / "auto_trade_audit.jsonl"
CHARTS_DIR = TRADING_DIR / "charts"
AGENT_CP_DIR = BASE_DIR / "memory" / "trading_agent"
HALT_SENTINEL = Path.home() / ".vibe-trading" / "live" / "HALT"
LIVE_JOURNAL = TRADING_DIR / "live_journal.jsonl"
LIVE_ENABLE_PHRASE = "ENABLE LIVE EXECUTION"
LIVE_ORDER_PHRASE = "PLACE LIVE ORDER"

# Real index instruments for Market Overview (yfinance symbols, not prices)
INDICES = ("^NSEI", "^BSESN", "^GSPC", "^IXIC", "^DJI")

_CONN_TTL_S = 30.0
_conn_cache: dict = {"t": 0.0, "data": None}

_watch_lock = threading.Lock()
_mission_lock = threading.Lock()
_running_missions: set[str] = set()

_SYMBOL_RE = re.compile(r"^[A-Za-z0-9^.\-=_]{1,24}$")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe_symbol(raw) -> str:
    s = str(raw or "").strip().upper()
    return s if _SYMBOL_RE.match(s) else ""


def _venue(raw) -> str:
    """Only the real venues are accepted; 'auto' is the legacy default."""
    v = str(raw or "").strip().lower()
    if v in ("", "auto"):
        return ""
    if v in ("delta", "delta_india", "delta_perp", "delta_spot"):
        return "delta"
    return "__invalid__"


def _md():
    """Real market data only — the simulated adapter is never chosen."""
    from orchestrator.agents.trading.data import MarketData
    return MarketData(prefer_sim=False)


def _halt_state() -> dict:
    """The vibe-trading kill switch: a file's EXISTENCE is the halt."""
    if HALT_SENTINEL.exists():
        detail = ""
        try:
            detail = HALT_SENTINEL.read_text(encoding="utf-8")[:300]
        except OSError:
            detail = "(unreadable sentinel — still treated as tripped)"
        return {"tripped": True, "path": str(HALT_SENTINEL), "detail": detail}
    return {"tripped": False, "path": str(HALT_SENTINEL), "detail": ""}


DELTA_DEMO_URL = "https://demo.delta.exchange"


def _delta_error_details(msg: str) -> dict:
    """Turn a signed-Delta failure into an actionable, UI-safe explanation.

    The exchange's own message stays the source of truth; this only adds the
    kind of failure, the client IP Delta saw (when it reports one) and the
    next action. No credential ever enters this payload.
    """
    text = str(msg or "").strip()
    low = text.lower()
    if "ip_not_whitelisted_for_api_key" in low:
        ip = ""
        match = re.search(
            r"client_ip['\"]?\s*[:=]\s*['\"]([^'\"]+)['\"]", text)
        if match:
            ip = match.group(1).strip()
        if not ip:
            match = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", text)
            ip = match.group(1) if match else ""
        subject = f"Add {ip} to" if ip else "Add this machine's egress IP to"
        return {
            "code": "ip_not_whitelisted",
            "client_ip": ip,
            "action_url": DELTA_DEMO_URL,
            "hint": (f"{subject} the API key's IP whitelist in Delta demo "
                     f"({DELTA_DEMO_URL}) → API Keys, then run "
                     f"\"Check account\" again."),
        }
    if "invalid_api_key" in low:
        return {
            "code": "invalid_api_key",
            "action_url": DELTA_DEMO_URL,
            "hint": ("Delta rejected the key itself. Save a demo API key from "
                     f"the Delta demo account ({DELTA_DEMO_URL}) in Settings, "
                     "then run the read-only probe again."),
        }
    if "expired_signature" in low or "invalid_signature" in low:
        return {
            "code": "signature",
            "hint": ("Sync this machine's clock with NTP "
                     "(timedatectl set-ntp true), then retry the probe."),
        }
    return {}


# ── live execution switch / authenticated Delta account ──────────────────
def _live_cache_clear() -> None:
    _conn_cache["t"] = 0.0
    _conn_cache["data"] = None


def _live_state() -> dict:
    """Status of the trading agent's separate live switch.

    Trading_safety.json's mode is shared with Binance and is NOT the switch.
    Nothing is armed unless live_execution.json says enabled AND credentials
    exist AND the HALT sentinel is absent.
    """
    from orchestrator.agents.trading.risk import load_live, trading_dir
    from orchestrator.agents.trading.delta_private import base_url_for
    from orchestrator.agents.delta_exchange import (
        resolve_credentials, credentials_source,
    )
    cfg = load_live()
    key, secret = resolve_credentials()
    has_creds = bool(key and secret)
    halt = _halt_state()
    try:
        base_url = base_url_for(str(cfg.get("environment") or "demo"))
    except Exception:
        base_url = ""
    armed = bool(cfg.get("enabled") and has_creds and not halt["tripped"])
    probe_ok = bool(cfg.get("last_probe_ok"))
    probe_error = {} if probe_ok else _delta_error_details(
        cfg.get("last_probe_detail") or "")
    return {
        "enabled": bool(cfg.get("enabled")),
        "armed": armed,
        "environment": str(cfg.get("environment") or "demo"),
        "base_url": base_url,
        "credentials_configured": has_creds,
        "credentials_source": credentials_source(),
        "max_order_usd": cfg.get("max_order_usd"),
        "allow_auto": bool(cfg.get("allow_auto")),
        "halted": bool(halt["tripped"]),
        "halt": halt,
        "enabled_at": cfg.get("enabled_at") or "",
        "enabled_by": cfg.get("enabled_by") or "",
        "last_probe_at": cfg.get("last_probe_at") or "",
        "last_probe_ok": probe_ok,
        "last_probe_detail": cfg.get("last_probe_detail") or "",
        "last_probe_code": probe_error.get("code") or "",
        "last_probe_client_ip": probe_error.get("client_ip") or "",
        "last_probe_hint": probe_error.get("hint") or "",
        "last_probe_action_url": probe_error.get("action_url") or "",
        "clock_offset_s": int(cfg.get("clock_offset_s") or 0),
        "phrase_required": LIVE_ORDER_PHRASE,
        "switch_file": str(trading_dir() / "live_execution.json"),
    }


def _live_probe(environment: str | None = None) -> dict:
    """Read-only signed probe. Does not enable anything."""
    from orchestrator.agents.trading.delta_private import (
        DeltaPrivateClient, DeltaPrivateError, base_url_for,
    )
    from orchestrator.agents.trading.risk import load_live, save_live
    env = str(environment or load_live().get("environment") or "demo").lower()
    if env not in ("demo", "prod"):
        return {"ok": False, "error": "environment must be demo or prod"}
    try:
        client = DeltaPrivateClient(env)
        if not client.credentials_configured():
            return {"ok": False, "environment": env,
                    "error": "Delta API key/secret are not configured"}
        account = client.probe()
        detail = (f"authenticated · net_equity="
                  f"{account.get('net_equity')} · "
                  f"positions={account.get('position_count', 0)}")
        try:
            save_live({"last_probe_at": _now(), "last_probe_ok": True,
                       "last_probe_detail": detail,
                       "clock_offset_s": int(client.clock_offset_s or 0)})
        except Exception:
            pass
        _live_cache_clear()
        account["credential_probe"] = True
        account["note"] = ("read-only wallet/position probe; no order was sent")
        return account
    except DeltaPrivateError as exc:
        msg = str(exc)
        details = _delta_error_details(msg)
        try:
            save_live({"last_probe_at": _now(), "last_probe_ok": False,
                       "last_probe_detail": msg[:300]})
        except Exception:
            pass
        _live_cache_clear()
        out = {"ok": False, "environment": env,
               "base_url": base_url_for(env), "error": msg,
               "credential_probe": False, **details}
        if details.get("code") == "signature":
            out["error"] = (msg + " · This is a system-clock skew problem, "
                            "not a key problem: the client retried once with "
                            "the server-reported offset and Delta still "
                            "rejected it. Sync this machine's clock with NTP "
                            "and run the probe again.")
        return out
    except Exception as exc:
        return {"ok": False, "environment": env,
                "error": f"{type(exc).__name__}: {str(exc)[:240]}",
                "credential_probe": False}


def _live_enable(environment: str, phrase: str,
                 by: str = "dashboard-confirm") -> dict:
    """Arm live execution only after an exact phrase + a real read-only probe."""
    from orchestrator.agents.trading.risk import load_live, save_live
    if str(phrase or "").strip().upper() != LIVE_ENABLE_PHRASE:
        return {"ok": False,
                "error": f'confirmation phrase must be exactly "{LIVE_ENABLE_PHRASE}"'}
    env = str(environment or "demo").strip().lower()
    if env not in ("demo", "prod"):
        return {"ok": False, "error": "environment must be demo or prod"}
    halt = _halt_state()
    if halt["tripped"]:
        return {"ok": False,
                "error": "kill switch HALT is set — clear the sentinel first",
                "halt": halt}
    cfg = load_live()
    try:
        cap = float(cfg.get("max_order_usd") or 0.0)
    except (TypeError, ValueError):
        cap = 0.0
    if cap <= 0:
        return {"ok": False,
                "error": "live_execution.json max_order_usd must be > 0"}

    probe = _live_probe(env)
    if not probe.get("ok"):
        hint = str(probe.get("hint") or "")
        if not hint and ("expired_signature" in str(probe.get("error") or "")
                         or "invalid_signature" in str(probe.get("error") or "")):
            hint = "Sync the machine clock with NTP and retry."
        extra = {k: probe[k] for k in
                 ("code", "client_ip", "hint", "action_url") if probe.get(k)}
        return {"ok": False,
                "error": ("credential probe failed — live execution not armed: "
                          + str(probe.get("error") or "unknown error")
                          + (f" · {hint}" if hint else "")),
                "probe": probe, "environment": env, **extra}
    saved = save_live({
        "enabled": True, "environment": env,
        "enabled_at": _now(), "enabled_by": str(by)[:80],
        "ack": LIVE_ENABLE_PHRASE,
        "last_probe_at": _now(), "last_probe_ok": True,
        "last_probe_detail": (f"authenticated · net_equity="
                              f"{probe.get('net_equity')}"),
    })
    _live_cache_clear()
    return {"ok": True, "live": _live_state(), "account": probe,
            "saved": saved,
            "note": ("LIVE switch armed. No order was placed. Each order still "
                     "requires a separate manual approval with the phrase "
                     f"'{LIVE_ORDER_PHRASE}'.")}


def _live_disable(by: str = "dashboard") -> dict:
    from orchestrator.agents.trading.risk import save_live
    save_live({"enabled": False, "ack": "", "enabled_at": "",
               "enabled_by": str(by)[:80]})
    _live_cache_clear()
    return {"ok": True, "live": _live_state(),
            "note": "LIVE switch disabled; the trading agent is paper-only."}


# ═════════════════════════════════════════════════════════════ Delta India
def _delta_contract_type(market: str) -> str | None:
    m = str(market or "").strip().lower()
    if m in ("perp", "perpetual", "delta_perp"):
        return "perpetual_futures"
    if m in ("spot", "delta_spot"):
        return "spot"
    return None


def _delta_resolve(symbol: str, market: str = "") -> dict:
    """Resolve a Delta product before any route touches it.

    Returns {ok, symbol, needs_input, options, error}. Ambiguity is a real
    choice, not a guess; an exact product symbol always wins.
    """
    from orchestrator.agents.delta_exchange import (
        DeltaAmbiguous, DeltaError, DeltaNotFound, get_client,
    )
    raw = str(symbol or "").strip().upper()
    if not raw:
        return {"ok": False, "error": "Delta product symbol required"}
    try:
        product = get_client().resolve_product(
            raw, contract_type=_delta_contract_type(market))
    except DeltaAmbiguous as exc:
        return {"ok": False, "needs_input": True,
                "options": [p.symbol for p in exc.candidates[:24]],
                "error": str(exc)}
    except DeltaNotFound as exc:
        return {"ok": False, "error": str(exc)}
    except DeltaError as exc:
        return {"ok": False, "error": f"Delta product discovery failed: {exc}"}
    return {"ok": True, "symbol": product.symbol,
            "product": product.to_dict()}


def _collect_delta_status() -> dict:
    try:
        from orchestrator.agents.delta_exchange import get_client
        st = get_client().status()
        st["products_endpoint"] = "/api/rio/trading/delta/products"
        st["mode"] = "public data + paper only"
        st["read_only"] = True
        return st
    except Exception as exc:
        return {"ok": False, "status": "DISCONNECTED",
                "mode": "public data + paper only", "read_only": True,
                "error": f"{type(exc).__name__}: {str(exc)[:240]}"}


def _collect_delta_products(query: str = "", market: str = "") -> dict:
    try:
        from orchestrator.agents.delta_exchange import get_client
        client = get_client()
        ct = _delta_contract_type(market)
        if query:
            rows = client.find_products(query, contract_type=ct, limit=500)
        else:
            rows = client.products()
            if ct:
                rows = [p for p in rows if p.contract_type == ct]
        rows = sorted(rows, key=lambda p: (
            {"BTC": 0, "ETH": 1, "SOL": 2, "XRP": 3, "XAUT": 4, "PAXG": 5}
            .get(p.underlying.upper(), 99),
            0 if p.contract_type == "perpetual_futures" else 1,
            p.symbol,
        ))
        return {"ok": True, "status": "CONNECTED",
                "base_url": client.base_url,
                "credentials_configured": client.credentials_configured(),
                "query": query, "market": market,
                "count": len(rows),
                "products": [p.to_dict() for p in rows[:500]],
                "read_only": True, "mode": "public data + paper only"}
    except Exception as exc:
        return {"ok": False, "status": "DISCONNECTED", "products": [],
                "count": 0, "query": query, "market": market,
                "error": f"{type(exc).__name__}: {str(exc)[:240]}",
                "read_only": True, "mode": "public data + paper only"}


def _collect_delta_quote(symbol: str) -> dict:
    from orchestrator.agents.trading.delta import DeltaTradingAdapter
    try:
        res = DeltaTradingAdapter().quote(symbol)
        out = res.to_dict()
        out["symbol"] = symbol
        out["connected"] = bool(res.ok)
        out["venue"] = "delta"
        return out
    except Exception as exc:
        return {"ok": False, "provenance": "unavailable",
                "source": "delta_india", "symbol": symbol, "connected": False,
                "error": f"{type(exc).__name__}: {str(exc)[:240]}"}


# ═════════════════════════════════════════════════════════════ connection
def _collect_connection() -> dict:
    now = time.time()
    cached = _conn_cache["data"]
    if cached is not None and now - _conn_cache["t"] < _CONN_TTL_S:
        data = dict(cached)
        data["cached"] = True
        data["age_s"] = round(now - _conn_cache["t"], 1)
        return data

    # data provider: one real probe quote (no simulation fallback)
    try:
        res = _md().quote(INDICES[0])
        if res.ok and (res.data or {}).get("last") is not None:
            data_state = {"status": "CONNECTED", "provider": "yfinance",
                          "provenance": res.provenance.value,
                          "detail": f"served {INDICES[0]} last="
                                    f"{res.data['last']} ({res.provenance.value})"}
        else:
            data_state = {"status": "DISCONNECTED", "provider": "yfinance",
                          "provenance": res.provenance.value,
                          "detail": res.error or "no price served"}
    except Exception as exc:
        data_state = {"status": "DISCONNECTED", "provider": "yfinance",
                      "provenance": "unavailable",
                      "detail": f"probe failed: {type(exc).__name__}"}

    from orchestrator.agents.trading.risk import load_safety
    safety = load_safety()
    live = _live_state()
    if live["armed"]:
        status = "CONNECTED"
        live_detail = (f"Delta {str(live['environment']).upper()} live execution "
                       f"ARMED · manual approval required · max "
                       f"${float(live.get('max_order_usd') or 0):,.0f}/order")
    elif live["halted"] and live["enabled"]:
        status = "HALTED"
        live_detail = ("HALT kill switch is tripped — live and auto execution "
                       "are blocked")
    elif live["enabled"] and not live["credentials_configured"]:
        status = "PAPER_ONLY"
        live_detail = ("LIVE switch is enabled but Delta API key/secret are "
                       "missing — paper only")
    elif live.get("last_probe_code") == "ip_not_whitelisted":
        status = "PAPER_ONLY"
        ip = live.get("last_probe_client_ip") or "this machine's egress IP"
        live_detail = (f"Delta DEMO key rejected: {ip} is not in the key's IP "
                       "whitelist. Add it in Delta demo → API Keys, then click "
                       "Check account. Paper only until then.")
    elif live.get("last_probe_code"):
        status = "PAPER_ONLY"
        live_detail = (live.get("last_probe_hint")
                       or "Last signed Delta probe failed — paper only.")
    else:
        status = "PAPER_ONLY"
        live_detail = ("Live execution switch is disabled — paper trading is "
                       "the default. Arm it explicitly on this page.")
    broker = {
        "status": status,
        "live_detail": live_detail,
        "paper": {"status": "ONLINE",
                  "detail": "PaperBroker active · human approval required"},
        "mode": str(safety.get("mode") or ""),
        "live": live,
        "credentials_configured": bool(live["credentials_configured"]),
        "environment": live["environment"],
        "armed": bool(live["armed"]),
    }
    data = {"ok": True, "checked_at": _now(), "data_provider": data_state,
            "broker": broker, "live_channel": _halt_state(),
            "delta": _collect_delta_status(),
            "cached": False, "age_s": 0.0}
    _conn_cache["t"] = time.time()
    _conn_cache["data"] = data
    return data


# ═════════════════════════════════════════════════════════════ watchlist
def _seed_watchlist() -> list[str]:
    """Derive a starting watchlist from REAL records — traded symbols,
    backtested symbols and the safety whitelist. Never invented."""
    out: list[str] = []

    def add(sym: str):
        s = _safe_symbol(sym)
        if s and s not in out:
            out.append(s)

    try:
        positions = json.loads(
            (TRADING_DIR / "paper_positions.json").read_text(encoding="utf-8"))
        if isinstance(positions, dict):
            for key in positions:
                add(str(key).split(":", 1)[0])
    except Exception:
        pass
    for rec in _tail_jsonl(TRADING_DIR / "performance.jsonl", n=200):
        add(str(rec.get("symbol") or ""))
        if len(out) >= 8:
            break
    try:
        safety = json.loads(
            (TRADING_DIR / "trading_safety.json").read_text(encoding="utf-8"))
        for s in (safety.get("symbols_whitelist") or []):
            add(str(s))
    except Exception:
        pass
    return out[:10]


def _load_watchlist() -> list[str]:
    try:
        data = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("symbols"), list):
            return [str(s) for s in data["symbols"]]
    except (OSError, ValueError):
        pass
    return _seed_watchlist()


def _save_watchlist(symbols: list[str]) -> None:
    WATCHLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = WATCHLIST_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"symbols": symbols, "updated_at": _now()},
                              indent=1), encoding="utf-8")
    tmp.replace(WATCHLIST_FILE)


# ══════════════════════════════════════════════════════════════ overview
def _quote_bundle(symbol: str) -> dict:
    try:
        res = _md().quote(symbol)
        out = res.to_dict()
        out["connected"] = bool(res.ok and (res.data or {}).get("last")
                                is not None)
        return out
    except Exception as exc:
        return {"ok": False, "provenance": "unavailable", "source": "yfinance",
                "error": f"{type(exc).__name__}: {exc}", "as_of": _now(),
                "data": {"symbol": symbol}, "connected": False}


def _collect_overview() -> dict:
    symbols = list(INDICES) + [s for s in _load_watchlist()
                               if s not in INDICES][:8]
    symbols = symbols[:12]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_quote_bundle, symbols))
    connected = sum(1 for r in results if r.get("connected"))
    return {"ok": True, "ts": _now(), "symbols": symbols,
            "connected": connected, "total": len(symbols),
            "status": "CONNECTED" if connected else "DISCONNECTED",
            "quotes": results}


# ═════════════════════════════════════════════════════════════════ chart
# period whitelist — no arbitrary user strings reach the adapter
_PERIODS = {"5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "max"}

_INTERVALS_YFINANCE = {"1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h", "1d", "5d", "1wk", "1mo", "3mo"}
_INTERVALS_DELTA = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d"}


def _collect_chart(symbol: str, period: str, venue: str = "", interval: str = "1d") -> dict:
    interval = str(interval or "1d").lower()
    # validate interval per venue
    v = str(venue or "").lower()
    if v in ("delta", "delta_perp", "delta_spot", "delta_india"):
        allowed = _INTERVALS_DELTA
    else:
        allowed = _INTERVALS_YFINANCE
    if interval not in allowed:
        # fall back to daily, keep request honest
        interval = "1d"
    try:
        res = _md().history(symbol, period=period, interval=interval,
                            venue=venue or None)
        out = res.to_dict()
        if res.ok:
            bars = (res.data or {}).get("bars") or []
            out["bars"] = bars[-160:]            # bounded payload
            out["count"] = len(out["bars"])
            out["venue"] = (res.data or {}).get("venue") or venue or "auto"
        else:
            out["bars"], out["count"] = [], 0
            out["venue"] = venue or "auto"
        out["symbol"] = symbol
        out["connected"] = bool(res.ok)
        return out
    except Exception as exc:
        return {"ok": False, "provenance": "unavailable", "error":
                f"{type(exc).__name__}: {exc}", "bars": [], "count": 0,
                "symbol": symbol, "connected": False}


def _collect_charts() -> dict:
    files = []
    try:
        for p in sorted(CHARTS_DIR.glob("*.png"),
                        key=lambda x: x.stat().st_mtime, reverse=True):
            st = p.stat()
            files.append({"name": p.name, "bytes": st.st_size,
                          "mtime": datetime.fromtimestamp(
                              st.st_mtime).isoformat(timespec="seconds")})
    except OSError:
        pass
    return {"ok": True, "ts": _now(), "files": files}


# ═══════════════════════════════════════════════════ signals + AI analysis
def _cp_status_view(cp: dict) -> dict:
    prop = cp.get("proposal") or {}
    return {
        "task_id": cp.get("task_id"),
        "symbol": cp.get("symbol"),
        "strategy": cp.get("strategy"),
        "status": cp.get("status"),
        "direction": prop.get("direction"),
        "entry": prop.get("entry"),
        "stop_loss": prop.get("stop_loss"),
        "targets": prop.get("targets"),
        "rr": prop.get("rr_to_target1"),
        "score": prop.get("score"),
        "approved_by": cp.get("approved_by") or cp.get("execution", {}).get(
            "position", {}).get("approved_by"),
        "execution": (cp.get("execution") or {}).get("ok"),
        "at": cp.get("at"),
    }


def _collect_signals(limit: int = 20) -> dict:
    # 1) checkpoint views (approval/execution outcome), keyed by task_id
    cp_map: dict[str, dict] = {}
    try:
        for p in sorted(AGENT_CP_DIR.glob("*.json"),
                        key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                cp = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(cp, dict) or "symbol" not in cp:
                continue
            view = _cp_status_view(cp)
            view["mtime"] = datetime.fromtimestamp(
                p.stat().st_mtime).isoformat(timespec="seconds")
            cp_map[str(cp.get("task_id") or "")] = view
    except OSError:
        pass

    items: list[dict] = []
    journaled: set[str] = set()
    # 2) every proposal the agent computed (journal keeps the risk verdict;
    #    the checkpoint view wins when the run went on to approval)
    try:
        for rec in _tail_jsonl(SIGNALS_JOURNAL, n=limit)[::-1]:
            tid = str(rec.get("task_id") or "")
            journaled.add(tid)
            if tid in cp_map:
                items.append(cp_map[tid])
                continue
            items.append({
                "task_id": tid,
                "symbol": rec.get("symbol"),
                "strategy": rec.get("strategy"),
                "status": ("risk_refused" if rec.get("risk_denied_by")
                           else "proposal"),
                "direction": rec.get("direction"),
                "entry": rec.get("entry"),
                "stop_loss": rec.get("stop_loss"),
                "targets": rec.get("targets"),
                "rr": rec.get("rr"),
                "score": rec.get("score"),
                "risk_denied_by": rec.get("risk_denied_by"),
                "at": rec.get("ts"),
                "mtime": rec.get("ts"),
            })
    except OSError:
        pass
    # 3) checkpoints with no journal row (older runs / non-proposal states)
    for tid, view in cp_map.items():
        if tid not in journaled:
            items.append(view)
    items.sort(key=lambda r: str(r.get("mtime") or ""), reverse=True)
    return {"ok": True, "ts": _now(), "signals": items[:limit]}


def _ai_display(cp: dict, news: dict | None = None, chain: dict | None = None
                ) -> dict:
    """Map a real checkpoint + live side-fetches onto the spec's display
    fields. A field the sources cannot answer stays null with a reason."""
    prop = cp.get("proposal") or {}
    ev = prop.get("evaluation") or {}
    analysis = cp.get("analysis") or {}
    tech = analysis.get("technicals") or {}
    vwap = analysis.get("vwap") or {}
    struct = analysis.get("structure") or {}
    vp = analysis.get("volume_profile") or {}
    verdicts = prop.get("verdicts") or []
    reasons = [{"direction": v.get("direction"), "fired": v.get("fired"),
                "reason": v.get("reason")} for v in verdicts]

    atm = ((chain or {}).get("greeks") or {}).get("atm") or {}
    oi = atm.get("iv")
    open_interest = None
    for leg in ((chain or {}).get("legs_atm") or []):
        open_interest = leg.get("open_interest")
        break

    news_items = []
    if news and news.get("ok"):
        raw = (news.get("data") or {}).get("items") or []
        news_items = [{"title": str(i.get("title"))[:140],
                       "published": str(i.get("published") or "")[:40],
                       "source": str(i.get("source") or "")[:40]}
                      for i in raw[:8] if isinstance(i, dict)]

    return {
        "instrument": cp.get("symbol"),
        "strategy": cp.get("strategy"),
        "checkpoint_status": cp.get("status"),
        "direction": prop.get("direction"),
        "entry": prop.get("entry"),
        "target": prop.get("targets"),
        "stop_loss": prop.get("stop_loss"),
        "risk_reward": ({"t1": prop.get("rr_to_target1"),
                         "t2": prop.get("rr_to_target2"),
                         "t3": prop.get("rr_to_target3")}
                        if prop else None),
        "confidence": ({"score": prop.get("score"),
                        "rules_fired": sum(1 for v in verdicts
                                           if v.get("fired")),
                        "rules_total": len(verdicts),
                        "min_signal_score": (cp.get("risk") or {}).get(
                            "sizing", {}).get("min_signal_score")}
                       if prop else None),
        "reason": ([v["reason"] for v in reasons if v["fired"]]
                   if prop else None),
        "reasons_all": reasons,
        "no_setup_note": None if prop else str(
            (cp.get("no_trade_note") or "no setup met the strategy rules")),
        "vwap": vwap if vwap.get("ok") else None,
        "volume": ({"last": tech.get("volume"),
                    "value_area_high": vp.get("value_area_high"),
                    "value_area_low": vp.get("value_area_low"),
                    "poc": vp.get("poc")}
                   if tech.get("ok") or vp.get("ok") else None),
        "market_structure": struct if struct.get("ok") else None,
        "oi": open_interest,
        "iv": oi,
        "news_sentiment": ({"items": news_items,
                            "sentiment": None,
                            "note": "no sentiment source configured — "
                                    "headlines served as-is"}
                           if news and news.get("ok") else
                           {"items": [], "sentiment": None,
                            "note": (news or {}).get("error")
                            or "news unavailable"}),
        "provenance": cp.get("data_provenance") or [],
        "disclaimer": prop.get("disclaimer") or
                      "proposal only — not financial advice",
    }


def _manual_approve(task_id: str, phrase: str = "") -> dict:
    """Approve a parked proposal. Live requires the exact phrase; paper does
    not. The broker re-checks the switch and HALT on the execution path."""
    from orchestrator.contracts import AgentRequest, TaskType
    from orchestrator.agents.trading.trading_agent import TradingAgent
    tid = str(task_id or "").strip()
    if not re.match(r"^[A-Za-z0-9_.:\-]{1,80}$", tid):
        return {"ok": False, "error": "invalid checkpoint id", "http": 400}
    live = _live_state()
    if live["armed"]:
        if str(phrase or "").strip().upper() != LIVE_ORDER_PHRASE:
            return {"ok": False, "http": 400,
                    "error": (f"live confirmation phrase must be exactly "
                              f"'{LIVE_ORDER_PHRASE}' — nothing was placed")}
        if live["halted"]:
            return {"ok": False, "http": 409,
                    "error": "HALT kill switch is tripped — live order refused"}
    agent = TradingAgent()
    cp = agent._cp.load(tid)
    if not cp:
        return {"ok": False, "http": 404,
                "error": f"no checkpoint {tid!r} — nothing was placed"}
    if cp.get("status") != "awaiting_approval":
        return {"ok": False, "http": 409,
                "error": (f"checkpoint {tid!r} is '{cp.get('status')}' — "
                          f"already handled")}
    approved_by = "user-live" if live["armed"] else "user-paper"
    try:
        result = agent.handle(AgentRequest(
            task_id=f"approve-{tid}", task_type=TaskType.TRADING, text="",
            metadata={"approved": True, "resume": tid,
                      "approved_by": approved_by}))
    except Exception as exc:
        return {"ok": False, "http": 500,
                "error": f"{type(exc).__name__}: {exc}"}
    data = result.data if isinstance(result.data, dict) else {}
    execution = data.get("execution") or {}
    status = getattr(result.status, "value", str(result.status))
    mode = str(data.get("mode") or execution.get("mode") or "paper")
    if live["armed"]:
        _live_cache_clear()
    out = {
        "ok": status == "completed",
        "agent_status": status,
        "text": (result.text or "")[:2000],
        "checkpoint": tid,
        "mode": mode,
        "live": _live_state(),
        "execution": execution,
        "approved_by": approved_by,
    }
    if out["ok"] is False:
        out["error"] = (result.text or execution.get("error")
                        or "approval did not execute")
    return out



def _run_analyze(symbol: str, strategy: str,
                 min_score: int = 2, venue: str = "") -> dict:
    """The REAL TradingAgent pipeline (data → analysis → signal → risk),
    invoked exactly like the orchestrator invokes it. Display only: nothing
    executes here unless Auto Trade is RUNNING, and then only the agent's
    own approval-resume path (paper, risk-gated, HALT-checked)."""
    from orchestrator.contracts import AgentRequest, TaskType
    from orchestrator.agents.trading.trading_agent import TradingAgent

    task_id = f"dashboard-{int(time.time() * 1000)}"
    agent = TradingAgent()
    req = AgentRequest(task_id=task_id, task_type=TaskType.TRADING,
                       text=f"analyze {symbol}",
                       metadata={"trading_action": "signal",
                                 "symbol": symbol, "strategy": strategy,
                                 "min_score": int(min_score),
                                 "venue": venue or ""})
    t0 = time.time()
    try:
        result = agent.handle(req)
    except Exception as exc:
        return {"ok": False, "error": f"trading agent crashed: "
                                      f"{type(exc).__name__}: {exc}",
                "symbol": symbol}
    took = round(time.time() - t0, 2)

    cp = None
    try:
        cp = agent._cp.load(task_id)
    except Exception:
        cp = None
    has_cp = cp is not None

    # no-setup (or risk-refused) runs never write a checkpoint — build the
    # display view from the data the agent DID return (analysis card +
    # signal evaluation/proposal + risk verdict).
    if cp is None and isinstance(result.data, dict) \
            and isinstance(result.data.get("analysis"), dict):
        sigd = result.data.get("signal") or {}
        prop = sigd.get("proposal")
        cp = {"symbol": symbol, "strategy": strategy, "status": "completed",
              "proposal": prop, "analysis": result.data.get("analysis"),
              "risk": result.data.get("risk"),
              "data_provenance": result.data.get("provenance") or [],
              "no_trade_note": (None if prop else
                                sigd.get("note") or sigd.get("error")
                                or "no setup met the strategy rules")}

    out = {"ok": True, "ts": _now(), "task_id": task_id,
           "symbol": symbol, "strategy": strategy, "venue": venue or "auto",
           "agent_status": getattr(result.status, "value", str(result.status)),
           "text": (result.text or "")[:2000], "took_s": took,
           "auto_executed": None}
    if cp is None and not (result.data or {}):
        out["ok"] = False
        out["error"] = result.error or "no checkpoint and no data returned"
        return out

    # enrich checkpoint with the analysis card + signal evaluation for the
    # AI display (the agent computed both; it just did not persist them)
    if cp is not None and not cp.get("analysis"):
        sig = ((result.data or {}).get("signal") or {})
        if isinstance(sig.get("evaluation"), dict):
            cp["evaluation"] = sig["evaluation"]
        analysis = (result.data or {}).get("analysis")
        if isinstance(analysis, dict):
            cp["analysis"] = analysis
        try:
            agent._cp.save(task_id, cp)
        except Exception:
            pass

    # ── Auto Trade consumer: only when RUNNING, only via the agent's own
    #    approval-resume (risk re-check + paper broker inside), and only
    #    when the kill switch is not tripped.
    status = out["agent_status"]
    auto_cfg = _autotrade_state()
    live = _live_state()
    if (status == "needs_approval"
            and auto_cfg.get("status") == "RUNNING"
            and not _halt_state()["tripped"]
            and live["armed"] and not live.get("allow_auto")):
        out["auto_executed"] = {
            "attempted": False, "ok": False,
            "status": "blocked_live_manual_required", "mode": "live",
            "error": ("LIVE execution is armed; Auto Trade is paper-only. "
                      "Use the manual live approval button."),
        }
    elif (status == "needs_approval"
            and auto_cfg.get("status") == "RUNNING"
            and not _halt_state()["tripped"]):
        try:
            resume = agent.handle(AgentRequest(
                task_id=f"auto-{task_id}", task_type=TaskType.TRADING,
                text="", metadata={"approved": True, "resume": task_id,
                                    "approved_by": "auto-trade"}))
            out["auto_executed"] = {
                "attempted": True,
                "status": getattr(resume.status, "value",
                                  str(resume.status)),
                "ok": resume.status.value == "completed",
                "text": (resume.text or "")[:600],
                "mode": ("live" if live["armed"] and live.get("allow_auto")
                         else "paper"),
            }
            out["agent_status"] = getattr(resume.status, "value",
                                          str(resume.status))
        except Exception as exc:
            out["auto_executed"] = {"attempted": True, "ok": False,
                                    "error": f"{type(exc).__name__}: {exc}"}

    out["display"] = _ai_display(cp or {"symbol": symbol,
                                        "strategy": strategy,
                                        "status": "no_checkpoint"},
                                 chain=_collect_options(symbol, venue=venue)
                                 if (cp or {}).get("proposal") else None)
    out["risk"] = cp.get("risk") if cp else None
    out["checkpoint"] = has_cp
    out["min_score"] = int(min_score)

    # journal EVERY real proposal (with its risk verdict) so the Signals
    # centre shows what the agent actually computed, not just what passed.
    prop = (cp or {}).get("proposal")
    if prop:
        risk = (cp or {}).get("risk") or {}
        try:
            SIGNALS_JOURNAL.parent.mkdir(parents=True, exist_ok=True)
            with SIGNALS_JOURNAL.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "ts": _now(), "task_id": task_id, "symbol": symbol,
                    "strategy": strategy, "direction": prop.get("direction"),
                    "entry": prop.get("entry"),
                    "stop_loss": prop.get("stop_loss"),
                    "targets": prop.get("targets"),
                    "rr": prop.get("rr_to_target1"),
                    "score": prop.get("score"),
                    "risk_ok": bool(risk.get("ok")),
                    "risk_denied_by": risk.get("denied_by"),
                }, default=str) + "\n")
        except OSError:
            pass
    return out


# ═══════════════════════════════════════════════════════ options + news
def _collect_options(symbol: str, venue: str = "") -> dict:
    try:
        md = _md()
        chain = md.options_chain(symbol, venue=venue or None)
        if not chain.ok:
            return {"ok": False, "symbol": symbol,
                    "provenance": chain.provenance.value,
                    "venue": venue or "auto",
                    "error": chain.error or "chain unavailable",
                    "connected": False}
        spot = None
        q = md.quote(symbol, venue=venue or None)
        if q.ok:
            spot = (q.data or {}).get("last")
        data = chain.data or {}
        greeks = md.greeks(chain, spot, days_to_expiry=30.0) if spot else {}
        legs_atm = []
        if spot:
            for kind in ("calls", "puts"):
                legs = sorted(data.get(kind) or [],
                              key=lambda l: abs(float(l.get("strike") or 0)
                                                - spot))[:4]
                legs_atm.extend(legs)
        return {"ok": True, "symbol": symbol, "connected": True,
                "venue": venue or "auto",
                "provenance": chain.provenance.value, "as_of": chain.as_of,
                "expiry": data.get("expiry"),
                "expiries_available": data.get("expiries_available") or [],
                "spot": spot,
                "calls": (data.get("calls") or [])[:40],
                "puts": (data.get("puts") or [])[:40],
                "greeks": greeks, "legs_atm": legs_atm}
    except Exception as exc:
        return {"ok": False, "symbol": symbol, "provenance": "unavailable",
                "error": f"{type(exc).__name__}: {exc}", "connected": False}


def _collect_news(symbol: str, venue: str = "") -> dict:
    try:
        res = _md().news(symbol, venue=venue or None)
        out = res.to_dict()
        out["symbol"] = symbol
        out["venue"] = venue or "auto"
        out["connected"] = bool(res.ok)
        if res.ok:
            raw = (res.data or {}).get("items") or []
            out["items"] = [{"title": str(i.get("title"))[:160],
                             "published": str(i.get("published") or "")[:40],
                             "source": str(i.get("source") or "")[:40],
                             "url": str(i.get("url") or "")[:300]}
                            for i in raw[:12] if isinstance(i, dict)]
        else:
            out["items"] = []
        return out
    except Exception as exc:
        return {"ok": False, "symbol": symbol, "items": [],
                "provenance": "unavailable",
                "error": f"{type(exc).__name__}: {exc}", "connected": False}


# ═══════════════════════════════════════════════ positions / orders / risk
def _collect_positions() -> dict:
    from orchestrator.agents.trading.broker import PaperBroker
    pb = PaperBroker()
    open_pos = pb.open_positions()
    closed = []
    try:
        all_pos = pb._load_positions()
        for k, v in all_pos.items():
            if v.get("status") != "open":
                row = dict(v)
                row["key"] = k
                closed.append(row)
        closed.sort(key=lambda r: str(r.get("closed_time") or ""),
                    reverse=True)
        closed = closed[:10]
    except Exception:
        pass
    return {"ok": True, "ts": _now(), "mode": "paper",
            "open": [{**v, "key": k} for k, v in open_pos.items()],
            "closed": closed}


def _collect_orders() -> dict:
    orders = _tail_jsonl(TRADING_DIR / "paper_journal.jsonl", n=30)
    live_orders = _tail_jsonl(LIVE_JOURNAL, n=30)
    events = _tail_jsonl(TRADING_DIR / "live_trades.jsonl", n=10)
    return {"ok": True, "ts": _now(), "orders": orders[::-1],
            "live_orders": live_orders[::-1],
            "mode_events": events[::-1]}


def _collect_risk() -> dict:
    from orchestrator.agents.trading.risk import load_safety, load_state
    safety = load_safety()
    state = load_state()
    live = _live_state()
    try:
        from orchestrator.agents.trading.broker import PaperBroker
        open_n = len(PaperBroker().open_positions())
    except Exception:
        open_n = None
    stopped = False
    stop_reason = ""
    try:
        from dashboard.autonomy_api import shared_engine
        bundle = shared_engine()
        if bundle:
            stopped = bool(bundle["engine"].stopped)
            stop_reason = str(bundle["engine"]._stop_reason or "")
    except Exception:
        pass
    return {"ok": True, "ts": _now(), "safety": safety, "state": state,
            "live": live, "open_positions": open_n,
            "emergency_stop": {"stopped": stopped, "reason": stop_reason}}


# ═════════════════════════════════════════════════════════════ auto trade
def _autotrade_state() -> dict:
    """Default STOPPED: no file → stopped. Nothing ever enables it
    implicitly — only POST /autotrade/start with confirm:true writes it."""
    try:
        data = json.loads(AUTOTRADE_FILE.read_text(encoding="utf-8"))
        enabled = bool(data.get("enabled"))
        return {"status": "RUNNING" if enabled else "STOPPED",
                "enabled": enabled,
                "since": data.get("since"),
                "updated_at": data.get("updated_at"),
                "file": str(AUTOTRADE_FILE)}
    except (OSError, ValueError):
        return {"status": "STOPPED", "enabled": False, "since": None,
                "updated_at": None, "file": str(AUTOTRADE_FILE)}


def _autotrade_save(enabled: bool, by: str) -> dict:
    prev = _autotrade_state()
    now = _now()
    doc = {"enabled": bool(enabled),
           "since": (now if enabled else None),
           "updated_at": now, "by": by}
    if not enabled and prev.get("since"):
        doc["stopped_since_prev"] = prev["since"]
    AUTOTRADE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = AUTOTRADE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    tmp.replace(AUTOTRADE_FILE)
    try:
        with AUTOTRADE_AUDIT.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": now, "action":
                                 "start" if enabled else "stop",
                                 "by": by}) + "\n")
    except OSError:
        pass
    return _autotrade_state()


def _autotrade_view() -> dict:
    from orchestrator.agents.trading.risk import load_safety
    state = _autotrade_state()
    halt = _halt_state()
    warnings = []
    safety = load_safety()
    live = _live_state()
    if live["armed"]:
        warnings.append(
            "LIVE execution is armed. Auto Trade remains PAPER-ONLY unless "
            "live_execution.json allow_auto=true; use manual approval for a "
            "live order.")
    if str(safety.get("mode")).lower() == "live" and not live["enabled"]:
        warnings.append("safety config mode=live is shared with Binance; the "
                        "trading agent's live switch is still disabled "
                        "(paper only)")
    if halt["tripped"]:
        warnings.append("kill switch HALT is set — auto execution blocked")
    state["warnings"] = warnings
    state["safety_mode"] = safety.get("mode")
    state["live"] = live
    state["halt"] = halt
    state["ts"] = _now()
    state["ok"] = True
    return state


def _trip_halt(reason: str, by: str) -> dict:
    """Trip the REAL vibe-trading kill switch (filesystem sentinel —
    enforced independently of any UI, SPEC Consent §4)."""
    try:
        HALT_SENTINEL.parent.mkdir(parents=True, exist_ok=True)
        HALT_SENTINEL.write_text(json.dumps({
            "tripped_at": datetime.now().astimezone().isoformat(
                timespec="seconds"),
            "by": by, "reason": reason[:200]}), encoding="utf-8")
        return _halt_state()
    except OSError as exc:
        return {"tripped": HALT_SENTINEL.exists(),
                "path": str(HALT_SENTINEL),
                "detail": f"could not write sentinel: {exc}"}


def _trip_autonomy_stop(reason: str) -> dict:
    """Trip the autonomy engine's emergency stop — the exact engine call
    behind the real POST /api/autonomy/stop endpoint (the UI button calls
    that endpoint directly too)."""
    try:
        from dashboard.autonomy_api import shared_engine
        bundle = shared_engine()
        if not bundle:
            return {"ok": False, "error": "engine unavailable"}
        res = bundle["engine"].trip_stop(reason=reason,
                                         by="trading-dashboard")
        return res if isinstance(res, dict) else {"ok": bool(res)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ══════════════════════════════════════════════════════════════ missions
def _mission_status(entry: dict, running: set) -> str:
    """Map real store state onto the spec's five states — no invention."""
    name = str(entry.get("name") or "")
    if name in running:
        return "running"
    if not entry.get("enabled", True):
        return "paused"
    last = str(entry.get("last_status") or "")
    if last == "ok":
        return "completed"
    if last:
        return "failed"                    # pending → awaiting approval
    return "queued"                        # enabled, never run / waiting


def _collect_missions() -> dict:
    bundle = None
    try:
        from dashboard.autonomy_api import shared_engine
        bundle = shared_engine()
    except Exception:
        bundle = None
    if not bundle:
        return {"ok": False, "error": "autonomy engine unavailable",
                "status_code": 503}
    store = bundle["store"]
    eng = bundle["engine"]
    with _mission_lock:
        running = set(_running_missions)
    missions = []
    for t in store.list():
        row = {
            "name": t.get("name"),
            "status": _mission_status(t, running),
            "enabled": bool(t.get("enabled", True)),
            "level": t.get("level"),
            "kind": t.get("kind"),
            "agents": list(t.get("agents") or []),
            "steps": list(t.get("steps") or []),
            "step_count": len(t.get("steps") or []),
            "trigger": t.get("trigger") or {},
            "last_status": t.get("last_status"),
            "failures": t.get("failures"),
            "last_value": t.get("last_value"),
            "created_at": t.get("created_at"),
        }
        for k in ("last_run", "next_run"):
            v = t.get(k)
            row[k] = (datetime.fromtimestamp(v).isoformat(timespec="seconds")
                      if isinstance(v, (int, float)) and v else None)
        missions.append(row)
    return {
        "ok": True, "ts": _now(),
        "emergency_stop": {"stopped": bool(eng.stopped),
                           "reason": str(eng._stop_reason or "")},
        "missions": missions,
        "history": store.history(15)[::-1],
        "running": sorted(running),
        "capabilities": {
            "start": True, "pause": True, "resume": True,
            # the backend has NO per-mission cancel and NO mid-run pause:
            "stop": False,
            "mid_run_pause": False,
            "notes": [
                "Stop exists only as the GLOBAL emergency stop "
                "(POST /api/autonomy/stop) — it halts every run between "
                "steps; there is no per-mission cancel in the engine.",
                "Pause/resume act on the scheduler's enabled flag — a run "
                "already in flight is not interrupted.",
            ],
        },
    }


def _mission_start(task_name: str) -> dict:
    bundle = None
    try:
        from dashboard.autonomy_api import shared_engine
        bundle = shared_engine()
    except Exception:
        bundle = None
    if not bundle:
        return {"ok": False, "error": "autonomy engine unavailable"}
    store, eng, sched = bundle["store"], bundle["engine"], bundle["scheduler"]
    entry = store.get(task_name)
    if not entry:
        return {"ok": False, "error": f"unknown mission '{task_name}'"}
    if eng.stopped:
        return {"ok": False, "error": "emergency stop active — resume "
                                       "autonomy first (/api/autonomy/resume)"}
    with _mission_lock:
        if task_name in _running_missions:
            return {"ok": False, "error": "mission already running"}
        _running_missions.add(task_name)

    def _run():
        try:
            sched.run_task(entry, "dashboard start")
        finally:
            with _mission_lock:
                _running_missions.discard(task_name)

    threading.Thread(target=_run, daemon=True,
                     name=f"mission-{task_name}").start()
    return {"ok": True, "started": task_name, "status": "running"}


# ═════════════════════════════════════════════════════════════════ REST
# ============================= Six Agent Desk ==============================
# The paper-trading desk (scripts/six_agent_desk.py) seated inside the
# TradingAgents graph: ATLAS scouts, VEGA reads the chart, ORION verifies
# news, LUNA reads the crowd, bull/bear challenge the idea, NOVA drafts,
# TITAN risk-checks with veto, NOVA decides TRADE/WAIT/REJECT. PAPER ONLY.
#
# Live cards: the runner writes per-stage JSON to DESK_STATE_FILE via
# --progress-file; this API reads it and starts runs as background threads.
# Nothing here touches a broker -- the desk's own rules keep TITAN's veto
# final and every decision paper-only.

DESK_STATE_FILE = TRADING_DIR / "desk_state.json"
DESK_SCRIPT     = BASE_DIR / "scripts" / "six_agent_desk.py"
DESK_PYTHON     = BASE_DIR / ".venv-tradingagents" / "bin" / "python"
DESK_JOURNAL_DIR = BASE_DIR / "desk_journal"
DESK_RUN_TIMEOUT_S = 1800     # a full desk run is ~10-15 min through the pool
DESK_STALE_RUN_S   = 2400     # a "running" state older than this is a dead run

DESK_AGENTS = (
    {"key": "atlas", "name": "ATLAS", "role": "Market scout",
     "output": "Ranked watchlist + focus pick"},
    {"key": "vega",  "name": "VEGA",  "role": "Chart analyst",
     "output": "Trend, key levels, invalidation"},
    {"key": "orion", "name": "ORION", "role": "News analyst",
     "output": "Verified facts + red flags"},
    {"key": "luna",  "name": "LUNA",  "role": "Sentiment analyst",
     "output": "Mood score + hype risk"},
    {"key": "titan", "name": "TITAN", "role": "Risk manager",
     "output": "PASS/FAIL checks - veto is final"},
    {"key": "nova",  "name": "NOVA",  "role": "Head trader",
     "output": "TRADE / WAIT / REJECT"},
)

_desk_lock = threading.Lock()
_desk_inproc_running = False


def _desk_state() -> dict:
    """Read the desk's progress file; a dead 'running' is marked honestly."""
    st: dict = {}
    try:
        with open(DESK_STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {"status": "idle"}
    if st.get("status") == "running":
        started = str(st.get("started_ts") or "")
        stale = True
        try:
            t0 = datetime.fromisoformat(started)
            stale = (time.time() - t0.timestamp()) > DESK_STALE_RUN_S
        except Exception:
            stale = started == ""   # no timestamp -> treat as stale shell
        if stale:
            st["status"] = "error"
            st["error"] = ("run marker went stale (the desk process died "
                           "without a terminal status) - cleared")
    return {
        "ok": True,
        "agents": DESK_AGENTS,
        "paper_only": True,
        "state": st,
    }


def _desk_merge_error(msg: str) -> None:
    """Mark a still-'running' state as errored (subprocess died badly)."""
    try:
        with _desk_lock:
            st = {}
            try:
                with open(DESK_STATE_FILE, "r", encoding="utf-8") as f:
                    st = json.load(f)
            except Exception:
                st = {}
            if st.get("status") == "running":
                st["status"] = "error"
                st["error"] = msg
                st["finished_ts"] = datetime.now().isoformat(timespec="seconds")
                tmp = str(DESK_STATE_FILE) + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(st, f, ensure_ascii=False, indent=1)
                os.replace(tmp, DESK_STATE_FILE)
    except Exception:
        pass


def _desk_run(ticker: str, candidates: str, skip_scout: bool,
              balance: float) -> dict:
    """Start one desk run in a background thread (paper trading only)."""
    global _desk_inproc_running
    ticker = (ticker or "").strip().upper()
    if not _SYMBOL_RE.match(ticker):
        return {"ok": False, "error": f"invalid ticker: {ticker!r}"}
    if not DESK_SCRIPT.exists() or not DESK_PYTHON.exists():
        return {"ok": False, "error": "desk runner or venv missing on disk"}
    # Cross-restart guard: a genuinely fresh state file must not be clobbered.
    state = _desk_state()["state"]
    if state.get("status") == "running":
        return {"ok": False, "error": "a desk run is already in progress"}
    with _desk_lock:
        if _desk_inproc_running:
            return {"ok": False, "error": "a desk run is already in progress"}
        _desk_inproc_running = True

    cmd = [str(DESK_PYTHON), str(DESK_SCRIPT), ticker,
           "--progress-file", str(DESK_STATE_FILE),
           "--journal-dir", str(DESK_JOURNAL_DIR),
           "--balance", str(max(1.0, float(balance or 100000.0)))]
    if candidates:
        cmd += ["--candidates", str(candidates)[:200]]
    if skip_scout:
        cmd.append("--skip-scout")

    def _run():
        global _desk_inproc_running
        try:
            subprocess.run(cmd, capture_output=True, text=True,
                           timeout=DESK_RUN_TIMEOUT_S,
                           cwd=str(BASE_DIR),
                           env={"HOME": os.path.expanduser("~"),
                                "PATH": "/usr/local/bin:/usr/bin:/bin"})
        except subprocess.TimeoutExpired:
            _desk_merge_error(f"desk run timed out after {DESK_RUN_TIMEOUT_S}s")
        except Exception as exc:
            _desk_merge_error(f"desk runner failed: {type(exc).__name__}: {exc}")
        finally:
            _desk_merge_error("desk process exited without a terminal status")
            with _desk_lock:
                _desk_inproc_running = False

    threading.Thread(target=_run, daemon=True,
                     name="six-agent-desk").start()
    return {"ok": True, "started": ticker, "status": "running",
            "paper_only": True}


def _desk_journal(path: str | None) -> dict:
    """Serve a desk journal's markdown (whitelisted under desk_journal/)."""
    try:
        base = DESK_JOURNAL_DIR.resolve()
        if path:
            candidate = (base / Path(path).name).resolve()
            if base not in candidate.parents and candidate != base:
                return {"ok": False, "error": "journal path not allowed"}
            target = candidate
        else:
            journals = sorted(base.glob("*.md"), key=lambda p: p.stat().st_mtime)
            if not journals:
                return {"ok": False, "error": "no desk journal saved yet"}
            target = journals[-1]
        text = target.read_text(encoding="utf-8", errors="replace")
        return {"ok": True, "path": str(target), "text": text[:60000]}
    except Exception as exc:
        return {"ok": False, "error": f"journal unavailable: {exc}"}


def register(app, _auth) -> None:
    """Attach /api/rio/trading/* and /api/rio/missions/* (shared auth)."""
    import asyncio

    def _denied(req: Request):
        if not _auth(req):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return None

    async def _run(fn, *args):
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: fn(*args))

    # ── trading ─────────────────────────────────────────────────────────
    @app.get("/api/rio/trading/connection", include_in_schema=False)
    async def t_connection(req: Request):
        if _denied(req):
            return _denied(req)
        return JSONResponse(await _run(_collect_connection))

    # ── live execution control (separate switch; never auto-enabled) ─────
    @app.get("/api/rio/trading/live/status", include_in_schema=False)
    async def t_live_status(req: Request):
        if _denied(req):
            return _denied(req)
        live = await _run(_live_state)
        return JSONResponse({"ok": True, "ts": _now(), "live": live})

    @app.post("/api/rio/trading/live/probe", include_in_schema=False)
    async def t_live_probe(req: Request):
        if _denied(req):
            return _denied(req)
        try:
            body = await req.json()
        except Exception:
            body = {}
        env = str(body.get("environment") or "").strip().lower() or None
        out = await _run(_live_probe, env)
        return JSONResponse(out, status_code=200 if out.get("ok") else 400)

    @app.post("/api/rio/trading/live/enable", include_in_schema=False)
    async def t_live_enable(req: Request):
        if _denied(req):
            return _denied(req)
        try:
            body = await req.json()
        except Exception:
            body = {}
        if body.get("confirm") is not True:
            return JSONResponse(
                {"ok": False, "error": "explicit confirmation required"},
                status_code=400)
        out = await _run(_live_enable,
                         str(body.get("environment") or "demo"),
                         str(body.get("phrase") or ""))
        return JSONResponse(out, status_code=200 if out.get("ok") else 400)

    @app.post("/api/rio/trading/live/disable", include_in_schema=False)
    async def t_live_disable(req: Request):
        if _denied(req):
            return _denied(req)
        out = await _run(_live_disable, "dashboard")
        return JSONResponse(out)

    @app.post("/api/rio/trading/approve", include_in_schema=False)
    async def t_approve(req: Request):
        if _denied(req):
            return _denied(req)
        try:
            body = await req.json()
        except Exception:
            body = {}
        if body.get("confirm") is not True:
            return JSONResponse(
                {"ok": False, "error": "explicit confirmation required"},
                status_code=400)
        out = await _run(_manual_approve,
                         str(body.get("task_id") or ""),
                         str(body.get("phrase") or ""))
        code = int(out.pop("http", 200 if out.get("ok") else 400))
        return JSONResponse(out, status_code=code)

    @app.get("/api/rio/trading/overview", include_in_schema=False)
    async def t_overview(req: Request):
        if _denied(req):
            return _denied(req)
        return JSONResponse(await _run(_collect_overview))

    @app.get("/api/rio/trading/watchlist", include_in_schema=False)
    async def t_watchlist(req: Request):
        if _denied(req):
            return _denied(req)
        syms = _load_watchlist()
        return JSONResponse({"ok": True, "ts": _now(), "symbols": syms,
                             "file": str(WATCHLIST_FILE)})

    @app.post("/api/rio/trading/watchlist", include_in_schema=False)
    async def t_watchlist_post(req: Request):
        if _denied(req):
            return _denied(req)
        try:
            body = await req.json()
        except Exception:
            body = {}
        action = str(body.get("action") or "").strip().lower()
        sym = _safe_symbol(body.get("symbol"))
        if not sym:
            return JSONResponse({"ok": False, "error": "invalid symbol"},
                                status_code=400)
        with _watch_lock:
            syms = _load_watchlist()
            if action == "add":
                if sym not in syms:
                    syms.append(sym)
            elif action == "remove":
                if sym not in syms:
                    return JSONResponse({"ok": False,
                                         "error": f"{sym} not in watchlist"},
                                        status_code=404)
                syms.remove(sym)
            else:
                return JSONResponse({"ok": False,
                                     "error": "action must be add|remove"},
                                    status_code=400)
            _save_watchlist(syms)
        return JSONResponse({"ok": True, "symbols": syms})

    @app.get("/api/rio/trading/chart", include_in_schema=False)
    async def t_chart(req: Request):
        if _denied(req):
            return _denied(req)
        sym = _safe_symbol(req.query_params.get("symbol"))
        if not sym:
            return JSONResponse({"ok": False, "error": "invalid symbol"},
                                status_code=400)
        period = str(req.query_params.get("period") or "6mo")
        if period not in _PERIODS:
            return JSONResponse({"ok": False,
                                 "error": f"period must be one of "
                                          f"{sorted(_PERIODS)}"},
                                status_code=400)
        venue = _venue(req.query_params.get("venue"))
        if venue == "__invalid__":
            return JSONResponse({"ok": False,
                                 "error": "venue must be auto or delta"},
                                status_code=400)
        interval = str(req.query_params.get("interval") or "1d")
        if venue == "delta":
            resolved = await _run(_delta_resolve, sym,
                                  req.query_params.get("market") or "")
            if not resolved.get("ok"):
                return JSONResponse({
                    "ok": False, "needs_input": resolved.get("needs_input"),
                    "error": resolved.get("error"),
                    "options": resolved.get("options") or [],
                }, status_code=200 if resolved.get("needs_input") else 400)
            sym = resolved["symbol"]
        return JSONResponse(await _run(_collect_chart, sym, period, venue, interval))

    @app.get("/api/rio/trading/charts", include_in_schema=False)
    async def t_charts(req: Request):
        if _denied(req):
            return _denied(req)
        return JSONResponse(await _run(_collect_charts))

    @app.get("/api/rio/trading/chart-file", include_in_schema=False)
    async def t_chart_file(req: Request):
        if _denied(req):
            return _denied(req)
        name = os.path.basename(str(req.query_params.get("name") or ""))
        if not name.endswith(".png") or "/" in name or "\\" in name:
            return JSONResponse({"ok": False, "error": "invalid name"},
                                status_code=400)
        path = (CHARTS_DIR / name).resolve()
        if CHARTS_DIR.resolve() not in path.parents or not path.exists():
            return JSONResponse({"ok": False, "error": "not found"},
                                status_code=404)
        return FileResponse(str(path), media_type="image/png")

    @app.get("/api/rio/trading/signals", include_in_schema=False)
    async def t_signals(req: Request):
        if _denied(req):
            return _denied(req)
        return JSONResponse(await _run(_collect_signals))

    @app.post("/api/rio/trading/analyze", include_in_schema=False)
    async def t_analyze(req: Request):
        if _denied(req):
            return _denied(req)
        try:
            body = await req.json()
        except Exception:
            body = {}
        sym = _safe_symbol(body.get("symbol"))
        if not sym:
            return JSONResponse({"ok": False, "error": "invalid symbol"},
                                status_code=400)
        strategy = str(body.get("strategy") or "trend_follow").strip().lower()
        if not re.match(r"^[a-z_]{3,24}$", strategy):
            return JSONResponse({"ok": False, "error": "invalid strategy"},
                                status_code=400)
        try:
            min_score = int(body.get("min_score") or 2)
        except (TypeError, ValueError):
            min_score = 2
        if min_score < 1 or min_score > 5:
            return JSONResponse({"ok": False,
                                 "error": "min_score must be 1..5"},
                                status_code=400)
        venue = _venue(body.get("venue"))
        if venue == "__invalid__":
            return JSONResponse({"ok": False,
                                 "error": "venue must be auto or delta"},
                                status_code=400)
        if venue == "delta":
            resolved = await _run(_delta_resolve, sym,
                                  body.get("market") or "")
            if not resolved.get("ok"):
                return JSONResponse({
                    "ok": False, "needs_input": resolved.get("needs_input"),
                    "error": resolved.get("error"),
                    "options": resolved.get("options") or [],
                }, status_code=200 if resolved.get("needs_input") else 400)
            sym = resolved["symbol"]
        return JSONResponse(await _run(_run_analyze, sym, strategy,
                                       min_score, venue))

    @app.get("/api/rio/trading/options", include_in_schema=False)
    async def t_options(req: Request):
        if _denied(req):
            return _denied(req)
        sym = _safe_symbol(req.query_params.get("symbol"))
        if not sym:
            return JSONResponse({"ok": False, "error": "invalid symbol"},
                                status_code=400)
        venue = _venue(req.query_params.get("venue"))
        if venue == "__invalid__":
            return JSONResponse({"ok": False,
                                 "error": "venue must be auto or delta"},
                                status_code=400)
        if venue == "delta":
            resolved = await _run(_delta_resolve, sym,
                                  req.query_params.get("market") or "")
            if not resolved.get("ok"):
                return JSONResponse({
                    "ok": False, "needs_input": resolved.get("needs_input"),
                    "error": resolved.get("error"),
                    "options": resolved.get("options") or [],
                }, status_code=200 if resolved.get("needs_input") else 400)
            sym = resolved["symbol"]
        return JSONResponse(await _run(_collect_options, sym, venue))

    @app.get("/api/rio/trading/news", include_in_schema=False)
    async def t_news(req: Request):
        if _denied(req):
            return _denied(req)
        sym = _safe_symbol(req.query_params.get("symbol"))
        if not sym:
            return JSONResponse({"ok": False, "error": "invalid symbol"},
                                status_code=400)
        venue = _venue(req.query_params.get("venue"))
        if venue == "__invalid__":
            return JSONResponse({"ok": False,
                                 "error": "venue must be auto or delta"},
                                status_code=400)
        if venue == "delta":
            resolved = await _run(_delta_resolve, sym,
                                  req.query_params.get("market") or "")
            if not resolved.get("ok"):
                return JSONResponse({
                    "ok": False, "needs_input": resolved.get("needs_input"),
                    "error": resolved.get("error"),
                    "options": resolved.get("options") or [],
                }, status_code=200 if resolved.get("needs_input") else 400)
            sym = resolved["symbol"]
        return JSONResponse(await _run(_collect_news, sym, venue))

    # ── Delta Exchange India public data ────────────────────────────────
    @app.get("/api/rio/trading/delta/status", include_in_schema=False)
    async def t_delta_status(req: Request):
        if _denied(req):
            return _denied(req)
        return JSONResponse(await _run(_collect_delta_status))

    @app.get("/api/rio/trading/delta/products", include_in_schema=False)
    async def t_delta_products(req: Request):
        if _denied(req):
            return _denied(req)
        query = str(req.query_params.get("query") or "").strip()[:80]
        market = str(req.query_params.get("market") or "").strip().lower()
        if market in ("perpetual", "delta_perp"):
            market = "perp"
        if market not in ("", "perp", "spot"):
            return JSONResponse({"ok": False,
                                 "error": "market must be perp or spot"},
                                status_code=400)
        return JSONResponse(await _run(_collect_delta_products, query, market))

    @app.get("/api/rio/trading/delta/quote", include_in_schema=False)
    async def t_delta_quote(req: Request):
        if _denied(req):
            return _denied(req)
        sym = _safe_symbol(req.query_params.get("symbol"))
        if not sym:
            return JSONResponse({"ok": False, "error": "invalid symbol"},
                                status_code=400)
        resolved = await _run(_delta_resolve, sym,
                              req.query_params.get("market") or "")
        if not resolved.get("ok"):
            return JSONResponse({
                "ok": False, "needs_input": resolved.get("needs_input"),
                "error": resolved.get("error"),
                "options": resolved.get("options") or [],
            }, status_code=200 if resolved.get("needs_input") else 400)
        return JSONResponse(await _run(_collect_delta_quote,
                                       resolved["symbol"]))

    @app.get("/api/rio/trading/delta/chart", include_in_schema=False)
    async def t_delta_chart(req: Request):
        if _denied(req):
            return _denied(req)
        sym = _safe_symbol(req.query_params.get("symbol"))
        if not sym:
            return JSONResponse({"ok": False, "error": "invalid symbol"},
                                status_code=400)
        period = str(req.query_params.get("period") or "6mo")
        if period not in _PERIODS:
            return JSONResponse({"ok": False,
                                 "error": f"period must be one of "
                                          f"{sorted(_PERIODS)}"},
                                status_code=400)
        resolved = await _run(_delta_resolve, sym,
                              req.query_params.get("market") or "")
        if not resolved.get("ok"):
            return JSONResponse({
                "ok": False, "needs_input": resolved.get("needs_input"),
                "error": resolved.get("error"),
                "options": resolved.get("options") or [],
            }, status_code=200 if resolved.get("needs_input") else 400)
        interval = str(req.query_params.get("interval") or "1d")
        return JSONResponse(await _run(_collect_chart, resolved["symbol"],
                                       period, "delta", interval))

    @app.get("/api/rio/trading/positions", include_in_schema=False)
    async def t_positions(req: Request):
        if _denied(req):
            return _denied(req)
        return JSONResponse(await _run(_collect_positions))

    @app.get("/api/rio/trading/orders", include_in_schema=False)
    async def t_orders(req: Request):
        if _denied(req):
            return _denied(req)
        return JSONResponse(await _run(_collect_orders))

    @app.get("/api/rio/trading/risk", include_in_schema=False)
    async def t_risk(req: Request):
        if _denied(req):
            return _denied(req)
        return JSONResponse(await _run(_collect_risk))

    # ── auto trade ──────────────────────────────────────────────────────
    @app.get("/api/rio/trading/autotrade", include_in_schema=False)
    async def t_autotrade(req: Request):
        if _denied(req):
            return _denied(req)
        return JSONResponse(_autotrade_view())

    @app.post("/api/rio/trading/autotrade/start", include_in_schema=False)
    async def t_autotrade_start(req: Request):
        if _denied(req):
            return _denied(req)
        try:
            body = await req.json()
        except Exception:
            body = {}
        if body.get("confirm") is not True:
            return JSONResponse(
                {"ok": False,
                 "error": "confirmation required — send {\"confirm\": true} "
                          "to start auto trade"}, status_code=400)
        halt = _halt_state()
        if halt["tripped"]:
            return JSONResponse(
                {"ok": False,
                 "error": "kill switch HALT is set — clear the sentinel "
                          f"({halt['path']}) before starting"},
                status_code=409)
        _autotrade_save(True, by="dashboard-confirm")
        return JSONResponse({"ok": True, **_autotrade_view()})

    @app.post("/api/rio/trading/autotrade/stop", include_in_schema=False)
    async def t_autotrade_stop(req: Request):
        if _denied(req):
            return _denied(req)
        _autotrade_save(False, by="dashboard")
        return JSONResponse({"ok": True, **_autotrade_view()})

    @app.post("/api/rio/trading/autotrade/emergency", include_in_schema=False)
    async def t_autotrade_emergency(req: Request):
        if _denied(req):
            return _denied(req)
        try:
            body = await req.json()
        except Exception:
            body = {}
        reason = str(body.get("reason") or "trading emergency stop")[:200]
        _autotrade_save(False, by="emergency")
        halt = _trip_halt(reason, by="trading-dashboard")
        autonomy = _trip_autonomy_stop(reason)
        try:
            with AUTOTRADE_AUDIT.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": _now(), "action": "emergency",
                                     "by": "dashboard", "reason": reason,
                                     "halt": halt.get("tripped"),
                                     "autonomy": autonomy}) + "\n")
        except OSError:
            pass
        # invalidate the 30s connection cache — the HALT state just changed
        _conn_cache["t"] = 0.0
        _conn_cache["data"] = None
        return JSONResponse({"ok": True, "auto_trade": _autotrade_view(),
                             "halt": halt, "autonomy": autonomy})

    # ── missions ────────────────────────────────────────────────────────
    @app.get("/api/rio/missions", include_in_schema=False)
    async def m_list(req: Request):
        if _denied(req):
            return _denied(req)
        data = await _run(_collect_missions)
        code = data.pop("status_code", 200) if data.get("ok") is False else 200
        return JSONResponse(data, status_code=code)

    @app.post("/api/rio/missions/start", include_in_schema=False)
    async def m_start(req: Request):
        if _denied(req):
            return _denied(req)
        try:
            body = await req.json()
        except Exception:
            body = {}
        name = str(body.get("task") or "").strip()
        if not name:
            return JSONResponse({"ok": False, "error": "task required"},
                                status_code=400)
        result = _mission_start(name)
        return JSONResponse(result,
                            status_code=200 if result.get("ok") else 409)

    @app.post("/api/rio/missions/pause", include_in_schema=False)
    async def m_pause(req: Request):
        if _denied(req):
            return _denied(req)
        return await _mission_toggle(req, False)

    @app.post("/api/rio/missions/resume", include_in_schema=False)
    async def m_resume(req: Request):
        if _denied(req):
            return _denied(req)
        return await _mission_toggle(req, True)

    async def _mission_toggle(req: Request, enabled: bool):
        try:
            body = await req.json()
        except Exception:
            body = {}
        name = str(body.get("task") or "").strip()
        if not name:
            return JSONResponse({"ok": False, "error": "task required"},
                                status_code=400)
        bundle = None
        try:
            from dashboard.autonomy_api import shared_engine
            bundle = shared_engine()
        except Exception:
            bundle = None
        if not bundle:
            return JSONResponse({"ok": False,
                                 "error": "autonomy engine unavailable"},
                                status_code=503)
        result = bundle["store"].update(name, enabled=enabled)
        if result.get("ok"):
            bundle["audit"].record(
                "task_" + ("resumed" if enabled else "paused"),
                workflow=name, actor="trading-dashboard")
        status = 200 if result.get("ok") else 404
        view = _collect_missions()
        row = next((m for m in view.get("missions", [])
                    if m.get("name") == name), None)
        return JSONResponse({**result,
                             "status": (row or {}).get("status")},
                            status_code=status)

    # ── Six Agent Desk (paper trading desk — live cards + run control) ──
    @app.get("/api/rio/trading/desk", include_in_schema=False)
    async def t_desk(req: Request):
        if _denied(req):
            return _denied(req)
        return JSONResponse(await _run(_desk_state))

    @app.post("/api/rio/trading/desk/run", include_in_schema=False)
    async def t_desk_run(req: Request):
        if _denied(req):
            return _denied(req)
        try:
            body = await req.json()
        except Exception:
            body = {}
        ticker = str(body.get("ticker") or "").strip()
        if not ticker:
            return JSONResponse({"ok": False, "error": "ticker required"},
                                status_code=400)
        result = await _run(
            _desk_run,
            ticker,
            str(body.get("candidates") or "").strip(),
            bool(body.get("skip_scout", False)),
            float(body.get("balance") or 100000.0),
        )
        return JSONResponse(result, status_code=200 if result.get("ok") else 409)

    @app.get("/api/rio/trading/desk/journal", include_in_schema=False)
    async def t_desk_journal(req: Request):
        if _denied(req):
            return _denied(req)
        path = req.query_params.get("path")
        return JSONResponse(await _run(_desk_journal, path))
