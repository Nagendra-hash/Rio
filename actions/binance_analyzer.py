"""
actions/binance_analyzer.py — Binance real-time market data for Jarvis.

Tracks BTC, ETH, SOL, PAXG, XAU via a single multiplexed WebSocket feed.
Every tick/candle is persisted to SQLite; price alerts fire through Hermes;
periodic summaries go to Telegram.

Exposes five tools to the assistant:
  binance_status   — current connection state + latest prices
  binance_query    — history / hourly stats / candle snapshots
  binance_set_alert— add or update a price-level or % move alert
  binance_rm_alert — remove an alert rule
  binance_summary  — force an immediate market summary push
"""
from __future__ import annotations

from core import binance_ws as _ws


def binance_status(parameters: dict, player=None) -> str:
    """Return the current state of the Binance market-data daemon."""
    st = _ws.status()
    transport = str(st.get("transport") or "none")
    label = {"ws": "🟢 WebSocket", "http": "🟡 HTTP poll"}.get(
        transport, "🔴 DISCONNECTED")
    lines = [
        f"Feed: {label}",
        f"Symbols: {', '.join(st['symbols_tracking'])}",
        f"Uptime: {st['uptime_s']:.0f}s",
        f"DB ticks: {st['db']['ticker_rows']:,} · candles: {st['db']['candle_rows']:,}",
        "",
    ]
    if transport == "none":
        lines.append("No live Binance feed right now — no prices are being reported.")
        return "\n".join(lines)
    for sym, t in st.get("latest", {}).items():
        emoji = "🟢" if t.get("change_24h", 0) >= 0 else "🔴"
        lines.append(
            f"{emoji} {sym}  ${t.get('price', 0):,.2f}"
            f"  24h {t.get('change_24h', 0):+.2f}%"
            f"  H:{t.get('high_24h',0):,.2f} L:{t.get('low_24h',0):,.2f}"
        )
    return "\n".join(lines)


def binance_query(parameters: dict, player=None) -> str:
    """Query historical data or hourly stats.

    Parameters:
      symbol  — e.g. BTC, ETH, SOL (default: BTC)
      hours   — lookback hours for stats (default: 1)
      bars    — number of candle bars (default: 20)
      interval — 1m, 5m, 15m (default: 1m)
    """
    params = parameters or {}
    sym = str(params.get("symbol", "BTC")).upper()
    try:
        hours = max(0.1, float(params.get("hours", 1)))
    except (TypeError, ValueError):
        hours = 1.0

    # Latest candles
    interval = str(params.get("interval", "1m"))
    try:
        bars = max(1, min(int(params.get("bars", 20)), 100))
    except (TypeError, ValueError):
        bars = 20
    candles = _ws.query_history(sym, interval=interval, bars=bars)
    stats = _ws.hourly_stats(sym, hours)
    if not stats and not candles:
        return (
            f"No {sym} data in the last {hours:g}h. "
            "The Binance feed may still be connecting — check binance_status."
        )

    lines: list[str] = []
    if stats:
        lines.extend([
            f"📈 {sym}  (last {hours:g}h)",
            f"  Price : ${stats['last']:,.2f}  (first: ${stats['first']:,.2f})",
            f"  Range : ${stats['min']:,.2f} – ${stats['max']:,.2f}",
            f"  Avg   : ${stats['avg']:,.2f}",
            f"  Change: {stats['change_pct']:+.2f}%  ({stats['count']} ticks)",
        ])
    else:
        lines.append(f"📈 {sym} — no tick history in the last {hours:g}h.")

    if candles:
        lines.append(f"\n  Last {len(candles)} {interval} candles:")
        for c in candles[-10:]:
            dir_arrow = "📗" if c["close"] >= c["open"] else "📕"
            lines.append(
                f"    {dir_arrow} O:${c['open']:,.2f} H:${c['high']:,.2f}"
                f" L:${c['low']:,.2f} C:${c['close']:,.2f} V:{c['volume']:,.0f}"
            )
    return "\n".join(lines)


def binance_set_alert(parameters: dict, player=None) -> str:
    """Set or update a price alert for a symbol.

    Parameters:
      symbol  — e.g. BTC, ETH
      below   — trigger when price drops below this level
      above   — trigger when price rises above this level
      pct_1m  — % move on closed 1m candle
      pct_5m  — % move on closed 5m candle
    """
    params = parameters or {}
    sym = str(params.get("symbol", "BTC")).lower()
    # Only real alert keys belong in the JSON config — passing the tool
    # envelope through also stored {"action": "set_alert"} as an alert field.
    allowed = ("below", "above", "pct_1m", "pct_5m")
    keep = {}
    for key in allowed:
        value = params.get(key)
        if value is None:
            continue
        try:
            keep[key] = float(value)
        except (TypeError, ValueError):
            return f"error: '{key}' must be a number, got {value!r}"
    if not keep:
        return ("error: give at least one of below / above / pct_1m / pct_5m "
                f"to set an alert for {sym.upper()}")
    result = _ws.set_alert(sym, **keep)
    parts = [f"⚡ Alert set for {sym.upper()}:"]
    for k, v in result.items():
        parts.append(f"  {k}: {v}")
    return "\n".join(parts)


def binance_rm_alert(parameters: dict, player=None) -> str:
    """Remove an alert key for a symbol."""
    params = parameters or {}
    sym = str(params.get("symbol", "BTC")).lower()
    key = str(params.get("key", "")).strip()
    if not key:
        return "error: 'key' required (e.g. below, above, pct_1m, pct_5m)"
    existing = _ws.get_alerts(sym)
    if key not in existing:
        current = ", ".join(existing) or "none"
        return f"⚠  {sym.upper()} has no '{key}' alert (current: {current})."
    _ws.remove_alert(sym, key)
    return f"✅ Alert '{key}' removed from {sym.upper()}"


def binance_summary(parameters: dict, player=None) -> str:
    """Force an immediate market summary to be sent."""
    if _ws.emit_summary() is False:
        return ("📊 Market summaries are disabled — alerts-only mode. "
                "Price-threshold alerts remain active.")
    return "📊 Summary queued — check your Telegram shortly."


def binance_dispatch(parameters: dict | None = None, player=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "status").strip().lower()
    if action == "status":
        return binance_status(params)
    if action == "query":
        return binance_query(params)
    if action in ("set_alert", "alert"):
        return binance_set_alert(params)
    if action in ("remove_alert", "rm_alert"):
        return binance_rm_alert(params)
    if action == "summary":
        return binance_summary(params)
    return f"Unknown action {action!r}. Use status/query/set_alert/remove_alert/summary."


TOOL = {
    "name": "binance_analyzer",
    "description": (
        "Real-time Binance crypto market data. Tracks BTC, ETH, SOL, PAXG, XAU "
        "via live WebSocket. Use for: current prices, 1h stats, candle history, "
        "setting/removing price alerts, and forcing a market summary push to "
        "Telegram via Hermes. Requires the binance-ws background daemon (started "
        "automatically when JARVIS_BINANCE_AGENT=true). Falls back to REST polling "
        "when WebSocket is blocked and reports DISCONNECTED honestly when neither "
        "works. No API key needed."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": ["status", "query", "set_alert", "remove_alert", "summary"],
                "description": "Which sub-action to perform.",
            },
            "symbol": {
                "type": "STRING",
                "description": "Symbol: BTC, ETH, SOL, PAXG, XAU (default BTC).",
            },
            "below": {"type": "NUMBER", "description": "Price level to alert below."},
            "above": {"type": "NUMBER", "description": "Price level to alert above."},
            "pct_1m": {"type": "NUMBER", "description": "% move on 1m candle."},
            "pct_5m": {"type": "NUMBER", "description": "% move on 5m candle."},
            "key": {"type": "STRING", "description": "Alert key to remove."},
            "hours": {"type": "INTEGER", "description": "Lookback hours for stats."},
            "bars": {"type": "INTEGER", "description": "Candle bars to return."},
            "interval": {"type": "STRING", "enum": ["1m", "5m", "15m"],
                         "description": "Candle interval."},
        },
        "required": ["action"],
    },
    # core/action_loader.py calls handlers as fn(parameters=<dict>, ...) —
    # a handler declaring `params` cannot be invoked and every call raised
    # TypeError before this signature was fixed.
    "handler": binance_dispatch,
}
