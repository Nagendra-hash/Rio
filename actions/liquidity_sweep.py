"""
actions/liquidity_sweep.py — Jarvis voice surface for the liquidity engine.

Read-only strategy status plus manual configuration. It never places an order
and never invents market data; every number comes from core/liquidity_sweep,
which consumes the existing Binance daemon.
"""
from __future__ import annotations

from core.liquidity_sweep import get_engine


def _fmt_price(value) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _status_text(snapshot: dict) -> str:
    if not snapshot or not snapshot.get("ok"):
        return f"Liquidity status unavailable: {(snapshot or {}).get('error', 'no data')}"
    lines = [
        f"{snapshot.get('asset')} ({snapshot.get('symbol')}) — "
        f"{snapshot.get('state')}",
        f"Price: {_fmt_price(snapshot.get('price'))} "
        f"({snapshot.get('timeframe')} timeframe)",
        f"SSL zone: {_fmt_price((snapshot.get('zone') or {}).get('low'))}–"
        f"{_fmt_price((snapshot.get('zone') or {}).get('high'))}",
    ]
    feed = snapshot.get("feed") or {}
    lines.append(
        f"Binance: {feed.get('transport', 'none')} · "
        f"live={'yes' if feed.get('live') else 'no'} · "
        f"stale={'yes' if feed.get('stale') else 'no'}")
    sweep = snapshot.get("sweep")
    if sweep:
        lines.append(
            f"Sweep: low {_fmt_price(sweep.get('price'))} · "
            f"previous swing low {_fmt_price(sweep.get('prev_swing_low'))} · "
            f"depth {sweep.get('depth_pct')}%")
        lines.append(
            f"Recovery: {_fmt_price(sweep.get('recovery_price'))} · "
            f"currently below zone={'yes' if snapshot.get('below_zone') else 'no'}")
    rejection = snapshot.get("rejection")
    if rejection:
        lines.append(f"Rejection: {', '.join(rejection.get('patterns') or [])}")
    volume = snapshot.get("volume")
    if volume:
        lines.append(
            f"Volume: {volume.get('multiplier')}x average "
            f"(min {volume.get('threshold')}x)")
    delta = snapshot.get("delta")
    if delta:
        lines.append(
            f"Delta: {'UNAVAILABLE' if not delta.get('available') else ('PASS' if delta.get('passed') else 'FAIL')}"
            f" ({delta.get('delta')}, taker estimate)")
    confirmation = snapshot.get("confirmation")
    if confirmation:
        lines.append(
            f"Next candle: {'PASS' if confirmation.get('passed') else 'FAIL'}"
            f" (close {_fmt_price(confirmation.get('close'))})")
    risk = snapshot.get("risk")
    if risk:
        lines.append(
            f"Entry: {_fmt_price(risk.get('entry_low'))}–"
            f"{_fmt_price(risk.get('entry_high'))} · "
            f"Stop: {_fmt_price(risk.get('stop_loss'))} · "
            f"Target: {_fmt_price(risk.get('target'))} · "
            f"R:R {risk.get('rr')}")
        if risk.get("risk_amount") is not None:
            lines.append(
                f"Risk: ${risk.get('risk_amount')} "
                f"({risk.get('risk_pct')}% of entry)")
    lines.append(f"Last update (UTC): {snapshot.get('last_update') or '—'}")
    lines.append("")
    lines.append(snapshot.get("advice", ""))
    return "\n".join(lines)


def _conditions_text(snapshot: dict) -> str:
    if not snapshot or not snapshot.get("ok"):
        return "Confirmation conditions unavailable."
    lines = [f"{snapshot.get('asset')} ({snapshot.get('symbol')}) "
             f"confirmation conditions:"]
    for cond in snapshot.get("conditions") or []:
        detail = f" — {cond['detail']}" if cond.get("detail") else ""
        lines.append(f"- {cond.get('name')}: {cond.get('status')}{detail}")
    lines.append("")
    lines.append(snapshot.get("advice", ""))
    return "\n".join(lines)


def _history_text(symbol: str, rows: list) -> str:
    if not rows:
        return f"No liquidity sweep history for {symbol} yet."
    lines = [f"{symbol} liquidity sweep history (UTC):"]
    for row in rows[:10]:
        if row["kind"] == "sweep":
            lines.append(
                f"- {row.get('time', '')} SWEEP {row.get('interval')} "
                f"low {row.get('sweep_price')} · depth {row.get('depth_pct')}%")
        elif row["kind"] == "signal":
            lines.append(
                f"- {row.get('time', '')} SIGNAL {row.get('status')} "
                f"({row.get('state')}) · R:R {row.get('rr')}"
                + (f" · blocked: {row.get('blocked_reason')}"
                   if row.get("blocked_reason") else ""))
        else:
            lines.append(
                f"- {row.get('time', '')} {str(row.get('outcome', '')).upper()} "
                f"{row.get('interval')} · {row.get('reason')}")
    return "\n".join(lines)


def liquidity_sweep(parameters: dict | None = None, player=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "status").strip().lower()
    symbol = str(params.get("symbol") or "").strip().upper()
    engine = get_engine()

    try:
        if action in ("status", "liquidity_status", "all"):
            if symbol:
                return _status_text(engine.get_status(symbol))
            state = engine.dashboard_state()
            parts = []
            for sym in state.get("symbols", {}):
                parts.append(_status_text(state["symbols"][sym]))
            return "\n\n".join(parts) if parts else "No liquidity profiles configured."

        if action in ("sweep_status", "sweep"):
            snapshot = engine.get_status(symbol or "BTCUSDT")
            sweep = snapshot.get("sweep")
            if not sweep:
                return (f"{snapshot.get('asset')}: no liquidity sweep detected. "
                        f"State {snapshot.get('state')}. No long signal.")
            return _status_text(snapshot)

        if action in ("reversal_status", "reversal", "should_i_buy",
                      "buy_signal", "explain"):
            return engine.voice_answer(symbol or "BTCUSDT")

        if action in ("history", "reversal_history"):
            pair = (engine.get_status(symbol or "BTCUSDT").get("symbol")
                    or symbol or "BTCUSDT")
            rows = engine.get_history(pair, limit=int(params.get("limit") or 20))
            return _history_text(pair, rows)

        if action in ("conditions", "confirmation_conditions"):
            return _conditions_text(engine.get_status(symbol or "BTCUSDT"))

        if action in ("set_zone", "set_liquidity_zone"):
            low = params.get("low")
            high = params.get("high")
            if low is None or high is None:
                return "error: set_zone needs low and high."
            snapshot = engine.set_zone(symbol or "BTCUSDT", low, high)
            return ("✅ Liquidity zone updated.\n" + _status_text(snapshot))

        if action in ("set_alert", "set_reversal_alert", "enable_alerts",
                      "disable_alerts"):
            enabled = params.get("enabled")
            if enabled is None:
                enabled = action != "disable_alerts"
            if isinstance(enabled, str):
                enabled = enabled.strip().lower() in ("1", "true", "yes", "on")
            snapshot = engine.set_alerts(symbol or "BTCUSDT", bool(enabled))
            return (f"✅ Reversal alerts "
                    f"{'enabled' if enabled else 'disabled'} for "
                    f"{snapshot.get('symbol')}.\n" + _status_text(snapshot))

        if action in ("auto_zone", "auto_liquidity_zone"):
            snapshot = engine.auto_set_zone(symbol or "BTCUSDT", persist=True)
            return ("✅ Zone derived from the recent swing low.\n"
                    + _status_text(snapshot))

        return ("Unknown action. Use status / sweep_status / reversal_status / "
                "history / conditions / set_zone / set_reversal_alert / "
                "should_i_buy / explain.")
    except Exception as exc:
        return (f"Liquidity engine error: {type(exc).__name__}: "
                f"{str(exc)[:240]}. No data was invented.")


TOOL = {
    "name": "liquidity_sweep",
    "description": (
        "Liquidity sweep and reversal confirmation status for crypto (BTC, ETH). "
        "Answers 'should I buy Bitcoin now?', 'what is the BTC sweep status?', "
        "'explain the Ethereum setup', 'what are the confirmation conditions?', "
        "and can set the SSL zone or toggle reversal alerts. Read-only: it never "
        "places an order. A sweep or a percentage move alone is NOT a buy signal; "
        "LONG_SIGNAL_READY requires a closed rejection candle, volume climax, "
        "positive taker-side delta and next-candle confirmation. Returns honest "
        "GREY/DISCONNECTED or UNAVAILABLE states when data is missing."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": [
                    "status", "sweep_status", "reversal_status", "history",
                    "conditions", "set_zone", "set_reversal_alert",
                    "auto_zone", "should_i_buy", "explain",
                ],
                "description": "Which liquidity/reversal operation to perform.",
            },
            "symbol": {
                "type": "STRING",
                "description": "BTC, ETH (or BTCUSDT / ETHUSDT). Default BTC.",
            },
            "low": {"type": "NUMBER", "description": "SSL zone low (set_zone)."},
            "high": {"type": "NUMBER", "description": "SSL zone high (set_zone)."},
            "enabled": {
                "type": "STRING",
                "description": "true/false for set_reversal_alert.",
            },
            "limit": {
                "type": "INTEGER",
                "description": "History rows to return (default 20).",
            },
        },
        "required": ["action"],
    },
    "handler": liquidity_sweep,
}
