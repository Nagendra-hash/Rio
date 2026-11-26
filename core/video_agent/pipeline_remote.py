"""
core/video_agent/pipeline_remote.py — Phase 7: the shared stage machine for
remote-GPU engines (WAN_2_1, LTX_VIDEO).

The engines expose this as their `stages()` (the Phase 2 engine-owned
pipeline seam — pipelines.stages_for resolves engine ids), so Wan and LTX
plug into the existing detached-worker stage machine without touching the
core orchestration.

Honest contract on this machine (no CUDA driver — measured):
  * with no configured/verified remote worker the PLANNING stage refuses
    with a clear NEEDS_CONFIGURATION-style message — nothing is faked
  * with a verified worker, generation runs provider-side (ssh/scp
    transport, explicit user configuration) and the fetched file is
    normalized to the output contract and ffprobe-validated
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from core.video_agent import remote
from core.video_agent.pipelines import FFMPEG, StageError, probe_video
from core.video_agent.resources import atomic_final, cleanup_run_dir, \
    ffmpeg_threads
from core.video_agent.encoder import run_encode


def remote_stages(engine_id: str) -> dict[str, Callable[[dict], dict | None]]:
    """The stage dict for one remote engine (engine-owned pipeline)."""

    def _worker_for(ctx: dict) -> dict:
        spec = ctx["spec"]
        wanted = str(spec.get("remote_worker") or "").strip()
        worker = remote.get_worker(wanted) if wanted else None
        if worker is None:
            cands = [w for w in remote.workers() if remote.is_verified(w)]
            if not cands:
                raise StageError(
                    f"{engine_id}: no verified remote GPU worker — "
                    "configure one under repo_agent."
                    "video_remote_workers and verify it ('check remote "
                    "worker <name>'). Nothing was sent anywhere.")
            worker = cands[0]
        return worker

    def planning(ctx: dict) -> dict | None:
        run_dir, spec, log = ctx["run_dir"], ctx["spec"], ctx["log"]
        from core.video_agent.resources import ensure_disk
        ensure_disk(run_dir)
        worker = _worker_for(ctx)
        log(f"[{engine_id.lower()}] remote worker: "
            f"{worker.get('name')} ({worker.get('host')})")
        return {"worker": worker.get("name"), "host": worker.get("host"),
                "mode": "remote-inference"}

    def scripting(ctx: dict) -> dict | None:
        spec = ctx["spec"]
        prompt = str(spec.get("topic") or "")
        scenes = spec.get("scene_prompts") or []
        if scenes:
            prompt = " | ".join(str(s) for s in scenes[:4])
        return {"prompt": prompt[:2000], "llm_used": False}

    def asset_collection(ctx: dict) -> dict | None:
        return {"assets": "none — the remote worker generates from the "
                          "prompt"}

    def generating(ctx: dict) -> dict | None:
        run_dir, spec, log = ctx["run_dir"], ctx["spec"], ctx["log"]
        worker = _worker_for(ctx)
        request = {
            "engine": engine_id,
            "prompt": str(spec.get("topic") or "")[:2000],
            "duration_s": int(spec.get("duration_s") or 5),
            "aspect_ratio": spec.get("aspect_ratio") or "16:9",
            "resolution": spec.get("resolution") or "720p",
        }
        out_dir = run_dir / "remote"
        try:
            deadline = int(ctx.get("deadline_s") or 3600)
        except (TypeError, ValueError):
            deadline = 3600
        result = remote.submit_and_fetch(
            worker, request, out_dir / "result.mp4",
            deadline_s=deadline,
            progress=None, cancel=None, log=log)
        if result.get("status") != "COMPLETED":
            raise StageError(f"{engine_id} remote generation failed: "
                             f"{result.get('error', 'unknown')[:300]}")
        log(f"[{engine_id.lower()}] fetched remote result")
        return {"remote_output": result.get("output"),
                "host": worker.get("host")}

    def editing(ctx: dict) -> dict | None:
        """Normalize the fetched clip to the output contract (H.264+AAC,
        fps, aspect) with the VAAPI→CPU encoder ladder."""
        run_dir, spec, log = ctx["run_dir"], ctx["spec"], ctx["log"]
        from core.video_agent.resources import ensure_disk
        ensure_disk(run_dir)
        fetched = run_dir / "remote" / "result.mp4"
        if not fetched.is_file():
            raise StageError(f"fetched remote result missing: {fetched}")
        final = run_dir / "render" / "output.mp4"
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = final.with_suffix(".mp4.part")
        w, h = ((1280, 720) if (spec.get("aspect_ratio") or "16:9") == "16:9"
                else (720, 1280))

        def build(enc):
            args = [FFMPEG, "-y", *enc.input_args(), "-i", str(fetched)]
            args += ["-vf", enc.upload_filter(
                f"scale={w}:{h}:force_original_aspect_ratio="
                f"decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2")]
            args += enc.video_codec_args(threads=ffmpeg_threads())
            args += ["-r", "30", "-c:a", "aac", "-b:a", "128k",
                     "-f", "mp4", "-movflags", "+faststart", str(tmp)]
            return args, enc.env()

        result = run_encode(build, cwd=run_dir, log=log,
                            what=f"{engine_id.lower()} normalize",
                            prefer=str(spec.get("encoder") or "") or None)
        atomic_final(tmp, final)
        return {"output": str(final), "encoder": result["backend"],
                "normalized_from": str(fetched)}

    def validating(ctx: dict) -> dict | None:
        run_dir = ctx["run_dir"]
        final = run_dir / "render" / "output.mp4"
        if not final.is_file() or final.stat().st_size == 0:
            raise StageError("validated: render/output.mp4 missing or empty")
        facts = probe_video(final)
        if facts["video_codec"] != "h264" or facts["audio_codec"] != "aac":
            raise StageError(f"validated: unexpected codecs {facts}")
        if facts["duration_s"] < 2:
            raise StageError(f"validated: too short ({facts['duration_s']}s)")
        (run_dir / "validation.json").write_text(json.dumps(
            {"ok": True, "ffprobe": facts, "mode": engine_id.lower(),
             "ai_generated": True,
             "note": f"shots generated remotely ({engine_id})"},
            indent=1), encoding="utf-8")
        cleanup_run_dir(run_dir)
        return {"validated": facts, "ai_generated": True}

    return {
        "planning": planning,
        "scripting": scripting,
        "asset_collection": asset_collection,
        "generating": generating,
        "editing": editing,
        "validating": validating,
    }
