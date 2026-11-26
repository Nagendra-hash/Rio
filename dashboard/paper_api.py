"""
dashboard/paper_api.py — read/control surface for the PAPER trading engine.

    GET  /api/rio/paper/state       account, strategy, positions, performance
    GET  /api/rio/paper/history     closed paper trades
    POST /api/rio/paper/start       enable new paper entries
    POST /api/rio/paper/stop        stop new entries, keep managing positions
    POST /api/rio/paper/reset       {confirm:true} archive + new paper account

There is deliberately NO order, broker or live-switch endpoint here. PAPER ONLY.
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse


def register(app, _auth) -> None:
    def _denied(req: Request):
        if not _auth(req):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return None

    @app.get("/api/rio/paper/state", include_in_schema=False)
    async def paper_state(req: Request):
        denied = _denied(req)
        if denied:
            return denied
        try:
            from core.paper_trading import get_engine
            return JSONResponse(get_engine().status())
        except Exception as exc:
            return JSONResponse(
                {"ok": False,
                 "error": f"{type(exc).__name__}: {str(exc)[:240]}"},
                status_code=200)

    @app.get("/api/rio/paper/history", include_in_schema=False)
    async def paper_history(req: Request, limit: int = 50):
        denied = _denied(req)
        if denied:
            return denied
        try:
            from core.paper_trading import get_engine
            engine = get_engine()
            return JSONResponse({"ok": True, "paper_only": True,
                                 "rows": engine.history(limit)})
        except Exception as exc:
            return JSONResponse(
                {"ok": False,
                 "error": f"{type(exc).__name__}: {str(exc)[:240]}"},
                status_code=200)

    @app.get("/api/rio/paper/settings", include_in_schema=False)
    async def paper_settings(req: Request):
        denied = _denied(req)
        if denied:
            return denied
        try:
            from core.paper_trading import SETTING_SPECS, get_engine
            engine = get_engine()
            return JSONResponse({
                "ok": True, "paper_only": True,
                "settings": engine.settings_snapshot(),
                "specs": {key: {k: spec.get(k) for k in
                                ("label", "default", "min", "max", "choices",
                                 "applies")}
                          for key, spec in SETTING_SPECS.items()},
                "history": engine.settings_history(20),
            })
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:240]}"},
                status_code=200)

    @app.post("/api/rio/paper/settings", include_in_schema=False)
    async def paper_update_setting(req: Request):
        denied = _denied(req)
        if denied:
            return denied
        try:
            body = await req.json()
        except Exception:
            body = {}
        key = str(body.get("setting") or "").strip()
        if not key or body.get("value") is None:
            return JSONResponse(
                {"ok": False, "error": "setting and value are required"},
                status_code=400)
        try:
            from core.paper_trading import get_engine
            result = get_engine().update_setting(
                key, body.get("value"), actor="dashboard", source="dashboard")
            return JSONResponse({"ok": True, "paper_only": True, **result})
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)},
                                status_code=400)
        except Exception as exc:
            return JSONResponse(
                {"ok": False,
                 "error": f"{type(exc).__name__}: {str(exc)[:240]}"},
                status_code=200)

    @app.post("/api/rio/paper/start", include_in_schema=False)
    async def paper_start(req: Request):
        denied = _denied(req)
        if denied:
            return denied
        try:
            from core.paper_trading import get_engine
            engine = get_engine()
            engine.start_trading()
            return JSONResponse({"ok": True, "paper_only": True,
                                 "accepting_entries": True,
                                 "account": engine.status()["account"]})
        except Exception as exc:
            return JSONResponse(
                {"ok": False,
                 "error": f"{type(exc).__name__}: {str(exc)[:240]}"},
                status_code=200)

    @app.post("/api/rio/paper/stop", include_in_schema=False)
    async def paper_stop(req: Request):
        denied = _denied(req)
        if denied:
            return denied
        try:
            from core.paper_trading import get_engine
            engine = get_engine()
            engine.stop_trading()
            return JSONResponse({"ok": True, "paper_only": True,
                                 "accepting_entries": False,
                                 "open_positions": len(engine.positions())})
        except Exception as exc:
            return JSONResponse(
                {"ok": False,
                 "error": f"{type(exc).__name__}: {str(exc)[:240]}"},
                status_code=200)

    @app.post("/api/rio/paper/reset", include_in_schema=False)
    async def paper_reset(req: Request):
        denied = _denied(req)
        if denied:
            return denied
        try:
            body = await req.json()
        except Exception:
            body = {}
        if body.get("confirm") is not True:
            return JSONResponse(
                {"ok": False,
                 "error": "explicit confirmation required: {confirm: true}"},
                status_code=400)
        try:
            from core.paper_trading import get_engine
            result = get_engine().reset(confirm=True)
            return JSONResponse({"ok": True, "paper_only": True, **result})
        except Exception as exc:
            return JSONResponse(
                {"ok": False,
                 "error": f"{type(exc).__name__}: {str(exc)[:240]}"},
                status_code=200)
