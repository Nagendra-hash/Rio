"""
dashboard/monitor_api.py — read-only API for the continuous trading monitor.

Routes (shared bearer _auth, same pattern as the other RIO surfaces):

    GET  /api/rio/monitor/health    worker/feed/health counters
    GET  /api/rio/monitor/state     health + per-symbol market snapshot
    GET  /api/rio/monitor/signals   confirmed signals (monitor ledger)
    GET  /api/rio/monitor/lifecycle tracked signal lifecycle (SL / T1 / T2)

There is no order, broker, or live-trading route on this surface — the
monitor itself has no order path.
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse


def register(app, _auth) -> None:
    def _denied(req: Request):
        if not _auth(req):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return None

    @app.get("/api/rio/monitor/health", include_in_schema=False)
    async def monitor_health(req: Request):
        denied = _denied(req)
        if denied:
            return denied
        try:
            from core.trading_monitor import get_monitor
            return JSONResponse({"ok": True,
                                 "trading_monitor": get_monitor().health()})
        except Exception as exc:
            return JSONResponse(
                {"ok": False,
                 "error": f"{type(exc).__name__}: {str(exc)[:240]}"})

    @app.get("/api/rio/monitor/state", include_in_schema=False)
    async def monitor_state(req: Request):
        denied = _denied(req)
        if denied:
            return denied
        try:
            from core.trading_monitor import get_monitor
            return JSONResponse({"ok": True, **get_monitor().state()})
        except Exception as exc:
            return JSONResponse(
                {"ok": False,
                 "error": f"{type(exc).__name__}: {str(exc)[:240]}"})

    @app.get("/api/rio/monitor/signals", include_in_schema=False)
    async def monitor_signals(req: Request, limit: int = 50):
        denied = _denied(req)
        if denied:
            return denied
        try:
            from core.trading_monitor import get_monitor
            return JSONResponse({"ok": True,
                                 "signals": get_monitor().signals(limit=limit)})
        except Exception as exc:
            return JSONResponse(
                {"ok": False,
                 "error": f"{type(exc).__name__}: {str(exc)[:240]}"})

    @app.get("/api/rio/monitor/lifecycle", include_in_schema=False)
    async def monitor_lifecycle(req: Request, limit: int = 50):
        denied = _denied(req)
        if denied:
            return denied
        try:
            from core.trading_monitor import get_monitor
            return JSONResponse({"ok": True, "rows": get_monitor().lifecycle(limit=limit)})
        except Exception as exc:
            return JSONResponse(
                {"ok": False,
                 "error": f"{type(exc).__name__}: {str(exc)[:240]}"})
