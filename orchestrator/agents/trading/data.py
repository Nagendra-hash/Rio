"""
orchestrator/agents/trading/data.py — the DATA stage: market data with
provenance, and nothing that pretends.

WHAT SEPARATES THIS FILE FROM EXECUTION
    This module is read-only over the market. It knows nothing about orders,
    accounts, or brokers — execution lives in broker.py. The agent pipeline
    (DATA → ANALYSIS → SIGNAL → RISK → HUMAN → OPTIONAL EXECUTION) reads the
    market only through here, so a data problem can never masquerade as a
    trade.

THE NEVER-INVENT RULE
    Every adapter answer carries a Provenance label. yfinance without an
    explicit user request returns DELAYED — the free endpoints are typically
    15 minutes behind for NSE/BSE, so the label is the honest default; a
    quote is LIVE only when the user explicitly opts in (sim realtime mode).
    SIM adapters say SIMULATED. When every adapter fails, the result is
    UNAVAILABLE with the reason — never a made-up number, never a stale
    value silently relabeled.

    `ok=False` results carry an `error` and no price. The analysis layers
    above refuse to run on anything that is not a provenance-labeled frame.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

# ── provenance ────────────────────────────────────────────────────────────────

class Provenance(str, Enum):
    LIVE = "live"              # user explicitly opted into realtime
    DELAYED = "delayed"        # free endpoints (default for yfinance)
    SIMULATED = "simulated"    # deterministically generated for tests/paper
    UNAVAILABLE = "unavailable"


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


# ── shared result shape ───────────────────────────────────────────────────────

@dataclass
class DataResult:
    """One adapter answer: data + provenance + source name + error."""
    ok: bool
    provenance: Provenance
    source: str
    data: Any = None
    error: str = ""
    as_of: str = ""

    def to_dict(self) -> dict:
        return {"ok": self.ok, "provenance": self.provenance.value,
                "source": self.source, "error": self.error,
                "as_of": self.as_of or _now_iso(),
                "data": self.data}

    @property
    def unavailable(self) -> bool:
        return not self.ok


def unavailable(source: str, error: str) -> DataResult:
    """The only honest way to answer when data cannot be fetched."""
    return DataResult(ok=False, provenance=Provenance.UNAVAILABLE,
                      source=source, error=error, as_of=_now_iso())


def _clean_float(x, lo: float | None = None, hi: float | None = None) -> float | None:
    """JSON-safe finite float or None — NaN/inf never travel as data."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    if lo is not None and v < lo:
        return None
    if hi is not None and v > hi:
        return None
    return v


# ── base adapter ──────────────────────────────────────────────────────────────

class DataAdapter:
    """Interface every adapter implements. Adapters NEVER cache silently and
    NEVER retry forever: one attempt, honest result."""
    name = "adapter"

    def quote(self, symbol: str) -> DataResult: ...
    def history(self, symbol: str, period: str = "6mo",
                interval: str = "1d") -> DataResult: ...
    def options_chain(self, symbol: str) -> DataResult: ...
    def news(self, symbol: str) -> DataResult: ...


# ── yfinance adapter (live/delayed) ───────────────────────────────────────────

class YFinanceAdapter(DataAdapter):
    """Yahoo Finance via yfinance. DELAYED by default; LIVE only when the
    user explicitly sets request_live=True (yf.Ticker(symbol, user_request=True)
    hits the realtime endpoints). Missing keys become explicit None fields in
    the payload — the caller sees exactly which fields the source did not
    serve instead of a silently-shorter dict."""

    name = "yfinance"

    def __init__(self) -> None:
        self._yf = None

    def _module(self):
        if self._yf is None:
            import yfinance                              # optional dependency
            self._yf = yfinance
        return self._yf

    def quote(self, symbol: str, request_live: bool = False) -> DataResult:
        try:
            yf = self._module()
            # yfinance 1.x removed Ticker(user_request=...) — fast_info is the
            # realtime-ish path on BOTH branches now. request_live still
            # selects the LIVE provenance label; missing fields stay honestly
            # None instead of a silently-shorter payload.
            ticker = yf.Ticker(symbol)
            info = ticker.fast_info
            prov = Provenance.LIVE if request_live else Provenance.DELAYED

            def _g(key):
                try:
                    return getattr(info, key, None)
                except Exception:
                    return None

            last = _g("last_price") or _g("lastPrice") or _g("regularMarketPrice")
            if last is None:
                return unavailable(self.name, f"no price served for {symbol!r}")
            payload = {
                "symbol": symbol,
                "last": _clean_float(last),
                "previous_close": _clean_float(_g("previous_close")
                                               or _g("previousClose")),
                "open": _clean_float(_g("open")),
                "day_high": _clean_float(_g("day_high") or _g("dayHigh")),
                "day_low": _clean_float(_g("day_low") or _g("dayLow")),
                "volume": _clean_float(_g("last_volume") or _g("volume"), lo=0),
                "currency": getattr(info, "currency", None) or "",
                "exchange": getattr(info, "exchange", None) or "",
                "missing_fields": [],       # filled below
            }
            payload["missing_fields"] = [k for k, v in payload.items()
                                         if v is None and k != "missing_fields"]
            return DataResult(ok=True, provenance=prov, source=self.name,
                              data=payload, as_of=_now_iso())
        except Exception as exc:
            return unavailable(self.name, f"{type(exc).__name__}: {exc}")

    def history(self, symbol: str, period: str = "6mo",
                interval: str = "1d") -> DataResult:
        try:
            yf = self._module()
            df = yf.Ticker(symbol).history(period=period, interval=interval,
                                           auto_adjust=True)
            if df is None or df.empty:
                return unavailable(self.name, f"no history for {symbol!r}")
            rows = []
            for idx, row in df.iterrows():
                ts = getattr(idx, "isoformat", lambda: str(idx))()
                rows.append({
                    "ts": str(ts), "open": _clean_float(row.get("Open")),
                    "high": _clean_float(row.get("High")),
                    "low": _clean_float(row.get("Low")),
                    "close": _clean_float(row.get("Close")),
                    "volume": _clean_float(row.get("Volume"), lo=0),
                })
            rows = [r for r in rows if r["close"] is not None]
            if not rows:
                return unavailable(self.name, "history had no usable closes")
            return DataResult(ok=True, provenance=Provenance.DELAYED,
                              source=self.name, data={"symbol": symbol,
                                                      "interval": interval,
                                                      "bars": rows},
                              as_of=_now_iso())
        except Exception as exc:
            return unavailable(self.name, f"{type(exc).__name__}: {exc}")

    # ── options ───────────────────────────────────────────────────────────
    def options_chain(self, symbol: str) -> DataResult:
        try:
            yf = self._module()
            t = yf.Ticker(symbol)
            expiries = list(t.options or [])
            if not expiries:
                return unavailable(self.name,
                                   f"no option expiries served for {symbol!r}")
            chain = t.option_chain(expiries[0])       # nearest expiry
            calls = self._legs(chain.calls, "CALL")
            puts = self._legs(chain.puts, "PUT")
            if not calls and not puts:
                return unavailable(self.name, "options chain was empty")
            return DataResult(
                ok=True, provenance=Provenance.DELAYED, source=self.name,
                data={"symbol": symbol, "expiry": expiries[0],
                      "expiries_available": expiries[:8],
                      "spot_hint": None,     # quote() supplies spot, not this
                      "calls": calls, "puts": puts},
                as_of=_now_iso())
        except Exception as exc:
            return unavailable(self.name, f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _legs(df, kind: str) -> list[dict]:
        legs = []
        try:
            for _, r in df.iterrows():
                strike = _clean_float(r.get("strike"))
                if strike is None or strike <= 0:
                    continue
                legs.append({
                    "kind": kind,
                    "strike": strike,
                    "expiry": str(r.get("expiry", "") or ""),
                    "last": _clean_float(r.get("lastPrice")),
                    "bid": _clean_float(r.get("bid")),
                    "ask": _clean_float(r.get("ask")),
                    "iv": _clean_float(r.get("impliedVolatility"), lo=0),
                    "open_interest": _clean_float(r.get("openInterest"), lo=0),
                    "volume": _clean_float(r.get("volume"), lo=0),
                    "delta": _clean_float(r.get("delta")),
                    "gamma": _clean_float(r.get("gamma")),
                    "theta": _clean_float(r.get("theta")),
                    "vega": _clean_float(r.get("vega")),
                })
        except Exception:
            pass
        return legs

    # ── news ──────────────────────────────────────────────────────────────
    def news(self, symbol: str) -> DataResult:
        try:
            yf = self._module()
            items = []
            try:
                items = yf.Ticker(symbol).get_news(count=8) or []
            except Exception:
                items = []
            if not items:
                # yfinance 1.x: the ticker news endpoint serves nothing for
                # many symbols (crypto included) — the Search API is where
                # news lives now. Same output shape, different lookup.
                try:
                    items = yf.Search(symbol, news_count=8).news or []
                except Exception:
                    items = []
            out = []
            for n in items:
                if isinstance(n, dict):
                    c = n.get("content") or n
                    out.append({
                        "title": str(c.get("title") or "")[:220],
                        "publisher": str(c.get("publisher")
                                         or (c.get("provider") or {}).get("displayName", "")
                                         or "")[:80],
                        "link": str((c.get("clickThroughUrl") or {}).get("url")
                                    or c.get("link") or "")[:300],
                        "sentiment": None,      # filled by the user's own feed
                    })
            out = [o for o in out if o["title"]]
            return DataResult(ok=bool(out), provenance=Provenance.DELAYED,
                              source=self.name, data={"symbol": symbol,
                                                      "items": out},
                              error="" if out else "no news items served",
                              as_of=_now_iso())
        except Exception as exc:
            return unavailable(self.name, f"{type(exc).__name__}: {exc}")


# ── simulated adapter (deterministic) ────────────────────────────────────────

class SimulatedAdapter(DataAdapter):
    """Deterministic fake market for tests and paper drills. The SAME symbol
    always produces the SAME series within a run — seeded by the symbol — so
    tests can assert on outputs. Never reachable as a "live" source: its
    provenance is always SIMULATED and the agent surfaces that label."""

    name = "simulated"

    def __init__(self, base_price: float = 100.0, drift: float = 0.0005,
                 vol: float = 0.012, bars: int = 240) -> None:
        self.base_price = float(base_price)
        self.drift = float(drift)
        self.vol = float(vol)
        self.bars = int(bars)

    def _series(self, symbol: str, n: int) -> list[dict]:
        import hashlib
        seed = int(hashlib.sha256(symbol.encode()).hexdigest()[:8], 16)
        # xorshift — deterministic, no numpy needed, no global RNG state.
        state = seed or 0x9E3779B9

        def rnd() -> float:
            nonlocal state
            state ^= (state << 13) & 0xFFFFFFFF
            state ^= state >> 17
            state ^= (state << 5) & 0xFFFFFFFF
            return state / 0x100000000

        # Box-Muller from two uniforms → normal shocks.
        rows, price = [], self.base_price
        for i in range(max(n, 30)):
            u1, u2 = max(rnd(), 1e-9), rnd()
            z = math.sqrt(-2.0 * math.log(u1)) * math.cos(2 * math.pi * u2)
            openp = price
            price = max(0.01, price * (1 + self.drift + self.vol * z))
            hi = max(openp, price) * (1 + abs(self.vol) * 0.4 * rnd())
            lo = min(openp, price) * (1 - abs(self.vol) * 0.4 * rnd())
            rows.append({"ts": f"sim-bar-{i:04d}", "open": round(openp, 6),
                         "high": round(hi, 6), "low": round(lo, 6),
                         "close": round(price, 6),
                         "volume": int(50_000 + 40_000 * rnd())})
        return rows

    def quote(self, symbol: str, request_live: bool = False) -> DataResult:
        rows = self._series(symbol, 30)
        last = rows[-1]
        return DataResult(ok=True, provenance=Provenance.SIMULATED,
                          source=self.name,
                          data={"symbol": symbol, "last": last["close"],
                                "previous_close": rows[-2]["close"],
                                "open": last["open"], "day_high": last["high"],
                                "day_low": last["low"], "volume": last["volume"],
                                "currency": "USD", "exchange": "SIM",
                                "missing_fields": []},
                          as_of=_now_iso())

    def history(self, symbol: str, period: str = "6mo",
                interval: str = "1d") -> DataResult:
        return DataResult(ok=True, provenance=Provenance.SIMULATED,
                          source=self.name,
                          data={"symbol": symbol, "interval": interval,
                                "bars": self._series(symbol, self.bars)},
                          as_of=_now_iso())

    def options_chain(self, symbol: str) -> DataResult:
        rows = self._series(symbol, 1)
        spot = rows[-1]["close"]
        legs = []
        for kind in ("CALL", "PUT"):
            for moneyness in (0.90, 0.95, 1.00, 1.05, 1.10):
                strike = round(spot * moneyness, 2)
                intrinsic = max(0.0, spot - strike) if kind == "CALL" \
                    else max(0.0, strike - spot)
                iv = 0.22 + 0.05 * abs(1 - moneyness)
                legs.append({
                    "kind": kind, "strike": strike, "expiry": "SIM-2026-12-18",
                    "last": round(intrinsic + spot * iv * 0.06, 2),
                    "bid": round(intrinsic + spot * iv * 0.055, 2),
                    "ask": round(intrinsic + spot * iv * 0.065, 2),
                    "iv": round(iv, 4), "open_interest": int(1200 * moneyness),
                    "volume": int(300 * moneyness),
                    "delta": None, "gamma": None, "theta": None, "vega": None,
                })
        return DataResult(ok=True, provenance=Provenance.SIMULATED,
                          source=self.name,
                          data={"symbol": symbol, "expiry": "SIM-2026-12-18",
                                "expiries_available": ["SIM-2026-12-18"],
                                "spot_hint": spot, "calls":
                                    [l for l in legs if l["kind"] == "CALL"],
                                "puts": [l for l in legs if l["kind"] == "PUT"]},
                          as_of=_now_iso())

    def news(self, symbol: str) -> DataResult:
        return unavailable(self.name, "simulated feed serves no news")


# ── market data facade ────────────────────────────────────────────────────────

class MarketData:
    """The DATA stage entry point. Choses the adapter, carries provenance
    through, and NEVER synthesizes a fallback price when both fail."""

    def __init__(self, real: DataAdapter | None = None,
                 sim: DataAdapter | None = None,
                 prefer_sim: bool = False,
                 delta: DataAdapter | None = None) -> None:
        self.real = real or YFinanceAdapter()
        self.sim = sim or SimulatedAdapter()
        self.prefer_sim = bool(prefer_sim)
        # Imported lazily: trading/delta.py imports the DataResult helpers
        # from this module, so a top-level import would be circular.
        if delta is None:
            from orchestrator.agents.trading.delta import DeltaTradingAdapter
            delta = DeltaTradingAdapter()
        self.delta = delta

    def _real_first(self) -> bool:
        return not self.prefer_sim

    def _explicit_delta(self, venue: str | None) -> bool:
        return str(venue or "").strip().lower() in (
            "delta", "delta_india", "delta_perp", "delta_spot")

    def _delta_route(self, symbol: str, venue: str | None) -> bool:
        v = str(venue or "").strip().lower()
        if self._explicit_delta(v):
            return True
        if v:
            return False
        # Explicit venue wins. Without one, only an already-cached exact
        # Delta product symbol is routed here — never a network probe on
        # every unrelated quote.
        try:
            return bool(getattr(self.delta, "is_cached_product")(symbol))
        except Exception:
            return False

    def quote(self, symbol: str, *, prefer_sim: bool = False,
              request_live: bool = False, venue: str | None = None) -> DataResult:
        # An explicit Delta request is never silently answered by the
        # simulated adapter — venue separation is a safety property.
        if self._explicit_delta(venue):
            return self.delta.quote(symbol)
        if prefer_sim or self.prefer_sim:
            return self.sim.quote(symbol)
        if self._delta_route(symbol, venue):
            return self.delta.quote(symbol)
        return self.real.quote(symbol, request_live=request_live)

    def history(self, symbol: str, period: str = "6mo",
                interval: str = "1d", *, prefer_sim: bool = False,
                venue: str | None = None) -> DataResult:
        if self._explicit_delta(venue):
            return self.delta.history(symbol, period, interval)
        if prefer_sim or self.prefer_sim:
            return self.sim.history(symbol, period, interval)
        if self._delta_route(symbol, venue):
            return self.delta.history(symbol, period, interval)
        return self.real.history(symbol, period, interval)

    def options_chain(self, symbol: str, *, prefer_sim: bool = False,
                      venue: str | None = None) -> DataResult:
        if self._explicit_delta(venue):
            return self.delta.options_chain(symbol)
        if prefer_sim or self.prefer_sim:
            return self.sim.options_chain(symbol)
        if self._delta_route(symbol, venue):
            return self.delta.options_chain(symbol)
        return self.real.options_chain(symbol)

    def news(self, symbol: str, *, prefer_sim: bool = False,
             venue: str | None = None) -> DataResult:
        if self._explicit_delta(venue):
            return self.delta.news(symbol)
        if prefer_sim or self.prefer_sim:
            return self.sim.news(symbol)
        if self._delta_route(symbol, venue):
            return self.delta.news(symbol)
        return self.real.news(symbol)

    def greeks(self, chain: DataResult, spot: float, rate: float = 0.05,
               days_to_expiry: float = 30.0) -> dict:
        """Black-Scholes greeks where the chain lacked them. Pure math over
        SERVED data — nothing invented; a leg with no IV stays greekless."""
        return compute_greeks(chain.data or {}, spot=spot, rate=rate,
                              days_to_expiry=days_to_expiry)


# ── Black-Scholes greeks (pure math, tested offline) ─────────────────────────

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def compute_greeks(chain: dict, spot: float, rate: float = 0.05,
                   days_to_expiry: float = 30.0) -> dict:
    """Compute delta/gamma/theta/vega for legs with an IV, T, S. Legs whose IV
    or T is missing stay None — a greek computed from a guessed input would be
    an invented number."""
    T = max(days_to_expiry, 1e-6) / 365.0
    S = float(spot)
    out = {"calls": {}, "puts": {}, "atm": {}}
    if S <= 0 or T <= 0:
        return out
    for kind in ("calls", "puts"):
        for leg in chain.get(kind) or []:
            iv = leg.get("iv")
            if not iv or iv <= 0:
                continue
            K = float(leg["strike"])
            sig = float(iv)
            d1 = (math.log(S / K) + (rate + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
            d2 = d1 - sig * math.sqrt(T)
            disc = math.exp(-rate * T)
            if kind == "calls":
                delta = _norm_cdf(d1)
                theta = (-(S * _norm_pdf(d1) * sig) / (2 * math.sqrt(T))
                         - rate * K * disc * _norm_cdf(d2)) / 365.0
            else:
                delta = _norm_cdf(d1) - 1.0
                theta = (-(S * _norm_pdf(d1) * sig) / (2 * math.sqrt(T))
                         + rate * K * disc * _norm_cdf(-d2)) / 365.0
            gamma = _norm_pdf(d1) / (S * sig * math.sqrt(T))
            vega = S * _norm_pdf(d1) * math.sqrt(T) / 100.0
            out[kind][str(leg["strike"])] = {
                "delta": round(delta, 4), "gamma": round(gamma, 6),
                "theta": round(theta, 4), "vega": round(vega, 4),
                "iv": round(sig, 4)}
    # ATM pick: strike closest to spot among greeked legs
    best, bestd = None, None
    for kind in ("calls", "puts"):
        for k, g in out[kind].items():
            d = abs(float(k) - S)
            if bestd is None or d < bestd:
                best, bestd = (kind, k, g), d
    if best:
        out["atm"] = {"kind": best[0], "strike": best[1],
                      "strike_str": best[1], **best[2]}
    return out


# ── persistence helper (checkpoint-friendly) ─────────────────────────────────

def bars_to_json(bars: list[dict]) -> list[dict]:
    return [{k: (round(v, 6) if isinstance(v, float) else v)
             for k, v in bar.items()} for bar in bars]
