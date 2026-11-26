"""
orchestrator/agents/trading/delta_private.py — authenticated Delta India client.

This module is the ONLY place in the trading stack that signs a Delta request.
It is deliberately separate from `delta_exchange.DeltaClient` (public data,
no order surface) so the read-only adapter stays read-only.

Safety contract
    * Nothing here places an order on import or on construction.
    * The live switch in risk.live_execution.json is checked by broker.py,
      not by this client; this client only executes what it is told to.
    * Base URL is selected by the live environment:
          demo → https://cdn-ind.testnet.deltaex.org
          prod → https://api.india.delta.exchange
      A demo key sent to prod (or vice versa) is rejected by the exchange.
    * Secrets are never included in exceptions, logs or status payloads.

Signature (Delta Exchange API v2)
    hex(HMAC_SHA256(secret, method + timestamp + path + query + body))
    headers: api-key, timestamp, signature, User-Agent
    timestamp is Unix seconds and must be < 5 seconds old.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any
from urllib.parse import urlencode

import requests

from orchestrator.agents.delta_exchange import resolve_credentials

PROD_BASE_URL = "https://api.india.delta.exchange"
DEMO_BASE_URL = "https://cdn-ind.testnet.deltaex.org"
DEFAULT_TIMEOUT = 15
USER_AGENT = "JARVIS-Delta/1.0 (authenticated trading; explicit approval)"

# Delta rejects a signature whose timestamp is >5s from server time and there
# is no public /v2/time endpoint. When Delta answers expired_signature it DOES
# include {request_time, server_time} in the error context, so we learn the
# clock skew from that, cache it per base URL for this process, and retry the
# request once with the corrected timestamp.
_CLOCK_OFFSETS: dict[str, int] = {}
_SIGNATURE_ERROR_CODES = ("expired_signature", "invalid_signature",
                          "signatureexpired")


class DeltaPrivateError(RuntimeError):
    """A signed Delta request failed. The message contains no secret."""


def sign_request(secret: str, method: str, timestamp: str, path: str,
                 query: str = "", body: str = "") -> str:
    """Delta's documented v2 signature.

    `query` must include the leading '?' when non-empty; `body` must be the
    exact bytes/string sent in the request. This helper is pure so the
    documented test vector can be checked without a network call.
    """
    message = f"{method.upper()}{timestamp}{path}{query}{body}"
    return hmac.new(str(secret).encode("utf-8"), message.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def base_url_for(environment: str) -> str:
    env = str(environment or "demo").strip().lower()
    if env not in ("demo", "prod"):
        raise DeltaPrivateError("live environment must be demo or prod")
    if env == "demo":
        return (os.environ.get("DELTA_DEMO_BASE_URL", "").strip()
                or DEMO_BASE_URL).rstrip("/")
    return (os.environ.get("DELTA_LIVE_BASE_URL", "").strip()
            or os.environ.get("DELTA_BASE_URL", "").strip()
            or PROD_BASE_URL).rstrip("/")


def _num(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def _price(value: Any, tick_size: float | None = None) -> str:
    v = _num(value)
    if v is None:
        raise DeltaPrivateError("price is missing or not finite")
    tick = _num(tick_size)
    if tick and tick > 0:
        v = round(round(v / tick) * tick, 12)
    # Delta accepts decimal strings; str() avoids scientific notation for the
    # normal price ranges and keeps the exact number we signed.
    return f"{v:.12f}".rstrip("0").rstrip(".") or "0"


def _error_message(payload: Any, status: int) -> str:
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            code = err.get("code") or err.get("error_code")
            ctx = err.get("context") or err.get("message")
            text = " ".join(str(x) for x in (code, ctx) if x)
            if text:
                return text
        for key in ("message", "error", "result"):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()[:400]
    return f"Delta HTTP {status}"


def _error_code(payload: Any) -> str:
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        return str(payload["error"].get("code") or "").strip().lower()
    return ""


def _skew_from_error(payload: Any) -> int | None:
    """server_time − request_time from an expired_signature context."""
    try:
        ctx = ((payload or {}).get("error") or {}).get("context") or {}
        req_t = int(float(ctx.get("request_time")))
        srv_t = int(float(ctx.get("server_time")))
    except (TypeError, ValueError, AttributeError):
        return None
    if req_t <= 0 or srv_t <= 0:
        return None
    return srv_t - req_t


def clock_offset_for(base_url: str) -> int:
    """Learned clock offset (server − local, seconds) for a base URL."""
    return int(_CLOCK_OFFSETS.get(str(base_url), 0))


class DeltaPrivateClient:
    """Minimal signed client: wallet, positions, orders, brackets, close."""

    def __init__(self, environment: str = "demo", *,
                 key: str | None = None, secret: str | None = None,
                 base_url: str | None = None, timeout: int = DEFAULT_TIMEOUT):
        self.environment = str(environment or "demo").strip().lower()
        if self.environment not in ("demo", "prod"):
            raise DeltaPrivateError("live environment must be demo or prod")
        env_key, env_secret = resolve_credentials()
        self.key = str(key if key is not None else env_key).strip()
        self.secret = str(secret if secret is not None else env_secret).strip()
        self.base_url = (base_url or base_url_for(self.environment)).rstrip("/")
        self.timeout = int(timeout or DEFAULT_TIMEOUT)

    def credentials_configured(self) -> bool:
        return bool(self.key and self.secret)

    # ── signing / transport ───────────────────────────────────────────────
    @property
    def clock_offset_s(self) -> int:
        """Learned clock offset (server − local) for this base URL."""
        return clock_offset_for(self.base_url)

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 body: dict | None = None, _retry: bool = True) -> dict:
        if not self.credentials_configured():
            raise DeltaPrivateError(
                "Delta API key/secret are not configured — save them in "
                "Settings before arming live execution")
        method = method.upper()
        query = ("?" + urlencode(params, doseq=True)) if params else ""
        body_text = ("" if body is None else
                     json.dumps(body, separators=(",", ":"),
                                ensure_ascii=False))
        timestamp = str(int(time.time() + self.clock_offset_s))
        signature = sign_request(self.secret, method, timestamp, path,
                                 query, body_text)
        headers = {
            "api-key": self.key,
            "timestamp": timestamp,
            "signature": signature,
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        url = f"{self.base_url}{path}{query}"
        try:
            resp = requests.request(method, url, data=(body_text or None),
                                    headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise DeltaPrivateError(
                f"Delta {self.environment} request failed: "
                f"{type(exc).__name__}") from None
        try:
            payload = resp.json()
        except ValueError:
            raise DeltaPrivateError(
                f"Delta {self.environment} returned non-JSON "
                f"(HTTP {resp.status_code})") from None
        failed = (resp.status_code >= 400 or
                  (isinstance(payload, dict) and payload.get("success") is False))
        if failed:
            code = _error_code(payload)
            if code in _SIGNATURE_ERROR_CODES:
                skew = _skew_from_error(payload)
                if skew is not None:
                    _CLOCK_OFFSETS[self.base_url] = skew
                if _retry:
                    # Retry exactly once with the corrected timestamp.
                    return self._request(method, path, params=params,
                                         body=body, _retry=False)
                off = self.clock_offset_s
                raise DeltaPrivateError(
                    f"Delta {self.environment}: {code} — the signed timestamp "
                    f"was rejected even after one clock-skew retry "
                    f"(applied offset {off:+d}s, skew observed "
                    f"{skew if skew is not None else 'unknown'}s). Sync this "
                    "machine's clock with NTP (e.g. `sudo ntpdate pool.ntp.org` "
                    "or `timedatectl set-ntp true`) and retry.")
            raise DeltaPrivateError(
                f"Delta {self.environment}: {_error_message(payload, resp.status_code)}")
        if not isinstance(payload, dict):
            raise DeltaPrivateError(
                f"Delta {self.environment} returned an unexpected payload")
        return payload

    # ── read-only account surface ─────────────────────────────────────────
    def wallet_balances(self) -> dict:
        return self._request("GET", "/v2/wallet/balances")

    def positions(self) -> dict:
        return self._request("GET", "/v2/positions/margined")

    def open_orders(self, product_ids: str | None = None) -> dict:
        params = {"state": "open"}
        if product_ids:
            params["product_ids"] = str(product_ids)
        return self._request("GET", "/v2/orders", params=params)

    def cancel_order(self, order_id: int) -> dict:
        """Cancel by exchange order id."""
        return self._request("DELETE", "/v2/orders", body={"id": int(order_id)})

    def probe(self) -> dict:
        """Read-only authentication/account probe. Never returns a secret."""
        balances = self.wallet_balances()
        meta = balances.get("meta") or {}
        assets = []
        for row in balances.get("result") or []:
            if not isinstance(row, dict):
                continue
            assets.append({
                "asset": row.get("asset_symbol"),
                "balance": row.get("balance"),
                "available_balance": row.get("available_balance"),
                "blocked_margin": row.get("blocked_margin"),
            })
        out = {
            "ok": True,
            "environment": self.environment,
            "base_url": self.base_url,
            "clock_offset_s": self.clock_offset_s,
            "net_equity": meta.get("net_equity"),
            "robo_trading_equity": meta.get("robo_trading_equity"),
            "assets": assets,
            "positions": [],
        }
        try:
            pos = self.positions()
            for row in pos.get("result") or []:
                if not isinstance(row, dict):
                    continue
                out["positions"].append({
                    "product_symbol": (row.get("product_symbol")
                                       or (row.get("product") or {}).get("symbol")),
                    "size": row.get("size"),
                    "side": row.get("side"),
                    "entry_price": row.get("entry_price"),
                    "mark_price": row.get("mark_price"),
                    "unrealized_pnl": row.get("unrealized_pnl"),
                    "liquidation_price": row.get("liquidation_price"),
                })
        except DeltaPrivateError as exc:
            out["positions_error"] = str(exc)
        out["position_count"] = len(out["positions"])
        return out

    # ── order surface (only called by DeltaLiveBroker) ────────────────────
    def place_order(self, order: dict) -> dict:
        if not isinstance(order, dict) or not order:
            raise DeltaPrivateError("order body is empty")
        return self._request("POST", "/v2/orders", body=order)

    def place_bracket(self, product_symbol: str, *, side: str,
                      stop_loss: float, take_profit: float,
                      tick_size: float | None = None,
                      buffer_pct: float = 0.5) -> dict:
        """Attach a stop-loss + take-profit bracket to an open position.

        Limit prices are placed slightly through the trigger so a triggered
        stop/target is not left resting unfilled. The position is closed by
        the bracket once either leg triggers.
        """
        side = str(side or "").lower()
        if side not in ("buy", "sell"):
            raise DeltaPrivateError("bracket side must be buy or sell")
        buf = max(0.0, min(float(buffer_pct or 0.0), 5.0)) / 100.0
        if side == "buy":          # long position: exit is a sell
            sl_limit = float(stop_loss) * (1.0 - buf)
            tp_limit = float(take_profit) * (1.0 - buf)
        else:                      # short position: exit is a buy
            sl_limit = float(stop_loss) * (1.0 + buf)
            tp_limit = float(take_profit) * (1.0 + buf)
        body = {
            "product_symbol": str(product_symbol).strip().upper(),
            "stop_loss_order": {
                "order_type": "limit_order",
                "stop_price": _price(stop_loss, tick_size),
                "limit_price": _price(sl_limit, tick_size),
            },
            "take_profit_order": {
                "order_type": "limit_order",
                "stop_price": _price(take_profit, tick_size),
                "limit_price": _price(tp_limit, tick_size),
            },
            "bracket_stop_trigger_method": "last_traded_price",
        }
        return self._request("POST", "/v2/orders/bracket", body=body)

    def emergency_close(self, product_symbol: str, *, side: str,
                        size: int) -> dict:
        """Reduce-only IOC market order in the opposite direction."""
        side = str(side or "").lower()
        if side not in ("buy", "sell"):
            raise DeltaPrivateError("close side must be buy or sell")
        if int(size) < 1:
            raise DeltaPrivateError("close size must be at least 1 contract")
        body = {
            "product_symbol": str(product_symbol).strip().upper(),
            "size": int(size),
            "side": "sell" if side == "buy" else "buy",
            "order_type": "market_order",
            "time_in_force": "ioc",
            "reduce_only": True,
        }
        return self.place_order(body)


__all__ = [
    "DeltaPrivateClient", "DeltaPrivateError", "sign_request",
    "base_url_for", "clock_offset_for", "PROD_BASE_URL", "DEMO_BASE_URL",
]
