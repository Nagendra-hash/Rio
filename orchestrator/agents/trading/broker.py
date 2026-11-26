"""
orchestrator/agents/trading/broker.py — the EXECUTION stage.

SEPARATION OF CONCERNS (the phase brief's hard rule):
    data.py knows the market (read-only over it, no orders);
    broker.py knows orders (no market views, no data adapters).
    The trading agent reads the market through data.MarketData and executes
    ONLY through this module.

Two execution paths, both behind the risk + human approval gates:
    * PaperBroker     — default; writes paper_positions.json / paper_journal.
    * DeltaLiveBroker — opt-in only; signs Delta India private requests. It is
      constructed by execute_approved() ONLY when the separate live switch
      (memory/trading/live_execution.json) is enabled, credentials exist, the
      kill switch is clear, and a human approved the specific proposal.
      Auto Trade is refused unless allow_auto is explicitly true.

`LiveBroker` remains as a hard-fail compatibility symbol: old code that
constructed it must still fail closed. The real implementation is
`DeltaLiveBroker`, which is never constructed by accident.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

from orchestrator.agents.trading.risk import (
    load_live, live_halted, load_safety, record_live_order, trading_dir,
)
from orchestrator.agents.trading.delta_private import (
    DeltaPrivateClient, DeltaPrivateError,
)


# ── order request / result ───────────────────────────────────────────────────

class OrderError(RuntimeError):
    pass


class PaperBroker:
    """Simulated fills at the served quote's last price with a configurable
    slippage (basis points), persisted to the paper files."""
    name = "paper"

    def __init__(self, slip_bps: float = 2.0) -> None:
        self.slip_bps = float(slip_bps)

    # file paths
    @staticmethod
    def _positions_path() -> Path:
        return trading_dir() / "paper_positions.json"

    @staticmethod
    def _journal_path() -> Path:
        return trading_dir() / "paper_journal.jsonl"

    def open_position(self, *, symbol: str, direction: str, entry: float,
                      stop_loss: float, targets: list[float], units: int,
                      strategy: str, signal_score: int,
                      approved_by: str, signal_id: str = "",
                      atr: float = 0.0, venue: str = "",
                      product_id: int | None = None,
                      contract_type: str = "",
                      quote_currency: str = "",
                      contract_value: float | None = None,
                      market_class: str = "") -> dict:
        """Open a PAPER position. Caller must have passed the risk check and
        obtained HUMAN APPROVAL — this method trusts the pipeline, and the
        pipeline is the only caller."""
        direction = direction.upper()
        if direction not in ("LONG", "SHORT"):
            raise OrderError(f"direction '{direction}' invalid")
        if units < 1:
            raise OrderError("units < 1")
        if entry <= 0 or stop_loss <= 0:
            raise OrderError("entry/stop must be positive")

        slip = entry * self.slip_bps / 10_000.0
        fill = entry + slip if direction == "LONG" else entry - slip

        pos = {
            "symbol": symbol, "method": strategy, "direction": direction,
            "entry_price": round(fill, 6), "stop_loss": float(stop_loss),
            "targets": [float(t) for t in targets],
            "position_size": int(units), "capital_at_entry": 0.0,
            "atr": float(atr or 0.0),
            "open_time": _now_iso(), "last_update": _now_iso(),
            "status": "open", "signal_score": int(signal_score),
            "approved_by": approved_by, "signal_id": str(signal_id),
            "slip_bps": self.slip_bps, "paper": True,
            "hits": [False] * len(targets),
            # Venue metadata keeps a Delta paper fill distinguishable from a
            # Yahoo/Binance paper fill with the same ticker. Fills remain
            # simulated; no broker/account client is involved.
            "venue": str(venue or ""),
            "product_id": product_id,
            "contract_type": str(contract_type or ""),
            "quote_currency": str(quote_currency or ""),
            "contract_value": contract_value,
            "market_class": str(market_class or ""),
        }
        positions = self._load_positions()
        key = (f"{venue}:{symbol}:{strategy}" if venue
               else f"{symbol}:{strategy}")
        positions[key] = pos
        self._save_positions(positions)

        rec = {"ts": _now_iso(), "action": "PAPER_OPEN", "key": key,
               "symbol": symbol, "direction": direction,
               "entry": pos["entry_price"], "stop": pos["stop_loss"],
               "targets": pos["targets"], "units": int(units),
               "strategy": strategy, "signal_score": int(signal_score),
               "approved_by": approved_by, "signal_id": signal_id,
               "venue": str(venue or ""), "product_id": product_id,
               "contract_type": str(contract_type or ""),
               "quote_currency": str(quote_currency or ""),
               "contract_value": contract_value,
               "market_class": str(market_class or ""),
               "mode": "paper"}
        self._append_journal(rec)
        return {"ok": True, "key": key, "position": pos, "fill": fill,
                "mode": "paper"}

    def close_position(self, key: str, exit_price: float,
                       reason: str) -> dict:
        """Close a paper position and journal the fill."""
        positions = self._load_positions()
        pos = positions.get(key)
        if not pos or pos.get("status") != "open":
            raise OrderError(f"no open paper position {key!r}")
        pos["status"] = "closed"
        pos["exit_price"] = round(float(exit_price), 6)
        pos["closed_reason"] = str(reason)
        pos["closed_time"] = _now_iso()
        sign = 1.0 if pos["direction"] == "LONG" else -1.0
        multiplier = float(pos.get("contract_value") or 1.0)
        pos["realized_pnl"] = round(sign * (exit_price - pos["entry_price"])
                                    * pos["position_size"] * multiplier, 4)
        pos["realized_pnl_pct"] = round(sign *
                                        (exit_price / pos["entry_price"] - 1.0)
                                        * 100.0, 4)
        pos["last_update"] = _now_iso()
        self._save_positions(positions)
        self._append_journal({"ts": _now_iso(), "action": "PAPER_CLOSE",
                              "key": key, "exit": pos["exit_price"],
                              "reason": str(reason),
                              "pnl": pos["realized_pnl"], "mode": "paper"})
        return {"ok": True, "key": key, "pnl": pos["realized_pnl"]}

    def open_positions(self) -> dict:
        return {k: v for k, v in self._load_positions().items()
                if v.get("status") == "open"}

    # ── file plumbing ─────────────────────────────────────────────────────
    def _load_positions(self) -> dict:
        try:
            data = json.loads(self._positions_path().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_positions(self, data: dict) -> None:
        p = self._positions_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        tmp.replace(p)                      # atomic-ish swap

    def _append_journal(self, rec: dict) -> None:
        p = self._journal_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")


class LiveBroker:
    """Compatibility hard-fail symbol. Kept deliberately.

    Older callers/tests assert that constructing ``LiveBroker`` fails closed.
    The actual opt-in implementation is ``DeltaLiveBroker`` below; it is only
    constructed by execute_approved() after every live gate passes.
    """

    def __init__(self, *args, **kwargs):
        raise OrderError(
            "LiveBroker is disabled by construction — use DeltaLiveBroker "
            "through the explicitly-armed live execution path.")

    name = "live"


class DeltaLiveBroker:
    """Signed Delta India execution: limit-IOC entry + bracket SL/TP.

    This class does NOT decide whether live trading is allowed. The caller
    (execute_approved) has already checked the live switch, credentials, the
    kill switch and human approval. This class adds a second layer of checks
    and refuses if an order would exceed the live notional cap.

    On a filled entry it immediately attaches a bracket. If the bracket fails,
    it attempts a reduce-only market close; the result is never reported as a
    protected success when either leg failed.
    """

    name = "delta_live"

    def __init__(self, *, environment: str | None = None,
                 client: DeltaPrivateClient | None = None) -> None:
        cfg = load_live()
        self.environment = str(environment or cfg.get("environment") or "demo")
        if self.environment not in ("demo", "prod"):
            raise OrderError("live environment must be demo or prod")
        self.live = cfg
        self.client = client or DeltaPrivateClient(self.environment)

    # ── paths ─────────────────────────────────────────────────────────────
    @staticmethod
    def _journal_path() -> Path:
        return trading_dir() / "live_journal.jsonl"

    def _journal(self, rec: dict) -> None:
        p = self._journal_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    # ── execution ─────────────────────────────────────────────────────────
    def open_position(self, *, symbol: str, direction: str, entry: float,
                      stop_loss: float, targets: list[float], units: int,
                      strategy: str, signal_score: int,
                      approved_by: str, signal_id: str = "",
                      atr: float = 0.0, venue: str = "",
                      product_id: int | None = None,
                      contract_type: str = "",
                      quote_currency: str = "",
                      contract_value: float | None = None,
                      market_class: str = "",
                      product_symbol: str = "",
                      tick_size: float | None = None) -> dict:
        direction = str(direction or "").upper()
        base = {
            "ok": False, "mode": "live", "environment": self.environment,
            "symbol": symbol, "approved_by": approved_by,
        }
        if not self.client.credentials_configured():
            return {**base, "error": "Delta API key/secret are not configured"}
        if live_halted():
            return {**base, "error": "HALT kill switch is tripped — live order refused"}
        if direction not in ("LONG", "SHORT"):
            return {**base, "error": f"direction '{direction}' invalid"}
        try:
            units = int(units)
        except (TypeError, ValueError):
            return {**base, "error": "contract size is not an integer"}
        if units < 1:
            return {**base, "error": "contract size < 1"}
        if float(entry) <= 0 or float(stop_loss) <= 0:
            return {**base, "error": "entry/stop must be positive"}
        cv = float(contract_value or 0.0)
        if cv <= 0:
            return {**base, "error": "Delta contract_value is required for live sizing"}
        psym = str(product_symbol or symbol or "").strip().upper()
        if not psym:
            return {**base, "error": "Delta product symbol is required"}

        # Belt-and-braces notional cap: sizing already applied this, but a
        # caller bug must not turn into an oversized real order. A live order
        # must always have a finite positive cap.
        live_cap = float(self.live.get("max_order_usd") or 0.0)
        if live_cap <= 0:
            return {**base,
                    "error": "live_execution.json max_order_usd must be > 0"}
        notional = float(entry) * cv * units
        caps = [live_cap]
        try:
            caps.append(float(load_safety().get("max_position_usd") or 0.0))
        except Exception:
            pass
        caps = [c for c in caps if c > 0]
        if notional > min(caps) + 1e-9:
            return {**base,
                    "error": (f"live notional {notional:.2f} exceeds cap "
                              f"{min(caps):.2f} — order refused")}

        side = "buy" if direction == "LONG" else "sell"
        cid = f"jarvis-{str(signal_id or 'sig')[:10]}-{int(time.time())}"[:32]
        order = {
            "product_symbol": psym,
            "size": units,
            "side": side,
            "order_type": "limit_order",
            "limit_price": _as_entry_price(entry, tick_size, side),
            "time_in_force": "ioc",
            "reduce_only": False,
            "client_order_id": cid,
        }
        self._journal({
            "ts": _now_iso(), "action": "LIVE_SUBMIT", "mode": "live",
            "environment": self.environment, "symbol": psym,
            "direction": direction, "size": units, "entry": float(entry),
            "stop": float(stop_loss), "targets": [float(t) for t in targets],
            "notional": round(notional, 4), "approved_by": approved_by,
            "signal_id": signal_id, "client_order_id": cid,
        })
        try:
            response = self.client.place_order(order)
        except DeltaPrivateError as exc:
            self._journal({"ts": _now_iso(), "action": "LIVE_REJECTED",
                           "mode": "live", "environment": self.environment,
                           "symbol": psym, "error": str(exc)})
            return {**base, "error": str(exc), "client_order_id": cid}

        result = response.get("result") if isinstance(response, dict) else None
        if not isinstance(result, dict):
            return {**base, "error": "Delta returned no order result",
                    "client_order_id": cid, "raw": response}
        order_id = result.get("id")
        state = str(result.get("state") or "").lower()
        reported_size = _int_or(result.get("size"), units)
        unfilled = _int_or(result.get("unfilled_size"), 0)
        filled = max(0, reported_size - unfilled)

        # An IOC entry must not remain resting. If the exchange says "open",
        # try to cancel it and account for what already filled.
        cancelled = False
        cancel_error = ""
        if state == "open" and order_id is not None:
            try:
                self.client.cancel_order(int(order_id))
                cancelled = True
            except DeltaPrivateError as exc:
                cancel_error = str(exc)
                self._journal({"ts": _now_iso(), "action": "LIVE_CANCEL_FAILED",
                               "mode": "live", "order_id": order_id,
                               "error": str(exc)})

        # A cancelled IOC may still have a partial fill: treat any filled
        # contracts as a real position and protect them. Only zero-filled or
        # rejected orders are "not filled".
        if filled < 1 or state == "rejected":
            self._journal({"ts": _now_iso(), "action": "LIVE_NOT_FILLED",
                           "mode": "live", "environment": self.environment,
                           "symbol": psym, "order_id": order_id,
                           "state": state, "filled": filled,
                           "unfilled": unfilled, "cancelled": cancelled})
            return {**base, "ok": False, "status": "not_filled",
                    "order_id": order_id, "state": state,
                    "filled_size": filled,
                    "error": "live entry order did not fill — nothing was opened"}

        fill_price = _f(result.get("average_fill_price")) or float(entry)
        bracket_ok = False
        bracket_error = ""
        bracket_res: dict = {}
        try:
            bracket_res = self.client.place_bracket(
                psym, side=side, stop_loss=float(stop_loss),
                take_profit=float(targets[0]), tick_size=tick_size)
            bracket_ok = bool(bracket_res.get("success", True))
            if not bracket_ok:
                bracket_error = str(bracket_res.get("error") or "bracket rejected")
        except (DeltaPrivateError, IndexError, TypeError) as exc:
            bracket_error = str(exc)

        emergency = None
        if not bracket_ok:
            # Never leave a filled live position unprotected. Close it with a
            # reduce-only market order; report the exact outcome.
            try:
                emergency = self.client.emergency_close(
                    psym, side=side, size=filled)
                emergency = {"ok": True, "response": emergency}
            except DeltaPrivateError as exc:
                emergency = {"ok": False, "error": str(exc)}
                self._journal({"ts": _now_iso(),
                               "action": "LIVE_UNPROTECTED",
                               "mode": "live", "order_id": order_id,
                               "symbol": psym, "size": filled,
                               "error": str(exc)})

        try:
            record_live_order(psym)
        except Exception:
            pass
        rec = {
            "ts": _now_iso(), "action": "LIVE_FILLED", "mode": "live",
            "environment": self.environment, "symbol": psym,
            "order_id": order_id, "state": state, "side": side,
            "size": filled, "entry": fill_price,
            "notional": round(fill_price * cv * filled, 4),
            "bracket_ok": bracket_ok, "bracket_error": bracket_error,
            "emergency_close": emergency, "approved_by": approved_by,
            "signal_id": signal_id, "client_order_id": cid,
            "cancelled": cancelled, "cancel_error": cancel_error,
        }
        self._journal(rec)
        if not bracket_ok:
            return {**base, "ok": False, "status": "bracket_failed",
                    "order_id": order_id, "filled_size": filled,
                    "entry_price": fill_price, "bracket_error": bracket_error,
                    "emergency_close": emergency,
                    "error": ("entry filled but bracket failed; "
                              + ("position was closed" if emergency and
                                 emergency.get("ok") else
                                 "POSITION MAY BE UNPROTECTED"))}
        return {**base, "ok": True, "status": "filled", "order_id": order_id,
                "filled_size": filled, "entry_price": fill_price,
                "stop_loss": float(stop_loss),
                "targets": [float(t) for t in targets],
                "bracket": bracket_res, "bracket_ok": True,
                "notional": round(fill_price * cv * filled, 4),
                "client_order_id": cid, "cancelled": cancelled,
                "cancel_error": cancel_error}


# ── helpers ──────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _f(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def _int_or(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _as_entry_price(value: Any, tick_size: float | None, side: str) -> str:
    """Round an IOC entry limit so it is *marketable* on that side.

    A buy limit rounds UP to the tick (willing to pay one tick more); a sell
    limit rounds DOWN (willing to take one tick less). This keeps the IOC
    entry from being non-marketable purely because of tick rounding.
    """
    v = _f(value)
    if v is None:
        raise OrderError("price is missing or not finite")
    tick = _f(tick_size)
    if tick and tick > 0:
        q = v / tick
        v = (math.ceil(q - 1e-12) if side == "buy"
             else math.floor(q + 1e-12)) * tick
        v = round(v, 12)
    return f"{v:.12f}".rstrip("0").rstrip(".") or "0"


def execute_approved(pipeline_ctx: dict,
                     *, broker: PaperBroker | DeltaLiveBroker | None = None
                     ) -> dict:
    """The only execution entry the agent calls — and only AFTER
    data → analysis → signal → risk → HUMAN APPROVED.

    Default is paper. Live is used only when the separate live switch is
    enabled, credentials are present, HALT is clear and the approval is not
    an auto-trade approval (unless allow_auto was explicitly set).
    """
    cfg = load_safety()
    mode = str(cfg.get("mode", "paper")).lower()
    live = load_live()
    live_on = bool(live.get("enabled"))
    if mode == "live" and not live_on:
        return {"ok": False, "mode": "paper",
                "error": ("safety config mode=live but the trading agent's "
                          "live execution switch is disabled; nothing was "
                          "placed (paper-only)")}
    if live_on:
        if live_halted():
            return {"ok": False, "mode": "live",
                    "error": "HALT kill switch is tripped — nothing was placed"}
        approved_by = str(pipeline_ctx.get("approved_by") or "user")
        if approved_by == "auto-trade" and not live.get("allow_auto"):
            return {"ok": False, "mode": "live",
                    "error": ("Auto Trade cannot place live orders unless "
                              "live_execution.json allow_auto=true. Use manual "
                              "approval on the Trading page.")}
        if broker is not None and not isinstance(broker, DeltaLiveBroker):
            return {"ok": False, "mode": "live",
                    "error": ("live switch is armed but a paper broker was "
                              "supplied — use DeltaLiveBroker")}

    proposal = pipeline_ctx.get("proposal") or {}
    sizing = pipeline_ctx.get("sizing") or {}
    if not proposal or not sizing.get("units"):
        return {"ok": False, "error": "missing proposal or sizing — nothing placed"}

    if live_on:
        if broker is None:
            broker = DeltaLiveBroker()
        if not isinstance(broker, DeltaLiveBroker):
            return {"ok": False, "mode": "live",
                    "error": "live execution requires DeltaLiveBroker"}
        return broker.open_position(
            symbol=str(pipeline_ctx["symbol"]),
            direction=str(proposal["direction"]),
            entry=float(proposal["entry"]),
            stop_loss=float(proposal["stop_loss"]),
            targets=[float(t) for t in proposal.get("targets") or []],
            units=int(sizing["units"]),
            strategy=str(proposal.get("strategy") or "manual"),
            signal_score=int(pipeline_ctx.get("signal_score") or 0),
            approved_by=str(pipeline_ctx.get("approved_by") or "user"),
            signal_id=str(pipeline_ctx.get("signal_id") or ""),
            atr=float((proposal.get("structure_context") or {}).get("atr") or 0.0),
            venue=str(pipeline_ctx.get("venue") or ""),
            product_id=pipeline_ctx.get("product_id"),
            contract_type=str(pipeline_ctx.get("contract_type") or ""),
            quote_currency=str(pipeline_ctx.get("quote_currency") or ""),
            contract_value=pipeline_ctx.get("contract_value"),
            market_class=str(pipeline_ctx.get("market_class") or ""),
            product_symbol=str(pipeline_ctx.get("product_symbol") or ""),
            tick_size=pipeline_ctx.get("tick_size"),
        )

    broker = broker or PaperBroker()
    return broker.open_position(
        symbol=str(pipeline_ctx["symbol"]),
        direction=str(proposal["direction"]),
        entry=float(proposal["entry"]),
        stop_loss=float(proposal["stop_loss"]),
        targets=[float(t) for t in proposal.get("targets") or []],
        units=int(sizing["units"]),
        strategy=str(proposal.get("strategy") or "manual"),
        signal_score=int(pipeline_ctx.get("signal_score") or 0),
        approved_by=str(pipeline_ctx.get("approved_by") or "user"),
        signal_id=str(pipeline_ctx.get("signal_id") or ""),
        atr=float((proposal.get("structure_context") or {}).get("atr") or 0.0),
        venue=str(pipeline_ctx.get("venue") or ""),
        product_id=pipeline_ctx.get("product_id"),
        contract_type=str(pipeline_ctx.get("contract_type") or ""),
        quote_currency=str(pipeline_ctx.get("quote_currency") or ""),
        contract_value=pipeline_ctx.get("contract_value"),
        market_class=str(pipeline_ctx.get("market_class") or ""),
    )
