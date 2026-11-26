"""
actions/video_creator.py — the Video Creation Agent (voice/RIO tool).

Phase 2 of the video upgrade. Three modes, detected from the request:

    DOCUMENTARY  — research → script → narration → visuals → ffmpeg (Phase 4)
    AI_CINEMATIC — ViMax idea2video (existing path, paid providers)
    SHORTS       — vertical 9:16 captions pipeline (Phase 7)

plus `smoke` — the full stage machine walked with SIMULATED content and a
REAL ffmpeg render + ffprobe validation (machinery proof, always labelled).

Stage machine: QUEUED → PLANNING → SCRIPTING → ASSET_COLLECTION → GENERATING
→ EDITING → VALIDATING → COMPLETED (+ WAITING_FOR_APPROVAL,
NEEDS_CONFIGURATION, FAILED, CANCELLED). Stages ride the existing
RepoJobStore `phase`/`progress` columns; the worker is the shared detached
repo-agent worker (kind="video").

Safety model unchanged:
    * AI_CINEMATIC only STARTS when the ViMax skill is READY and the user
      confirmed the paid-API run (confirm=true)
    * output goes under the user's home (default ~/Videos/JARVIS), never into
      code/secret directories
    * jobs are deduplicated — repeated speech/Telegram retries never launch
      two renders
    * unimplemented pipelines are refused honestly — no doomed jobs
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from core.repo_agent import manifests as M          # noqa: E402
from core.repo_agent import skills                  # noqa: E402
from core.repo_agent.jobs import RepoJobStore, start_job  # noqa: E402
from core.video_agent import pipelines              # noqa: E402
from core.video_agent import providers              # noqa: E402
from core.video_agent import cinematic              # noqa: E402
from core.video_agent import encoder as enc_mod     # noqa: E402
from core.video_agent import spec as vspec          # noqa: E402
from core.video_agent.runner import describe_job    # noqa: E402
from core.video_agent import approval as vappr      # noqa: E402
from core.video_agent.engines import registry as engine_registry  # noqa: E402
from core.video_agent.spec import (AI_CINEMATIC, BLENDER_3D, DOCUMENTARY,  # noqa: E402
                                   SHORTS, VIDEO_STAGES)

SKILL_ID = "vimax-video"
VIDEO_KIND = "video"
_DEFAULT_OUT = Path.home() / "Videos" / "JARVIS"


def _protected_dirs() -> tuple:
    """Protected output roots, resolved at CALL time — an import-time
    snapshot of Path.home() broke the guard whenever the module was first
    imported while HOME was relocated (tests, pendrive mode)."""
    return (BASE, Path.home() / "JarvisTools",
            Path.home() / ".ssh", Path.home() / ".hermes")


def _out_dir(raw: str) -> Path:
    """Validate the destination folder — never inside code or secret dirs."""
    candidate = Path(str(raw or "").strip() or _DEFAULT_OUT).expanduser()
    if not candidate.is_absolute():
        candidate = Path.home() / candidate
    candidate = candidate.resolve()
    for protected in _protected_dirs():
        try:
            candidate.relative_to(protected.resolve())
            raise ValueError(
                f"refusing to write videos into protected path {protected}")
        except ValueError as exc:
            if "refusing" in str(exc):
                raise
            continue
    candidate.mkdir(parents=True, exist_ok=True)
    if not os_access_writable(candidate):
        raise ValueError(f"output folder is not writable: {candidate}")
    return candidate


def os_access_writable(path: Path) -> bool:
    return path.exists() and (path.is_dir())


def video_creator(parameters: dict, player=None, speak=None) -> str:
    parameters = parameters or {}
    action = str(parameters.get("action") or "create").strip().lower()

    if player:
        player.write_log(f"[video-creator] {action}")

    try:
        if action == "create":
            return _create(parameters, speak)
        if action == "status":
            job_id = str(parameters.get("job_id") or "").strip()
            store = RepoJobStore()
            store.recover_stale()
            store.dispatch_queued()
            if job_id:
                job = store.get(job_id)
                return describe_job(job) if job else f"Unknown job '{job_id}'."
            rows = _recent_jobs(store, limit=5)
            if not rows:
                return ("No video jobs yet. Ask me to create a video and I "
                        "will route it through the right pipeline.")
            return ("Recent video jobs:\n" +
                    "\n".join("• " + describe_job(r) for r in rows))
        if action == "list":
            store = RepoJobStore()
            rows = _recent_jobs(store,
                                limit=int(parameters.get("limit") or 8))
            if not rows:
                return "No video jobs recorded."
            return ("Video jobs:\n" +
                    "\n".join("• " + describe_job(r) for r in rows))
        if action == "cancel":
            job_id = str(parameters.get("job_id") or "").strip()
            if not job_id:
                return _cancel_latest(RepoJobStore())
            result = RepoJobStore().cancel(job_id)
            return (f"Cancelled {job_id}." if result.get("ok")
                    else f"Could not cancel: {result.get('error')}")
        if action == "retry":
            return _retry(RepoJobStore(),
                          str(parameters.get("job_id") or "").strip())
        if action == "ready":
            return _readiness_text()
        if action == "stages":
            return _stages_text()
        if action == "providers":
            return providers.registry_table()
        if action == "encoder":
            return enc_mod.status_text()
        if action == "cinematic":
            return cinematic.matrix_text()
        if action == "artifacts":
            return _artifacts_text(str(parameters.get("save_dir")
                                       or parameters.get("out_dir") or ""))
        if action == "split":
            return _split_gated(RepoJobStore(), parameters)
        if action == "where":
            return _where(RepoJobStore(),
                          str(parameters.get("job_id") or "").strip())
        if action == "engines":
            # Phase 2 (Universal Video Studio): the measured method list.
            if str(parameters.get("refresh") or "").strip().lower() in \
                    ("1", "true", "yes", "refresh"):
                payload = engine_registry.refresh()
                return (f"Hardware refreshed (fingerprint "
                        f"{payload['hardware_fingerprint']}).\n"
                        + engine_registry.status_text(force=False))
            return engine_registry.status_text()
        if action == "hardware":
            # Phase 3: one measured hardware report (startup/refresh rule).
            from core.video_agent import hardware as hw_mod
            report = hw_mod.refresh() if \
                str(parameters.get("refresh") or "").strip().lower() in \
                ("1", "true", "yes", "refresh") else hw_mod.probe()
            return hw_mod.summary_text(report)
        if action == "approvals":
            # Phase 6: pending/recent method approvals (secrets-free view).
            token = str(parameters.get("approve_token") or "").strip()
            if token:
                return vappr.status_text(token)
            rows = vappr.recent(limit=5)
            if not rows:
                return ("No video approvals yet. Creating a video always "
                        "asks 'Which method would you like to use?' first.")
            return ("Recent video approvals (newest first):\n" + "\n".join(
                f"• {r['token']} — {r.get('state')} — "
                f"{str((r.get('plan') or {}).get('topic') or '')[:60]}"
                + (f" → {r['approved']['method']}" if r.get("approved")
                   else "") for r in rows))
        return ("Unknown video action. Use create, status, list, cancel, "
                "retry, ready, stages, providers, encoder, engines, "
                "hardware, approvals, cinematic, split, artifacts or "
                "where.")
    except ValueError as exc:
        return f"Refused: {exc}. Nothing was started."
    except Exception as exc:                              # noqa: BLE001
        return (f"The video creator is unavailable: {type(exc).__name__}: "
                f"{str(exc)[:180]}. Nothing was run.")


def _recent_jobs(store: RepoJobStore, limit: int = 8) -> list[dict]:
    """Both job kinds (ViMax run_skill + native video), newest first."""
    rows = (store.list(limit=limit, kind="run_skill", ref=SKILL_ID)
            + store.list(limit=limit, kind=VIDEO_KIND))
    rows.sort(key=lambda r: float(r.get("created_at") or 0), reverse=True)
    return rows[:limit]


def _readiness_text() -> str:
    view = skills.resolve_skill(SKILL_ID)
    entry = view.get("entry") or {}
    if not view.get("ok"):
        return ("ViMax is not installed yet. Say 'Jarvis, install "
                "https://github.com/HKUDS/ViMax' and I will set it up in an "
                "isolated environment.")
    if not entry.get("enabled", True):
        return "The ViMax skill is disabled. Enable it with the repo agent."
    status = str(entry.get("status") or "")
    if status == "NEEDS_CONFIGURATION":
        return (f"ViMax is installed but not configured: "
                f"{entry.get('detail') or 'provider API keys missing'}. "
                "Video generation needs LLM, image and video provider keys "
                "(OpenRouter covers all three) in config/api_keys.json under "
                "the vimax section.")
    if status == "READY":
        return ("ViMax is installed and ready (providers configured). "
                "Cinematic renders use paid provider credits, so I ask "
                "before each run.")
    return f"ViMax skill status: {status or 'unknown'}."


def _stages_text() -> str:
    """The stage machine + honest pipeline availability (mission contract)."""
    avail = pipelines.available_pipelines()
    smoke = "all 6 stages" if "smoke" in avail else "not registered"
    return (
        "Stage machine: QUEUED → PLANNING → SCRIPTING → ASSET_COLLECTION → "
        "GENERATING → EDITING → VALIDATING → COMPLETED (plus "
        "WAITING_FOR_APPROVAL, NEEDS_CONFIGURATION, FAILED, CANCELLED).\n"
        f"Pipelines today: smoke — {smoke}, SIMULATED content with a real "
        "ffmpeg render (say 'run a video smoke test'); documentary; shorts.\n"
        "Encoder: Intel VAAPI H.264 (verified by a real probe) with automatic "
        "CPU libx264 fallback; 1080x1920 @ 30 fps, AAC audio.\n"
        "AI cinematic: existing ViMax path — paid providers, confirm-gated.")


def _create(parameters: dict, speak=None) -> str:
    # 0. script-based cinematic: the script IS the content — no topic needed.
    #    Force the mode (free-text detection would misread a screenplay) and
    #    derive a short label from the script's first line.
    if str(parameters.get("script") or "").strip():
        if not str(parameters.get("mode") or "").strip():
            parameters = {**parameters, "mode": "AI_CINEMATIC"}
        if not str(parameters.get("topic") or "").strip():
            first = next((l.strip() for l in
                          str(parameters["script"]).splitlines() if l.strip()),
                         "")[:100]
            parameters = {**parameters,
                          "topic": first or "user-provided script"}
    if isinstance(parameters.get("scenes"), (list, tuple)) or \
            str(parameters.get("scenes") or "").strip():
        if not str(parameters.get("mode") or "").strip():
            parameters = {**parameters, "mode": "AI_CINEMATIC"}
        if not str(parameters.get("topic") or "").strip():
            first_scene = parameters.get("scenes")
            first = (first_scene[0] if isinstance(first_scene, (list, tuple))
                     and first_scene else str(first_scene or ""))
            parameters = {**parameters,
                          "topic": str(first).strip()[:100]
                          or "user scene descriptions"}
    if str(parameters.get("image") or "").strip():
        if not str(parameters.get("mode") or "").strip():
            parameters = {**parameters, "mode": "AI_CINEMATIC"}
        if not str(parameters.get("topic") or "").strip():
            # label only — the MOTION prompt stays the one essential and is
            # asked in _create_vimax when genuinely missing
            from pathlib import Path as _P2
            parameters = {**parameters, "topic":
                          "image-to-video: " + _P2(str(parameters["image"]))
                          .name}

    # 1. resolve the request into a typed spec (mode detection, defaults,
    #    clamps). One clarifying question when the essential is missing.
    request_text = " ".join(str(parameters.get(k) or "") for k in
                            ("request", "topic", "audience", "style"))
    try:
        spec = vspec.build_spec(parameters, request_text=request_text)
    except vspec.SpecError as exc:
        return exc.question

    # 2. route by pipeline — through MANDATORY method approval (mission
    #    Phase 6). Nothing is queued and nothing is generated before the
    #    user picks a method and the token is consumed.
    if spec["pipeline"] != "smoke" and spec["mode"] == DOCUMENTARY:
        from core.video_agent.pipeline_doc import apply_sample_policy
        spec = apply_sample_policy(spec)

    if spec["mode"] == BLENDER_3D and \
            str(spec.get("characters") or "default") != "none":
        # Blender honesty (mission rule): the character sheet is an
        # illustration, not a 3D model — say what Blender will render
        try:
            from core.video_agent import characters as cast
            if cast.image_reference() is not None:
                spec.setdefault("notes", []).append(
                    "Blender honesty: no 3D models/rigs exist for the "
                    "default cast (the sheet is an illustration) — the "
                    "render uses the original procedural robot avatar")
        except Exception:                          # noqa: BLE001
            pass

    token = str(parameters.get("approve_token") or "").strip()
    chosen = str(parameters.get("method")
                 or parameters.get("engine") or "").strip().upper()
    if not token:
        # store-level dedup FIRST: a repeated identical utterance after the
        # job exists must report the job, never re-ask for approval
        if spec["mode"] != AI_CINEMATIC:
            dup = _identical_native_job(spec, parameters)
            if dup:
                return dup
        return _ask_method(parameters, spec)

    if not chosen:
        # a token without a choice: re-ask, keeping the same token
        return _ask_method(parameters, spec, token=token)

    # pre-flight: the dispatch-time clarifying question (ViMax i2v motion
    # prompt) must NOT consume the token — nothing is approved yet
    question = _dispatch_preflight(chosen, parameters, spec)
    if question:
        return question

    approved = vappr.approve(token, chosen, _approval_config(parameters))
    if not approved.get("ok"):
        return f"Refused: {approved.get('error')}. Nothing was started."
    consumed = vappr.consume(token,
                             project_key_expected=vappr.project_key(spec))
    if not consumed.get("ok"):
        return f"Refused: {consumed.get('error')}. Nothing was started."

    record = consumed["record"]
    method = record["approved"]["method"]
    overrides = record["approved"].get("config") or {}
    if overrides:
        # the approved configuration IS the final configuration — rebuild
        # the spec with it (clamped honestly, recorded in notes)
        merged = {**parameters,
                  **{k: v for k, v in overrides.items() if v not in (None,
                                                                     "")}}
        merged.pop("approve_token", None)
        merged.pop("method", None)
        try:
            spec = vspec.build_spec(merged, request_text=request_text)
        except vspec.SpecError as exc:
            return exc.question
        if spec["pipeline"] != "smoke" and spec["mode"] == DOCUMENTARY:
            from core.video_agent.pipeline_doc import apply_sample_policy
            spec = apply_sample_policy(spec)

    if method == "VIMAX":
        # paid consent was captured by the approval (cost + privacy were
        # shown with the method); the legacy gate is satisfied by it
        return _create_vimax({**parameters, "confirm": True}, spec, speak)
    if method in ("CANVAS", "BLENDER_3D"):
        return _create_video_job(parameters, spec, speak)
    if method in ("WAN_2_1", "LTX_VIDEO", "STOCK_FOOTAGE"):
        # Phase 7: engine-owned pipelines — queue only when the engine's
        # backend MEASURES ready (worker verified / provider configured)
        engine = engine_registry.get(method)
        if engine is not None:
            status = engine.probe()
            if not status.is_runnable():
                return (f"Refused: {engine.descriptor.name} cannot generate "
                        f"this video yet ({status.status}: "
                        f"{status.detail[:220]}). Nothing was started, "
                        "nothing was downloaded, nothing was queued.")
        spec = {**spec, "pipeline": method.lower()}
        return _create_video_job(parameters, spec, speak)
    if method == "CHARACTER_ANIMATION":
        engine = engine_registry.get(method)
        if engine is not None and not engine.probe().is_runnable():
            status = engine.probe()
            return (f"Refused: {engine.descriptor.name} cannot generate "
                    f"this video yet ({status.status}: {status.detail[:200]})."
                    " Nothing was started.")
        spec = {**spec, "pipeline": "character_animation"}
        return _create_video_job(parameters, spec, speak)
    if method == "HYBRID":
        spec = {**spec, "pipeline": "hybrid",
                "scene_plan": _parse_scene_plan(parameters.get("scene_plan"))}
        return _create_video_job(parameters, spec, speak)

    engine = engine_registry.get(method)
    if engine is not None:
        status = engine.probe()
        return (f"Refused: {engine.descriptor.name} cannot generate this "
                f"video yet ({status.status}: {status.detail[:220]}). "
                "Nothing was started, nothing was downloaded, nothing was "
                "queued.")

    return (f"Refused: unknown method '{method}'. Nothing was started.")

def _parse_scene_plan(raw: Any) -> list[dict[str, Any]]:
    """Accept [{scene:1, method:'CANVAS'}, …] or '1:CANVAS,2:BLENDER_3D'."""
    plan: list[dict[str, Any]] = []
    if isinstance(raw, str) and raw.strip():
        for part in raw.split(","):
            if ":" in part:
                scene, method = part.split(":", 1)
                plan.append({"scene": scene.strip(), "method":
                             method.strip().upper()})
        return plan
    if isinstance(raw, (list, tuple)):
        for i, entry in enumerate(raw, start=1):
            if isinstance(entry, dict):
                plan.append({"scene": entry.get("scene", i),
                             "method": str(entry.get("method") or "")
                             .upper(),
                             "prompt": str(entry.get("prompt")
                                           or entry.get("title") or "")})
            elif isinstance(entry, str) and ":" in entry:
                scene, method = entry.split(":", 1)
                plan.append({"scene": scene.strip(),
                             "method": method.strip().upper()})
    return plan


def _hybrid_preflight(parameters: dict) -> str | None:
    """The mission's HYBRID rule: show the COMPLETE scene→engine table and
    approve it BEFORE anything runs. Without a plan (or with an unroutable
    scene) the token stays PENDING and the user gets the table/question."""
    raw = parameters.get("scene_plan")
    plan = _parse_scene_plan(raw)
    if not plan:
        default = "1:CANVAS,2:CANVAS,3:CANVAS"
        return ("For HYBRID I need the complete scene→engine table — for "
                f"example scene_plan='{default}' (scene 1 and 2 as Canvas "
                "motion graphics, scene 3 in Blender 3D). Which engine "
                "should render which scene? Nothing was approved or "
                "started.")
    problems: list[str] = []
    for entry in plan:
        method_id = str(entry.get("method") or "").upper()
        engine = engine_registry.get(method_id)
        if engine is None:
            problems.append(f"scene {entry.get('scene')}: unknown engine "
                            f"[{method_id}]")
            continue
        status = engine.probe()
        if not status.is_runnable():
            problems.append(f"scene {entry.get('scene')}: "
                            f"{engine.descriptor.name} [{method_id}] is "
                            f"{status.status}")
    if problems:
        return ("Refused: the scene plan cannot run — "
                + "; ".join(problems)[:300]
                + ". Nothing was approved; pick runnable engines for every "
                "scene (the method question lists them).")
    return None


def _dispatch_preflight(method: str, parameters: dict,
                        spec: dict | None = None) -> str | None:
    """Dispatch-time questions that must NOT consume the approval token.
    Mirrors _create_vimax's own checks so the user is asked once, the token
    stays PENDING, and the next call (with the answer) proceeds."""
    spec = spec or {}
    if method == "CHARACTER_ANIMATION":
        if not str(parameters.get("narration") or "").strip():
            return ("What should the narration say? Give me the story text "
                    "for the dance video (the characters will act it out) "
                    "and I will animate it — nothing was approved yet.")
        return None
    if method == "HYBRID":
        return _hybrid_preflight(parameters)
    if method != "VIMAX":
        return None
    image = str(parameters.get("image") or "").strip()
    if not image:
        # permanent cast: when the default characters are requested, the
        # design sheet is the image reference for image-capable tools
        # (opt out with characters="none")
        try:
            if str(spec.get("characters") or "default") != "none":
                from core.video_agent import characters as cast
                sheet = cast.image_reference()
                if sheet is not None:
                    image = str(sheet)
        except Exception:                          # noqa: BLE001
            pass
    if image:
        from pathlib import Path as _P
        if not _P(image).expanduser().is_file():
            return (f"Refused: the reference image '{image}' does not exist. "
                    "Nothing was started.")
        if not str(parameters.get("prompt") or "").strip():
            return ("What should happen in the video? Describe the motion "
                    "or scene you want from that image (one clear question).")
    user_script = str(parameters.get("script") or "").strip()
    if user_script and len(user_script) > 8000:
        return ("That script is too long for the ViMax script2video "
                "operation (8000 character limit) — split it into parts "
                "and render them separately.")
    return None


def _identical_native_job(spec: dict, parameters: dict) -> str | None:
    """Read-only dedup probe for native video jobs: is an identical job
    already ACTIVE (or freshly completed within the dedup window)? Mirrors
    the store's own signature so the answer matches what start_job would
    decide. Returns the user-facing dedup message, or None."""
    import time as _time
    from core.repo_agent.jobs import _sig
    try:
        out_dir = _out_dir(str(parameters.get("save_dir") or ""))
    except ValueError as exc:
        return str(exc)
    job_params = {"spec": dict(spec, out_dir=str(out_dir)),
                  "out_dir": str(out_dir)}
    key = f"{VIDEO_KIND}:{spec['pipeline']}:{_sig(job_params)}"
    store = RepoJobStore()
    for row in store.list(limit=60, kind=VIDEO_KIND):
        if row.get("dedup_key") != key:
            continue
        status = str(row.get("status") or "")
        if status in ("QUEUED", "INSTALLING", "RUNNING"):
            return (f"An identical video job is already {status} "
                    f"({row.get('job_id')}) — nothing duplicated. Ask "
                    f"'status video job {row.get('job_id')}' for progress.")
        if status == "COMPLETED":
            finished = float(row.get("finished_at") or 0)
            window = 300
            try:
                from core.repo_agent.jobs import config as _jcfg
                window = int(_jcfg().get("dedup_window_s") or 300)
            except Exception:                      # noqa: BLE001
                pass
            if finished and _time.time() - finished < window:
                return (f"An identical video job is already COMPLETED "
                        f"({row.get('job_id')}) — nothing duplicated. Ask "
                        f"'status video job {row.get('job_id')}' for "
                        "progress.")
    return None


def _approval_config(parameters: dict) -> dict:
    """Whitelisted user-settable approval knobs (mission Phase 6: the user
    may set duration/resolution/format/language/cloud/budget at approval)."""
    config = {}
    for key in ("duration_s", "resolution", "language", "subtitles",
                "transitions", "encoder", "cloud_allowed", "max_budget"):
        value = parameters.get(key)
        if value not in (None, ""):
            config[key] = value
    return config


def _ask_method(parameters: dict, spec: dict, token: str | None = None
                ) -> str:
    """The mission's mandatory question. Issues (or reuses) a PENDING
    approval and lists the eligible methods with their facts. NEVER starts
    a job, never downloads, never spends."""
    methods = engine_registry.methods_for_spec(spec)
    if not methods:
        return ("Refused: no registered engine can generate this video yet "
                f"({spec['mode']}). Nothing was started.")
    # Phase 7: HYBRID is offered when two or more engines are eligible for
    # the mode (runnability of each scene is enforced by the pre-flight and
    # at planning — an unavailable scene engine never burns the token)
    if len(methods) >= 2:
        methods = methods + [{
            "method_id": "HYBRID",
            "name": "Hybrid (per-scene engines)",
            "status": "READY_LOCAL",
            "detail": "different engine per scene — you approve the "
                      "complete scene→engine table before anything runs",
            "execution": ["local"],
            "estimated_cost": "sum of the chosen scene engines",
            "privacy": "per chosen engines",
            "resolutions": [str(spec.get("resolution") or "720p")],
            "duration_range_s": [10, 900],
            "recommended": False,
        }]
    if token is None:
        issued = vappr.issue(spec, methods)
        record = issued["record"]
        token = record["token"]
        dedup_note = (" (you already have this approval pending — same "
                      "token)" if issued.get("deduplicated") else "")
    else:
        record = vappr.get(token) or {"plan": {}}
        dedup_note = ""
    if record.get("state") not in ("PENDING", None):
        return (f"Refused: approval {token} is {record.get('state')} — "
                "every video needs its own approval. Ask again and I will "
                "issue a fresh one. Nothing was started.")

    lines = [
        record.get("question")
        or "Which method would you like to use for this video?",
        "",
        f"Plan: {spec['mode']} — '{str(spec['topic'])[:80]}' · "
        f"{spec['duration_s']}s · {spec['resolution']} · "
        f"{spec['aspect_ratio']} · language: {spec['language']}"
        + (" · 30-second sample first (say 'full' for the entire piece)"
           if spec.get("sample") else ""),
    ]
    notes = "; ".join(spec.get("notes") or [])
    if notes:
        lines.append(f"Notes: {notes}")
    lines += ["", "Available methods:"]
    for i, m in enumerate(methods, 1):
        rec = " (recommended)" if m.get("recommended") else ""
        lines.append(
            f"  {i}. {m['name']} [{m['method_id']}]{rec} — {m['status']} · "
            f"{m['estimated_cost']} · {'/'.join(m['execution'])} · "
            f"res: {'/'.join(m['resolutions'])} · privacy: {m['privacy']}")
        if m.get("detail") and m["status"] not in ("READY_LOCAL",
                                                   "READY_REMOTE"):
            lines.append(f"      — {m['detail'][:180]}")
    lines += [
        "",
        f"Nothing has been generated; no job is queued{dedup_note}. "
        f"Tell me your choice for token {token} — for example "
        f"'use method 1' or 'use Blender 3D for this video'.",
    ]
    return "\n".join(lines)


def _create_video_job(parameters: dict, spec: dict, speak=None) -> str:
    """Queue a native video-pipeline job (kind='video', shared worker)."""
    try:
        out_dir = _out_dir(str(parameters.get("save_dir") or ""))
    except ValueError as exc:
        return str(exc)
    spec = dict(spec)
    spec["out_dir"] = str(out_dir)
    pipeline = str(spec.get("pipeline") or "smoke")
    result = start_job(VIDEO_KIND, pipeline,
                       source="jarvis",
                       params={"spec": spec, "out_dir": str(out_dir)})
    if not result.get("ok"):
        return f"Could not start the video job: {result.get('error')}"
    job = result.get("job") or {}
    if result.get("deduplicated"):
        return (f"An identical video job is already {job.get('status')} "
                f"({result.get('job_id')}) — nothing duplicated. Ask 'status "
                f"video job {result.get('job_id')}' for progress.")
    if speak:
        speak("Video job started, sir. I will report the finished file.")
    if pipeline == "documentary":
        sample = bool(spec.get("sample"))
        notes = "; ".join(spec.get("notes") or [])
        return (f"Documentary job queued ({result.get('job_id')}) → "
                f"{out_dir}. "
                + ("30-second sample first — say 'make the full documentary' "
                   "once you have reviewed it. " if sample else "")
                + "Research and script via the free model chain, narration "
                "via free TTS, visuals are motion graphics and your own "
                "images (AI-generated shots stay blocked until provider "
                "billing — I will never pass stills off as AI video). "
                + (f"Notes: {notes}. " if notes else "")
                + f"Progress: 'status video job {result.get('job_id')}'.")
    if pipeline == "shorts":
        reused = bool(spec.get("reuse_narration_from"))
        extras = []
        if spec.get("transitions"):
            extras.append("fade transitions")
        if str(spec.get("language") or "").lower() not in ("", "english",
                                                           "en",
                                                           "simple english"):
            extras.append(f"{spec.get('language')} narration")
        enc = enc_mod.select_encoder(str(spec.get("encoder") or "") or None)
        extras.append(f"{enc.backend} encoder"
                      + (" with CPU fallback" if enc.hardware else ""))
        return (f"Short queued ({result.get('job_id')}) → {out_dir}. "
                "Vertical 1080x1920 @ 30 fps with burned captions — "
                + ("narration reused from your documentary, " if reused else
                   "script via the free model chain, narration via free TTS, ")
                + "visuals are vertical motion graphics and your own images "
                f"(zero cost; {', '.join(extras)}). Progress: 'status video "
                f"job {result.get('job_id')}'.")
    if pipeline == "character_animation":
        enc = enc_mod.select_encoder(str(spec.get("encoder") or "") or None)
        return (f"3D character animation queued ({result.get('job_id')}) → "
                f"{out_dir}. Blender builds the playground, animates both "
                "articulated characters (per-limb keyframes, joint pivots — "
                "not armature/skinning, no lip-sync), renders 720x1280 at "
                "24 fps with 5 directed shots, then mixes original 100 BPM "
                f"music under the narration ({enc.backend} encoder with CPU "
                f"fallback). Progress: 'status video job {result.get('job_id')}'.")
    if pipeline == "blender":
        return (f"3D animation queued ({result.get('job_id')}) → {out_dir}. "
                "Blender Eevee builds a real 3D robot scene (procedural "
                "geometry, keyframe animation), renders frames, and encodes "
                f"to MP4 with VAAPI/CPU fallback. Progress: 'status video "
                f"job {result.get('job_id')}'.")
    return (f"Video job queued ({result.get('job_id')}) → {out_dir}. "
            f"Stages: {' → '.join(s.upper() for s in VIDEO_STAGES)}. "
            "SIMULATED content, real render — progress: 'status video job "
            f"{result.get('job_id')}'.")


def _create_vimax(parameters: dict, spec: dict, speak=None) -> str:
    """AI_CINEMATIC — the existing, proven ViMax path (unchanged behavior)."""
    # 1. skill readiness — honest gating, never aspirational
    view = skills.resolve_skill(SKILL_ID)
    entry = view.get("entry") or {}
    if not view.get("ok"):
        return ("The ViMax skill is not installed. Say 'Jarvis, install "
                "https://github.com/HKUDS/ViMax' first — I will inspect and "
                "install it in an isolated environment.")
    if not entry.get("enabled", True):
        return "The ViMax skill is disabled. Enable it first (repo agent)."
    if str(entry.get("status") or "") != "READY":
        return _readiness_text()

    # 2. paid-API confirmation — generation always costs provider credits
    confirm = parameters.get("confirm") in (True, 1, "1", "true", "yes")
    if not confirm:
        return ("Video generation runs cloud providers and spends paid API "
                "credits (LLM script, image keyframes, video shots — expect "
                "a few dollars for a 60-second piece depending on your "
                "provider plan). Confirm with confirm=true and I will start "
                "the render.")

    # 3. map the spec onto the ViMax input schema. Ops (Phase 5/6):
    #    idea2video (idea), script2video (user script or scene list),
    #    i2v (user image + prompt). The script/scene/i2v ops skip the LLM
    #    story stage or the whole story pipeline.
    try:
        out_dir = _out_dir(str(parameters.get("save_dir") or ""))
    except ValueError as exc:
        return str(exc)
    image = str(parameters.get("image") or "").strip()
    user_script = str(parameters.get("script") or "").strip()
    scenes = parameters.get("scenes")
    if image:
        from pathlib import Path as _P
        src = _P(image).expanduser()
        if not src.is_file():
            return (f"Refused: the reference image '{image}' does not exist. "
                    "Nothing was started.")
        prompt = str(parameters.get("prompt") or "").strip()
        if not prompt:
            return ("What should happen in the video? Describe the motion "
                    "or scene you want from that image (one clear question).")
        op = "i2v"
        args = {"image": str(src), "prompt": prompt[:2000],
                "aspect_ratio": spec["aspect_ratio"]}
        op_note = "your image + prompt → generated video (provider-side)"
    elif scenes is not None and str(scenes).strip():
        op = "script2video"
        args = {"script": cinematic.scenes_to_script(scenes),
                "style": str(parameters.get("style") or spec.get("style")
                             or "Cinematic"),
                "user_requirement": str(parameters.get("narration") or "")}
        op_note = "your scenes → storyboard → shots → final MP4"
    elif user_script:
        if len(user_script) > 8000:
            return ("That script is too long for the ViMax script2video "
                    "operation (8000 character limit) — split it into parts "
                    "and render them separately.")
        op = "script2video"
        args = {"script": user_script,
                "style": str(parameters.get("style") or spec.get("style")
                             or "Cinematic"),
                "user_requirement": str(parameters.get("narration") or "")}
        op_note = "your script → storyboard → shots → final MP4"
    else:
        op = "idea2video"
        args = {
            "topic": spec["topic"],
            "duration_s": int(spec["duration_s"]),
            "style": str(parameters.get("style") or spec.get("style")
                         or "Cinematic"),
            "aspect_ratio": spec["aspect_ratio"],
            "audience": str(parameters.get("audience") or ""),
            "language": spec.get("language") or "simple English",
            "narration": str(parameters.get("narration") or ""),
        }
        op_note = "script → storyboard → shots → final MP4"

    # 4. start the detached, deduplicated job
    result = start_job("run_skill", SKILL_ID, source="jarvis",
                       params={"op": op, "args": args,
                               "out_dir": str(out_dir)})
    if not result.get("ok"):
        return f"Could not start the render: {result.get('error')}"
    job = result.get("job") or {}
    if result.get("deduplicated"):
        return (f"An identical video job is already {job.get('status')} "
                f"({result.get('job_id')}) — nothing duplicated. Ask 'status "
                f"video job {result.get('job_id')}' for progress.")
    if speak:
        speak("Render started, sir. I will verify the finished file and "
              "report the path when it is done.")
    return (f"Cinematic render queued (job {result.get('job_id')}, {op}) → "
            f"{out_dir}. Pipeline: {op_note}, with live stage progress, "
            "verified on disk before I call it done. Progress: 'status "
            f"video job {result.get('job_id')}'.")


def _split_gated(store: RepoJobStore, parameters: dict) -> str:
    """Documentary → Shorts split: NEW video generation, so it goes through
    the same mandatory method approval (mission rule: an earlier project's
    approval never authorizes a new one). The split reuses the documentary's
    REAL narration — free, local, Canvas."""
    token = str(parameters.get("approve_token") or "").strip()
    if not token:
        # honest pre-check BEFORE asking for approval: nothing to split →
        # say so instead of gating a doomed request
        doc_dir = str(parameters.get("save_dir") or "").strip()
        if not doc_dir and not _latest_completed_documentary(store):
            return _split(store, parameters)
        count = parameters.get("count", 3)
        pseudo_spec = {
            "mode": "SHORTS",
            "topic": f"Shorts split ×{count} from my latest documentary",
            "duration_s": 45, "resolution": "1080p",
            "aspect_ratio": "9:16",
        }
        methods = [{
            "method_id": "CANVAS",
            "name": "Canvas (stills + motion graphics)",
            "status": "READY_LOCAL",
            "detail": "reuses the documentary's real narration and timing "
                      "— no new narration, no LLM cost",
            "execution": ["local"], "estimated_cost": "free",
            "privacy": "runs locally", "resolutions": ["1080p"],
            "duration_range_s": [15, 90], "recommended": True,
        }]
        issued = vappr.issue(pseudo_spec, methods,
                             question="Split the documentary into Shorts? "
                                      "Approve the method:")
        rec = issued["record"]
        return (
            "Making Shorts from a documentary generates NEW videos, so I "
            "need your approval. "
            + (rec.get("question") or "Approve the method?") + "\n"
            f"  1. Canvas [CANVAS] (recommended) — READY_LOCAL · free · "
            "local · reuses the documentary's real narration\n"
            f"Nothing has been generated. Approve with token "
            f"{rec['token']} (e.g. 'yes, split it — token {rec['token']}').")
    approved = vappr.approve(token, "CANVAS")
    if not approved.get("ok"):
        return f"Refused: {approved.get('error')}. Nothing was started."
    consumed = vappr.consume(token)
    if not consumed.get("ok"):
        return f"Refused: {consumed.get('error')}. Nothing was started."
    return _split(store, parameters)


def _latest_completed_documentary(store: RepoJobStore) -> str:
    """out_dir of the most recent COMPLETED documentary, or ''."""
    for row in _recent_jobs(store, limit=30):
        try:
            if row.get("kind") == "video" \
                    and row.get("status") == "COMPLETED" \
                    and json.loads(row.get("params") or "{}") \
                    .get("spec", {}).get("pipeline") == "documentary":
                return str(json.loads(row["params"]).get("out_dir") or "")
        except Exception:                          # noqa: BLE001
            continue
    return ""


def _split(store: RepoJobStore, parameters: dict) -> str:
    """'Jarvis, make three Shorts from my latest documentary' — reuse the
    documentary's REAL narration audio (no re-synthesis, no invented timing)
    and queue one vertical Shorts job per selected segment."""
    from core.video_agent.pipeline_shorts import split_documentary
    count = max(1, min(int(parameters.get("count") or 3), 6))
    doc_dir = str(parameters.get("save_dir") or "").strip()
    if not doc_dir:
        rows = [r for r in _recent_jobs(store, limit=30)
                if r.get("kind") == "video" and r.get("status") == "COMPLETED"
                and json.loads(r.get("params") or "{}").get("spec", {})
                .get("pipeline") == "documentary"]
        if not rows:
            return ("I could not find a completed documentary to split. "
                    "Create one first ('Jarvis, create a documentary "
                    "about …') and then say 'make Shorts from it'.")
        params = json.loads(rows[0]["params"])
        doc_dir = str(params.get("out_dir") or "")
    try:
        picks = split_documentary(doc_dir, count)
    except Exception as exc:                       # noqa: BLE001
        return f"Could not split that documentary: {exc}"
    out_base = _out_dir("")
    queued = []
    for i, pick in enumerate(picks, start=1):
        run_dir = out_base / f"short_{i}_seg{pick['segment_ids'][0]}"
        spec = {
            "mode": "SHORTS", "pipeline": "shorts",
            "topic": f"Short {i} from my documentary",
            "duration_s": 45, "aspect_ratio": "9:16", "resolution": "1080p",
            "subtitles": True, "out_dir": str(run_dir),
            "reuse_narration_from": doc_dir,
            "segment_ids": pick["segment_ids"],
            "branding": str(parameters.get("branding") or ""),
            "detection": "split", "notes": [
                f"narration reused from documentary {doc_dir} "
                f"(segment {pick['segment_ids'][0]}, "
                f"{pick['duration_s']}s, real timing)"],
        }
        result = start_job(VIDEO_KIND, "shorts", source="jarvis",
                           params={"spec": spec, "out_dir": str(run_dir)})
        if result.get("ok"):
            queued.append(str(result.get("job_id")))
    if not queued:
        return "Could not queue any Shorts jobs (worker refused) — nothing duplicated."
    return (f"{len(queued)} Short(s) queued from the documentary ({doc_dir}): "
            f"{', '.join(queued)}. Each reuses the documentary's real "
            "narration (no re-synthesis) with vertical visuals and burned "
            "captions. Progress: 'status video job <id>'.")


def _cancel_latest(store: RepoJobStore) -> str:
    """'Jarvis, cancel my latest video job' — the most recent ACTIVE video
    job (native or ViMax), or an honest answer when nothing is running."""
    rows = _recent_jobs(store, limit=20)
    for row in rows:
        if row.get("status") in ("QUEUED", "INSTALLING", "RUNNING"):
            result = store.cancel(str(row["job_id"]))
            return (f"Cancelled {row['job_id']} "
                    f"({row.get('phase') or row.get('status')})."
                    if result.get("ok")
                    else f"Could not cancel {row['job_id']}: "
                         f"{result.get('error')}")
    return ("No video jobs are running right now — nothing to cancel.")


def _retry(store: RepoJobStore, job_id: str) -> str:
    """'Jarvis, retry the video job' — re-queue a FAILED/CANCELLED job with
    identical parameters (fresh job id, worker and deadline). Caps attempts
    (Phase 10: no infinite retry loops)."""
    from core.video_agent.resources import max_video_attempts
    cap = max_video_attempts()
    job = store.get(job_id) if job_id else None
    if job is None:
        rows = [r for r in _recent_jobs(store, limit=30)
                if r.get("status") in ("FAILED", "CANCELLED")]
        job = rows[0] if rows else None
    if job is None:
        return "No failed video jobs to retry."
    if job.get("status") not in ("FAILED", "CANCELLED"):
        return (f"{job.get('job_id')} is {job.get('status')} — retry is for "
                "failed or cancelled jobs.")
    attempts = int(job.get("attempts") or 0)
    if attempts >= cap:
        return (f"{job.get('job_id')} has already been attempted {attempts} "
                f"time(s) — the retry cap is {cap}. Change something about "
                "the request (different settings) and create a new job "
                "instead.")
    # SAFETY (Phase 10): never re-queue a job without its output directory —
    # the runner would refuse, but the honest answer belongs here too.
    if not str((json.loads(job.get("params") or "{}").get("out_dir")
                or "")).strip():
        return (f"{job.get('job_id')} has no output directory recorded — "
                "cannot retry it safely. Create the video again instead.")
    try:
        params = json.loads(job.get("params") or "{}")
    except Exception:                              # noqa: BLE001
        params = {}
    kind = str(job.get("kind") or "video")
    result = start_job(kind, str(job.get("ref") or ""), source="jarvis-retry",
                       params=params)
    if not result.get("ok"):
        return f"Could not retry: {result.get('error')}"
    if result.get("deduplicated"):
        return (f"An identical job is already active "
                f"({result.get('job_id')}) — nothing duplicated.")
    return (f"Retried as {result.get('job_id')} (was {job.get('job_id')}, "
            f"{job.get('status')}, attempt {attempts + 1}/{cap}). Same "
            "parameters, fresh worker — progress: 'status video job "
            f"{result.get('job_id')}'.")


def _artifacts_text(raw_dir: str) -> str:
    """'Jarvis, what video files exist in <folder>?' — result retrieval
    (Phase 5), local listing of the ViMax output dir."""
    try:
        out = _out_dir(raw_dir)
    except ValueError as exc:
        return str(exc)
    items = []
    for path in sorted(out.rglob("*")):
        if path.is_file() and path.suffix.lower() in (".mp4", ".mov", ".webm"):
            items.append((path, path.stat().st_size))
    if not items:
        return (f"No finished video files (mp4/mov/webm) under {out} yet. "
                "Completed jobs land in their render/ subfolder.")
    lines = [f"Video files under {out}:"]
    for path, size in items[:12]:
        lines.append(f"  • {path} ({size / 1024 / 1024:.1f} MB)")
    return "\n".join(lines)


def _where(store: RepoJobStore, job_id: str) -> str:
    """'Jarvis, where did you save my video?' — the finished file's path."""
    job = store.get(job_id) if job_id else None
    if job is None:
        rows = [r for r in _recent_jobs(store, limit=20)
                if r.get("status") == "COMPLETED"]
        job = rows[0] if rows else None
    if job is None:
        return ("No completed video jobs yet. Ask me to create a video and "
                "I will tell you where it lands.")
    outputs = _loads(job.get("outputs"))
    if not outputs:
        return (f"{job.get('job_id')} is {job.get('status')} — no file yet. "
                f"Current phase: {job.get('phase') or 'queued'}.")
    return (f"{job.get('job_id')} — saved to {outputs[0]} "
            f"({len(outputs)} file(s) total).")


def _job_text(job: dict, one_line: bool = False) -> str:
    status = str(job.get("status") or "?")
    pct = int(float(job.get("progress") or 0) * 100)
    base = (f"{job.get('job_id')} [{status}] {pct}% — "
            f"{str(job.get('message') or '')[:120]}")
    if one_line:
        return base
    outputs = _loads(job.get("outputs"))
    if status == "COMPLETED" and outputs:
        return (f"{job.get('job_id')} completed — video saved to "
                f"{outputs[0]}")
    if status == "NEEDS_CONFIGURATION":
        return (f"{job.get('job_id')} is waiting for provider configuration "
                f"({job.get('message') or 'missing API keys'})")
    if job.get("error"):
        # tail, not head: provider errors bury the real cause at the END of
        # their traceback ("... ClientError: 429 RESOURCE_EXHAUSTED"), while
        # the head is often framework noise (aiohttp deques, traceback lines)
        err = str(job['error'])
        base += (f" · error: …{err[-160:]}" if len(err) > 160
                 else f" · error: {err}")
    if (job.get("kind") == "run_skill" and status == "FAILED"):
        # the mission's honest alternative: offer the zero-cost path when AI
        # video generation is blocked — never silently substitute it
        base += ("\nA zero-cost alternative: 'Jarvis, create a documentary "
                 "about it' renders stills, narration and ffmpeg (clearly "
                 "labelled, never passed off as AI video).")
    return base


def _loads(raw) -> list:
    try:
        value = json.loads(raw) if isinstance(raw, str) else (raw or [])
        return value if isinstance(value, list) else []
    except Exception:
        return []


TOOL = {
    "name": "video_creator",
    "description": (
        "The Video Creation Agent: create finished videos in three modes, "
        "auto-detected from the request. DOCUMENTARY — narrated, researched, "
        "ffmpeg-edited pieces ('create a 10-minute documentary about the "
        "psychology of procrastination'); SHORTS — vertical 9:16 captioned "
        "clips ('make a 45-second YouTube Short about Bitcoin'); "
        "AI_CINEMATIC — generated shots via the installed ViMax skill "
        "('cinematic AI video of a futuristic city at night'); "
        "CHARACTER_ANIMATION — real articulated 3D characters (the "
        "permanent cast, stylized) dancing in a colorful playground with a "
        "directed 5-shot camera, original beat-locked music and local TTS "
        "narration ('make a dance video with the characters' — needs a "
        "narration text). Also: 'run a "
        "video smoke test' (machinery walk, real render, SIMULATED content), "
        "'show my video progress' (status), 'cancel my latest video job' "
        "(no id needed), 'retry the failed video job', 'make Shorts from my "
        "latest documentary', 'status/list video jobs', 'cancel video job "
        "rt-…', 'where is my video saved', 'which encoder will you use' "
        "(encoder status: Intel VAAPI H.264 with CPU fallback), 'show "
        "available video engines' (measured engine registry: Blender 3D, "
        "ViMax, Wan 2.1, LTX-Video, Stock footage, Canvas + custom), 'check "
        "whether my GPU supports LTX-Video' (engines + hardware reports — "
        "answer from their measured statuses, never guess), 'refresh my "
        "video hardware' (re-measures CPU/GPU/VAAPI/CUDA and re-classifies "
        "every engine), 'generate a hybrid documentary' (asks for the "
        "scene→engine table, e.g. 1:CANVAS,2:CANVAS,3:BLENDER_3D). Accepts "
        "topic, duration, language (e.g. Hindi), voice, style, aspect "
        "ratio, target platform, resolution, music, subtitles, fade "
        "transitions, encoder and save folder — anything missing uses the "
        "mode's default. "
        "PERMANENT CAST: every video features the user's two default "
        "characters by default (Psychology Expert + Little Boy from the "
        "character design sheet — their reference image is passed to "
        "image-capable tools and scripts are written as their lesson) "
        "unless characters='none' or different characters are named. "
        "Blender honesty: the sheet is an illustration, NOT a 3D model — "
        "Blender renders the original procedural robot avatar instead. "
        "EVERY create goes through mandatory method approval (mission "
        "Phase 6): the first create call returns the plan and the question "
        "'Which method would you like to use for this video?' with a token "
        "— relay it to the user, then call create again with the SAME "
        "parameters plus approve_token=<token> and method=<chosen id>. "
        "Nothing is generated, queued or downloaded before that approval; "
        "tokens are single-use and expire. "
        "AI_CINEMATIC uses PAID cloud providers: choosing VIMAX at "
        "approval accepts the cost (the legacy confirm=true also works)."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": ("create | status | list | cancel | retry | "
                                "ready | stages | providers | encoder | "
                                "engines | hardware | approvals | "
                                "cinematic | split | artifacts | where"),
            },
            "mode": {
                "type": "STRING",
                "description": ("DOCUMENTARY | AI_CINEMATIC | SHORTS — "
                                "auto-detected from the request when "
                                "omitted."),
            },
            "request": {
                "type": "STRING",
                "description": ("The user's original phrasing, for mode/"
                                "duration detection ('make a 45-second "
                                "Short about Bitcoin')."),
            },
            "topic": {
                "type": "STRING",
                "description": ("The video idea/topic, e.g. 'the psychology "
                                "of procrastination'."),
            },
            "duration_s": {
                "type": "INTEGER",
                "description": ("Target total length in seconds. Mode "
                                "defaults: documentary 600 (60–900), "
                                "cinematic 10 (10–600), shorts 45 (15–90)."),
            },
            "style": {
                "type": "STRING",
                "description": ("Visual style, e.g. 'calm professional "
                                "documentary', 'dark cinematic', 'anime'."),
            },
            "aspect_ratio": {
                "type": "STRING",
                "description": "16:9 (default), 9:16 (shorts) or 1:1.",
            },
            "resolution": {
                "type": "STRING",
                "description": "1080p (default) or 720p.",
            },
            "language": {
                "type": "STRING",
                "description": "Narration/on-screen language (default simple English).",
            },
            "voice": {
                "type": "STRING",
                "description": "Narrator voice preference, e.g. 'calm male' "
                               "(resolved by the TTS engine).",
            },
            "music": {
                "type": "STRING",
                "description": "Music preference, e.g. 'ambient, low' / "
                               "'upbeat' / 'none'.",
            },
            "subtitles": {
                "type": "BOOLEAN",
                "description": "Burn captions into the video (shorts default "
                               "true).",
            },
            "transitions": {
                "type": "BOOLEAN",
                "description": ("true → fade-through-black transitions between "
                                "scene clips (Shorts production), with matching "
                                "audio fades. Default false."),
            },
            "encoder": {
                "type": "STRING",
                "description": ("auto (default) | vaapi | libx264. Auto uses "
                                "the verified Intel VAAPI H.264 encoder and "
                                "falls back to CPU libx264 automatically."),
            },
            "target_platform": {
                "type": "STRING",
                "description": "youtube | telegram | local (default local).",
            },
            "full": {
                "type": "BOOLEAN",
                "description": ("true → render the full documentary now; "
                                "default is sample-first (a ~30s verification "
                                "cut) for long documentaries."),
            },
            "user_assets_dir": {
                "type": "STRING",
                "description": ("Folder with the user's own images (jpg/png) "
                                "to use as visuals, matched to chapters in "
                                "order. License recorded as user-provided."),
            },
            "music_file": {
                "type": "STRING",
                "description": ("Path to the user's own background music "
                                "file, mixed at low volume."),
            },
            "audience": {
                "type": "STRING",
                "description": "Intended audience, e.g. 'beginners'.",
            },
            "script": {
                "type": "STRING",
                "description": ("A user-provided narration script (max 8000 "
                                "chars) — cinematic renders it directly via "
                                "ViMax script2video, skipping the LLM story "
                                "stage."),
            },
            "image": {
                "type": "STRING",
                "description": ("Path to the user's own image — cinematic "
                                "IMAGE_TO_VIDEO via ViMax i2v (image + "
                                "prompt)."),
            },
            "prompt": {
                "type": "STRING",
                "description": "Motion/scene description for image-to-video.",
            },
            "scenes": {
                "type": "STRING",
                "description": ("Scene descriptions (one per line or a text "
                                "block) — cinematic SCENE_TO_VIDEO via "
                                "script2video."),
            },
            "count": {
                "type": "INTEGER",
                "description": "How many Shorts to split a documentary into "
                               "(split action, default 3, max 6).",
            },
            "branding": {
                "type": "STRING",
                "description": "Optional branding text drawn on Shorts cards "
                               "(e.g. a channel name).",
            },
            "narration": {
                "type": "STRING",
                "description": "Narration requirements, e.g. 'calm male voice-over'.",
            },
            "save_dir": {
                "type": "STRING",
                "description": ("Output folder (default ~/Videos/JARVIS). "
                                "Never inside code or config directories."),
            },
            "smoke": {
                "type": "BOOLEAN",
                "description": ("true → machinery smoke walk: full stage "
                                "machine with SIMULATED content and a real "
                                "ffmpeg render."),
            },
            "job_id": {
                "type": "STRING",
                "description": "rt-… job id for status/cancel/where.",
            },
            "refresh": {
                "type": "BOOLEAN",
                "description": ("engines/hardware actions: force a fresh "
                                "measured probe instead of the cached one "
                                "(Phase 11 hardware-refresh rule)."),
            },
            "confirm": {
                "type": "BOOLEAN",
                "description": ("Must be true to accept the paid provider "
                                "cost and start an AI_CINEMATIC render."),
            },
            "approve_token": {
                "type": "STRING",
                "description": ("Approval token issued by a previous create "
                                "call. Mandatory to start ANY video: Jarvis "
                                "asks 'Which method would you like to use "
                                "for this video?' and the user's choice is "
                                "bound to this token (single-use, "
                                "expiring, project-bound)."),
            },
            "method": {
                "type": "STRING",
                "description": ("Engine the user chose at approval time: "
                                "CANVAS | BLENDER_3D | VIMAX | WAN_2_1 | "
                                "LTX_VIDEO | STOCK_FOOTAGE | CUSTOM_"
                                "VIDEO_MODEL (or 'hybrid' with a scene "
                                "plan). Consumed together with "
                                "approve_token."),
            },
        },
        "required": ["action"],
    },
    "handler": video_creator,
}
