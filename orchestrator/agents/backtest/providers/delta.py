"""
orchestrator/agents/backtest/providers/delta.py — Delta Exchange India.

READ-ONLY market data. Two separate markets, never mixed with another venue:

    delta_perp   live perpetual_futures products (BTCUSD, ETHUSD, …)
    delta_spot   live spot products (BTC_INR, ETH_INR, SOL_INR, XRP_INR)

The product catalog is discovered from /v2/products (states=live,
contract_types=perpetual_futures,spot) and cached by the shared client.
Candles come from /v2/history/candles and are paginated backwards because
Delta returns the newest window for a request.

There is no order, account, balance or position code path in this file.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from orchestrator.agents.backtest.providers.base import (
    FetchResult, Provider, drop_incomplete, failed, normalise_frame,
    validate_frame,
)
from orchestrator.agents.delta_exchange import (
    TIMEFRAMES, DeltaClient, DeltaError, DeltaProduct,
)


class DeltaIndia(Provider):
    name = "delta_india"
    venue = "Delta Exchange India"
    supported_markets = ("delta_perp", "delta_spot")
    native_timeframes = TIMEFRAMES

    def __init__(self, cfg=None, client: DeltaClient | None = None) -> None:
        self.cfg = cfg
        self.client = client or DeltaClient()

    # ── product checks ───────────────────────────────────────────────────
    def supports(self, instrument) -> bool:
        return (instrument.market_class in self.supported_markets
                and bool(instrument.provider_symbols.get(self.name)
                         or instrument.symbol))

    def available(self, symbol: str,
                  market_class: str | None = None) -> tuple[bool, str]:
        """Is this exact product live and of the requested market type?"""
        want = str(symbol or "").strip().upper()
        if not want:
            return False, "no Delta venue symbol"
        try:
            product = self.client.product(want)
        except Exception as exc:
            return False, f"Delta product lookup failed: {type(exc).__name__}: {exc}"
        if not product:
            return False, f"{want} is not a live Delta product"
        if market_class:
            expected = ("perpetual_futures" if market_class == "delta_perp"
                        else "spot" if market_class == "delta_spot" else "")
            if expected and product.contract_type != expected:
                return False, (f"{want} is {product.contract_type}, not {expected}")
        if product.state != "live":
            return False, f"{want} state={product.state}"
        if product.trading_status not in ("", "operational"):
            return False, f"{want} trading_status={product.trading_status}"
        return True, (f"{product.description or want} · "
                      f"{product.contract_type} · quote "
                      f"{product.quote_currency or '?'}")

    def products(self) -> list[DeltaProduct]:
        return self.client.products()

    # ── candles ──────────────────────────────────────────────────────────
    def fetch(self, instrument, timeframe: str, start: datetime,
              end: datetime, *, max_candles: int | None = None) -> FetchResult:
        venue_symbol = str((instrument.provider_symbols or {}).get(self.name)
                           or instrument.symbol).upper()
        ok, detail = self.available(venue_symbol, instrument.market_class)
        if not ok:
            return failed(self.name, timeframe, detail)

        start_s = int(start.timestamp())
        end_s = int(end.timestamp())
        cap = int(max_candles or 600_000)
        try:
            result = self.client.candles(venue_symbol, timeframe,
                                         start_s, end_s, max_candles=cap)
        except DeltaError as exc:
            return failed(self.name, timeframe, str(exc))

        if not result.rows:
            return failed(self.name, timeframe,
                          result.error or "Delta returned no candles")
        idx = pd.to_datetime([r["time"] for r in result.rows],
                             unit="s", utc=True)
        frame = pd.DataFrame(
            [{k: r[k] for k in ("open", "high", "low", "close", "volume")}
             for r in result.rows],
            index=pd.DatetimeIndex(idx, name="ts"),
        )
        frame, duplicates = normalise_frame(frame)
        frame, dropped = drop_incomplete(frame, timeframe)
        problem = validate_frame(frame, timeframe)
        if problem:
            return failed(self.name, timeframe, problem,
                          limitations=[f"venue {venue_symbol}"])

        limitations = [
            f"Delta Exchange India {instrument.market} product {venue_symbol}; "
            "this venue is a separate dataset and is never averaged with "
            "Binance, Kraken or Yahoo.",
            "Public exchange candles; no account, order or position data is "
            "read by this provider.",
        ]
        product = self.client.product(venue_symbol)
        if product:
            if product.contract_value:
                limitations.append(
                    f"Contract value {product.contract_value:g} "
                    f"{product.underlying or 'units'} per contract; the "
                    "backtest sizes underlying units, not exchange lots.")
            if instrument.market_class == "delta_perp":
                limitations.append(
                    "Perpetual funding is charged at the product's "
                    "rate_exchange_interval when funding history is served.")
        if result.error:
            limitations.append(f"pagination note: {result.error}")
        return FetchResult(
            ok=True, provider=self.name, timeframe=timeframe, frame=frame,
            limitations=limitations, requests_made=result.requests_made,
            truncated=result.truncated, incomplete_dropped=dropped,
            duplicates_dropped=duplicates, venue_symbol=venue_symbol,
            note=detail,
        )

    # ── funding ──────────────────────────────────────────────────────────
    def funding_history(self, venue_symbol: str, start: datetime,
                        end: datetime) -> tuple[pd.Series, str]:
        """Decimal funding rates indexed by Delta's funding timestamps."""
        try:
            events, err = self.client.funding_history(
                venue_symbol, int(start.timestamp()), int(end.timestamp()))
        except Exception as exc:
            return pd.Series(dtype="float64"), (
                f"{type(exc).__name__}: {str(exc)[:180]}")
        if not events:
            return pd.Series(dtype="float64"), err or "no funding events"
        series = pd.Series(
            {pd.Timestamp(int(e["time"]), unit="s", tz="UTC"):
             float(e["rate"]) for e in events}
        ).sort_index()
        return series, err

    def health(self) -> dict[str, Any]:
        try:
            rows = self.client.products()
            return {"provider": self.name, "venue": self.venue,
                    "markets": list(self.supported_markets),
                    "timeframes": list(self.native_timeframes),
                    "products": len(rows),
                    "base_url": self.client.base_url,
                    "read_only": True}
        except Exception as exc:
            return {"provider": self.name, "venue": self.venue,
                    "markets": list(self.supported_markets),
                    "timeframes": list(self.native_timeframes),
                    "products": 0, "base_url": self.client.base_url,
                    "error": f"{type(exc).__name__}: {str(exc)[:180]}",
                    "read_only": True}


__all__ = ["DeltaIndia"]
