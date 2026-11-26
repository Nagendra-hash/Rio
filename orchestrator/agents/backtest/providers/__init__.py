"""
orchestrator/agents/backtest/providers — provider registry and selection order.

Selection is configuration-driven and explicit:

    instrument.market_class -> config.provider_order[market_class]

The lab tries each provider in that order and keeps the first one whose actual
returned candles pass the coverage gate. It never merges two venues, never
substitutes spot for futures (or vice versa), and never treats a continuous
futures series as a tradable contract without saying so.
"""
from __future__ import annotations

from typing import Iterable

from orchestrator.agents.backtest.config import Config, default_config
from orchestrator.agents.backtest.providers.base import FetchResult, Provider
from orchestrator.agents.backtest.providers.binance import (
    BinanceFutures, BinanceSpot,
)
from orchestrator.agents.backtest.providers.delta import DeltaIndia
from orchestrator.agents.backtest.providers.dhan import DhanMcxGold
from orchestrator.agents.backtest.providers.kraken import KrakenProvider
from orchestrator.agents.backtest.providers.twelvedata import TwelveDataXauUsd
from orchestrator.agents.backtest.providers.yahoo import YahooGoldProvider


def build_providers(cfg: Config | None = None) -> dict[str, Provider]:
    cfg = cfg or default_config()
    out: dict[str, Provider] = {}
    for p in (
        BinanceSpot(cfg),
        BinanceFutures(cfg),
        KrakenProvider(cfg),
        DeltaIndia(cfg),
        YahooGoldProvider(cfg, symbol="GC=F", name="yahoo_gc",
                          market_class="gold_futures"),
        YahooGoldProvider(cfg, symbol="XAUUSD=X", name="yahoo_xauusd",
                          market_class="gold_xauusd"),
        TwelveDataXauUsd(cfg),
        DhanMcxGold(cfg),
    ):
        out[p.name] = p
    return out


def providers_for(instrument, cfg: Config | None = None,
                  registry: dict[str, Provider] | None = None
                  ) -> list[Provider]:
    cfg = cfg or default_config()
    registry = registry or build_providers(cfg)
    order = (cfg.get("provider_order") or {}).get(instrument.market_class) or []
    selected = [registry[name] for name in order if name in registry]
    if not selected:
        selected = [p for p in registry.values() if p.supports(instrument)]
    return [p for p in selected if p.supports(instrument)]


def provider_health(cfg: Config | None = None,
                    registry: dict[str, Provider] | None = None,
                    *, instrument=None) -> list[dict]:
    """Probe each adapter. `available()` may need a venue symbol, so an
    instrument is passed when the caller has one; without it we report
    "not probed" instead of inventing a symbol."""
    import inspect

    registry = registry or build_providers(cfg)
    rows = []
    for name, provider in registry.items():
        row = {"provider": name, "venue": provider.venue,
               "markets": list(provider.supported_markets),
               "timeframes": list(provider.native_timeframes)}
        available = getattr(provider, "available", None)
        if callable(available):
            symbol = ""
            if instrument is not None:
                symbol = str((instrument.provider_symbols or {}).get(name) or "")
            required = []
            try:
                required = [p for p in inspect.signature(available).parameters.values()
                            if p.default is inspect.Parameter.empty and
                            p.kind in (p.POSITIONAL_ONLY,
                                       p.POSITIONAL_OR_KEYWORD)]
            except (TypeError, ValueError):
                required = []
            if required and not symbol:
                row.update({"available": None,
                            "detail": "not probed (provider needs a venue symbol)"})
            else:
                try:
                    ok, detail = available(symbol) if required else available()
                    row.update({"available": bool(ok), "detail": detail})
                except Exception as exc:
                    row.update({"available": False,
                                "detail": f"{type(exc).__name__}: {exc}"})
        else:
            row.update({"available": None, "detail": "not probed"})
        rows.append(row)
    return rows


__all__ = ["Provider", "FetchResult", "build_providers", "providers_for",
           "provider_health", "BinanceSpot", "BinanceFutures", "KrakenProvider",
           "DeltaIndia", "YahooGoldProvider", "TwelveDataXauUsd", "DhanMcxGold"]
