"""
orchestrator/agents/delta_exchange.py — Delta Exchange India public data.

READ-ONLY BY CONSTRUCTION
    This module speaks only to Delta's public market-data endpoints:
    products, tickers and historical candles. There is no order, position,
    balance or account-mutation method here, and no private key is required
    for any call. The trading stack uses it for quotes/candles and for
    *paper* fills; live execution is a separate, explicitly armed path in
    trading/delta_private.py + broker.py.

Environment
    DELTA_BASE_URL   default https://api.india.delta.exchange (India prod)
    DELTA_API_KEY    optional; presence is reported, value is never logged
    DELTA_API_SECRET optional; presence is reported, value is never logged

    When the env pair is absent, the same fields stored by the RIO Settings
    page in config/api_keys.json (delta_api_key / delta_api_secret) are used.
    Env always wins, and no call here ever signs a request.

Notes learned from the live API (2026-10):
    * /v2/products paginates with meta.after; page_size max is 100.
    * /v2/history/candles returns newest-first rows and can return ~4000
      candles per response even though the docs say 2000. We therefore page
      backwards by the oldest returned timestamp instead of assuming a cap.
    * funding history is served as candles for symbol FUNDING:<symbol>.
      The values are percent (0.01 == 0.01%), so they are divided by 100
      before they reach the backtest engine, which expects a decimal rate.
    * ticker fields are a mix of numbers and strings; every value is parsed
      defensively and a missing field stays missing.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

import requests

DEFAULT_BASE_URL = "https://api.india.delta.exchange"
PRODUCT_STATES = ("live",)
CONTRACT_TYPES = ("perpetual_futures", "spot")
TIMEFRAMES = ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d")
_PRODUCT_TTL_S = 60.0
_CANDLE_PAGE_GUESS = 4000          # observed; never used as a hard contract
_MAX_CANDLE_REQUESTS = 800
_USER_AGENT = "JARVIS-Delta/1.0 (public market data; read-only)"


class DeltaError(RuntimeError):
    """A public-data request failed. The message is safe to show."""


def resolve_credentials() -> tuple[str, str]:
    """Effective Delta API key + secret; environment first, then config.

    Delta's public endpoints are unsigned, so these are informational for the
    read-only integration. The env pair wins so an explicit deployment export
    (systemd EnvironmentFile, shell profile) is never overridden by a stale
    value in config/api_keys.json — which is where the Settings page stores
    them for launches that export nothing.
    """
    env_key = os.environ.get("DELTA_API_KEY", "").strip()
    env_secret = os.environ.get("DELTA_API_SECRET", "").strip()
    if env_key and env_secret:
        return env_key, env_secret
    try:
        from memory.config_manager import (
            get_delta_api_key, get_delta_api_secret,
        )
        file_key = get_delta_api_key()
        file_secret = get_delta_api_secret()
    except Exception:
        file_key = file_secret = ""
    return env_key or file_key, env_secret or file_secret


def credentials_source() -> str:
    """"env", "settings" or "none" — where the effective pair came from."""
    key, secret = resolve_credentials()
    if not (key and secret):
        return "none"
    if (os.environ.get("DELTA_API_KEY", "").strip()
            and os.environ.get("DELTA_API_SECRET", "").strip()):
        return "env"
    return "settings"


class DeltaNotFound(DeltaError):
    pass


class DeltaAmbiguous(DeltaError):
    def __init__(self, query: str, candidates: list["DeltaProduct"]):
        self.query = query
        self.candidates = candidates
        names = ", ".join(p.symbol for p in candidates[:12])
        super().__init__(f"'{query}' matches more than one Delta product: {names}")


def _f(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def _i(value: Any) -> int | None:
    v = _f(value)
    return int(v) if v is not None else None


def _s(value: Any) -> str:
    return str(value or "").strip()


@dataclass(frozen=True)
class DeltaProduct:
    """One live Delta product, normalised enough for the lab and the UI."""

    symbol: str
    product_id: int | None
    contract_type: str
    underlying: str
    quote_currency: str
    description: str
    state: str
    trading_status: str
    tick_size: float | None
    contract_value: float | None
    default_leverage: float | None
    rate_exchange_interval: int | None
    raw: dict = field(default_factory=dict, compare=False, repr=False)

    @property
    def market_class(self) -> str:
        return ("delta_perp" if self.contract_type == "perpetual_futures"
                else "delta_spot" if self.contract_type == "spot" else "")

    @property
    def market(self) -> str:
        return ("perp" if self.contract_type == "perpetual_futures"
                else "spot" if self.contract_type == "spot" else "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "product_id": self.product_id,
            "contract_type": self.contract_type,
            "market": self.market,
            "market_class": self.market_class,
            "underlying": self.underlying,
            "quote_currency": self.quote_currency,
            "description": self.description,
            "state": self.state,
            "trading_status": self.trading_status,
            "tick_size": self.tick_size,
            "contract_value": self.contract_value,
            "default_leverage": self.default_leverage,
            "rate_exchange_interval": self.rate_exchange_interval,
        }


def _parse_product(raw: dict) -> DeltaProduct | None:
    symbol = _s(raw.get("symbol"))
    if not symbol:
        return None
    underlying = _s((raw.get("underlying_asset") or {}).get("symbol"))
    quote = _s((raw.get("quoting_asset") or {}).get("symbol"))
    specs = raw.get("product_specs") or {}
    return DeltaProduct(
        symbol=symbol,
        product_id=_i(raw.get("id")),
        contract_type=_s(raw.get("contract_type")),
        underlying=underlying,
        quote_currency=quote,
        description=_s(raw.get("description") or raw.get("short_description")),
        state=_s(raw.get("state")),
        trading_status=_s(raw.get("trading_status")),
        tick_size=_f(raw.get("tick_size")),
        contract_value=_f(raw.get("contract_value")),
        default_leverage=_f(raw.get("default_leverage")),
        rate_exchange_interval=_i(specs.get("rate_exchange_interval")),
        raw=raw,
    )


@dataclass
class CandleResult:
    ok: bool
    rows: list[dict[str, Any]] = field(default_factory=list)
    requests_made: int = 0
    truncated: bool = False
    error: str = ""

    @property
    def count(self) -> int:
        return len(self.rows)


class DeltaClient:
    """Public Delta Exchange India client with a short-lived product cache."""

    def __init__(self, *, base_url: str | None = None,
                 timeout: float = 20.0, product_ttl_s: float = _PRODUCT_TTL_S
                 ) -> None:
        self.base_url = (base_url or os.environ.get("DELTA_BASE_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = float(timeout)
        self.product_ttl_s = float(product_ttl_s)
        self._lock = threading.RLock()
        self._products: list[DeltaProduct] = []
        self._products_at = 0.0
        self._products_error = ""
        self.last_error = ""

    # ── env / status ─────────────────────────────────────────────────────
    @staticmethod
    def credentials_configured() -> bool:
        key, secret = resolve_credentials()
        return bool(key and secret)

    @property
    def configured(self) -> bool:
        return self.credentials_configured()

    # ── HTTP ─────────────────────────────────────────────────────────────
    def _get(self, path: str, params: dict | None = None,
             *, timeout: float | None = None) -> Any:
        url = f"{self.base_url}{path}"
        try:
            resp = requests.get(url, params=params or {},
                                headers={"User-Agent": _USER_AGENT,
                                         "Accept": "application/json"},
                                timeout=timeout or self.timeout)
        except Exception as exc:
            raise DeltaError(f"Delta request failed: "
                             f"{type(exc).__name__}: {str(exc)[:160]}") from exc
        if resp.status_code == 429:
            raise DeltaError("Delta rate limited (HTTP 429)")
        if resp.status_code >= 400:
            body = (resp.text or "")[:180]
            raise DeltaError(f"Delta HTTP {resp.status_code}: {body}")
        try:
            payload = resp.json()
        except Exception as exc:
            raise DeltaError(f"Delta returned non-JSON: {str(exc)[:120]}") from exc
        if isinstance(payload, dict) and payload.get("success") is False:
            err = payload.get("error") or {}
            raise DeltaError(f"Delta error: {str(err)[:220]}")
        return payload

    # ── products ─────────────────────────────────────────────────────────
    def products(self, *, force: bool = False,
                 contract_types: Iterable[str] = CONTRACT_TYPES
                 ) -> list[DeltaProduct]:
        """Every live perpetual/spot product, cached for ~60s."""
        with self._lock:
            fresh = (not force and self._products
                     and time.time() - self._products_at < self.product_ttl_s)
            if fresh:
                return list(self._products)
            try:
                rows = self._fetch_products(contract_types=contract_types)
            except Exception as exc:
                self._products_error = str(exc)[:240]
                self.last_error = self._products_error
                raise
            self._products = rows
            self._products_at = time.time()
            self._products_error = ""
            self.last_error = ""
            return list(rows)

    def _fetch_products(self, *, contract_types: Iterable[str]
                        ) -> list[DeltaProduct]:
        out: list[DeltaProduct] = []
        after = ""
        seen = set()
        for _ in range(20):
            params = {
                "states": "live",
                "contract_types": ",".join(contract_types),
                "page_size": 100,
            }
            if after:
                params["after"] = after
            payload = self._get("/v2/products", params)
            rows = payload.get("result") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                raise DeltaError("Delta /v2/products returned no result list")
            for raw in rows:
                if not isinstance(raw, dict):
                    continue
                p = _parse_product(raw)
                if p and p.symbol not in seen:
                    seen.add(p.symbol)
                    out.append(p)
            meta = payload.get("meta") or {}
            after = _s(meta.get("after"))
            if not after:
                break
        return out

    def cached_product(self, symbol: str) -> DeltaProduct | None:
        """Exact product from the last product fetch, without any network I/O.

        Used by the trading facade to detect an already-cached Delta symbol
        without turning every unrelated quote into a Delta catalog request.
        """
        want = _s(symbol).upper()
        if not want:
            return None
        with self._lock:
            rows = list(self._products)
        for p in rows:
            if p.symbol.upper() == want:
                return p
        return None

    def product(self, symbol: str, *, live_only: bool = True
                ) -> DeltaProduct | None:
        want = _s(symbol).upper()
        if not want:
            return None
        # A None return is normally "not a live product", but a venue outage
        # must not be misreported as a delisting. last_error carries that
        # distinction to the action/CLI without inventing a symbol.
        self.last_error = ""
        cached = self.cached_product(want)
        if cached is not None:
            if live_only and (cached.state != "live"
                              or cached.trading_status not in ("", "operational")):
                return None
            return cached
        try:
            for p in self.products():
                if p.symbol.upper() == want:
                    if live_only and (p.state != "live"
                                      or p.trading_status not in ("", "operational")):
                        return None
                    self.last_error = ""
                    return p
        except DeltaError as exc:
            # fall through to the exact product endpoint; a cache failure
            # must not make an exact symbol unresolvable.
            self.last_error = str(exc)[:240]
        try:
            payload = self._get(f"/v2/products/{want}")
        except DeltaError as exc:
            # A 404 is a real "no such product", not a venue outage.
            if "HTTP 404" not in str(exc):
                self.last_error = str(exc)[:240]
            return None
        raw = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(raw, dict):
            return None
        p = _parse_product(raw)
        if not p:
            return None
        if live_only and (p.state != "live"
                          or p.trading_status not in ("", "operational")):
            return None
        self.last_error = ""
        return p

    _ALIASES = {
        "BTC": ("BTC", "BITCOIN", "XBT"),
        "ETH": ("ETH", "ETHEREUM", "ETHER"),
        "SOL": ("SOL", "SOLANA"),
        "XRP": ("XRP", "RIPPLE"),
        "XAUT": ("XAUT", "TETHER GOLD"),
        "PAXG": ("PAXG", "PAX GOLD"),
        "DOGE": ("DOGE", "DOGECOIN"),
        "ADA": ("ADA", "CARDANO"),
    }

    def find_products(self, query: str, *,
                      contract_type: str | None = None,
                      limit: int = 60) -> list[DeltaProduct]:
        """Find live products by exact symbol, base asset, or description.

        Ambiguity is returned, never guessed. The caller decides whether to
        ask the user or pick an explicitly requested market.
        """
        q = _s(query).upper()
        if not q:
            return []
        # Requests often arrive as "delta BTCUSD" or "Delta Exchange BTC".
        # Strip the venue words before matching, but keep the original for
        # the ambiguity message.
        words = q.replace("/", " ").replace("-", " ").split()
        words = [w for w in words if w not in
                 ("DELTA", "EXCHANGE", "INDIA", "PERP", "PERPETUAL",
                  "PERPS", "SPOT", "FUTURES", "BACKTEST", "TEST",
                  "RESEARCH", "RUN", "ON", "THE")]
        core = " ".join(words).strip()
        rows = self.products()
        if contract_type:
            rows = [p for p in rows if p.contract_type == contract_type]

        for candidate in (q, core):
            if not candidate:
                continue
            exact = [p for p in rows if p.symbol.upper() == candidate]
            if exact:
                return exact[:limit]

        tokens = set()
        for raw in core.replace("_", " ").split():
            if len(raw) >= 2:
                tokens.add(raw)
        # A base asset (BTC / BITCOIN) selects only that underlying's
        # products. This avoids "bitcoin" also matching "Bitcoin Cash".
        alias_base = ""
        for base, aliases in self._ALIASES.items():
            if core in aliases:
                alias_base = base
                break
        if alias_base:
            rows = [p for p in rows if p.underlying.upper() == alias_base]
            tokens.add(alias_base)

        candidates: list[DeltaProduct] = []
        for p in rows:
            base = p.underlying.upper()
            names = set(self._ALIASES.get(base, (base,)))
            if p.symbol.upper() in tokens:
                candidates.append(p)
                continue
            if base and (base in tokens or names & tokens):
                candidates.append(p)
                continue
            # Exact token inside the description is a last-resort text match.
            desc = p.description.upper()
            if any(tok in desc for tok in tokens if len(tok) >= 3):
                candidates.append(p)
        # Stable, useful ordering: major crypto, perps before spot, then name.
        major = {"BTC": 0, "ETH": 1, "SOL": 2, "XRP": 3, "XAUT": 4, "PAXG": 5}
        candidates.sort(key=lambda p: (
            major.get(p.underlying.upper(), 99),
            0 if p.contract_type == "perpetual_futures" else 1,
            p.symbol,
        ))
        return candidates[:limit]

    def resolve_product(self, query: str, *,
                        contract_type: str | None = None) -> DeltaProduct:
        """Exactly one product or an explicit ambiguity/not-found error."""
        rows = self.find_products(query, contract_type=contract_type)
        if not rows:
            want = f" ({contract_type})" if contract_type else ""
            raise DeltaNotFound(f"no live Delta product matches '{query}'{want}")
        exact = [p for p in rows if p.symbol.upper() == _s(query).upper()]
        if len(exact) == 1:
            return exact[0]
        if len(rows) == 1:
            return rows[0]
        raise DeltaAmbiguous(query, rows)

    # ── ticker ───────────────────────────────────────────────────────────
    def ticker(self, symbol: str) -> dict[str, Any]:
        sym = _s(symbol).upper()
        if not sym:
            raise DeltaError("ticker needs a symbol")
        payload = self._get(f"/v2/tickers/{sym}")
        row = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(row, dict) or not row:
            raise DeltaError(f"Delta returned no ticker for {sym}")
        return row

    # ── candles ──────────────────────────────────────────────────────────
    def candles(self, symbol: str, resolution: str, start: int | float,
                end: int | float, *, max_candles: int = 600_000
                ) -> CandleResult:
        """Completed OHLC candles, oldest-first, paginated backwards.

        Delta returns the NEWEST window for a request. We therefore move the
        request's `end` to (oldest returned timestamp - 1) until the requested
        start is reached. No bar is invented, filled or smoothed.
        """
        sym = _s(symbol).upper()
        res = _s(resolution).lower()
        if not sym:
            return CandleResult(False, error="candles need a symbol")
        if res not in TIMEFRAMES:
            return CandleResult(False, error=f"unsupported resolution '{res}'")
        try:
            start_i = int(float(start))
            end_i = int(float(end))
        except (TypeError, ValueError):
            return CandleResult(False, error="start/end must be Unix seconds")
        if end_i <= start_i:
            return CandleResult(False, error="end must be after start")

        out: dict[int, dict[str, Any]] = {}
        cursor_end = end_i
        requests_made = 0
        truncated = False
        error = ""
        for _ in range(_MAX_CANDLE_REQUESTS):
            if cursor_end <= start_i or len(out) >= max_candles:
                break
            try:
                payload = self._get("/v2/history/candles", {
                    "resolution": res,
                    "symbol": sym,
                    "start": start_i,
                    "end": cursor_end,
                })
            except DeltaError as exc:
                error = str(exc)
                break
            rows = payload.get("result") if isinstance(payload, dict) else None
            if not isinstance(rows, list) or not rows:
                break
            requests_made += 1
            oldest = None
            for raw in rows:
                if not isinstance(raw, dict):
                    continue
                ts = _i(raw.get("time"))
                if ts is None:
                    continue
                oldest = ts if oldest is None else min(oldest, ts)
                o, h, l, c = (_f(raw.get(k)) for k in
                              ("open", "high", "low", "close"))
                if None in (o, h, l, c):
                    continue
                out[ts] = {"time": ts, "open": o, "high": h, "low": l,
                           "close": c, "volume": _f(raw.get("volume")) or 0.0}
            if oldest is None:
                break
            if oldest <= start_i:
                break
            # If the venue ignored `end`, paging would loop forever.
            if oldest >= cursor_end:
                error = ("Delta candle pagination did not advance "
                         f"(oldest {oldest} >= cursor {cursor_end})")
                break
            cursor_end = oldest - 1
            if len(rows) < 2:
                break
            time.sleep(0.12)

        rows_out = [out[k] for k in sorted(out)]
        if max_candles and len(rows_out) > max_candles:
            rows_out = rows_out[-max_candles:]
            truncated = True
        return CandleResult(True, rows=rows_out, requests_made=requests_made,
                            truncated=truncated, error=error)

    # ── funding ──────────────────────────────────────────────────────────
    def funding_history(self, symbol: str, start: int | float,
                        end: int | float) -> tuple[list[dict[str, Any]], str]:
        """Funding events as [{time, rate}] with rate as a decimal.

        Delta serves FUNDING:<symbol> as candles. The close is a percent
        value (0.01 == 0.01%), so it is divided by 100. Events are sampled
        at the product's `rate_exchange_interval`; the 1h source series
        repeats the same rate between events, which is why we filter to
        interval boundaries instead of summing every hour.
        """
        sym = _s(symbol).upper()
        if not sym:
            return [], "funding needs a symbol"
        product = self.product(sym)
        interval = (product.rate_exchange_interval
                    if product and product.rate_exchange_interval
                    else 28800)
        interval = max(60, int(interval))
        source_tf = "1h" if interval >= 3600 else "1m"
        result = self.candles(f"FUNDING:{sym}", source_tf, start, end,
                              max_candles=200_000)
        if not result.ok and not result.rows:
            return [], result.error or "funding candles unavailable"
        events = []
        for row in result.rows:
            ts = int(row["time"])
            if ts % interval:
                continue
            rate = _f(row.get("close"))
            if rate is None:
                continue
            events.append({"time": ts, "rate": rate / 100.0})
        return events, result.error

    # ── health ───────────────────────────────────────────────────────────
    def status(self) -> dict[str, Any]:
        """Cheap public-data health probe. Never returns a secret."""
        checked = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            rows = self.products(force=False)
            perps = sum(1 for p in rows
                        if p.contract_type == "perpetual_futures")
            spot = sum(1 for p in rows if p.contract_type == "spot")
            ticker_ok = False
            ticker_detail = ""
            try:
                t = self.ticker("BTCUSD")
                ticker_ok = _f(t.get("close")) is not None
                ticker_detail = (f"BTCUSD last={_f(t.get('close'))}"
                                 if ticker_ok else "ticker served no close")
            except DeltaError as exc:
                ticker_detail = str(exc)[:180]
            return {
                "ok": True,
                "status": "CONNECTED" if ticker_ok else "PUBLIC DATA ONLY",
                "base_url": self.base_url,
                "credentials_configured": self.credentials_configured(),
                "products": len(rows),
                "perpetuals": perps,
                "spot": spot,
                "ticker_ok": ticker_ok,
                "ticker_detail": ticker_detail,
                "products_error": self._products_error,
                "checked_at": checked,
                "mode": "public data + paper only",
                "read_only": True,
            }
        except Exception as exc:
            return {
                "ok": False,
                "status": "DISCONNECTED",
                "base_url": self.base_url,
                "credentials_configured": self.credentials_configured(),
                "products": 0,
                "perpetuals": 0,
                "spot": 0,
                "ticker_ok": False,
                "ticker_detail": "",
                "products_error": f"{type(exc).__name__}: {str(exc)[:220]}",
                "checked_at": checked,
                "mode": "public data + paper only",
                "read_only": True,
            }


# ── shared default client (cheap for the dashboard and Hermes CLI) ───────────

_default_client: DeltaClient | None = None
_default_lock = threading.Lock()


def get_client() -> DeltaClient:
    global _default_client
    with _default_lock:
        if _default_client is None:
            _default_client = DeltaClient()
        return _default_client


def reset_client() -> None:
    """Test helper: drop the cached client (and its product cache)."""
    global _default_client
    with _default_lock:
        _default_client = None


def products_payload(*, client: DeltaClient | None = None,
                     limit: int = 500) -> dict[str, Any]:
    """Catalog payload shared by the Backtest room, Trading page and CLI."""
    c = client or get_client()
    try:
        rows = c.products()
    except Exception as exc:
        return {"ok": False, "status": "DISCONNECTED",
                "base_url": c.base_url,
                "error": f"{type(exc).__name__}: {str(exc)[:220]}",
                "products": []}
    rows = sorted(rows, key=lambda p: (
        {"BTC": 0, "ETH": 1, "SOL": 2, "XRP": 3, "XAUT": 4, "PAXG": 5}
        .get(p.underlying.upper(), 99),
        0 if p.contract_type == "perpetual_futures" else 1,
        p.symbol,
    ))
    return {
        "ok": True,
        "status": "CONNECTED",
        "base_url": c.base_url,
        "credentials_configured": c.credentials_configured(),
        "count": len(rows),
        "products": [p.to_dict() for p in rows[:limit]],
        "truncated": len(rows) > limit,
    }


__all__ = [
    "DEFAULT_BASE_URL", "CONTRACT_TYPES", "TIMEFRAMES", "DeltaClient",
    "DeltaProduct", "DeltaError", "DeltaNotFound", "DeltaAmbiguous",
    "CandleResult", "get_client", "reset_client", "products_payload",
    "resolve_credentials", "credentials_source",
]
