"""
core/video_agent/runner.py — video job executor.

Runs ONE video job through the stage machine. Called from the detached worker
(scripts/repo_agent_run.py, kind="video") with the store's progress/cancel
callbacks, and directly from tests with fake callbacks — the orchestration is
identical either way.

Persistence contract (Phase 2):
  * stage machine rides the job row's `phase`/`progress` columns — visible to
    voice (`status video job …`), Hermes and RIO without any new store
  * each completed stage is recorded in `<run_dir>/stage_state.json` so a
    later resume implementation (Phase 4+) has a durable foothold
  * restart safety: the store's recover_stale() marks a video job whose worker
    process died as FAILED ("interrupted — start the job again"), so nothing
    is ever stuck in RUNNING; QUEUED jobs survive a restart and are spawned
    by the next dispatch_queued() (status poll or any start_job call)
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from core.repo_agent.manager import RepoCancelled
from core.video_agent import pipelines
from core.video_agent.spec import (STAGE_PROGRESS, VIDEO_STAGES, job_state)

ProgressFn = Callable[[str, float, str], None]
CancelFn = Callable[[], bool]
LogFn = Callable[[str], None]

_NOOP_LOG: LogFn = lambda _m: None          # noqa: E731


def execute_video_job(job: dict, params: dict, *, progress: ProgressFn,
                      cancelled: CancelFn, log: LogFn | None = None,
                      deadline: float | None = None,
                      registry: dict | None = None) -> dict[str, Any]:
    """Walk VIDEO_STAGES for this job. Returns the worker result dict
    ({status, outputs, message, …}); raises RepoCancelled when the user
    cancels between stages (the worker maps that to status CANCELLED)."""
    log = log or _NOOP_LOG
    spec = dict((params or {}).get("spec") or {})
    out_dir = Path(str((params or {}).get("out_dir")
                       or spec.get("out_dir") or "")).expanduser()
    pipeline_name = str(spec.get("pipeline") or "smoke")
    # SAFETY (Phase 10): an empty or relative out_dir would resolve to the
    # worker's CWD — the repository root. Never render there; fail honestly.
    if not str(out_dir).strip() or not out_dir.is_absolute():
        raise pipelines.StageError(
            "video job has no absolute output directory configured — "
            "refusing to render into the working directory (the job row "
            "should carry out_dir)")
    if registry is not None:
        stages = registry.get(pipeline_name)
    else:
        stages = pipelines.stages_for(pipeline_name)
    if not stages:
        raise pipelines.StageNotImplemented(
            f"pipeline '{pipeline_name}' is not implemented yet — the stage "
            "machine and worker are live, this mode's stages arrive in a "
            "later phase of the video upgrade")

    out_dir.mkdir(parents=True, exist_ok=True)
    state_path = out_dir / "stage_state.json"
    state: dict[str, Any] = {"pipeline": pipeline_name,
                             "mode": spec.get("mode"),
                             "topic": spec.get("topic"),
                             "completed_stages": [], "stage_notes": {}}
    ctx = {"run_dir": out_dir, "spec": spec, "log": log, "job_id": job.get("job_id")}
    notes: dict[str, Any] = {}
    outputs: list[str] = []

    log(f"[video] job {job.get('job_id')} mode={spec.get('mode')} "
        f"pipeline={pipeline_name} topic={str(spec.get('topic'))[:60]!r}")
    started = time.time()

    job_started = time.time()
    for stage in VIDEO_STAGES:
        if cancelled():
            log(f"[video] cancelled before stage {stage}")
            raise RepoCancelled(
                f"cancelled before stage {stage} "
                f"(completed: {', '.join(state['completed_stages']) or 'none'})")
        if deadline is not None and time.time() > deadline:
            return {"status": "FAILED", "outputs": outputs,
                    "error": "video job exceeded its deadline",
                    "message": f"deadline hit before stage {stage}"}
        stage_fn = stages.get(stage)
        if stage_fn is None:
            raise pipelines.StageNotImplemented(
                f"stage '{stage}' of pipeline '{pipeline_name}' is not "
                "implemented yet")
        if stage == "planning":
            # mission rule: check free disk BEFORE starting (Phase 10)
            from core.video_agent.resources import ensure_disk
            free = ensure_disk(out_dir)
            log(f"[resources] {free:.1f} GB free under {out_dir} — ok")
            # and free RAM (Phase 2 encoder upgrade): a single render must not
            # start into an already-swapping machine
            from core.video_agent.encoder import ensure_memory
            mem = ensure_memory()
            log(f"[resources] {mem:.0f} MB RAM available — ok")
        progress(stage, STAGE_PROGRESS[stage],
                 f"{stage} ({pipeline_name} pipeline)")
        stage_started = time.time()
        try:
            if stage == "editing":
                # ONE heavy render at a time across worker processes
                from core.video_agent.resources import RenderLock
                from core.video_agent.encoder import ensure_memory
                ensure_memory()
                with RenderLock(out_dir, log=log):
                    note = stage_fn(ctx) or {}
            else:
                note = stage_fn(ctx) or {}
        except pipelines.StageError as exc:
            raise pipelines.StageError(f"{stage}: {exc}") from None
        except Exception as exc:                   # noqa: BLE001
            # unexpected failures get the stage prefix too — a FAILED job row
            # must say WHERE it broke, not just how
            raise pipelines.StageError(
                f"{stage}: {type(exc).__name__}: {str(exc)[:300]}") from exc
        state["completed_stages"].append(stage)
        state["stage_notes"][stage] = note
        state["last_completed_at"] = time.time()
        state_path.write_text(json.dumps(state, indent=1), encoding="utf-8")
        notes.update(note if isinstance(note, dict) else {stage: note})
        for candidate in ("output", "plan"):
            value = note.get(candidate) if isinstance(note, dict) else None
            if value and value not in outputs:
                outputs.append(str(value))
        log(f"[video] stage {stage} done in "
            f"{time.time() - stage_started:.1f}s")

    # the final MP4 is the output that matters for the library/`where`
    final = notes.get("output")
    if final:
        outputs = [str(final)] + [o for o in outputs if o != str(final)]

    return {
        "status": "COMPLETED",
        "outputs": outputs,
        "runtime_s": round(time.time() - job_started, 1),
        "simulated": pipeline_name == "smoke",
        "stage_notes": notes,
        "message": ("SIMULATED smoke render complete — machinery verified "
                    "(stages: " + ", ".join(VIDEO_STAGES) + ")"
                    if pipeline_name == "smoke" else
                    f"{spec.get('mode')} render complete"
                    + (f" — {notes['encoder']} encoder"
                       if notes.get("encoder") else "")),
        "state_view": "COMPLETED",
        "pipeline": pipeline_name,
        "duration_s": notes.get("duration_s"),
    }


def describe_job(job: dict | None) -> str:
    """Human one-liner using the mission-named states (for voice/RIO)."""
    if not job:
        return "No such video job."
    jid = job.get("job_id")
    state = job_state(job)
    pct = int(float(job.get("progress") or 0) * 100)
    line = f"{jid} [{state}] {pct}%"
    msg = str(job.get("message") or "")[:140]
    if msg:
        line += f" — {msg}"
    if job.get("error"):
        err = str(job["error"])
        line += f" · error: …{err[-120:]}" if len(err) > 120 else f" · error: {err}"
    outputs = job.get("outputs")
    if isinstance(outputs, str):
        try:
            outputs = json.loads(outputs)
        except Exception:                     # noqa: BLE001
            outputs = []
    if state == "COMPLETED" and outputs:
        line += f" — saved to {outputs[0]}"
    if job.get("kind") == "run_skill" and state == "FAILED":
        # the mission's honest alternative: when AI video generation is
        # blocked, offer the zero-cost path — never a silent substitution
        line += ("\nA zero-cost alternative: 'Jarvis, create a documentary "
                 "about it' renders stills, narration and ffmpeg (clearly "
                 "labelled, never passed off as AI video).")
    return line
