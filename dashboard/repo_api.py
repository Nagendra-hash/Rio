"""
dashboard/repo_api.py — RIO/dashboard API for the Universal Repository Agent.

Mounted by DashboardServer exactly like trading_api/backtest_api:

    from dashboard.repo_api import register as _repo_register
    _repo_register(app, _auth)

Every handler authenticates with the same _auth callable (paired device
token or the static dashboard_auth_token). All state-changing operations
go through the durable job store (detached workers) — nothing repository-
related runs inside a request.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from core.repo_agent import manifests as M
from core.repo_agent import skills
from core.repo_agent.jobs import RepoJobStore, start_job
from core.repo_agent.security import RepoSecurityError, validate_repo_url

__all__ = ["register"]

MAX_BODY_JSON = 64 * 1024


def register(app, _auth) -> None:
    """Mount the repo-agent API. SYNC — same contract as every other
    dashboard/<name>_api.register called from DashboardServer._build_app."""

    def _denied() -> JSONResponse:
        return JSONResponse({"ok": False, "error": "Unauthorized"},
                            status_code=401)

    async def _body(request: Request) -> dict[str, Any]:
        try:
            raw = await request.body()
            if not raw or len(raw) > MAX_BODY_JSON:
                return {}
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @app.get("/api/repo/overview", include_in_schema=False)
    async def repo_overview(request: Request) -> JSONResponse:
        if not _auth(request):
            return _denied()
        reg = M.load_registry()
        store = RepoJobStore()
        store.recover_stale()
        store.dispatch_queued()
        jobs = store.list(limit=20)
        provider = {}
        try:
            from core.repo_agent.vimax import provider_status
            provider = provider_status()
        except Exception:
            pass
        return JSONResponse({"ok": True, "registry": reg, "jobs": jobs,
                             "vimax_providers": provider})

    @app.get("/api/repo/jobs/{job_id}", include_in_schema=False)
    async def repo_job(request: Request, job_id: str) -> JSONResponse:
        if not _auth(request):
            return _denied()
        job = RepoJobStore().get(job_id)
        if not job:
            return JSONResponse({"ok": False, "error": "unknown job"},
                                status_code=404)
        result_path = Path(M.tools_root().parent)  # unused; results live in memory/
        results = (RepoJobStore().path.parent / "results" /
                   f"{job_id}.json")
        result = None
        try:
            result = json.loads(results.read_text(encoding="utf-8"))
        except Exception:
            pass
        return JSONResponse({"ok": True, "job": job, "result": result})

    @app.post("/api/repo/install", include_in_schema=False)
    async def repo_install(request: Request) -> JSONResponse:
        if not _auth(request):
            return _denied()
        body = await _body(request)
        url = str(body.get("url") or "").strip()
        try:
            owner, repo = validate_repo_url(url)
        except RepoSecurityError as exc:
            return JSONResponse({"ok": False, "error": str(exc)},
                                status_code=400)
        result = start_job("install", repo.lower(), source="dashboard",
                           params={"url": f"https://github.com/{owner}/{repo}"})
        status = 200 if result.get("ok") else 400
        return JSONResponse(result, status_code=status)

    @app.post("/api/repo/run", include_in_schema=False)
    async def repo_run(request: Request) -> JSONResponse:
        if not _auth(request):
            return _denied()
        body = await _body(request)
        skill_id = str(body.get("skill_id") or "").strip().lower()
        op = str(body.get("op") or "").strip().lower()
        args = body.get("args") if isinstance(body.get("args"), dict) else {}
        if body.get("confirm") not in (True, 1, "1", "true", "yes"):
            return JSONResponse(
                {"ok": False,
                 "error": "confirmation required: this operation may spend "
                          "paid provider credits"},
                status_code=400)
        try:
            view = skills.resolve_skill(skill_id)
            entry = view.get("entry") or {}
            if not view.get("ok"):
                return JSONResponse({"ok": False,
                                     "error": f"unknown skill {skill_id}"},
                                    status_code=404)
            if str(entry.get("status") or "") != "READY":
                return JSONResponse(
                    {"ok": False,
                     "error": f"skill {skill_id} is "
                              f"{entry.get('status') or 'not ready'}"},
                    status_code=409)
            result = start_job("run_skill", skill_id, source="dashboard",
                               params={"op": op, "args": args,
                                       "out_dir": str(body.get("out_dir") or "")})
        except RepoSecurityError as exc:
            return JSONResponse({"ok": False, "error": str(exc)},
                                status_code=400)
        return JSONResponse(result, status_code=200 if result.get("ok")
                            else 400)

    @app.post("/api/repo/jobs/{job_id}/cancel", include_in_schema=False)
    async def repo_cancel(request: Request, job_id: str) -> JSONResponse:
        if not _auth(request):
            return _denied()
        result = RepoJobStore().cancel(job_id)
        return JSONResponse(result, status_code=200 if result.get("ok")
                            else 400)

    @app.post("/api/repo/repos/{name}/remove", include_in_schema=False)
    async def repo_remove(request: Request, name: str) -> JSONResponse:
        if not _auth(request):
            return _denied()
        body = await _body(request)
        if body.get("confirm") not in (True, 1, "1", "true", "yes"):
            return JSONResponse(
                {"ok": False,
                 "error": "confirmation required: removal deletes the "
                          "managed directory"},
                status_code=400)
        result = start_job("remove", str(name).strip().lower(),
                           source="dashboard", params={})
        return JSONResponse(result, status_code=200 if result.get("ok")
                            else 400)

    @app.post("/api/repo/skills/{skill_id}/state", include_in_schema=False)
    async def repo_skill_state(request: Request, skill_id: str) -> JSONResponse:
        if not _auth(request):
            return _denied()
        body = await _body(request)
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            return JSONResponse({"ok": False, "error": "enabled must be a "
                                                       "boolean"},
                                status_code=400)
        entry = M.set_skill_state(skill_id.strip().lower(), enabled=enabled)
        if entry is None:
            return JSONResponse({"ok": False, "error": "unknown skill"},
                                status_code=404)
        return JSONResponse({"ok": True, "skill": entry})

    @app.get("/api/repo/skills/{skill_id}/health", include_in_schema=False)
    async def repo_skill_health(request: Request, skill_id: str) -> JSONResponse:
        if not _auth(request):
            return _denied()
        try:
            result = skills.skill_health(skill_id.strip().lower())
        except RepoSecurityError as exc:
            return JSONResponse({"ok": False, "error": str(exc)},
                                status_code=400)
        except Exception as exc:                          # noqa: BLE001
            return JSONResponse({"ok": False,
                                 "error": f"{type(exc).__name__}: "
                                          f"{str(exc)[:200]}"},
                                status_code=500)
        return JSONResponse({"ok": bool(result.get("ok")),
                             "health": result})
