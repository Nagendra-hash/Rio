"""
dashboard/liquidity_api.py — read-through API for the liquidity sweep engine.

Routes (shared bearer _auth, same pattern as the other RIO surfaces):

    GET  /api/rio/liquidity/state    zones, state, confirmations, risk, feed
    GET  /api/rio/liquidity/history  sweeps / signals / outcomes
    POST /api/rio/liquidity/zone     {symbol, low, high} manual zone update
    POST /api/rio/liquidity/alerts   {symbol, enabled} toggle reversal alerts

Read-only strategy surface: no order endpoint exists here or in the engine.
All values come from core/liquidity_sweep; unavailable data is reported as
UNAVAILABLE/DISCONNECTED rather than filled in.
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse


def register(app, _auth) -> None:
    def _denied(req: Request):
        if not _auth(req):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return None

    @app.get("/api/rio/liquidity/state", include_in_schema=False)
    async def liquidity_state(req: Request):
        denied = _denied(req)
        if denied:
            return denied
        try:
            from core.liquidity_sweep import get_engine
            state = get_engine().dashboard_state()
            return JSONResponse({"ok": True, **state})
        except Exception as exc:
            return JSONResponse(
                {"ok": False,
                 "error": f"{type(exc).__name__}: {str(exc)[:240]}"})

    @app.get("/api/rio/liquidity/history", include_in_schema=False)
    async def liquidity_history(req: Request, symbol: str = "BTCUSDT",
                                limit: int = 20):
        denied = _denied(req)
        if denied:
            return denied
        try:
            from core.liquidity_sweep import get_engine
            engine = get_engine()
            snapshot = engine.get_status(symbol)
            if not snapshot.get("ok"):
                return JSONResponse(
                    {"ok": False, "error": snapshot.get("error", "unknown symbol")},
                    status_code=400)
            rows = engine.get_history(snapshot["symbol"], limit=limit)
            return JSONResponse({"ok": True, "symbol": snapshot["symbol"],
                                 "rows": rows})
        except Exception as exc:
            return JSONResponse(
                {"ok": False,
                 "error": f"{type(exc).__name__}: {str(exc)[:240]}"})

    @app.post("/api/rio/liquidity/zone", include_in_schema=False)
    async def liquidity_zone(req: Request):
        denied = _denied(req)
        if denied:
            return denied
        try:
            body = await req.json()
        except Exception:
            body = {}
        try:
            from core.liquidity_sweep import get_engine
            snapshot = get_engine().set_zone(
                body.get("symbol") or "BTCUSDT",
                body.get("low"), body.get("high"))
            return JSONResponse({"ok": True, "symbol": snapshot["symbol"],
                                 "zone": snapshot["zone"]})
        except (TypeError, ValueError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)},
                                status_code=400)
        except Exception as exc:
            return JSONResponse(
                {"ok": False,
                 "error": f"{type(exc).__name__}: {str(exc)[:240]}"})

    @app.post("/api/rio/liquidity/alerts", include_in_schema=False)
    async def liquidity_alerts(req: Request):
        denied = _denied(req)
        if denied:
            return denied
        try:
            body = await req.json()
        except Exception:
            body = {}
        enabled = body.get("enabled")
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() in ("1", "true", "yes", "on")
        if enabled is None:
            return JSONResponse({"ok": False, "error": "enabled is required"},
                                status_code=400)
        try:
            from core.liquidity_sweep import get_engine
            snapshot = get_engine().set_alerts(
                body.get("symbol") or "BTCUSDT", bool(enabled))
            return JSONResponse({"ok": True, "symbol": snapshot["symbol"],
                                 "alerts_enabled": snapshot["profile"]["alerts_enabled"]})
        except Exception as exc:
            return JSONResponse(
                {"ok": False,
                 "error": f"{type(exc).__name__}: {str(exc)[:240]}"})
