"""
dashboard/video_api.py — RIO/dashboard API for the Video Creation Agent.

Mounted by DashboardServer exactly like repo_api:

    from dashboard.video_api import register as _video_register
    _video_register(app, _auth)

Every handler authenticates with the same _auth callable. All state-changing
operations go through actions.video_creator / the durable job store (detached
workers) — nothing video-related runs inside a request. File serving is
whitelisted: only finished media under a known output root, never the
filesystem at large. Progress is ALWAYS the store's real phase/progress —
no fake percentages.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse

from actions import video_creator as vc
from core.repo_agent.jobs import RepoJobStore
from core.video_agent import cinematic
from core.video_agent import encoder as enc_mod
from core.video_agent import gpu_effects
from core.video_agent import pipelines
from core.video_agent.runner import describe_job

__all__ = ["register"]

MEDIA_EXTS = (".mp4", ".mov", ".webm")
_PROBE_CACHE: dict[str, tuple[float, dict]] = {}


def _thumb_dir() -> Path:
    # call-time Path.home(): an import-time snapshot breaks JARVIS_HOME /
    # HOME relocations (tests, pendrive mode)
    return Path.home() / ".jarvis_video_thumbs"


def _denied() -> JSONResponse:
    return JSONResponse({"ok": False, "error": "Unauthorized"},
                        status_code=401)


def _out_roots(store: RepoJobStore) -> list[Path]:
    """Every directory finished/known video jobs may have written into —
    the whitelist for file serving and library scans."""
    roots = {Path.home() / "Videos" / "JARVIS"}
    for row in store.list(limit=100):
        try:
            params = json.loads(row.get("params") or "{}")
            out = str(params.get("out_dir") or "").strip()
            if out:
                roots.add(Path(out).resolve())
        except Exception:                          # noqa: BLE001
            continue
    return [r for r in roots if r.is_dir()]


def _safe_media_path(raw: str, store: RepoJobStore) -> Path | None:
    """Resolve + whitelist: the path must sit under a known output root and
    be a media file. Returns None otherwise (no filesystem traversal)."""
    try:
        path = Path(str(raw or "")).expanduser().resolve()
    except Exception:                              # noqa: BLE001
        return None
    if path.suffix.lower() not in MEDIA_EXTS:
        return None
    for root in _out_roots(store):
        try:
            path.relative_to(root)
            return path
        except ValueError:
            continue
    return None


def _probe(path: Path) -> dict[str, Any]:
    """ffprobe facts, cached per (path, mtime) — the library stays fast."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    cached = _PROBE_CACHE.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]
    facts: dict[str, Any] = {}
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=30)
        data = json.loads(out.stdout or "{}")
        fmt = data.get("format") or {}
        streams = data.get("streams") or []
        video = next((s for s in streams
                      if s.get("codec_type") == "video"), {})
        facts = {"duration_s": round(float(fmt.get("duration") or 0), 1),
                 "width": int(video.get("width") or 0),
                 "height": int(video.get("height") or 0)}
    except Exception:                              # noqa: BLE001
        facts = {}
    _PROBE_CACHE[str(path)] = (mtime, facts)
    return facts


def _title_for(path: Path) -> str:
    """Best-effort human title: sibling metadata.txt/script title → filename."""
    for candidate in (path.parent / "metadata.txt",
                      path.parent.parent / "render" / "metadata.txt"):
        if candidate.is_file():
            first = (candidate.read_text(errors="replace").strip()
                     .splitlines() or [""])[0].strip()
            if first:
                return first[:120]
    return path.stem.replace("_", " ").title()


def register(app, _auth) -> None:
    """Mount the video API. SYNC — same contract as repo_api.register."""

    def _overview_payload() -> dict[str, Any]:
        store = RepoJobStore()
        store.recover_stale()
        store.dispatch_queued()
        jobs = []
        for row in store.list(limit=30):
            if row.get("kind") not in ("video", "run_skill"):
                continue
            entry = {k: row.get(k) for k in
                     ("job_id", "kind", "ref", "status", "phase", "progress",
                      "message", "error", "created_at", "started_at",
                      "finished_at", "runtime_s")}
            try:
                params = json.loads(row.get("params") or "{}")
                spec = params.get("spec") or {}
                entry["mode"] = spec.get("mode")
                entry["pipeline"] = spec.get("pipeline")
                entry["out_dir"] = params.get("out_dir")
                entry["topic"] = str(spec.get("topic") or "")[:120]
            except Exception:                      # noqa: BLE001
                pass
            try:
                outputs = json.loads(row.get("outputs") or "[]")
                entry["outputs"] = outputs[:2]
            except Exception:                      # noqa: BLE001
                entry["outputs"] = []
            jobs.append(entry)
        caps = cinematic.capability_matrix()
        return {"ok": True, "jobs": jobs, "capabilities": caps,
                "pipelines": pipelines.available_pipelines(),
                "stages": list(pipelines.VIDEO_STAGES),
                # real encoder selection + measured NVIDIA capability
                "encoder": enc_mod.encoder_status(),
                "gpu_effects": gpu_effects.effects_for_render()}

    @app.get("/api/video/overview", include_in_schema=False)
    async def video_overview(request: Request) -> JSONResponse:
        if not _auth(request):
            return _denied()
        return JSONResponse(_overview_payload())

    @app.get("/api/video/jobs", include_in_schema=False)
    async def video_jobs(request: Request) -> JSONResponse:
        if not _auth(request):
            return _denied()
        store = RepoJobStore()
        store.recover_stale()
        store.dispatch_queued()
        jobs = [describe_job(r) for r in vc._recent_jobs(store, limit=20)]
        return JSONResponse({"ok": True, "lines": jobs})

    @app.post("/api/video/create", include_in_schema=False)
    async def video_create(request: Request) -> JSONResponse:
        if not _auth(request):
            return _denied()
        try:
            raw = await request.body()
            params = json.loads(raw) if raw else {}
        except Exception:                          # noqa: BLE001
            params = {}
        if not isinstance(params, dict):
            params = {}
        params.setdefault("action", "create")
        message = vc.video_creator(params)
        # a clarifying QUESTION means nothing was queued — the RIO form
        # shows it as "needs input", not success (the question may not END
        # with "?", so check membership + that no job language appears)
        # Phase 6: the mandatory method question is an explicit needs-input
        # signal — detect it directly and hand the token to the UI
        needs_method = message.startswith(
            "Which method would you like to use for this video?") or \
            message.startswith("Split the documentary into Shorts?")
        token = ""
        if needs_method:
            import re as _re
            match = _re.search(r"token ([0-9a-f]{16})", message)
            token = match.group(1) if match else ""
        refused = needs_method or \
            message.lower().startswith("refused") or \
            ("?" in message and "queued" not in message.lower()
             and "Retried as" not in message)
        return JSONResponse({"ok": not refused, "message": message,
                             "needs_method": needs_method,
                             "approval_token": token})

    @app.post("/api/video/cancel", include_in_schema=False)
    async def video_cancel(request: Request) -> JSONResponse:
        if not _auth(request):
            return _denied()
        try:
            raw = await request.body()
            params = json.loads(raw) if raw else {}
        except Exception:                          # noqa: BLE001
            params = {}
        message = vc.video_creator({"action": "cancel",
                                    "job_id": (params or {}).get("job_id")})
        return JSONResponse({"ok": True, "message": message})

    @app.post("/api/video/retry", include_in_schema=False)
    async def video_retry(request: Request) -> JSONResponse:
        if not _auth(request):
            return _denied()
        try:
            raw = await request.body()
            params = json.loads(raw) if raw else {}
        except Exception:                          # noqa: BLE001
            params = {}
        message = vc.video_creator({"action": "retry",
                                    "job_id": (params or {}).get("job_id")})
        return JSONResponse({"ok": True, "message": message})

    @app.post("/api/video/split", include_in_schema=False)
    async def video_split(request: Request) -> JSONResponse:
        if not _auth(request):
            return _denied()
        try:
            raw = await request.body()
            params = json.loads(raw) if raw else {}
        except Exception:                          # noqa: BLE001
            params = {}
        message = vc.video_creator({"action": "split",
                                    "save_dir": (params or {}).get("save_dir"),
                                    "count": (params or {}).get("count", 3),
                                    "branding":
                                        (params or {}).get("branding")})
        return JSONResponse({"ok": True, "message": message})

    @app.get("/api/video/library", include_in_schema=False)
    async def video_library(request: Request) -> JSONResponse:
        if not _auth(request):
            return _denied()
        store = RepoJobStore()
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for root in _out_roots(store):
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.suffix.lower() not in MEDIA_EXTS:
                    continue
                key = str(path)
                if key in seen or path.name.startswith("."):
                    continue
                seen.add(key)
                try:
                    stat = path.stat()
                except OSError:
                    continue
                facts = _probe(path)
                items.append({
                    "path": key, "title": _title_for(path),
                    "size_bytes": stat.st_size,
                    "created_at": stat.st_mtime, **facts})
        items.sort(key=lambda i: i.get("created_at") or 0, reverse=True)
        return JSONResponse({"ok": True, "items": items[:60]})

    @app.get("/api/video/file", include_in_schema=False)
    async def video_file(request: Request) -> Any:
        if not _auth(request):
            return _denied()
        path = _safe_media_path(request.query_params.get("path", ""),
                                RepoJobStore())
        if path is None or not path.is_file():
            return JSONResponse({"ok": False, "error": "not found or "
                                                      "not allowed"},
                                status_code=404)
        return FileResponse(str(path), media_type="video/mp4")

    @app.get("/api/video/thumb", include_in_schema=False)
    async def video_thumb(request: Request) -> Any:
        if not _auth(request):
            return _denied()
        path = _safe_media_path(request.query_params.get("path", ""),
                                RepoJobStore())
        if path is None or not path.is_file():
            return JSONResponse({"ok": False, "error": "not found or "
                                                      "not allowed"},
                                status_code=404)
        key = hashlib.sha1(f"{path}|{path.stat().st_mtime}"
                           .encode()).hexdigest()[:16]
        thumb = _thumb_dir() / f"{key}.jpg"
        if not thumb.is_file():
            thumb.parent.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(
                ["ffmpeg", "-y", "-ss", "0", "-i", str(path), "-frames:v",
                 "1", "-vf", "scale=320:-2", str(thumb)],
                capture_output=True, timeout=60)
            if proc.returncode != 0 or not thumb.is_file():
                return JSONResponse({"ok": False, "error": "thumbnail "
                                                          "failed"},
                                    status_code=500)
        return FileResponse(str(thumb), media_type="image/jpeg")

    @app.get("/api/video/encoder", include_in_schema=False)
    async def video_encoder(request: Request) -> JSONResponse:
        if not _auth(request):
            return _denied()
        return JSONResponse({"ok": True,
                             "encoder": enc_mod.encoder_status(),
                             "text": enc_mod.status_text(),
                             "gpu": gpu_effects.probe_nvidia(),
                             "gpu_text": gpu_effects.status_text()})

    @app.get("/api/video/capabilities", include_in_schema=False)
    async def video_capabilities(request: Request) -> JSONResponse:
        if not _auth(request):
            return _denied()
        return JSONResponse({"ok": True,
                             "matrix": cinematic.matrix_text(),
                             "providers": vc.providers.registry_table(),
                             "encoder": enc_mod.status_text(),
                             "gpu_effects": gpu_effects.status_text()})

    # ── Phase 2/3 (Universal Video Studio): engine registry + hardware ──────

    @app.get("/api/video/engines", include_in_schema=False)
    async def video_engines(request: Request) -> JSONResponse:
        if not _auth(request):
            return _denied()
        from core.video_agent.engines import registry as engine_registry
        refresh = request.query_params.get("refresh", "") in ("1", "true",
                                                              "yes")
        if refresh:
            payload = engine_registry.refresh()
            return JSONResponse({"ok": True, "engines": payload["engines"],
                                 "hardware_fingerprint":
                                     payload["hardware_fingerprint"],
                                 "text": engine_registry.status_text()})
        table = engine_registry.status_table()
        return JSONResponse({"ok": True, "engines": table,
                             "text": engine_registry.status_text()})

    @app.get("/api/video/hardware", include_in_schema=False)
    async def video_hardware(request: Request) -> JSONResponse:
        if not _auth(request):
            return _denied()
        from core.video_agent import hardware as hw_mod
        refresh = request.query_params.get("refresh", "") in ("1", "true",
                                                              "yes")
        report = hw_mod.refresh() if refresh else hw_mod.probe()
        return JSONResponse({"ok": True, "hardware": report,
                             "text": hw_mod.summary_text(report)})

    # ── Phase 4/5 (Universal Video Studio): installer + director ────────────

    @app.get("/api/video/installer", include_in_schema=False)
    async def video_installer_survey(request: Request) -> JSONResponse:
        if not _auth(request):
            return _denied()
        from core.video_agent import installer
        return JSONResponse({"ok": True, "survey": installer.survey()})

    @app.post("/api/video/installer", include_in_schema=False)
    async def video_installer_action(request: Request) -> JSONResponse:
        if not _auth(request):
            return _denied()
        from core.video_agent import installer
        from core.video_agent import approval as vappr
        try:
            raw = await request.body()
            params = json.loads(raw) if raw else {}
        except Exception:                          # noqa: BLE001
            params = {}
        params = params if isinstance(params, dict) else {}
        action = str(params.get("action") or "plan").strip().lower()
        engine_id = str(params.get("engine_id") or "").strip().upper()
        if action == "survey":
            return JSONResponse({"ok": True,
                                 "survey": installer.survey()})
        if action == "plan":
            result = installer.plan(engine_id)
            payload = {"ok": result.get("ok", False),
                       "plan": result.get("plan"),
                       "message": result.get("message", "")}
            if result.get("ok") and result.get("plan") \
                    and not result["plan"].get("blocked"):
                issued = vappr.issue(
                    installer.install_approval_spec(engine_id),
                    [{"method_id": "INSTALL", "name": "Install",
                      "status": "READY_LOCAL",
                      "estimated_cost": "free (download only)",
                      "privacy": "official source only",
                      "recommended": True}])
                payload["approval_token"] = issued["record"]["token"]
            return JSONResponse(payload)
        if action == "install":
            result = installer.start(
                engine_id, str(params.get("approve_token") or ""))
            return JSONResponse({"ok": result.get("ok", False),
                                 "job_id": result.get("job_id"),
                                 "message": result.get(
                                     "message", result.get("error", ""))})
        return JSONResponse({"ok": False,
                             "error": "action must be survey|plan|install"})

    @app.post("/api/video/direct", include_in_schema=False)
    async def video_direct(request: Request) -> JSONResponse:
        if not _auth(request):
            return _denied()
        from core.video_agent import director
        try:
            raw = await request.body()
            params = json.loads(raw) if raw else {}
        except Exception:                          # noqa: BLE001
            params = {}
        params = params if isinstance(params, dict) else {}
        params.setdefault("action", "plan")
        plan = director.plan_video(params)
        if not plan.get("ok"):
            return JSONResponse({"ok": False,
                                 "question": plan.get("question")})
        return JSONResponse({"ok": True, "plan": plan})
