"""
actions/trading_monitor.py — voice/tool surface for the continuous trading
monitor (core/trading_monitor.py).

Read-only: status, health, signals, lifecycle. There is no order, start/stop of
live trading, or broker call here — the monitor has no order path.
"""
from __future__ import annotations

from core.trading_monitor import get_monitor

_ICON = {"RUNNING": "🟢", "CONNECTED": "🟢", "MONITORING": "🟢",
         "STALE": "🟡", "STOPPED": "⚪", "DISCONNECTED": "🔴"}


def _pct(value) -> str:
    return f"{value:+.2f}%" if isinstance(value, (int, float)) else "—"


def _val(value) -> str:
    return "—" if value is None else str(value)


def _money(value) -> str:
    return f"{value:,.6f}" if isinstance(value, (int, float)) else "—"


def _health_text(h: dict) -> str:
    lines = [
        f"RIO TRADING MONITOR — {_ICON.get(h.get('trading_monitor'), '⚪')} "
        f"{h.get('trading_monitor')}",
        f"Market data: {_ICON.get(h.get('market_data'), '⚪')} "
        f"{h.get('market_data')} ({h.get('transport')})",
        f"Execution mode: {h.get('execution_mode')} · verbosity {h.get('verbosity')}",
        "",
        f"Active setups: {h.get('active_setups')}",
        f"Confirmed signals today: {h.get('confirmed_signals_today')}",
        f"Alerts sent today: {h.get('alerts_sent_today')}",
        f"Monitor errors: {h.get('monitor_errors')}",
        f"Last market update: {h.get('last_market_update') or '—'}",
        f"Last candle processed: {h.get('last_candle_processed') or '—'}",
        "",
        "Symbols: " + ", ".join(
            f"{sym} {_ICON.get(state, '')}{state}"
            for sym, state in (h.get("symbols") or {}).items()),
    ]
    return "\n".join(lines)


def _symbol_text(sym: str, s: dict) -> str:
    zone = s.get("ssl_zone") or {}
    zone_txt = (f"{zone.get('low')}–{zone.get('high')}"
                if zone.get("low") is not None else "not configured")
    delta = ("UNAVAILABLE" if s.get("delta_available") is False
             else _val(s.get("delta")))
    return (
        f"{sym} — {s.get('monitoring_status')}\n"
        f"  Price: {_money(s.get('price'))} ({_pct(s.get('change_24h'))} 24h) · "
        f"trend {_val(s.get('trend'))}\n"
        f"  SSL zone: {zone_txt} · BSL {_money(s.get('bsl_level'))}\n"
        f"  Volume: {_val(s.get('volume'))} (avg {_val(s.get('avg_volume'))}, "
        f"rel {_val(s.get('rel_volume'))}x)\n"
        f"  Delta: {delta} · timeframe {_val(s.get('candle_timeframe'))}\n"
        f"  Sweep: {_val(s.get('liquidity_sweep_state'))} · "
        f"rejection {_val(s.get('rejection_state'))} · "
        f"entry {_val(s.get('entry_confirmation_state'))}\n"
        f"  Setup A configured: {'yes' if s.get('setup_a_configured') else 'no'} · "
        f"Setup B configured: {'yes' if s.get('setup_b_configured') else 'no'}"
    )


def _signal_text(s: dict) -> str:
    return (
        f"{s.get('created_at')} · {s.get('symbol')} {s.get('direction')} · "
        f"setup {s.get('setup')} ({_val(s.get('timeframe'))})\n"
        f"  entry {_money(s.get('entry'))} · SL {_money(s.get('stop_loss'))} · "
        f"T1 {_money(s.get('target_1'))} · T2 {_money(s.get('target_2'))} · "
        f"R:R 1:{_val(s.get('rr'))}\n"
        f"  qty {_val(s.get('quantity'))} · risk {_val(s.get('risk_amount'))} · "
        f"margin {_val(s.get('margin'))} · {_val(s.get('execution'))}"
    )


def trading_monitor(parameters: dict | None = None, player=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "status").strip().lower()
    symbol = str(params.get("symbol") or "").strip().upper()
    monitor = get_monitor()

    try:
        if action in ("status", "monitor_status", "health"):
            return _health_text(monitor.health())

        if action in ("symbols", "state", "all"):
            state = monitor.state()
            parts = [_health_text(state["health"]), ""]
            for sym, snap in (state.get("symbols") or {}).items():
                parts.append(_symbol_text(sym, snap))
                parts.append("")
            return "\n".join(parts).strip()

        if action in ("symbol", "symbol_status", "detail"):
            if not symbol:
                return "error: pass a symbol (BTCUSDT / ETHUSDT / SOLUSDT / PAXGUSDT)."
            snap = monitor.state()["symbols"].get(symbol)
            if snap is None:
                return f"{symbol} is not in the monitor's configured symbol list."
            return _symbol_text(symbol, snap)

        if action in ("signals", "history"):
            rows = monitor.signals(limit=int(params.get("limit") or 10))
            if not rows:
                return ("No confirmed signals yet. The monitor emits one only "
                        "when every configured condition passes.")
            return "\n\n".join(_signal_text(r) for r in rows)

        if action in ("lifecycle", "positions", "tracked"):
            rows = monitor.lifecycle(limit=int(params.get("limit") or 10))
            if not rows:
                return "No tracked signals. Nothing is being followed to SL/T1/T2."
            out = []
            for r in rows:
                out.append(
                    f"{r.get('symbol')} {r.get('direction')} · {r.get('status')} · "
                    f"entry {r.get('entry')} · SL {r.get('stop_loss')} · "
                    f"T1 {r.get('target_1')} · T2 {r.get('target_2')}"
                    + (f" · realized {r.get('realized_pnl')}"
                       if r.get("realized_pnl") is not None else ""))
            return "\n".join(out)

        return ("Unknown action. Use status / state / symbol / signals / "
                "lifecycle.")
    except Exception as exc:
        return (f"Trading monitor error: {type(exc).__name__}: "
                f"{str(exc)[:240]}. No data was invented.")


TOOL = {
    "name": "trading_monitor",
    "description": (
        "Continuous background trading monitor status for BTC, ETH, SOL and "
        "tokenised gold (PAXG). Answers 'is the trading monitor running?', "
        "'are my setups being watched?', 'what signals fired today?', "
        "'is the market feed connected?'. Reports the worker state, market "
        "data connection, per-symbol sweep/volume/delta state, confirmed "
        "signals and their SL/T1/T2 lifecycle. Read-only: it never places an "
        "order and never enables live trading. Reports STOPPED / DISCONNECTED "
        "/ UNAVAILABLE honestly instead of claiming monitoring is active."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": ["status", "state", "symbol", "signals", "lifecycle"],
                "description": "Which monitor query to run.",
            },
            "symbol": {
                "type": "STRING",
                "description": "BTCUSDT / ETHUSDT / SOLUSDT / PAXGUSDT "
                               "(symbol action).",
            },
            "limit": {"type": "INTEGER",
                      "description": "Max signals/lifecycle rows to return."},
        },
        "required": ["action"],
    },
    "handler": trading_monitor,
}
