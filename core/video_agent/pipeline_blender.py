"""
core/video_agent/pipeline_blender.py — the BLENDER_3D pipeline.

Builds a 3D scene with Blender (bpy), renders frames, encodes with FFmpeg
(VAAPI with CPU fallback), and validates the output. Purely offline after
the Blender standalone is installed — no paid APIs, no network calls.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from core.repo_agent.jobs import config as jobs_config
from core.video_agent import encoder as enc_mod
from core.video_agent.pipelines import FFMPEG, StageError, probe_video
from core.video_agent.resources import atomic_final, cleanup_run_dir, \
    ffmpeg_threads
from core.video_agent.spec import VIDEO_STAGES

BLENDER_BIN = os.environ.get("JARVIS_BLENDER_BIN") or \
    str(Path.home() / "JarvisTools" / "blender-3.6.23-linux-x64" / "blender")

SCENE_BUILD_SCRIPT = Path(__file__).parent / "blender" / "build_robot_scene.py"


def _ffmpeg(args: list[str], cwd: Path, log: Callable[[str], None],
            what: str, timeout: int = 900) -> None:
    proc = subprocess.run([str(a) for a in args], cwd=str(cwd), timeout=timeout,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise StageError(f"{what} failed (exit {proc.returncode}): "
                         f"{(proc.stderr or '')[-400:]}")
    log(f"[ffmpeg] {what}: ok")


def _run_blender(args: list[str], cwd: Path, log: Callable[[str], None],
                 what: str, timeout: int = 600) -> None:
    proc = subprocess.run([str(a) for a in args], cwd=str(cwd), timeout=timeout,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip()[-500:]
        raise StageError(f"{what} failed (exit {proc.returncode}): {tail}")
    if proc.stdout:
        for line in proc.stdout.strip().splitlines():
            if line.startswith("SCENE_BUILT"):
                log(f"[blender] {line}")
    log(f"[blender] {what}: ok")


def plan_blender(ctx: dict) -> dict | None:
    """Create the .blend file from the Python scene builder."""
    run_dir, spec = ctx["run_dir"], ctx["spec"]
    blend_path = run_dir / "robot.blend"
    log = ctx["log"]

    from core.video_agent.resources import ensure_disk
    ensure_disk(run_dir)

    if not SCENE_BUILD_SCRIPT.is_file():
        raise StageError(f"Scene builder not found: {SCENE_BUILD_SCRIPT}")

    res_w = 720 if str(spec.get("resolution") or "720p") == "720p" else 1080
    res_h = 1280 if res_w == 720 else 1920
    fps = int(spec.get("duration_s") or 15)
    duration = int(spec.get("duration_s") or 15)
    end_frame = fps * duration

    log(f"[blender] building scene: {res_w}x{res_h} @ {fps}fps, "
        f"{duration}s ({end_frame} frames)")

    _run_blender([
        BLENDER_BIN, "-b", "--factory-startup",
        "--python", str(SCENE_BUILD_SCRIPT), "--",
        "--out", str(blend_path),
        "--res", f"{res_w}x{res_h}",
        "--fps", str(fps),
        "--duration", str(duration),
    ], cwd=run_dir, log=log, what="scene build")

    if not blend_path.is_file() or blend_path.stat().st_size < 1000:
        raise StageError(".blend file missing or too small after build")

    log(f"[blender] scene built: {blend_path} ({blend_path.stat().st_size} bytes)")
    plan_data = {"frames": end_frame, "fps": fps, "resolution": f"{res_w}x{res_h}",
                 "duration_s": duration}
    (run_dir / "plan.json").write_text(json.dumps(plan_data, indent=1))
    return {"blend": str(blend_path), "frames": end_frame, "fps": fps,
            "resolution": f"{res_w}x{res_h}"}


def no_free_generation(ctx: dict) -> dict | None:
    """No free AI generation — the Blender path IS the generation."""
    return {"generated": "blender", "note": "3D scene built and rendered by Blender"}


def _render_frames(ctx: dict, run_dir: Path, log: Callable[[str], None]) -> dict:
    """Render all frames to PNG sequence."""
    blend_path = run_dir / "robot.blend"
    frames_dir = run_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    plan = json.loads((run_dir / "plan.json").read_text())
    end_frame = int(plan.get("frames") or 360)
    fps = int(plan.get("fps") or 24)

    log(f"[blender] rendering {end_frame} frames @ {fps}fps → {frames_dir}")

    t0 = time.time()
    _run_blender([
        BLENDER_BIN, "-b", str(blend_path),
        "-o", str(frames_dir / "frame_"),
        "-F", "PNG",
        "-s", "1", "-e", str(end_frame),
        "-f", f"1..{end_frame}",
    ], cwd=run_dir, log=log, what=f"render {end_frame} frames")

    elapsed = round(time.time() - t0, 2)
    count = len(list(frames_dir.glob("frame_*.png")))
    if count != end_frame:
        raise StageError(f"Expected {end_frame} frames, got {count}")
    log(f"[blender] rendered {count} frames in {elapsed}s")
    return {"frames": count, "elapsed_s": elapsed, "fps": fps}


def _encode_video(ctx: dict, run_dir: Path, log: Callable[[str], None]) -> dict:
    """Encode PNG sequence to MP4 using CPU libx264 (reliable on this hardware)."""
    frames_dir = run_dir / "frames"
    plan = json.loads((run_dir / "plan.json").read_text())
    fps = int(plan.get("fps") or 24)
    duration = int(plan.get("duration_s") or 15)
    final = run_dir / "render" / "robot_adventure.mp4"
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp_final = final.with_suffix(".mp4.part")

    def build(enc):
        args = [FFMPEG, "-y", *enc.input_args(),
                "-framerate", str(fps),
                "-i", str(frames_dir / "frame_%04d.png"),
                # Generate silent audio track matching video duration
                "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo",
                "-t", str(duration)]
        args += ["-c:v", "libx264", "-preset", "veryfast", "-b:v", "5M",
                 "-maxrate", "7M", "-bufsize", "10M", "-threads", "4"]
        args += ["-c:a", "aac", "-b:a", "128k", "-ar", "44100",
                 "-map", "0:v", "-map", "1:a",
                 "-f", "mp4", "-movflags", "+faststart", str(tmp_final)]
        return args, enc.env()

    result = enc_mod.run_encode(build, cwd=run_dir, log=log,
                                what="encode to MP4", prefer="libx264")
    atomic_final(tmp_final, final)

    facts = probe_video(final)
    log(f"[blender] encoded: {facts.get('width')}x{facts.get('height')} "
        f"@ {facts.get('duration_s')}s, {facts.get('video_codec')}"
        f"/{facts.get('audio_codec')}")
    return {"output": str(final), "encoder": result["backend"],
            "elapsed_s": result["elapsed_s"], "ffprobe": facts}


def generate_blender(ctx: dict) -> dict | None:
    """GENERATING: render frames, then encode."""
    run_dir, log = ctx["run_dir"], ctx["log"]
    _render_frames(ctx, run_dir, log)
    enc = _encode_video(ctx, run_dir, log)
    return enc


def validate_blender(ctx: dict) -> dict | None:
    """VALIDATING: verify the final MP4."""
    run_dir = ctx["run_dir"]
    final = run_dir / "render" / "robot_adventure.mp4"
    if not final.is_file() or final.stat().st_size == 0:
        raise StageError("validated: render/robot_adventure.mp4 missing or empty")
    facts = probe_video(final)
    if facts["video_codec"] != "h264" or facts["audio_codec"] != "aac":
        raise StageError(f"validated: unexpected codecs {facts}")
    if (facts["width"], facts["height"]) != (720, 1280):
        raise StageError(f"validated: unexpected dimensions {facts}")
    if facts["duration_s"] < 10:
        raise StageError(f"validated: suspiciously short ({facts['duration_s']}s)")
    (run_dir / "validation.json").write_text(json.dumps(
        {"ok": True, "ffprobe": facts, "mode": "blender_3d"},
        indent=1), encoding="utf-8")
    cleanup_run_dir(run_dir)
    return {"validated": facts}


STAGES = {
    "planning": plan_blender,
    "scripting": lambda ctx: {"scripted": "blender (no script needed)"},
    "asset_collection": lambda ctx: {"assets": "procedural (no assets needed)"},
    "generating": generate_blender,
    "editing": lambda ctx: {"edited": "encoded in generating stage"},
    "validating": validate_blender,
}