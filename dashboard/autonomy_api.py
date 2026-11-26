"""
dashboard/autonomy_api.py — REST surface for the Phase 20 autonomy engine.

Endpoints (bearer auth via the dashboard's _auth, like every /api route):

    GET  /api/autonomy/status        engine + scheduler snapshot
    POST /api/autonomy/stop          trip the global emergency stop {reason?}
    POST /api/autonomy/resume        release it (audited)
    POST /api/autonomy/agent         {agent, enabled?|level?}
    GET  /api/autonomy/approvals     pending human gates
    POST /api/autonomy/approve       {approval_id, approved, by?}
    GET  /api/autonomy/tasks         scheduled tasks (?enabled_only=1)
    POST /api/autonomy/tasks         add/update/pause/resume/remove task
    GET  /api/autonomy/runs          task run history
    GET  /api/autonomy/audit         audit tail (?limit=&event=)

The engine is a process singleton (shared_engine) so the scheduler loop and
the REST surface steer the same state. All enforcement lives in
orchestrator/autonomy — this module only maps HTTP to it.
"""
from __future__ import annotations

import threading

from fastapi import Request
from fastapi.responses import JSONResponse

_engine_lock = threading.Lock()
_engine = None


def shared_engine() -> "object | None":
    """The process-wide engine + scheduler, created lazily and safely."""
    global _engine
    with _engine_lock:
        if _engine is not None:
            return _engine
        try:
            from orchestrator.autonomy import (AutonomyAudit, AutonomyEngine,
                                               Notifier, Scheduler, TaskStore)
            from orchestrator.checkpoint import PlanCheckpointStore
            audit = AutonomyAudit()
            engine = AutonomyEngine(
                audit=audit, notifier=Notifier(audit=audit),
                checkpoints=PlanCheckpointStore("autonomy_runs"))
            scheduler = Scheduler(engine, TaskStore(), audit=audit)
            _engine = {"engine": engine, "scheduler": scheduler,
                       "audit": audit, "store": scheduler.store}
        except Exception:
            return None
        return _engine


def _deny_auth() -> JSONResponse:
    return JSONResponse({"error": "Unauthorized"}, status_code=401)


def _status_for(result: dict) -> int:
    if result.get("ok"):
        return 200
    return 403 if "denied" in str(result.get("error", "")).lower() else 400


def register(app, auth_check) -> None:
    """Attach /api/autonomy/* endpoints to the running FastAPI app."""

    @app.get("/api/autonomy/status")
    async def autonomy_status(req: Request):
        if not auth_check(req):
            return _deny_auth()
        bundle = shared_engine()
        if not bundle:
            return JSONResponse({"ok": False, "error": "engine unavailable"},
                                status_code=503)
        eng = bundle["engine"]
        return JSONResponse({
            "ok": True,
            "emergency_stop": eng.stopped,
            "stop_reason": eng._stop_reason,
            "agents_disabled": sorted(k for k, v in
                                      eng._agent_enabled.items() if not v),
            "agent_levels": dict(eng._agent_levels),
            "tools": sorted(eng.tools.keys()),
            "tasks": len(bundle["store"].list()),
        })

    @app.post("/api/autonomy/stop")
    async def autonomy_stop(req: Request):
        if not auth_check(req):
            return _deny_auth()
        bundle = shared_engine()
        if not bundle:
            return JSONResponse({"ok": False}, status_code=503)
        try:
            body = await req.json()
        except Exception:
            body = {}
        return JSONResponse(bundle["engine"].trip_stop(
            reason=str(body.get("reason") or "dashboard emergency stop"),
            by="dashboard"))

    @app.post("/api/autonomy/resume")
    async def autonomy_resume(req: Request):
        if not auth_check(req):
            return _deny_auth()
        bundle = shared_engine()
        if not bundle:
            return JSONResponse({"ok": False}, status_code=503)
        return JSONResponse(bundle["engine"].release_stop(by="dashboard"))

    @app.post("/api/autonomy/agent")
    async def autonomy_agent(req: Request):
        """Per-agent enable/disable and per-agent autonomy level."""
        if not auth_check(req):
            return _deny_auth()
        bundle = shared_engine()
        if not bundle:
            return JSONResponse({"ok": False}, status_code=503)
        try:
            body = await req.json()
        except Exception:
            body = {}
        eng = bundle["engine"]
        agent = str(body.get("agent") or "").strip()
        if not agent:
            return JSONResponse({"ok": False, "error": "agent required"},
                                status_code=400)
        if "enabled" in body:
            result = eng.set_agent_enabled(agent, bool(body["enabled"]))
        elif "level" in body:
            result = eng.set_agent_level(agent, int(body["level"] or 1))
        else:
            return JSONResponse({"ok": False, "error": "enabled or level required"},
                                status_code=400)
        return JSONResponse(result, status_code=_status_for(result))

    @app.get("/api/autonomy/approvals")
    async def autonomy_approvals(req: Request):
        if not auth_check(req):
            return _deny_auth()
        bundle = shared_engine()
        if not bundle:
            return JSONResponse({"ok": False}, status_code=503)
        eng = bundle["engine"]
        pending = []
        if eng.approvals is not None:
            with eng.approvals._lock:
                pending = [s.__dict__ | {"pending": True}
                           for s in eng.approvals._pending.values()]
        return JSONResponse({"ok": True, "pending": pending})

    @app.post("/api/autonomy/approve")
    async def autonomy_approve(req: Request):
        if not auth_check(req):
            return _deny_auth()
        bundle = shared_engine()
        if not bundle:
            return JSONResponse({"ok": False}, status_code=503)
        try:
            body = await req.json()
        except Exception:
            body = {}
        approval_id = str(body.get("approval_id") or "")
        if not approval_id:
            return JSONResponse({"ok": False, "error": "approval_id required"},
                                status_code=400)
        result = bundle["engine"].resolve_approval(
            approval_id, approved=bool(body.get("approved")),
            by=str(body.get("by") or "dashboard"))
        return JSONResponse(result)

    @app.get("/api/autonomy/tasks")
    async def autonomy_tasks(req: Request):
        if not auth_check(req):
            return _deny_auth()
        bundle = shared_engine()
        if not bundle:
            return JSONResponse({"ok": False}, status_code=503)
        enabled_only = req.query_params.get("enabled_only") == "1"
        return JSONResponse({"ok": True,
                             "tasks": bundle["store"].list(enabled_only)})

    @app.post("/api/autonomy/tasks")
    async def autonomy_tasks_post(req: Request):
        """Scheduled-task management: {action: add|update|pause|resume|
        remove, task: name, ...spec}."""
        if not auth_check(req):
            return _deny_auth()
        bundle = shared_engine()
        if not bundle:
            return JSONResponse({"ok": False}, status_code=503)
        try:
            body = await req.json()
        except Exception:
            body = {}
        store = bundle["store"]
        action = str(body.get("action") or "add").strip().lower()
        name = str(body.get("task") or "").strip()
        if action in ("pause", "resume") and name:
            result = store.update(name, enabled=(action == "resume"))
            if result.get("ok"):
                bundle["audit"].record(
                    "task_" + ("paused" if action == "pause" else "resumed"),
                    workflow=name, actor="dashboard")
            return JSONResponse(result, status_code=_status_for(result))
        if action == "remove":
            result = store.remove(name)
            if result.get("ok"):
                bundle["audit"].record("task_removed", workflow=name,
                                       actor="dashboard")
            return JSONResponse(result, status_code=_status_for(result))
        if action == "update":
            fields = {k: v for k, v in body.items()
                      if k in ("level", "max_steps", "timeout_s", "enabled",
                               "trigger", "notify", "agents", "tools", "steps",
                               "condition_cooldown_s")}
            result = store.update(name, **fields)
            return JSONResponse(result, status_code=_status_for(result))
        if action == "add":
            result = store.add(body)
            if result.get("ok"):
                bundle["audit"].record("task_added", workflow=name,
                                       actor="dashboard",
                                       detail={"level": body.get("level"),
                                               "kind": body.get("kind")})
            return JSONResponse(result, status_code=_status_for(result))
        return JSONResponse({"ok": False, "error": f"unknown action {action}"},
                            status_code=400)

    @app.get("/api/autonomy/runs")
    async def autonomy_runs(req: Request):
        if not auth_check(req):
            return _deny_auth()
        bundle = shared_engine()
        if not bundle:
            return JSONResponse({"ok": False}, status_code=503)
        limit = int(req.query_params.get("limit") or 50)
        return JSONResponse({"ok": True,
                             "history": bundle["store"].history(limit)})

    @app.get("/api/autonomy/audit")
    async def autonomy_audit(req: Request):
        if not auth_check(req):
            return _deny_auth()
        bundle = shared_engine()
        if not bundle:
            return JSONResponse({"ok": False}, status_code=503)
        limit = int(req.query_params.get("limit") or 50)
        event = req.query_params.get("event") or None
        return JSONResponse({"ok": True,
                             "audit": bundle["audit"].tail(limit, event)})
