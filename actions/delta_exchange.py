"""
actions/delta_exchange.py — Hermes/JARVIS read-only Delta Exchange India tool.

Public data only: product discovery, quote, candles and funding. There is no
account, order, position or transfer method in the shared client, and this
action never imports the paper broker. A paper trade is only possible through
the Trading Agent's approval gate, not from here.

Environment (never logged):
    DELTA_BASE_URL    default https://api.india.delta.exchange
    DELTA_API_KEY     optional; presence is reported, value is never returned
    DELTA_API_SECRET  optional; presence is reported, value is never returned

    The RIO Settings page can store the same key/secret pair in
    config/api_keys.json; the environment pair takes precedence.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _base_dir() -> Path:
    import os
    home = os.environ.get("JARVIS_HOME", "").strip()
    if home:
        return Path(home).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


BASE_DIR = _base_dir()
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


def _fmt(value, digits: int = 4) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _period_days(period: str) -> int:
    p = str(period or "3d").strip().lower()
    table = {"1d": 1, "3d": 3, "5d": 5, "1wk": 7, "1mo": 30, "3mo": 90,
             "6mo": 180, "1y": 365, "2y": 730, "5y": 1825}
    if p in table:
        return table[p]
    if p.endswith("d") and p[:-1].isdigit():
        return max(1, int(p[:-1]))
    return 3


def delta_exchange(parameters: dict, player=None, speak=None) -> str:
    """Read public Delta Exchange India data and return a short text answer."""
    parameters = parameters or {}
    action = str(parameters.get("action") or "status").strip().lower()
    symbol = str(parameters.get("symbol") or "").strip().upper()
    market = str(parameters.get("market") or "").strip().lower()
    query = str(parameters.get("query") or symbol).strip()
    interval = str(parameters.get("interval") or "1h").strip().lower()
    period = str(parameters.get("period") or "3d").strip().lower()

    if player:
        player.write_log(f"[delta] {action} {symbol or query} {market}")

    try:
        from orchestrator.agents.delta_exchange import (
            DeltaAmbiguous, DeltaError, DeltaNotFound, get_client,
        )
        client = get_client()
    except Exception as exc:
        return (f"Delta Exchange India is unavailable: {type(exc).__name__}: "
                f"{str(exc)[:180]}. No data was invented.")

    try:
        if action in ("status", "health"):
            st = client.status()
            if not st.get("ok"):
                return ("Delta Exchange India public data is DISCONNECTED: "
                        f"{st.get('products_error') or 'no status'}. "
                        "No substitute venue was used.")
            return (
                f"Delta Exchange India: {st.get('status')} · "
                f"{st.get('products')} live products "
                f"({st.get('perpetuals')} perps, {st.get('spot')} spot) · "
                f"{st.get('base_url')} · public data + paper only · "
                f"BTCUSD probe: {st.get('ticker_detail') or 'unavailable'}")

        if action in ("products", "list"):
            ct = {"perp": "perpetual_futures", "perpetual": "perpetual_futures",
                  "spot": "spot", "delta_perp": "perpetual_futures",
                  "delta_spot": "spot"}.get(market)
            rows = (client.find_products(query, contract_type=ct, limit=40)
                    if query else client.products())
            if ct:
                rows = [p for p in rows if p.contract_type == ct]
            if not rows:
                return (f"No live Delta product matched '{query or market}'. "
                        "Products are discovered from the venue, not guessed.")
            lines = [f"Delta Exchange India — {len(rows)} matching live "
                     f"product{'s' if len(rows) != 1 else ''}:"]
            for p in rows[:20]:
                lines.append(
                    f"- {p.symbol} · {p.contract_type} · "
                    f"{p.underlying}/{p.quote_currency} · "
                    f"contract {_fmt(p.contract_value)} · "
                    f"funding {p.rate_exchange_interval or 'n/a'}s")
            if len(rows) > 20:
                lines.append(f"… and {len(rows) - 20} more.")
            lines.append("Read-only public data; no order path exists.")
            return "\n".join(lines)

        if action in ("resolve", "find"):
            if not query:
                return ("Name a Delta product or base asset (e.g. BTCUSD, "
                        "ETHUSD, BTC_INR, BTC).")
            ct = {"perp": "perpetual_futures", "perpetual": "perpetual_futures",
                  "spot": "spot", "delta_perp": "perpetual_futures",
                  "delta_spot": "spot"}.get(market)
            p = client.resolve_product(query, contract_type=ct)
            return (f"{p.symbol} · {p.contract_type} · "
                    f"{p.underlying}/{p.quote_currency} · product {p.product_id} "
                    f"· contract value {_fmt(p.contract_value)} · "
                    f"tick {_fmt(p.tick_size)} · leverage "
                    f"{_fmt(p.default_leverage, 1)}× · "
                    f"funding interval {p.rate_exchange_interval or 'n/a'}s")

        if action in ("quote", "price", "ticker"):
            if not symbol:
                return "Name the exact Delta product (e.g. BTCUSD, BTC_INR)."
            p = client.product(symbol)
            if not p:
                err = str(getattr(client, "last_error", "") or "").strip()
                return (f"{symbol} is not a live Delta perpetual/spot product"
                        + (f" (venue error: {err})" if err else "")
                        + ". Ask for Delta products to see the live list.")
            t = client.ticker(symbol)
            return (
                f"{symbol} · {p.contract_type} · last {_fmt(t.get('close'))} "
                f"{p.quote_currency} · open {_fmt(t.get('open'))} · "
                f"high {_fmt(t.get('high'))} · low {_fmt(t.get('low'))} · "
                f"mark {_fmt(t.get('mark_price'))} · "
                f"spot {_fmt(t.get('spot_price'))} · "
                f"volume {_fmt(t.get('volume'), 2)} · "
                f"OI {_fmt(t.get('oi'), 2)} · "
                f"funding {_fmt(t.get('funding_rate'))}%/interval · "
                f"public exchange data, read-only")

        if action in ("candles", "history", "ohlc"):
            if not symbol:
                return "Name the exact Delta product (e.g. BTCUSD, BTC_INR)."
            p = client.product(symbol)
            if not p:
                err = str(getattr(client, "last_error", "") or "").strip()
                return (f"{symbol} is not a live Delta perpetual/spot product"
                        + (f" (venue error: {err})" if err else "")
                        + ". No substitute symbol was used.")
            if interval not in ("1m", "3m", "5m", "15m", "30m", "1h", "2h",
                                "4h", "1d"):
                return (f"Delta does not serve interval '{interval}'. Use one "
                        "of 1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 1d.")
            days = _period_days(period)
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=days)
            res = client.candles(symbol, interval, int(start.timestamp()),
                                 int(end.timestamp()), max_candles=600_000)
            if not res.rows:
                return (f"Delta returned no candles for {symbol} {interval} "
                        f"over {days}d: {res.error or 'empty result'}. "
                        "No bar was invented.")
            first, last = res.rows[0], res.rows[-1]
            return (
                f"{symbol} {interval} · {len(res.rows)} completed candles · "
                f"{datetime.fromtimestamp(first['time'], timezone.utc):%Y-%m-%d %H:%M} UTC "
                f"→ {datetime.fromtimestamp(last['time'], timezone.utc):%Y-%m-%d %H:%M} UTC · "
                f"last close {_fmt(last['close'])} {p.quote_currency} · "
                f"{res.requests_made} request(s)"
                + (f" · truncated: {res.error}" if res.truncated else "")
                + " · public exchange data, read-only")

        if action in ("funding", "funding_history"):
            if not symbol:
                return "Name the exact Delta perpetual (e.g. BTCUSD)."
            p = client.product(symbol)
            if not p or p.contract_type != "perpetual_futures":
                err = str(getattr(client, "last_error", "") or "").strip()
                return (f"{symbol or 'that product'} is not a live Delta "
                        "perpetual; funding history is only for perpetuals"
                        + (f" (venue error: {err})" if err else "") + ".")
            days = _period_days(period)
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=days)
            events, err = client.funding_history(
                symbol, int(start.timestamp()), int(end.timestamp()))
            if not events:
                return (f"Delta served no funding events for {symbol} over "
                        f"{days}d: {err or 'empty result'}. Nothing was "
                        "invented.")
            rates = [e["rate"] for e in events]
            return (
                f"{symbol} funding · {len(events)} events over {days}d · "
                f"last {rates[-1] * 100:.4f}% · "
                f"min {min(rates) * 100:.4f}% · max {max(rates) * 100:.4f}% · "
                f"interval {p.rate_exchange_interval or 28800}s · "
                "decimal rates were divided by 100 from the venue's percent "
                "series" + (f" · {err}" if err else ""))

        return ("Unknown Delta action. Use status, products, resolve, quote, "
                "candles or funding. This tool is read-only.")
    except (DeltaAmbiguous, DeltaNotFound, DeltaError) as exc:
        return f"Delta public data: {str(exc)[:300]}"
    except Exception as exc:
        return (f"Delta public data failed: {type(exc).__name__}: "
                f"{str(exc)[:240]}. No data was invented.")


TOOL = {
    "name": "delta_exchange",
    "description": (
        "Read public Delta Exchange India perpetual/spot market data: live "
        "product discovery, exact product resolution, ticker quote, historical "
        "candles and funding history. Use for 'Delta BTC price', 'list Delta "
        "perps', 'BTCUSD 1h candles', 'Delta funding'. READ-ONLY: it cannot "
        "place, modify or cancel an order and has no account/balance access. "
        "Paper trades happen only through the Trading Agent approval gate. "
        "Never guess a Delta symbol; use products/resolve when unsure."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": ("status | products | resolve | quote | candles "
                                "| funding (default status)"),
            },
            "symbol": {
                "type": "STRING",
                "description": ("Exact Delta product, e.g. BTCUSD, ETHUSD, "
                                "BTC_INR (required for quote/candles/funding)."),
            },
            "market": {
                "type": "STRING",
                "description": ("Optional filter for products/resolve: "
                                "perp | spot."),
            },
            "query": {
                "type": "STRING",
                "description": ("Search text for products/resolve, e.g. BTC, "
                                "SOL, tokenized equity."),
            },
            "interval": {
                "type": "STRING",
                "description": "Candle interval: 1m,3m,5m,15m,30m,1h,2h,4h,1d.",
            },
            "period": {
                "type": "STRING",
                "description": "Lookback: 1d, 3d, 5d, 1wk, 1mo, 3mo, 6mo, 1y.",
            },
        },
        "required": [],
    },
    "handler": delta_exchange,
}
