#!/usr/bin/env python3
"""
scripts/delta_cli.py — read-only Delta Exchange India public-data CLI.

    python scripts/delta_cli.py status
    python scripts/delta_cli.py products --query BTC --market perp
    python scripts/delta_cli.py resolve BTC --market perp
    python scripts/delta_cli.py quote BTCUSD
    python scripts/delta_cli.py candles BTCUSD --interval 1h --days 3
    python scripts/delta_cli.py funding BTCUSD --days 30
    python scripts/delta_cli.py quote BTCUSD --json

Public products/tickers/candles only. No order, account or position endpoint
exists in the shared client, and this CLI never imports the broker.

Exit codes: 0 success, 2 usage, 3 not found, 4 venue/data failure.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orchestrator.agents.delta_exchange import (                    # noqa: E402
    DeltaAmbiguous, DeltaError, DeltaNotFound, get_client,
)

_MARKETS = {
    "perp": "perpetual_futures", "perpetual": "perpetual_futures",
    "delta_perp": "perpetual_futures", "spot": "spot",
    "delta_spot": "spot",
}


def _iso(ts: int | float | None) -> str:
    if ts is None:
        return "—"
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC")


def _num(v, digits: int = 4) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):,.{digits}f}"
    except (TypeError, ValueError):
        return str(v)


def _print(obj, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, sort_keys=True, default=str))
        return
    for key, value in obj.items():
        if isinstance(value, (dict, list)):
            print(f"{key}: {json.dumps(value, default=str)[:1000]}")
        else:
            print(f"{key}: {value}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Read-only Delta Exchange India public market data.")
    ap.add_argument("action", choices=[
        "status", "products", "resolve", "quote", "candles", "funding"])
    ap.add_argument("symbol", nargs="?", default="", help="e.g. BTCUSD")
    ap.add_argument("--query", default="", help="product search text")
    ap.add_argument("--market", default="", help="perp | spot")
    ap.add_argument("--interval", default="1h",
                    choices=["1m", "3m", "5m", "15m", "30m", "1h", "2h",
                             "4h", "1d"])
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    client = get_client()
    ct = _MARKETS.get(args.market.strip().lower())
    if args.market and not ct:
        print("market must be perp or spot", file=sys.stderr)
        return 2
    symbol = args.symbol.strip().upper()
    query = (args.query or symbol).strip()
    days = max(1, min(int(args.days), 3650))

    try:
        if args.action == "status":
            payload = client.status()
            if args.json:
                _print(payload, True)
            else:
                print(f"Delta Exchange India: {payload.get('status')}")
                print(f"base_url: {payload.get('base_url')}")
                print(f"products: {payload.get('products')} "
                      f"(perps {payload.get('perpetuals')}, "
                      f"spot {payload.get('spot')})")
                print(f"BTCUSD probe: {payload.get('ticker_detail') or '—'}")
                print(f"credentials_configured: "
                      f"{payload.get('credentials_configured')}")
                print("mode: public data + paper only")
            return 0 if payload.get("ok") else 4

        if args.action == "products":
            rows = (client.find_products(query, contract_type=ct,
                                         limit=max(1, min(args.limit, 500)))
                    if query else client.products())
            if ct:
                rows = [p for p in rows if p.contract_type == ct]
            rows = rows[:max(1, min(args.limit, 500))]
            if args.json:
                _print({"count": len(rows),
                        "products": [p.to_dict() for p in rows]}, True)
            else:
                print(f"{len(rows)} live Delta products"
                      + (f" matching '{query}'" if query else ""))
                for p in rows:
                    print(f"{p.symbol:16} {p.contract_type:20} "
                          f"{p.underlying}/{p.quote_currency:5} "
                          f"contract={_num(p.contract_value)}")
            return 0

        if args.action == "resolve":
            if not query:
                print("name a product or base asset", file=sys.stderr)
                return 2
            p = client.resolve_product(query, contract_type=ct)
            if args.json:
                _print(p.to_dict(), True)
            else:
                print(f"{p.symbol} · {p.contract_type} · "
                      f"{p.underlying}/{p.quote_currency}")
                print(f"product_id: {p.product_id}")
                print(f"contract_value: {_num(p.contract_value)}")
                print(f"tick_size: {_num(p.tick_size)}")
                print(f"default_leverage: {_num(p.default_leverage, 1)}x")
                print(f"funding_interval: {p.rate_exchange_interval or '—'}s")
            return 0

        if not symbol:
            print(f"{args.action} needs an exact symbol (e.g. BTCUSD)",
                  file=sys.stderr)
            return 2
        product = client.product(symbol)
        if not product:
            err = str(getattr(client, "last_error", "") or "").strip()
            if err:
                print(f"Delta product lookup failed: {err}", file=sys.stderr)
                return 4
            print(f"{symbol} is not a live Delta perpetual/spot product",
                  file=sys.stderr)
            return 3

        if args.action == "quote":
            t = client.ticker(symbol)
            payload = {"symbol": symbol, **t,
                       "contract_type": product.contract_type,
                       "quote_currency": product.quote_currency,
                       "product_id": product.product_id,
                       "read_only": True}
            if args.json:
                _print(payload, True)
            else:
                print(f"{symbol} · {product.contract_type} · "
                      f"{product.underlying}/{product.quote_currency}")
                for key in ("close", "open", "high", "low", "mark_price",
                            "spot_price", "volume", "turnover", "oi",
                            "funding_rate", "timestamp"):
                    if key in t:
                        print(f"{key}: {t.get(key)}")
                print("read_only: True")
            return 0

        if args.action == "candles":
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=days)
            result = client.candles(
                symbol, args.interval, int(start.timestamp()),
                int(end.timestamp()), max_candles=600_000)
            payload = {
                "symbol": symbol, "interval": args.interval, "days": days,
                "count": len(result.rows), "requests": result.requests_made,
                "truncated": result.truncated, "error": result.error,
                "first": result.rows[0] if result.rows else None,
                "last": result.rows[-1] if result.rows else None,
                "read_only": True,
            }
            if args.json:
                payload["rows"] = result.rows
                _print(payload, True)
            else:
                if not result.rows:
                    print(f"no candles: {result.error or 'empty result'}",
                          file=sys.stderr)
                    return 4
                print(f"{symbol} {args.interval} · {len(result.rows)} candles "
                      f"· {result.requests_made} request(s)")
                print(f"first: {_iso(result.rows[0]['time'])} "
                      f"close={_num(result.rows[0]['close'])}")
                print(f"last:  {_iso(result.rows[-1]['time'])} "
                      f"close={_num(result.rows[-1]['close'])}")
                if result.truncated:
                    print(f"truncated: {result.error}")
                print("read_only: True")
            return 0

        if args.action == "funding":
            if product.contract_type != "perpetual_futures":
                print(f"{symbol} is not a perpetual; no funding history",
                      file=sys.stderr)
                return 2
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=days)
            events, err = client.funding_history(
                symbol, int(start.timestamp()), int(end.timestamp()))
            payload = {"symbol": symbol, "days": days, "count": len(events),
                       "events": events, "error": err, "read_only": True}
            if args.json:
                _print(payload, True)
            else:
                print(f"{symbol} funding · {len(events)} events over {days}d")
                for e in events[-12:]:
                    print(f"{_iso(e['time'])}  {e['rate'] * 100:.6f}%")
                if err:
                    print(f"note: {err}")
                print("read_only: True")
            return 0 if events else 4

    except DeltaAmbiguous as exc:
        print(str(exc), file=sys.stderr)
        for p in exc.candidates[:24]:
            print(f"  {p.symbol} ({p.contract_type})", file=sys.stderr)
        return 3
    except DeltaNotFound as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except DeltaError as exc:
        print(f"Delta public data error: {exc}", file=sys.stderr)
        return 4
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
