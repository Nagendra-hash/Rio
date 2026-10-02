"""
plugins/binance_trader.py — JARVIS Live Crypto Trading via Binance API.

Drop-in plugin for automated crypto trading on Binance.
Works with the existing trading_strategy signals to execute real trades.

IMPORTANT SAFETY NOTES:
- Requires Binance API key + secret in config/api_keys.json
- Default mode: PAPER_TRADING (no real orders) until explicitly enabled
- Has built-in safety limits: max daily loss, max position size, cooldowns
- All trades are logged for audit trail

Dependencies: python-binance (optional, falls back to requests + REST API)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import os

_BASE = Path(__file__).resolve().parent.parent
# JARVIS_HOME (pendrive mode): trading state (live_trades.jsonl, safety config)
# is writable per-drive state, and the API config lives with it — same contract
# as memory_manager.get_base_dir.
_home = os.environ.get("JARVIS_HOME", "").strip()
if _home:
    _BASE = Path(_home).expanduser().resolve()
_TRADING_DIR = _BASE / "memory" / "trading"
_API_CONFIG = _BASE / "config" / "api_keys.json"
_TRADE_LOG = _TRADING_DIR / "live_trades.jsonl"
_SAFETY_CONFIG = _TRADING_DIR / "trading_safety.json"

# ── Default Safety Settings ───────────────────────────────────────────────────

DEFAULT_SAFETY = {
    "enabled": True,
    "mode": "paper",  # "paper" or "live"
    "max_daily_loss_pct": 2.0,       # Stop trading if daily loss exceeds this %
    "max_position_usd": 1000,        # Max position size per trade
    "max_daily_trades": 10,          # Max trades per day
    "cooldown_seconds": 300,         # Min time between trades
    "require_confirmation": True,    # Require user confirmation for live trades
    "symbols_whitelist": [],         # Empty = all symbols allowed
    "symbols_blacklist": [],         # Symbols to never trade
    "min_signal_score": 3.0,         # Minimum signal score to trade
    "market": "spot",                # "spot" (api.binance.com) or "futures" (fapi.binance.com, USDT-M perps)
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def _load_json(path: Path, default: Any = None) -> Any:
    try:
        data = json.loads(path.read_text())
        return data if data is not None else default
    except Exception:
        return default

def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))

def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")

def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text().splitlines():
        try:
            records.append(json.loads(line))
        except Exception:
            continue
    return records

def _get_api_keys() -> dict:
    """Load API keys from config file."""
    try:
        return json.loads(_API_CONFIG.read_text())
    except Exception:
        return {}

def _get_binance_keys() -> tuple[str, str]:
    """Extract Binance API key and secret.

    Priority:
      1. top-level "binance_api_key" / "binance_api_secret"
      2. plugin_config.trading.live_api_key / live_api_secret (the dashboard
         settings form writes there — keep both copies working)

    Whitespace, quotes, BOM and zero-width characters are stripped: a
    copy/paste with a trailing newline or an invisible char produces a valid
    HMAC over the WRONG key and shows up only as Binance -1022 "Signature
    for this request is not valid" — one of the hardest errors to debug.
    """
    keys = _get_api_keys()
    trading_cfg = (keys.get("plugin_config") or {}).get("trading") or {}

    def _clean(value) -> str:
        if not isinstance(value, str):
            return ""
        # BOM, zero-width space, word-joiner, bidi marks — invisible paste
        # contaminants that break HMACs.
        for ch in ("\ufeff", "\u200b", "\u200c", "\u200d", "\u2060", "\u200e", "\u200f"):
            value = value.replace(ch, "")
        return value.strip().strip('"\'').strip()

    key = _clean(keys.get("binance_api_key")) or _clean(trading_cfg.get("live_api_key"))
    secret = _clean(keys.get("binance_api_secret")) or _clean(trading_cfg.get("live_api_secret"))
    return key, secret

# ── Binance API Client (minimal, no external dependency) ─────────────────────

class BinanceClient:
    """
    Minimal Binance API client using REST API directly.
    No python-binance dependency required.
    """

    def __init__(self, api_key: str = "", api_secret: str = "", market: str = "spot"):
        self.api_key = api_key
        self.api_secret = api_secret
        self.market = market if market in ("spot", "futures") else "spot"
        if self.market == "futures":
            # USDⓈ-M perpetuals — the contracts TradingView shows as
            # BINANCE:PERSP (BTCUSDT.P, SOLUSDT.P, …). Same signing scheme as
            # spot, different host and endpoint map.
            self.base_url = "https://fapi.binance.com"
            self.testnet_url = "https://testnet.binancefuture.com"
        else:
            self.base_url = "https://api.binance.com"
            self.testnet_url = "https://testnet.binance.vision"
        self.use_testnet = False
        self._time_offset_ms: Optional[int] = None   # server-clock offset; synced lazily

        # Endpoint map — keeps every method market-agnostic below.
        if self.market == "futures":
            self._ep = {
                "time":         "/fapi/v1/time",
                "ticker":       "/fapi/v1/ticker/price",
                "klines":       "/fapi/v1/klines",
                "exchangeInfo": "/fapi/v1/exchangeInfo",
                "account":      "/fapi/v2/balance",      # returns a LIST — normalized in get_account
                "order":        "/fapi/v1/order",
                "openOrders":   "/fapi/v1/openOrders",
                "positions":    "/fapi/v2/positionRisk",
                "leverage":     "/fapi/v1/leverage",
            }
        else:
            self._ep = {
                "time":         "/api/v3/time",
                "ticker":       "/api/v3/ticker/price",
                "klines":       "/api/v3/klines",
                "exchangeInfo": "/api/v3/exchangeInfo",
                "account":      "/api/v3/account",
                "order":        "/api/v3/order",
                "openOrders":   "/api/v3/openOrders",
                "positions":    None,                    # spot has no positionRisk
                "leverage":     None,
            }

    def _sign(self, params: dict) -> str:
        """Create HMAC SHA256 signature."""
        query = urllib.parse.urlencode(params)
        return hmac.new(
            self.api_secret.encode(),
            query.encode(),
            hashlib.sha256
        ).hexdigest()

    def _request(self, endpoint: str, params: dict = None, method: str = "GET") -> dict:
        """Make authenticated or unauthenticated request."""
        url = f"{self.base_url}{endpoint}" if not self.use_testnet else f"{self.testnet_url}{endpoint}"
        params = params or {}

        # Public endpoints (no signature required). Anything NOT listed here is
        # treated as signed — new authenticated endpoints must then work by
        # default. Match on the path PREFIX before the query string, on both
        # hosts' endpoint maps.
        _PUBLIC_PREFIXES = (
            "/api/v3/time", "/api/v3/ticker", "/api/v3/klines",
            "/api/v3/exchangeInfo", "/api/v3/depth", "/api/v3/trades",
            "/api/v3/avgPrice",
            "/fapi/v1/time", "/fapi/v1/ticker", "/fapi/v1/klines",
            "/fapi/v1/exchangeInfo", "/fapi/v1/depth",
        )
        is_signed = bool(self.api_key) and not any(
            endpoint.split("?", 1)[0].startswith(p) for p in _PUBLIC_PREFIXES
        )

        if is_signed:
            # Binance requires the local clock to be within recvWindow of the
            # server's. A skewed box (or a stale long-lived session) gets
            # rejected with -1021 "Timestamp for this request is outside of
            # the recvWindow" even when the keys are perfect. Sync once per
            # client from /api/v3/time (public, unsigned) and sign with the
            # SERVER's clock.
            if self._time_offset_ms is None:
                try:
                    # NOTE: the time endpoint is public on both hosts, so this
                    # call does NOT re-enter the signing branch — no recursion.
                    server_ms = self._request(self._ep["time"])
                    self._time_offset_ms = int(server_ms.get("serverTime", 0)) - int(time.time() * 1000)
                except Exception:
                    self._time_offset_ms = 0        # fall back to local clock
            params["timestamp"] = int(time.time() * 1000) + self._time_offset_ms
            params.setdefault("recvWindow", 10000)  # 10 s — generous but bounded
            params["signature"] = self._sign(params)

        # SIGNED params must go in the QUERY STRING for every method — including
        # GET. Sending them as a POST body makes the server sign an empty
        # string, producing the misleading -1022 "Signature for this request
        # is not valid" even with perfect keys.
        encoded = urllib.parse.urlencode(params) if params else ""
        if encoded:
            url = f"{url}{'&' if '?' in url else '?'}{encoded}"

        req = urllib.request.Request(
            url,
            data=None,
            headers={
                "X-MBX-APIKEY": self.api_key,
            } if self.api_key else {"Content-Type": "application/json"},
            method=method
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            error_body = e.read().decode()
            # Surface Binance's own payload first — {"code":-1022,"msg":"Signature
            # for this request is not valid."} — it names the actual failure;
            # the HTTP status alone (400/401/403) is too vague to act on.
            raise RuntimeError(f"Binance API error {e.code}: {error_body[:200]}")

    # ── Account Info ──────────────────────────────────────────────────────────

    def get_account(self) -> dict:
        """Account info. Spot: /api/v3/account. Futures: /fapi/v2/balance is a
        LIST of asset balances — wrapped in the same shape spot returns so all
        call sites stay uniform."""
        if self.market == "futures":
            balances = self._request(self._ep["account"], method="GET")
            return {
                "canTrade": True,       # futures list has no flag; check positions instead
                "balances": [
                    {"asset": b.get("asset", ""),
                     "free":  b.get("availableBalance", b.get("balance", "0")),
                     "locked": b.get("crossUnPnl", "0")}
                    for b in balances
                ],
                "raw": balances,
            }
        return self._request(self._ep["account"], method="GET")

    def get_balance(self, asset: str = "USDT") -> dict:
        return self.get_account()

    def get_positions(self) -> list:
        """Open perpetual positions (futures only). Empty list on spot."""
        if self.market != "futures" or not self._ep.get("positions"):
            return []
        positions = self._request(self._ep["positions"], method="GET")
        try:
            return [p for p in positions if abs(float(p.get("positionAmt", 0))) > 0]
        except (TypeError, ValueError):
            return []

    def set_leverage(self, symbol: str, leverage: int) -> dict:
        """Set leverage for a futures symbol (no-op on spot)."""
        if self.market != "futures" or not self._ep.get("leverage"):
            return {"leverage": 1}
        return self._request(
            self._ep["leverage"],
            {"symbol": symbol, "leverage": max(1, int(leverage))},
            method="POST",
        )

    # ── Market Data ───────────────────────────────────────────────────────────

    def get_ticker(self, symbol: str) -> dict:
        """Get current price ticker."""
        return self._request(f"{self._ep['ticker']}?symbol={symbol}")

    def get_klines(self, symbol: str, interval: str = "1h", limit: int = 100) -> list:
        """Get kline/candlestick data."""
        params = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        }
        data = self._request(self._ep["klines"], params)
        return data

    def get_server_time(self) -> int:
        """Get Binance server time."""
        data = self._request(self._ep["time"])
        return data.get("serverTime", 0)

    # ── Orders ────────────────────────────────────────────────────────────────

    def create_order(
        self,
        symbol: str,
        side: str,          # "BUY" or "SELL"
        type: str,          # "MARKET" or "LIMIT"
        quantity: float,
        price: float = None,
        stop_price: float = None,
        time_in_force: str = "GTC"
    ) -> dict:
        """
        Create an order.
        type: MARKET | LIMIT | STOP_LOSS | STOP_LOSS_LIMIT | TAKE_PROFIT | TAKE_PROFIT_LIMIT
        """
        params = {
            "symbol": symbol,
            "side": side,
            "type": type,
            "quantity": quantity,
            "timeInForce": time_in_force
        }

        if type in ("LIMIT", "STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT"):
            if price is None:
                raise ValueError("Price required for LIMIT orders")
            params["price"] = price
            if type == "LIMIT":
                # Need recvWindow for LIMIT orders
                pass

        if type in ("STOP_LOSS", "STOP_LOSS_LIMIT", "TAKE_PROFIT", "TAKE_PROFIT_LIMIT"):
            if stop_price is None:
                raise ValueError("stopPrice required for stop orders")
            params["stopPrice"] = stop_price
            if type in ("STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT"):
                if price is None:
                    raise ValueError("Price required for LIMIT stop orders")
                params["price"] = price

        return self._request(self._ep["order"], params, method="POST")

    def create_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float
    ) -> dict:
        """Create a market order (simplest form)."""
        return self.create_order(symbol, side, "MARKET", quantity)

    def create_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float
    ) -> dict:
        """Create a limit order."""
        return self.create_order(symbol, side, "LIMIT", quantity, price=price)

    def create_stop_loss(
        self,
        symbol: str,
        side: str,
        quantity: float,
        stop_price: float,
        close_price: float = None
    ) -> dict:
        """Create a stop loss order."""
        order_type = "STOP_MARKET" if close_price is None else "STOP_LOSS_LIMIT"
        params = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "quantity": quantity,
            "stopPrice": stop_price,
        }
        if close_price:
            params["price"] = close_price
        return self._request(self._ep["order"], params, method="POST")

    def create_take_profit(
        self,
        symbol: str,
        side: str,
        quantity: float,
        profit_price: float,
        close_price: float = None
    ) -> dict:
        """Create a take profit order."""
        order_type = "TAKE_PROFIT_MARKET" if close_price is None else "TAKE_PROFIT_LIMIT"
        params = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "quantity": quantity,
            "stopPrice": profit_price,
        }
        if close_price:
            params["price"] = close_price
        return self._request(self._ep["order"], params, method="POST")

    def cancel_order(self, symbol: str, order_id: int) -> dict:
        """Cancel an order."""
        return self._request(self._ep["order"], {
            "symbol": symbol,
            "orderId": order_id
        }, method="DELETE")

    def get_open_orders(self, symbol: str = None) -> list:
        """Get open orders."""
        params = {"symbol": symbol} if symbol else {}
        return self._request(self._ep["openOrders"], params)

    def get_order(self, symbol: str, order_id: int) -> dict:
        """Get order status."""
        return self._request("/api/v3/order", {
            "symbol": symbol,
            "orderId": order_id
        })

    def get_all_orders(self, symbol: str, limit: int = 100) -> list:
        """Get all orders (open and closed)."""
        return self._request("/api/v3/allOrders", {
            "symbol": symbol,
            "limit": limit
        })

    def get_my_trades(self, symbol: str, limit: int = 100) -> list:
        """Get executed trades for an account."""
        return self._request("/api/v3/myTrades", {
            "symbol": symbol,
            "limit": limit
        })

    # ── Convenience Methods ───────────────────────────────────────────────────

    def get_symbol_info(self, symbol: str) -> dict:
        """Get trading rules for a symbol."""
        return self._request(self._ep["exchangeInfo"], {
            "symbol": symbol
        })

    def get_qty_precision(self, symbol: str) -> int:
        """Get quantity precision for a symbol."""
        try:
            info = self.get_symbol_info(symbol)
            for f in info.get("symbols", [{}])[0].get("filters", []):
                if f.get("filterType") == "LOT_SIZE":
                    step = f.get("stepSize", "0.00000000")
                    precision = len(step.rstrip("0").split(".")[-1]) if "." in step else 0
                    return precision if precision > 0 else 5
        except Exception:
            pass
        return 5  # Default precision

    def get_price_precision(self, symbol: str) -> int:
        """Get price precision for a symbol."""
        try:
            info = self.get_symbol_info(symbol)
            for f in info.get("symbols", [{}])[0].get("filters", []):
                if f.get("filterType") == "PRICE_FILTER":
                    step = f.get("tickSize", "0.00000000")
                    precision = len(step.rstrip("0").split(".")[-1]) if "." in step else 0
                    return precision if precision > 0 else 2
        except Exception:
            pass
        return 2  # Default precision

    def format_qty(self, symbol: str, qty: float) -> float:
        """Format quantity to symbol's precision."""
        precision = self.get_qty_precision(symbol)
        return round(qty, precision)

    def format_price(self, symbol: str, price: float) -> float:
        """Format price to symbol's precision."""
        precision = self.get_price_precision(symbol)
        return round(price, precision)


# ── Safety Manager ────────────────────────────────────────────────────────────

class TradingSafety:
    """
    Manages safety limits for live trading.
    """

    def __init__(self):
        self.config = _load_json(_SAFETY_CONFIG, DEFAULT_SAFETY)
        self._daily_pnl = 0.0
        self._daily_trades = 0
        self._last_trade_time = 0
        self._load_state()

    def _load_state(self):
        """Load runtime state from disk."""
        state_path = _TRADING_DIR / "trading_state.json"
        try:
            state = json.loads(state_path.read_text())
            self._daily_pnl = state.get("daily_pnl", 0.0)
            self._daily_trades = state.get("daily_trades", 0)
            self._last_trade_time = state.get("last_trade_time", 0)
            # Reset if new day
            today = datetime.now().strftime("%Y-%m-%d")
            if state.get("date") != today:
                self._daily_pnl = 0.0
                self._daily_trades = 0
            self._save_state()
        except Exception:
            pass

    def _save_state(self):
        """Save runtime state to disk."""
        state_path = _TRADING_DIR / "trading_state.json"
        today = datetime.now().strftime("%Y-%m-%d")
        _save_json(state_path, {
            "date": today,
            "daily_pnl": self._daily_pnl,
            "daily_trades": self._daily_trades,
            "last_trade_time": self._last_trade_time
        })

    def can_trade(self, symbol: str = "") -> tuple[bool, str]:
        """
        Check if trading is allowed.
        Returns (allowed, reason_if_not).
        """
        config = self.config

        # Check if trading is enabled
        if not config.get("enabled", True):
            return False, "Trading is disabled in safety settings"

        # Check mode
        mode = config.get("mode", "paper")
        if mode == "paper":
            return True, "Paper trading mode - no real orders will be placed"

        # Check whitelist/blacklist
        if config.get("symbols_whitelist") and symbol:
            if symbol not in config["symbols_whitelist"]:
                return False, f"{symbol} not in whitelist"

        if config.get("symbols_blacklist") and symbol:
            if symbol in config["symbols_blacklist"]:
                return False, f"{symbol} is blacklisted"

        # Check daily loss limit
        max_loss = config.get("max_daily_loss_pct", 2.0)
        if abs(self._daily_pnl) > max_loss:
            return False, f"Daily loss limit exceeded ({self._daily_pnl:+.2f}%)"

        # Check daily trade limit
        max_trades = config.get("max_daily_trades", 10)
        if self._daily_trades >= max_trades:
            return False, f"Daily trade limit reached ({max_trades})"

        # Check cooldown
        cooldown = config.get("cooldown_seconds", 300)
        now = time.time()
        if now - self._last_trade_time < cooldown:
            remaining = cooldown - (now - self._last_trade_time)
            return False, f"Cooldown active ({remaining:.0f}s remaining)"

        return True, "OK"

    def record_trade(self, pnl_pct: float):
        """Record a trade for safety tracking."""
        self._daily_pnl += pnl_pct
        self._daily_trades += 1
        self._last_trade_time = time.time()
        self._save_state()

    def update_settings(self, **kwargs):
        """Update safety settings."""
        for key, value in kwargs.items():
            if key in self.config:
                self.config[key] = value
        _save_json(_SAFETY_CONFIG, self.config)


# ── Live Trader ───────────────────────────────────────────────────────────────

class LiveTrader:
    """
    Main trading engine that connects signals to Binance execution.
    """

    def __init__(self):
        self.client = None
        self.safety = TradingSafety()
        self._positions = {}  # Track open positions

    def _client_for(self, market: str, api_key: str = None, api_secret: str = None) -> tuple[BinanceClient, bool]:
        """A client for a specific market (spot/futures), independent of the
        configured one — the kill-switch must reach BOTH markets even when the
        configured market is only one of them."""
        api_key = api_key if api_key is not None else _get_binance_keys()[0]
        api_secret = api_secret if api_secret is not None else _get_binance_keys()[1]
        if not api_key or not api_secret:
            return BinanceClient(market=market), False
        return BinanceClient(api_key, api_secret, market=market), True

    def _ensure_client(self) -> tuple[BinanceClient, bool]:
        """Get or create Binance client. Returns (client, is_live).
        Rebuilt whenever the key or the configured market changes."""
        api_key, api_secret = _get_binance_keys()
        market = str(self.safety.config.get("market", "spot")).lower()
        if market not in ("spot", "futures"):
            market = "spot"

        if self.client and self.client.market == market and self.client.api_key == api_key:
            return self.client, self.client.api_key != ""

        self.client, is_live = self._client_for(market, api_key, api_secret)
        return self.client, is_live

    def get_positions(self) -> dict:
        """Get current open positions."""
        return self._positions.copy()

    def _update_positions(self):
        """Refresh positions from exchange."""
        client, is_live = self._ensure_client()
        if not is_live:
            return

        try:
            account = client.get_account()
            self._positions = {}
            for bal in account.get("balances", []):
                free = float(bal.get("free", 0))
                locked = float(bal.get("locked", 0))
                if free > 0.0001 or locked > 0.00000001:
                    asset = bal.get("asset", "")
                    # Convert to USDT equivalent
                    usdt_balance = self._asset_to_usdt(asset, free + locked)
                    if usdt_balance > 1:  # Only track positions > $1
                        self._positions[asset] = {
                            "asset": asset,
                            "free": free,
                            "locked": locked,
                            "usdt_value": usdt_balance
                        }
        except Exception as e:
            print(f"[BinanceTrader] Error updating positions: {e}")

    def _asset_to_usdt(self, asset: str, amount: float) -> float:
        """Convert asset amount to USDT value."""
        if asset == "USDT":
            return amount

        client, _ = self._ensure_client()
        try:
            # Try to get price
            symbol = f"{asset}USDT"
            ticker = client.get_ticker(symbol)
            price = float(ticker.get("price", 0))
            return amount * price
        except Exception:
            return 0.0

    def execute_trade(
        self,
        symbol: str,
        direction: str,      # "LONG" or "SHORT"
        entry_price: float,
        stop_loss: float,
        targets: list[float],
        position_size_usd: float,
        capital: float,
        atr: float,
        signal_score: float,
        rules_votes: dict = None
    ) -> dict:
        """
        Execute a trade on Binance.
        Returns trade result dict.
        """
        result = {
            "timestamp": _now_iso(),
            "symbol": symbol,
            "direction": direction,
            "status": "pending",
            "details": {}
        }

        # Get client and check if live
        client, is_live = self._ensure_client()

        # Check safety
        allowed, reason = self.safety.can_trade(symbol)
        if not allowed:
            result["status"] = "blocked"
            result["reason"] = reason
            return result

        # Determine side and quantity
        side = "BUY" if direction == "LONG" else "SELL"

        # Convert USD to symbol quantity
        try:
            symbol_info = client.get_symbol_info(symbol)
            price_tick = None
            qty_tick = None
            for f in symbol_info.get("symbols", [{}])[0].get("filters", []):
                if f.get("filterType") == "PRICE_FILTER":
                    price_tick = float(f.get("tickSize", "0.01"))
                if f.get("filterType") == "LOT_SIZE":
                    qty_tick = float(f.get("stepSize", "0.00000001"))

            # Get current price
            ticker = client.get_ticker(symbol)
            current_price = float(ticker.get("price", entry_price))

            # Calculate quantity
            qty = position_size_usd / current_price

            # Apply precision
            if qty_tick:
                qty = math.floor(qty / qty_tick) * qty_tick
            else:
                qty = round(qty, 5)

            # Min notional check
            if qty * current_price < 10:  # Binance min order is $10
                result["status"] = "blocked"
                result["reason"] = f"Order too small: ${qty * current_price:.2f} (min $10)"
                return result

            qty = float(f"{qty:.5f}")

        except Exception as e:
            result["status"] = "error"
            result["reason"] = f"Quantity calculation failed: {e}"
            return result

        # Paper trading mode
        if not is_live or self.safety.config.get("mode") == "paper":
            result["status"] = "paper_executed"
            result["details"] = {
                "side": side,
                "symbol": symbol,
                "quantity": qty,
                "price": current_price,
                "stop_loss": stop_loss,
                "targets": targets,
                "note": "Paper trade - no real order placed"
            }
            print(f"[BinanceTrader] PAPER TRADE: {side} {qty} {symbol} @ ~{current_price}")
            self.safety.record_trade(0)  # Paper trades don't affect PnL initially
            return result

        # ── LIVE TRADING ──────────────────────────────────────────────────────
        # Safety: require confirmation unless disabled
        if self.safety.config.get("require_confirmation", True):
            result["status"] = "pending_confirmation"
            result["details"] = {
                "side": side,
                "symbol": symbol,
                "quantity": qty,
                "price": current_price,
                "stop_loss": stop_loss,
                "targets": targets,
                "note": " awaiting user confirmation"
            }
            return result

        # Live order placement — futures first: cap leverage before the entry
        # so a market order can't fill at unbounded exposure. Best-effort: a
        # leverage rejection (e.g. symbol already configured) shouldn't sink
        # the trade.
        if client.market == "futures":
            try:
                lev = int(self.safety.config.get("futures_leverage", 2))
                client.set_leverage(symbol, lev)
            except Exception as _lev_err:
                print(f"[BinanceTrader] leverage set failed (continuing): {_lev_err}")

        # Execute market order
        try:
            order = client.create_market_order(symbol, side, qty)

            result["status"] = "executed"
            result["details"] = {
                "order_id": order.get("orderId"),
                "side": side,
                "symbol": symbol,
                "quantity": qty,
                "price": current_price,
                "stop_loss": stop_loss,
                "targets": targets,
                "fills": order.get("fills", [])
            }

            # Set stop loss and take profit orders
            stop_side = "SELL" if side == "BUY" else "BUY"
            stop_result = client.create_stop_loss(
                symbol, stop_side, qty, stop_loss
            )
            result["details"]["stop_loss_order"] = stop_result.get("orderId")

            # Record trade
            self.safety.record_trade(0)  # Will update when closed

            _append_jsonl(_TRADE_LOG, {
                "timestamp": _now_iso(),
                "symbol": symbol,
                "direction": direction,
                "side": side,
                "quantity": qty,
                "price": current_price,
                "stop_loss": stop_loss,
                "targets": targets,
                "signal_score": signal_score,
                "rules_votes": rules_votes or {},
                "order_id": order.get("orderId"),
                "status": "open"
            })

            return result

        except Exception as e:
            result["status"] = "error"
            result["reason"] = str(e)[:200]
            return result

    def close_position(self, symbol: str, quantity: float = None) -> dict:
        """Close an open position."""
        result = {
            "timestamp": _now_iso(),
            "symbol": symbol,
            "status": "pending"
        }

        client, is_live = self._ensure_client()

        if not is_live:
            result["status"] = "paper_closed"
            return result

        # Futures: read the live position directly — positionAmt's SIGN tells
        # the closing side (long > 0 → SELL, short < 0 → BUY), which a
        # sell-only close cannot express.
        if client.market == "futures":
            try:
                positions = client.get_positions()
                pos = next((p for p in positions if p.get("symbol") == symbol), None)
                if pos is None:
                    result["status"] = "no_position"
                    result["reason"] = f"No open futures position in {symbol}"
                    return result
                amt = float(pos.get("positionAmt", 0))
                qty = abs(quantity) if quantity else abs(amt)
                close_side = "SELL" if amt > 0 else "BUY"

                for order in client.get_open_orders(symbol):
                    try:
                        client.cancel_order(symbol, order.get("orderId"))
                    except Exception:
                        pass

                order = client.create_market_order(symbol, close_side, qty)
                result["status"] = "closed"
                result["details"] = {
                    "order_id": order.get("orderId"),
                    "quantity": qty,
                    "side": close_side,
                    "price": float(pos.get("markPrice", 0) or 0),
                }
                _append_jsonl(_TRADE_LOG, {
                    "timestamp": _now_iso(), "symbol": symbol, "action": "close",
                    "side": close_side, "quantity": qty, "market": "futures",
                    "status": "closed"
                })
                return result
            except Exception as e:
                result["status"] = "error"
                result["reason"] = str(e)[:200]
                return result

        # Spot close path.
        self._update_positions()
        if symbol not in self._positions:
            result["status"] = "no_position"
            result["reason"] = f"No open position in {symbol}"
            return result

        pos = self._positions[symbol]
        qty = quantity or pos["free"]

        try:
            # Determine side (opposite of what we bought)
            # For simplicity, SELL to close LONG, BUY to close SHORT
            ticker = client.get_ticker(symbol)
            current_price = float(ticker.get("price", 0))

            # Cancel any open orders first
            open_orders = client.get_open_orders(symbol)
            for order in open_orders:
                try:
                    client.cancel_order(symbol, order.get("orderId"))
                except Exception:
                    pass

            # Close position (simplified - in real trading would track direction)
            order = client.create_market_order(symbol, "SELL", qty)

            result["status"] = "closed"
            result["details"] = {
                "order_id": order.get("orderId"),
                "quantity": qty,
                "price": current_price
            }

            # Record in trade log
            _append_jsonl(_TRADE_LOG, {
                "timestamp": _now_iso(),
                "symbol": symbol,
                "action": "close",
                "quantity": qty,
                "price": current_price,
                "status": "closed"
            })

            return result

        except Exception as e:
            result["status"] = "error"
            result["reason"] = str(e)[:200]
            return result

    def get_recent_trades(self, limit: int = 20) -> list[dict]:
        """Get recent trade history."""
        trades = _load_jsonl(_TRADE_LOG)
        return trades[-limit:]


# ── Singleton Instance ────────────────────────────────────────────────────────

_trader = None

def get_trader() -> LiveTrader:
    """Get or create the singleton trader instance."""
    global _trader
    if _trader is None:
        _trader = LiveTrader()
    return _trader


def kill_switch(reason: str = "manual") -> dict:
    """Hard kill-switch — the single button/voice command that makes everything
    stop trading NOW.

    Actions, in order:
      1. mode -> paper FIRST (nothing new can fire while we work)
      2. cancel every open order (both markets)
      3. flatten every open futures position (market order, opposite side)
      4. flatten every spot balance with a USDT pair (above min-notional)
      5. log everything to live_trades.jsonl

    Returns a summary dict - every failure is listed, never swallowed.
    Deliberately NOT instant-perfect: an unreachable exchange is reported,
    not hidden - the summary tells you exactly what survived.
    """
    trader = get_trader()
    summary = {
        "timestamp": _now_iso(),
        "reason": reason,
        "mode_set_to": "paper",
        "cancelled_orders": [],
        "failed_cancels": [],
        "flattened": [],
        "failed_flattens": [],
        "skipped_markets": [],
    }

    # 1. Mode -> paper FIRST (blocks any new live signal mid-flatten).
    try:
        trader.safety.update_settings(mode="paper")
    except Exception:
        summary["mode_set_to"] = "FAILED"

    # Reuse the trader's per-market singletons when present (tests, and any
    # caller that already pinned a client); otherwise build from config.
    clients: dict = getattr(trader, "_clients", None) or {}

    for market in ("futures", "spot"):
        client = clients.get(market)
        if client is not None:
            is_live = bool(client.api_key)
        else:
            client, is_live = trader._client_for(market)
        if not is_live:
            summary["skipped_markets"].append(market)
            continue

        # 2. Cancel every open order.
        try:
            for o in client.get_open_orders():
                sym = o.get("symbol", "?")
                try:
                    client.cancel_order(sym, o.get("orderId"))
                    summary["cancelled_orders"].append(f"{market}:{sym}#{o.get('orderId')}")
                except Exception as ce:
                    summary["failed_cancels"].append(f"{market}:{sym}: {ce}")
        except Exception as e:
            summary["failed_cancels"].append(f"{market}:list: {e}")

        if market == "futures":
            # 3. Flatten via positionRisk (sign of positionAmt gives the side).
            try:
                for pos in client.get_positions():
                    sym = pos.get("symbol", "?")
                    try:
                        amt = float(pos.get("positionAmt", 0))
                        if abs(amt) < 1e-12:
                            continue
                        side = "SELL" if amt > 0 else "BUY"
                        client.create_market_order(sym, side, abs(amt))
                        summary["flattened"].append(f"{market}:{sym} {side} {abs(amt)}")
                    except Exception as fe:
                        summary["failed_flattens"].append(f"{market}:{sym}: {fe}")
            except Exception as e:
                summary["failed_flattens"].append(f"{market}:list: {e}")
        else:
            # 4. Spot: sell every asset with a USDT pair, above min-notional.
            try:
                account = client.get_account()
                for bal in account.get("balances", []):
                    asset = bal.get("asset", "")
                    try:
                        free = float(bal.get("free", 0))
                        if free <= 0 or asset in ("USDT", ""):
                            continue
                        pair = f"{asset}USDT"
                        try:
                            price = float(client.get_ticker(pair).get("price", 0))
                        except Exception:
                            continue   # no USDT pair -> nothing to do for this asset
                        if free * price < 10:
                            continue   # below Binance min-notional
                        client.create_market_order(pair, "SELL", free)
                        summary["flattened"].append(f"{market}:{pair} SELL {free}")
                    except Exception as fe:
                        summary["failed_flattens"].append(f"{market}:{asset}: {fe}")
            except Exception as e:
                summary["failed_flattens"].append(f"{market}:account: {e}")

    _append_jsonl(_TRADE_LOG, {"kill_switch": True, **summary})
    return summary


def kill_switch_result_text(summary: dict) -> str:
    """Human-readable kill-switch report for voice/dashboard rendering."""
    icon = "\u26d4"   # no-entry
    lines = [f"{icon} KILL SWITCH executed ({summary.get('reason', 'manual')})",
             f"   Mode: {summary.get('mode_set_to', '?').upper()}"]
    if summary.get("cancelled_orders"):
        lines.append(f"   Orders cancelled: {len(summary['cancelled_orders'])}")
    if summary.get("flattened"):
        lines.append("   Positions flattened:")
        lines += [f"     {f}" for f in summary["flattened"]]
    if summary.get("failed_cancels") or summary.get("failed_flattens"):
        lines.append("   \u26a0\ufe0f Failures (need manual attention):")
        lines += [f"     {f}" for f in summary["failed_cancels"]]
        lines += [f"     {f}" for f in summary["failed_flattens"]]
    if summary.get("skipped_markets"):
        lines.append(f"   Skipped (no keys/market): {', '.join(summary['skipped_markets'])}")
    return "\n".join(lines)


def status() -> str:
    """Get trading status as readable string."""
    trader = get_trader()
    client, is_live = trader._ensure_client()
    safety = trader.safety

    lines = ["=== TRADING STATUS ==="]

    market = str(safety.config.get("market", "spot")).lower()
    lines.append(f"  Market: {market.upper()}" + ("  (USDⓈ-M perpetuals)" if market == "futures" else ""))

    # Account
    lines.append("\n--- ACCOUNT ---")
    if is_live:
        try:
            account = client.get_account()
            lines.append(f"  Binance account: {account.get('canTrade', 'unknown')}")
            bal = account.get("balances", [])
            usdt_bal = next((float(b["free"]) for b in bal if b["asset"] == "USDT"), 0)
            lines.append(f"  USDT Balance: ${usdt_bal:,.2f}")
        except Exception as e:
            lines.append(f"  Account: error ({e})")
    else:
        lines.append("  No API keys configured → paper trading only")

    # Mode
    mode = safety.config.get("mode", "paper")
    mode_display = "🟢 LIVE TRADING" if mode == "live" else "📝 PAPER TRADING"
    lines.append(f"\n--- MODE: {mode_display} ---")

    # Safety
    lines.append("\n--- SAFETY LIMITS ---")
    lines.append(f"  Daily loss limit: {safety.config.get('max_daily_loss_pct', 2):.1f}%")
    lines.append(f"  Max position: ${safety.config.get('max_position_usd', 1000):,.0f}")
    lines.append(f"  Max trades/day: {safety.config.get('max_daily_trades', 10)}")
    lines.append(f"  Cooldown: {safety.config.get('cooldown_seconds', 300)}s")
    lines.append(f"  Confirmation required: {safety.config.get('require_confirmation', True)}")

    # Daily stats
    lines.append("\n--- DAILY STATS ---")
    lines.append(f"  Trades today: {safety._daily_trades}")
    lines.append(f"  Daily P&L: {safety._daily_pnl:+.2f}%")

    # Open positions
    if market == "futures" and is_live:
        try:
            fpos = client.get_positions()
            if fpos:
                lines.append("\n--- OPEN FUTURES POSITIONS ---")
                for p in fpos:
                    amt   = float(p.get("positionAmt", 0))
                    side  = "LONG" if amt > 0 else "SHORT"
                    entry = float(p.get("entryPrice", 0) or 0)
                    mark  = float(p.get("markPrice", 0) or 0)
                    upnl  = float(p.get("unRealizedProfit", 0) or 0)
                    lev   = p.get("leverage", "?")
                    lines.append(f"  {p.get('symbol')}: {side} {abs(amt)} @ ${entry:.2f} "
                                 f"(mark ${mark:.2f}, {lev}x, PnL {upnl:+.2f} USDT)")
        except Exception as e:
            lines.append(f"  Futures positions: error ({e})")
    positions = trader.get_positions()
    if positions:
        lines.append("\n--- OPEN POSITIONS ---")
        total_usdt = 0
        for asset, pos in positions.items():
            lines.append(f"  {asset}: {pos['free']:.6f} (~${pos['usdt_value']:.2f})")
            total_usdt += pos['usdt_value']
        lines.append(f"  Total: ${total_usdt:,.2f}")

    # Recent trades
    trades = trader.get_recent_trades(5)
    if trades:
        lines.append("\n--- RECENT TRADES ---")
        for t in trades[-5:]:
            side_icon = "▲" if t.get("side") == "BUY" else "▼"
            lines.append(f"  {side_icon} {t.get('symbol', '?')} {t.get('side', '?')} "
                        f"@{t.get('price', '?')} | {t.get('timestamp', '?')} | "
                        f"status: {t.get('status', '?')}")

    return "\n".join(lines)


def configure(
    mode: str = None,
    max_daily_loss: float = None,
    max_position: float = None,
    max_daily_trades: int = None,
    cooldown: int = None,
    require_confirmation: bool = None,
    market: str = None,
    futures_leverage: int = None,
) -> str:
    """Configure trading safety settings."""
    trader = get_trader()
    safety = trader.safety

    updates = {}
    if mode is not None:
        if mode not in ("paper", "live"):
            return f"Invalid mode '{mode}'. Use 'paper' or 'live'."
        updates["mode"] = mode

    if market is not None:
        if market not in ("spot", "futures"):
            return f"Invalid market '{market}'. Use 'spot' or 'futures'."
        updates["market"] = market

    if futures_leverage is not None:
        lev = max(1, min(int(futures_leverage), 20))
        updates["futures_leverage"] = lev
        # A leverage cap is a safety setting — keep require_confirmation on
        # unless the user manages it explicitly.

    if max_daily_loss is not None:
        updates["max_daily_loss_pct"] = max_daily_loss
    if max_position is not None:
        updates["max_position_usd"] = max_position
    if max_daily_trades is not None:
        updates["max_daily_trades"] = max_daily_trades
    if cooldown is not None:
        updates["cooldown_seconds"] = cooldown
    if require_confirmation is not None:
        updates["require_confirmation"] = require_confirmation

    safety.update_settings(**updates)

    mode_display = "LIVE" if safety.config["mode"] == "live" else "PAPER"
    market_display = str(safety.config.get("market", "spot")).upper()
    lev_display = safety.config.get("futures_leverage", 2)
    return (f"Trading configured: {mode_display} mode on {market_display}"
            + (f" (leverage cap {lev_display}x)" if market_display == "FUTURES" else "") + "\n"
            f"  Max daily loss: {safety.config['max_daily_loss_pct']}%\n"
            f"  Max position: ${safety.config['max_position_usd']}\n"
            f"  Max trades/day: {safety.config['max_daily_trades']}\n"
            f"  Cooldown: {safety.config['cooldown_seconds']}s\n"
            f"  Confirmation required: {safety.config['require_confirmation']}")


def enable_live_trading() -> str:
    """Enable live trading mode."""
    trader = get_trader()
    api_key, api_secret = _get_binance_keys()

    if not api_key or not api_secret:
        return ("Cannot enable live trading: no Binance API keys found.\n"
                "Add to config/api_keys.json:\n"
                '  {"binance_api_key": "your_key", "binance_api_secret": "your_secret"}')

    safety = trader.safety
    safety.update_settings(mode="live")

    # Verify API works
    client, _ = trader._ensure_client()
    try:
        account = client.get_account()
        lines = [
            "✓ Live trading enabled and verified",
            f"  Account: {account.get('accountId', 'unknown')}",
            "  ⚠️ WARNING: Real money will be traded. Proceed with caution.",
            "  Set require_confirmation=true to review trades before execution."
        ]
        return "\n".join(lines)
    except Exception as e:
        return f"Live trading enabled but API verification failed: {e}"


def disable_live_trading() -> str:
    """Disable live trading (switch to paper mode)."""
    trader = get_trader()
    trader.safety.update_settings(mode="paper")
    return "Live trading disabled. Switched to paper trading mode."


def trade(
    symbol: str,
    direction: str,
    entry_price: float,
    stop_loss: float,
    targets: list[float],
    position_size: float,
    capital: float,
    atr: float,
    signal_score: float,
    rules_votes: dict = None
) -> str:
    """
    Execute a trade based on strategy signal.
    This is the main entry point from trading_strategy.
    """
    trader = get_trader()

    # Normalize symbol for Binance (BTCUSDT format)
    binance_symbol = symbol.replace("-USD", "USDT").replace("-USDT", "USDT")
    if not binance_symbol.endswith("USDT"):
        binance_symbol = f"{binance_symbol}USDT"

    result = trader.execute_trade(
        symbol=binance_symbol,
        direction=direction,
        entry_price=entry_price,
        stop_loss=stop_loss,
        targets=targets,
        position_size_usd=position_size,
        capital=capital,
        atr=atr,
        signal_score=signal_score,
        rules_votes=rules_votes
    )

    # Format response
    status_emoji = {
        "executed": "✅",
        "paper_executed": "📝",
        "pending_confirmation": "⏳",
        "blocked": "🚫",
        "error": "❌"
    }.get(result["status"], "❓")

    response = f"{status_emoji} {direction} {symbol}"

    if result["status"] == "executed":
        d = result["details"]
        response += (f"\n   Order: {d['order_id']} | {d['side']} {d['quantity']} @ ~${d['price']:.2f}"
                    f"\n   Stop: ${d['stop_loss']:.2f} | Targets: {', '.join(f'${t:.2f}' for t in d['targets'])}")
        response += f"\n   Estimated value: ${d['quantity'] * d['price']:,.2f}"

    elif result["status"] == "paper_executed":
        d = result["details"]
        response += (f"\n   PAPER TRADE: {d['side']} {d['quantity']} {symbol} @ ~${d['price']:.2f}"
                    f"\n   Stop: ${d['stop_loss']:.2f} | Targets: {', '.join(f'${t:.2f}' for t in d['targets'])}")
        response += "\n   (Set mode to 'live' to execute real orders)"

    elif result["status"] == "pending_confirmation":
        d = result["details"]
        response += (f"\n   Awaiting confirmation:\n"
                    f"   {d['side']} {d['quantity']} {symbol} @ ~${d['price']:.2f}\n"
                    f"   Stop: ${d['stop_loss']:.2f} | Targets: {', '.join(f'${t:.2f}' for t in d['targets'])}")
        response += "\n   Say 'confirm trade' to execute or 'cancel trade' to abort."

    elif result["status"] == "blocked":
        response += f"\n   Blocked: {result.get('reason', 'unknown')}"

    elif result["status"] == "error":
        response += f"\n   Error: {result.get('reason', 'unknown')}"

    return response


def confirm_trade(symbol: str) -> str:
    """Confirm a pending trade."""
    trader = get_trader()
    safety = trader.safety
    safety.update_settings(require_confirmation=False)
    return f"Confirmation enabled for next trade. Execute: {symbol}"


def cancel_trade(symbol: str) -> str:
    """Cancel pending trade."""
    trader = get_trader()
    safety = trader.safety
    safety.update_settings(require_confirmation=True)
    return f"Trade cancelled. {symbol} will require confirmation next time."


def close(symbol: str) -> str:
    """Close an open position."""
    trader = get_trader()
    result = trader.close_position(symbol)

    status_emoji = {
        "closed": "✅",
        "no_position": "📭",
        "error": "❌"
    }.get(result["status"], "❓")

    response = f"{status_emoji} Close {symbol}: {result['status']}"
    if result.get("reason"):
        response += f" — {result['reason']}"
    if result.get("details"):
        d = result["details"]
        response += f"\n   Order: {d.get('order_id')} | {d.get('quantity')} @ ${d.get('price', 0):.2f}"

    return response


def test_api() -> str:
    """Test Binance API connection (uses the configured market)."""
    trader = get_trader()
    market = str(trader.safety.config.get("market", "spot")).lower()
    client, _ = trader._ensure_client()

    lines = [f"=== BINANCE API TEST ({market.upper()}) ==="]

    # Test market data (no auth needed)
    lines.append("\n1. Market Data (public):")
    try:
        ticker = client.get_ticker("BTCUSDT")
        price = ticker.get("price", "?")
        lines.append(f"   ✓ BTC price: ${price}")
    except Exception as e:
        lines.append(f"   ✗ Failed: {e}")

    # Test account (auth needed)
    lines.append("\n2. Account Access:")
    api_key, api_secret = _get_binance_keys()
    if api_key and api_secret:
        try:
            account = client.get_account()
            lines.append(f"   ✓ Connected as: {account.get('accountId', 'unknown')}")
            bal = account.get("balances", [])
            usdt = next((float(b["free"]) for b in bal if b["asset"] == "USDT"), 0)
            lines.append(f"   ✓ USDT Balance: ${usdt:,.2f}")
            if market == "futures":
                fpos = client.get_positions()
                lines.append(f"   ✓ Open futures positions: {len(fpos)}")
            lines.append("\n   API is working! Use 'configure mode=live' to enable real trading.")
        except Exception as e:
            msg = str(e)
            lines.append(f"   ✗ Auth failed: {msg}")
            # Parse Binance's numeric error code so the advice is targeted
            # instead of a raw blob — the three common failures have
            # completely different fixes.
            if "-2014" in msg or "-2015" in msg or "Invalid API-key" in msg:
                lines.append("   → API key itself is wrong/revoked, or IP is not on the key's")
                lines.append("     allowlist, or the key lacks trading permission. Binance →")
                lines.append("     API Management → enable 'Enable Reading' + 'Enable Spot &")
                lines.append("     Margin Trading' (and 'Enable Futures' for futures mode), and")
                lines.append("     add this machine's IP under 'Restrict access' if set.")
            elif "-1022" in msg or "Signature" in msg:
                lines.append("   → Signature mismatch: the SECRET doesn't match this key.")
                lines.append("     Re-copy the secret from Binance (it's shown only once) into")
                lines.append("     config/api_keys.json — no quotes/spaces/newlines around it.")
            elif "-1021" in msg or "Timestamp" in msg:
                lines.append("   → Clock skew: this machine's time is off. The client now")
                lines.append("     auto-syncs to Binance's server clock — retry the test.")
            else:
                lines.append("   Check your API keys in config/api_keys.json")
    else:
        lines.append("   ✗ No API keys configured")
        lines.append("   Add to config/api_keys.json:")
        lines.append('   {"binance_api_key": "xxx", "binance_api_secret": "yyy"}')

    return "\n".join(lines)


# ── Plugin Registration ───────────────────────────────────────────────────────

PLUGIN = {
    "name": "binance_trader",
    "description": (
        "Automated crypto trading on Binance. Executes real trades based on "
        "trading strategy signals. Requires Binance API keys in config/api_keys.json. "
        "Use action=test to verify connection, status to see account state, "
        "configure to adjust safety limits, trade to execute a trade, close to "
        "close a position. Default mode is PAPER (no real orders) until you "
        "explicitly enable live trading with configure mode=live.\n\n"
        "Safety features: daily loss limits, position size caps, trade cooldowns, "
        "and confirmation requirement for live trades. All trades logged for audit."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "test | status | configure | enable | disable | trade | confirm | cancel | close | kill (emergency: flatten everything + paper mode)"
            },
            "symbol": {"type": "STRING", "description": "Trading pair: BTCUSDT, ETHUSDT, etc."},
            "direction": {"type": "STRING", "description": "LONG or SHORT"},
            "mode": {"type": "STRING", "description": "paper or live (for configure)"},
            "market": {"type": "STRING", "description": "spot or futures (for configure; futures = USDT-M perpetuals)"},
            "futures_leverage": {"type": "NUMBER", "description": "Futures leverage cap 1-20 (for configure)"},
            "max_daily_loss": {"type": "NUMBER", "description": "Max daily loss %"},
            "max_position": {"type": "NUMBER", "description": "Max position USD"},
            "max_daily_trades": {"type": "NUMBER", "description": "Max trades per day"},
            "cooldown": {"type": "NUMBER", "description": "Cooldown seconds between trades"},
            "require_confirmation": {"type": "BOOLEAN", "description": "Require confirmation for live trades"},
        },
        "required": ["action"],
    },
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Plugin entry point."""
    action = str(parameters.get("action") or "status").lower().strip()

    try:
        if action == "test":
            return test_api()

        elif action == "status":
            return status()

        elif action == "configure":
            return configure(
                mode=parameters.get("mode"),
                max_daily_loss=parameters.get("max_daily_loss"),
                max_position=parameters.get("max_position"),
                max_daily_trades=parameters.get("max_daily_trades"),
                cooldown=parameters.get("cooldown"),
                require_confirmation=parameters.get("require_confirmation"),
                market=parameters.get("market"),
                futures_leverage=parameters.get("futures_leverage"),
            )

        elif action == "enable":
            return enable_live_trading()

        elif action == "disable":
            return disable_live_trading()

        elif action == "trade":
            symbol = str(parameters.get("symbol") or "").upper()
            if not symbol:
                return "Error: symbol required for trade"

            direction = str(parameters.get("direction") or "LONG").upper()
            if direction not in ("LONG", "SHORT"):
                direction = "LONG"

            try:
                entry_price = float(parameters.get("entry_price") or 0)
                stop_loss = float(parameters.get("stop_loss") or 0)
                position_size = float(parameters.get("position_size") or 100)
                capital = float(parameters.get("capital") or 10000)
                atr = float(parameters.get("atr") or 0)
                signal_score = float(parameters.get("signal_score") or 0)
            except (TypeError, ValueError):
                return "Error: Invalid numeric parameters"

            targets = []
            for t in parameters.get("targets", []):
                try:
                    targets.append(float(t))
                except (TypeError, ValueError):
                    pass

            return trade(
                symbol=symbol,
                direction=direction,
                entry_price=entry_price,
                stop_loss=stop_loss,
                targets=targets,
                position_size=position_size,
                capital=capital,
                atr=atr,
                signal_score=signal_score,
                rules_votes=parameters.get("rules_votes")
            )

        elif action == "confirm":
            symbol = str(parameters.get("symbol") or "")
            return confirm_trade(symbol)

        elif action == "cancel":
            symbol = str(parameters.get("symbol") or "")
            return cancel_trade(symbol)

        elif action == "close":
            symbol = str(parameters.get("symbol") or "")
            if not symbol:
                return "Error: symbol required to close position"
            return close(symbol)

        elif action == "kill":
            summary = kill_switch(reason=str(parameters.get("reason") or "voice command"))
            return kill_switch_result_text(summary)

        else:
            return (f"Unknown action '{action}'. Use: test, status, configure, "
                    "enable, disable, trade, confirm, cancel, close")

    except Exception as e:
        return f"Trading error: {e}"
