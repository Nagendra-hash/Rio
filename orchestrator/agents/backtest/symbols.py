"""
orchestrator/agents/backtest/symbols.py — symbol → instrument resolution.

ONE symbol in, one explicit instrument out. Ambiguity is never resolved by
guessing: every crypto instrument names its exchange, quote currency and
market type (spot vs perpetual), and gold is either XAUUSD spot, COMEX
front-month futures or a configured MCX contract — never silently swapped.

Gold: if the user has not saved a preference, resolution returns NEEDS_INPUT
with the minimum viable question. The answer is saved once (config.gold_market)
and every later request uses it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping

from orchestrator.agents.delta_exchange import (
    DeltaAmbiguous, DeltaClient, DeltaError, DeltaNotFound, DeltaProduct,
)

GOLD_OPTIONS = ("futures", "xauusd", "mcx")
DELTA_MARKETS = ("delta_perp", "delta_spot")
_DELTA_WORD = re.compile(r"\bdelta\b", re.IGNORECASE)

_ALIASES = {
    "BTC": "BTC", "BITCOIN": "BTC", "XBT": "BTC", "BTCUSD": "BTC",
    "BTCUSDT": "BTC", "BTC-USD": "BTC", "BTCUSDC": "BTC",
    "ETH": "ETH", "ETHEREUM": "ETH", "ETHER": "ETH", "ETHUSD": "ETH",
    "ETHUSDT": "ETH", "ETH-USD": "ETH",
    "SOL": "SOL", "SOLANA": "SOL", "SOLUSD": "SOL", "SOLUSDT": "SOL",
    "SOL-USD": "SOL",
    "GOLD": "GOLD", "XAU": "GOLD", "XAUUSD": "GOLD", "GC": "GOLD",
    "GOLD=F": "GOLD", "GC=F": "GOLD",
}

_NAMES = {"BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana",
          "GOLD": "Gold"}

_CRYPTO_BINANCE = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}
_CRYPTO_KRAKEN = {"BTC": "XXBTZUSD", "ETH": "XETHZUSD", "SOL": "SOLUSD"}


@dataclass(frozen=True)
class Instrument:
    """A fully specified research instrument."""

    symbol: str                 # canonical: BTC / ETH / SOL / GOLD
    name: str
    asset_class: str            # crypto | gold
    market: str                 # spot | perp | xauusd | futures | mcx
    market_class: str           # crypto_spot | crypto_perp | gold_*
    exchange: str
    instrument_type: str
    quote_currency: str
    provider_symbols: Mapping[str, str] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.symbol}:{self.market}"

    @property
    def label(self) -> str:
        return f"{self.symbol} ({self.instrument_type} on {self.exchange})"

    def describe(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "asset_class": self.asset_class,
            "market": self.market,
            "market_class": self.market_class,
            "exchange": self.exchange,
            "instrument_type": self.instrument_type,
            "quote_currency": self.quote_currency,
            "provider_symbols": dict(self.provider_symbols),
            "notes": list(self.notes),
        }


@dataclass
class Resolution:
    ok: bool
    instrument: Instrument | None = None
    needs_input: bool = False
    question: str = ""
    options: tuple[str, ...] = ()
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "needs_input": self.needs_input,
            "question": self.question,
            "options": list(self.options),
            "error": self.error,
            "instrument": self.instrument.describe() if self.instrument else None,
        }


_SYMBOL_RE = re.compile(
    r"\b(BTC|BITCOIN|XBT|ETH|ETHEREUM|ETHER|SOL|SOLANA|GOLD|XAUUSD|XAU|GC=F)\b",
    re.IGNORECASE)
_MARKET_HINT = {
    "spot": re.compile(r"\b(?:spot|cash)\b", re.IGNORECASE),
    "perp": re.compile(r"\b(?:perp(?:etual)?|futures?\s+perp|\bperps?\b)", re.IGNORECASE),
    "futures": re.compile(r"\b(?:futures?|comex|gc=f|continuous)\b", re.IGNORECASE),
    "xauusd": re.compile(r"\bxau\s*/?\s*usd\b|\bxauusd\b|\bspot\s+gold\b", re.IGNORECASE),
    "mcx": re.compile(r"\bmcx\b|\bmulti\s+commodity\s+exchange\b", re.IGNORECASE),
}


def extract_symbol(text: str) -> str:
    """The canonical symbol named in a request, or ''."""
    m = _SYMBOL_RE.search(str(text or ""))
    if not m:
        return ""
    return _ALIASES.get(m.group(1).upper().replace(" ", ""), "")


def market_hint(text: str) -> str:
    """Explicit market wording in the request, or ''."""
    s = str(text or "")
    for market in ("perp", "mcx", "xauusd", "futures", "spot"):
        if _MARKET_HINT[market].search(s):
            return market
    return ""


def _crypto(symbol: str, market: str) -> Instrument:
    base = _NAMES[symbol]
    if market == "perp":
        return Instrument(
            symbol=symbol, name=base, asset_class="crypto", market="perp",
            market_class="crypto_perp", exchange="Binance USDⓈ-M Futures",
            instrument_type=f"{symbol}USDT perpetual future",
            quote_currency="USDT",
            provider_symbols={"binance_futures": _CRYPTO_BINANCE[symbol]},
            notes=("Perpetual futures: funding costs apply; leverage capped by "
                   "config; not the same instrument as spot.",
                   "Liquidation modelling is intentionally out of scope: the "
                   "engine sizes positions at or below the configured leverage "
                   "and uses a protective stop."),
        )
    return Instrument(
        symbol=symbol, name=base, asset_class="crypto", market="spot",
        market_class="crypto_spot", exchange="Binance Spot",
        instrument_type=f"{symbol}USDT spot pair", quote_currency="USDT",
        provider_symbols={"binance_spot": _CRYPTO_BINANCE[symbol],
                          "kraken_spot": _CRYPTO_KRAKEN[symbol]},
        notes=("Spot pair, 24/7 market, quote currency USDT. Kraken quotes USD; "
               "the two venues are separate datasets and are never averaged.",
               "Second venue (Kraken) is used only when Binance is unavailable "
               "or fails the coverage gate."),
    )


def has_delta_hint(text: str) -> bool:
    return bool(_DELTA_WORD.search(str(text or "")))


def _delta_instrument(product: DeltaProduct) -> Instrument:
    market = product.market or "spot"
    quote = product.quote_currency or "?"
    notes = [
        f"Delta Exchange India product {product.symbol} "
        f"({product.contract_type}); quote currency {quote}. "
        "This is a separate venue and is never averaged with Binance, "
        "Kraken or Yahoo.",
        "Public exchange candles only: no account, order or position data "
        "is read by the backtest lab.",
    ]
    if product.contract_value:
        notes.append(
            f"Contract value {product.contract_value:g} "
            f"{product.underlying or 'units'} per contract; results size "
            "underlying units, not exchange lots.")
    if market == "perp":
        interval = product.rate_exchange_interval or 28800
        notes.append(
            f"Perpetual funding is exchanged every {interval}s; funding "
            "history is used when Delta serves it.")
    return Instrument(
        symbol=product.symbol,
        name=product.underlying or product.symbol,
        asset_class="crypto",
        market=market,
        market_class=product.market_class,
        exchange="Delta Exchange India",
        instrument_type=(product.description
                         or f"{product.symbol} {product.contract_type}"),
        quote_currency=quote,
        provider_symbols={"delta_india": product.symbol},
        notes=tuple(notes),
    )


def resolve_delta(text: str, market: str = "", *,
                  cfg: Any = None,
                  client: DeltaClient | None = None) -> Resolution:
    """Resolve a Delta product by exact symbol, base asset or description.

    The exact live product is always returned. When a base asset matches more
    than one live product (e.g. BTC -> BTCUSD perp and BTC_INR spot), the
    answer is NEEDS_INPUT with the real choices — never a silent guess.
    """
    chosen = str(market or "").strip().lower()
    if chosen in ("perp", "perpetual"):
        chosen = "delta_perp"
    elif chosen == "spot":
        chosen = "delta_spot"
    if chosen not in ("", "auto", *DELTA_MARKETS):
        return Resolution(
            ok=False,
            error=f"'{market}' is not a Delta market. Use delta_perp or "
                  "delta_spot (or omit it and name an exact product).")
    contract_type = ("perpetual_futures" if chosen == "delta_perp"
                     else "spot" if chosen == "delta_spot" else None)
    query = str(text or "").strip()
    if not query:
        return Resolution(
            ok=False, needs_input=True,
            question=("Which Delta Exchange India product should I research? "
                      "Name an exact symbol (e.g. BTCUSD, ETHUSD, BTC_INR) or "
                      "a base asset plus a market."),
            options=("BTCUSD delta_perp", "ETHUSD delta_perp",
                     "BTC_INR delta_spot"),
            error="Delta product not specified")
    c = client or DeltaClient()
    try:
        product = c.resolve_product(query, contract_type=contract_type)
    except DeltaAmbiguous as exc:
        options = tuple(p.symbol for p in exc.candidates[:24])
        return Resolution(
            ok=False, needs_input=True,
            question=(f"'{query}' matches more than one Delta product. "
                      "Which exact product should I use?"),
            options=options,
            error=str(exc))
    except DeltaNotFound as exc:
        examples = ("BTCUSD", "ETHUSD", "SOLUSD", "BTC_INR")
        return Resolution(
            ok=False,
            error=(f"{exc}. Delta products are discovered live from "
                   f"{c.base_url}/v2/products; examples: "
                   + ", ".join(examples)))
    except DeltaError as exc:
        return Resolution(
            ok=False,
            error=f"Delta product discovery failed: {str(exc)[:240]}")
    return Resolution(ok=True, instrument=_delta_instrument(product))


def _gold(market: str, cfg: Any) -> Instrument | Resolution:
    if market == "xauusd":
        return Instrument(
            symbol="GOLD", name="Gold", asset_class="gold", market="xauusd",
            market_class="gold_xauusd", exchange="OTC spot (provider-quoted)",
            instrument_type="XAUUSD spot", quote_currency="USD",
            provider_symbols={"twelvedata_xauusd": "XAU/USD",
                              "yahoo_xauusd": "XAUUSD=X"},
            notes=("XAUUSD spot is not exchange-traded; the provider's quote is "
                   "a benchmark/OTC feed. Requires Twelve Data credentials when "
                   "configured; Yahoo does not serve XAUUSD=X.",
                   "Session-aware: gold trades ~23h/day Sun 22:00 UTC – Fri "
                   "21:00 UTC with a daily 1h break; gaps are reported, never "
                   "filled."),
        )
    if market == "futures":
        return Instrument(
            symbol="GOLD", name="Gold", asset_class="gold", market="futures",
            market_class="gold_futures",
            exchange="COMEX (continuous front-month via Yahoo Finance)",
            instrument_type="Gold futures continuous front-month (GC=F)",
            quote_currency="USD",
            provider_symbols={"yahoo_gc": "GC=F"},
            notes=("GC=F is a CONTINUOUS front-month series, not one tradable "
                   "contract: contract rolls are not modelled as cash flows, so "
                   "results are research evidence, not an executable P&L.",
                   "Yahoo intraday depth is limited (1h ≈ 730d, 15m/5m ≈ 60d, "
                   "1m ≈ 7d); intervals below the one-year floor are rejected."),
        )
    if market == "mcx":
        contract = (cfg.get("mcx_gold_contract") if cfg else None) or {}
        security_id = str(contract.get("security_id") or "").strip()
        expiry = str(contract.get("expiry") or "").strip()
        if not security_id or not expiry:
            return Resolution(
                ok=False, needs_input=True,
                question=("MCX gold needs an explicit contract — I will not guess "
                          "an expiry. Reply with the contract you trade, or set "
                          "mcx_gold_contract (security_id, expiry, lot_size) in "
                          "memory/backtest/config.json."),
                options=("configure mcx_gold_contract",),
                error="MCX contract not configured")
        try:
            expiry_date = date.fromisoformat(expiry)
        except ValueError:
            return Resolution(ok=False, error=f"invalid MCX expiry '{expiry}'")
        if expiry_date <= date.today():
            return Resolution(ok=False,
                              error=f"configured MCX contract expired on {expiry}")
        return Instrument(
            symbol="GOLD", name="Gold", asset_class="gold", market="mcx",
            market_class="gold_mcx", exchange="MCX (Dhan)",
            instrument_type=f"MCX gold futures {expiry}",
            quote_currency="INR",
            provider_symbols={"dhan_mcx": security_id},
            notes=(f"Contract expiry {expiry} comes from configuration, not a "
                   "guess. Lot size and tick size are applied where configured.",
                   "Requires Dhan Data API credentials with historical-data "
                   "permission; MCX prices are INR-denominated."),
        )
    return Resolution(ok=False, error=f"unknown gold market '{market}'")


def resolve(text: str, *, market: str | None = None,
            cfg: Any = None, allow_gold_default: bool = False,
            venue: str | None = None) -> Resolution:
    """Resolve a free-text request to exactly one instrument.

    `allow_gold_default` is used only by scheduled jobs that already ran with
    a saved preference; a user-facing request never silently picks gold.
    `venue="delta"` (or a delta_* market) routes to the live Delta product
    catalog instead of the Binance/Kraken/Yahoo instruments.
    """
    symbol = extract_symbol(text)
    hinted = market_hint(text)
    chosen = (market or hinted or "").strip().lower()
    venue_key = str(venue or "").strip().lower()
    if venue_key in ("delta_perp", "delta_spot") and not chosen:
        chosen = venue_key
    delta_requested = (venue_key in (
                           "delta", "delta_india", "delta_perp", "delta_spot")
                       or chosen in DELTA_MARKETS
                       or has_delta_hint(text))
    if delta_requested:
        return resolve_delta(text, chosen, cfg=cfg)

    if not symbol:
        return Resolution(
            ok=False,
            error=("I could not find an instrument in that request. Name one of "
                   "BTC, ETH, SOL, GOLD, or a Delta product such as BTCUSD."))

    if symbol != "GOLD":
        if chosen in ("", "auto"):
            chosen = str(cfg.get("default_crypto_market", "spot")
                         if cfg else "spot").lower()
        if chosen in ("futures", "xauusd", "mcx"):
            return Resolution(
                ok=False,
                error=f"{symbol} is a crypto instrument; '{chosen}' is a gold "
                      f"market. Use spot or perp.")
        if chosen not in ("spot", "perp"):
            return Resolution(ok=False, error=f"unsupported crypto market '{chosen}'")
        return Resolution(ok=True, instrument=_crypto(symbol, chosen))

    # Gold: saved preference > explicit request > ask once.
    # `gold_market()` returns None when unset; str(None) is the truthy string
    # "none", so normalise through a falsy check before stringifying.
    raw_saved = cfg.gold_market() if cfg else None
    saved = str(raw_saved).strip().lower() if raw_saved else ""
    if chosen in GOLD_OPTIONS:
        target = chosen
    elif saved in GOLD_OPTIONS:
        target = saved
    else:
        return Resolution(
            ok=False, needs_input=True,
            question=("Gold needs one clarification (asked once, then saved): "
                      "which instrument do you trade — XAUUSD spot, COMEX gold "
                      "futures, or MCX gold futures?"),
            options=("GOLD xauusd", "GOLD futures", "GOLD mcx"),
            error="gold market not configured")

    inst = _gold(target, cfg)
    if isinstance(inst, Resolution):
        return inst
    return Resolution(ok=True, instrument=inst)


def set_gold_preference(cfg: Any, market: str) -> dict[str, Any]:
    return cfg.set_gold_market(market)
