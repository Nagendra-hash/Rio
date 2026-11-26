"""
orchestrator/agents/trading/delta.py — Delta Exchange India DATA adapter.

This is the read-only bridge between the shared public Delta client
(orchestrator/agents/delta_exchange.py) and the trading DATA stage. It serves
quotes and historical candles for live perpetual/spot products. It does NOT
serve an options chain or news, and it has no order/account path.

The adapter is deliberately separate from the backtest provider:
    * the trading stage needs a quote + a recent chart, not a multi-year
      research frame;
    * provenance stays LIVE for the public exchange ticker/candles, and an
      unavailable answer is returned when Delta cannot serve the product.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from orchestrator.agents.delta_exchange import (
    DeltaClient, DeltaError, DeltaProduct, TIMEFRAMES, get_client,
)
from orchestrator.agents.trading.data import (
    DataAdapter, DataResult, Provenance, _clean_float, _now_iso, unavailable,
)

_PERIOD_DAYS = {
    "1d": 1, "5d": 5, "1wk": 7, "1mo": 30, "3mo": 90, "6mo": 180,
    "1y": 365, "2y": 730, "5y": 1825, "10y": 3650, "max": 3650,
}
_PERIOD_RE = re.compile(r"^(\d+)(d|wk|mo|y|h)$", re.IGNORECASE)
_RESOLUTION_NOTE = {
    "1wk": "Delta serves no weekly resolution; 1d candles are returned.",
    "1mo": "Delta serves no monthly resolution; 1d candles are returned.",
}


def _period_days(period: str) -> int | None:
    p = str(period or "").strip().lower()
    if p in _PERIOD_DAYS:
        return _PERIOD_DAYS[p]
    m = _PERIOD_RE.match(p)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    if n <= 0:
        return None
    if unit == "d":
        return n
    if unit == "wk":
        return n * 7
    if unit == "mo":
        return n * 30
    if unit == "y":
        return n * 365
    if unit == "h":
        return max(1, n // 24)
    return None


def _iso_from_epoch(value: Any) -> str:
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return ""
    if ts > 1e14:                 # Delta ticker timestamps are microseconds
        ts /= 1_000_000.0
    elif ts > 1e11:               # milliseconds
        ts /= 1_000.0
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(
            timespec="seconds")
    except Exception:
        return ""


def _clean_symbol(symbol: str) -> str:
    s = str(symbol or "").strip().upper()
    for prefix in ("DELTA:", "DELTA ", "DELTA-"):
        if s.startswith(prefix):
            s = s[len(prefix):].strip()
    return s


class DeltaTradingAdapter(DataAdapter):
    """Public Delta Exchange India quotes/candles for the trading stage."""

    name = "delta_india"
    venue = "delta"

    def __init__(self, client: DeltaClient | None = None) -> None:
        self.client = client or get_client()

    # ── product helpers ──────────────────────────────────────────────────
    def product(self, symbol: str) -> DeltaProduct | None:
        try:
            return self.client.product(_clean_symbol(symbol))
        except Exception:
            return None

    def is_product(self, symbol: str) -> bool:
        return self.product(symbol) is not None

    def is_cached_product(self, symbol: str) -> bool:
        """Exact product from the cached catalog only — no network I/O."""
        try:
            return self.client.cached_product(_clean_symbol(symbol)) is not None
        except Exception:
            return False

    def _missing_product(self, symbol: str) -> str:
        """Say whether a missing product is a delisting or a venue failure."""
        detail = (f"{symbol} is not a live Delta Exchange India perpetual/spot "
                  "product")
        err = str(getattr(self.client, "last_error", "") or "").strip()
        return f"{detail}; venue error: {err}" if err else detail

    def products(self) -> list[dict[str, Any]]:
        try:
            return [p.to_dict() for p in self.client.products()]
        except DeltaError:
            return []

    def status(self) -> dict[str, Any]:
        return self.client.status()

    # ── quote ────────────────────────────────────────────────────────────
    def quote(self, symbol: str, request_live: bool = False) -> DataResult:
        sym = _clean_symbol(symbol)
        if not sym:
            return unavailable(self.name, "Delta quote needs a product symbol")
        product = self.product(sym)
        if not product:
            return unavailable(
                self.name,
                self._missing_product(sym)
                + " (products are discovered live, not guessed)")
        try:
            t = self.client.ticker(sym)
        except DeltaError as exc:
            return unavailable(self.name, str(exc))
        except Exception as exc:
            return unavailable(self.name, f"{type(exc).__name__}: {exc}")

        last = _clean_float(t.get("close"))
        if last is None:
            return unavailable(self.name, f"Delta ticker served no close for {sym}")

        funding_pct = _clean_float(t.get("funding_rate"))
        payload = {
            "symbol": sym,
            "last": last,
            "previous_close": None,       # Delta ticker does not serve it
            "open": _clean_float(t.get("open")),
            "day_high": _clean_float(t.get("high")),
            "day_low": _clean_float(t.get("low")),
            "volume": _clean_float(t.get("volume"), lo=0),
            "turnover": _clean_float(t.get("turnover"), lo=0),
            "currency": product.quote_currency or "",
            "exchange": "Delta Exchange India",
            "venue": "delta",
            "market": product.market,
            "market_class": product.market_class,
            "product_id": product.product_id,
            "product_symbol": product.symbol,
            "contract_type": product.contract_type,
            "underlying": product.underlying,
            "quote_currency": product.quote_currency,
            "contract_value": product.contract_value,
            "tick_size": _clean_float(t.get("tick_size")) or product.tick_size,
            "default_leverage": product.default_leverage,
            "mark_price": _clean_float(t.get("mark_price")),
            "spot_price": _clean_float(t.get("spot_price")),
            "funding_rate_pct": funding_pct,
            "funding_rate": (funding_pct / 100.0
                             if funding_pct is not None else None),
            "open_interest": _clean_float(t.get("oi"), lo=0),
            "rate_exchange_interval": product.rate_exchange_interval,
            "exchange_time": _iso_from_epoch(t.get("timestamp")),
            "missing_fields": [],
        }
        payload["missing_fields"] = [
            k for k, v in payload.items()
            if v is None and k not in ("missing_fields", "previous_close")
        ]
        return DataResult(ok=True, provenance=Provenance.LIVE,
                          source=self.name, data=payload, as_of=_now_iso())

    # ── history ──────────────────────────────────────────────────────────
    def history(self, symbol: str, period: str = "6mo",
                interval: str = "1d") -> DataResult:
        sym = _clean_symbol(symbol)
        if not sym:
            return unavailable(self.name, "Delta history needs a product symbol")
        product = self.product(sym)
        if not product:
            return unavailable(self.name, self._missing_product(sym))
        requested = str(interval or "1d").strip().lower()
        resolution = requested
        note = ""
        if resolution not in TIMEFRAMES:
            if resolution in ("1wk", "1mo"):
                resolution = "1d"
                note = _RESOLUTION_NOTE.get(requested, "")
            else:
                return unavailable(
                    self.name,
                    f"Delta does not serve interval '{requested}'. Supported: "
                    + ", ".join(TIMEFRAMES))
        days = _period_days(period)
        if days is None:
            return unavailable(self.name, f"unsupported period '{period}'")
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        try:
            result = self.client.candles(
                sym, resolution, int(start.timestamp()), int(end.timestamp()),
                max_candles=600_000)
        except DeltaError as exc:
            return unavailable(self.name, str(exc))
        except Exception as exc:
            return unavailable(self.name, f"{type(exc).__name__}: {exc}")
        if not result.rows:
            return unavailable(
                self.name,
                result.error or f"Delta returned no candles for {sym}")
        bars = []
        for row in result.rows:
            bars.append({
                "ts": _iso_from_epoch(row.get("time")),
                "open": _clean_float(row.get("open")),
                "high": _clean_float(row.get("high")),
                "low": _clean_float(row.get("low")),
                "close": _clean_float(row.get("close")),
                "volume": _clean_float(row.get("volume"), lo=0) or 0.0,
            })
        bars = [b for b in bars if b["close"] is not None]
        if not bars:
            return unavailable(self.name, "Delta history had no usable closes")
        return DataResult(
            ok=True, provenance=Provenance.LIVE, source=self.name,
            data={
                "symbol": sym,
                "interval": requested,
                "resolution": resolution,
                "bars": bars,
                "venue": "delta",
                "market": product.market,
                "market_class": product.market_class,
                "product_id": product.product_id,
                "contract_type": product.contract_type,
                "quote_currency": product.quote_currency,
                "contract_value": product.contract_value,
                "truncated": bool(result.truncated),
                "pagination_note": result.error,
                "note": note,
            },
            as_of=_now_iso())

    # ── unsupported surfaces stay honest ─────────────────────────────────
    def options_chain(self, symbol: str) -> DataResult:
        return unavailable(
            self.name,
            "Delta Exchange India public perp/spot data serves no options "
            "chain in this integration; no chain is synthesized")

    def news(self, symbol: str) -> DataResult:
        return unavailable(
            self.name,
            "Delta public market data serves no news; use the news feed")


__all__ = ["DeltaTradingAdapter", "_period_days", "_clean_symbol"]
