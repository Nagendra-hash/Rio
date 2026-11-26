"""
core/video_agent/pipelines.py — stage-runner registry for video pipelines.

A pipeline is a dict {stage_name: fn(ctx) -> dict|None}. `ctx` carries the
run directory, the spec and a log callable. The runner (runner.py) walks
VIDEO_STAGES in order, so a pipeline only implements what it needs; a stage
missing from the dict is an honest StageNotImplemented.

Phase 2 registers exactly ONE pipeline:

  smoke — walks the full stage machine (planning → … → validating) with
  SIMULATED content (colour cards + tone audio instead of AI visuals/narration)
  and a REAL ffmpeg render + ffprobe validation. It proves the machinery
  end-to-end and is always labelled SIMULATED — it is never presented as
  AI-generated content.

The documentary (Phase 4) and shorts (Phase 7) pipelines register here later;
AI_CINEMATIC keeps routing through the existing ViMax path and is therefore
not a registry pipeline at all.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from core.video_agent.engines import registry as engine_registry

from core.video_agent.spec import VIDEO_STAGES

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"
FFMPEG_TIMEOUT_S = 120          # per single ffmpeg/ffprobe call, Phase-2 smoke

# Software-only decode env — prevents any ffmpeg subprocess from attempting
# VAAPI / DRM hardware acceleration. This machine's Intel HD 4400 (Haswell)
# reports VA-API devices but cannot actually drive them; every attempt to
# create a VA context emits assertions or aborts. AV1 content in particular
# must always use libdav1d (software); forcing VAAPI on it crashes hard.
# These vars are merged into every subprocess env so ffprobe / ffmpeg calls
# that incidentally encounter an AV1 stream never hit the hardware path.
_FFMPEG_ENV: dict[str, str] = {
    "VA_DISABLE_DRM": "1",
    "VA_NO_HARDWARE": "1",
    "LIBVA_MUTABLE_CONFIG": "0",
}


class StageError(RuntimeError):
    """A stage failed; message is safe to show the user (no secrets)."""


class StageNotImplemented(StageError):
    """The stage/mode pipeline has not been built yet (Phase 4/7)."""


def _run(args: list[str], cwd: Path, log: Callable[[str], None],
         what: str, timeout: int = FFMPEG_TIMEOUT_S) -> str:
    """Argument-array subprocess with timeout — the house subprocess rule.

    Merges `_FFMPEG_ENV` into the child environment so no ffmpeg call ever
    attempts VAAPI / DRM hardware decoding. On this machine those paths assert
    or return `-EINVAL`; software fallback is faster than crashing.
    """
    base_env = dict(os.environ)
    base_env.update(_FFMPEG_ENV)
    proc = subprocess.run([str(a) for a in args], cwd=str(cwd),
                          timeout=timeout, capture_output=True, text=True,
                          env=base_env)
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-500:].strip()
        raise StageError(f"{what} failed (exit {proc.returncode}): {tail}")
    if proc.stderr:
        log(f"[ffmpeg] {what}: ok")
    return proc.stdout or ""


def probe_video(path: Path) -> dict[str, Any]:
    """ffprobe → {duration, width, height, video_codec, audio_codec}. Honest
    structure check — the mission forbids partial/corrupt outputs."""
    out = _run([FFPROBE, "-v", "quiet", "-print_format", "json",
                "-show_format", "-show_streams", path],
               cwd=path.parent, log=lambda _m: None, what="ffprobe")
    data = json.loads(out or "{}")
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    return {
        "duration_s": float(fmt.get("duration") or 0.0),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "video_codec": str(video.get("codec_name") or ""),
        "audio_codec": str(audio.get("codec_name") or ""),
    }


# ── smoke pipeline: real machinery, simulated content ────────────────────────

_SMOKE_SCENE_S = 5          # two scenes → 10 s fixed machinery render
_SMOKE_W, _SMOKE_H = 640, 360
_SMOKE_COLORS = ("0x1b2a4a", "0x4a1b2a")


def _smoke_plan(ctx: dict) -> dict | None:
    run_dir, spec = ctx["run_dir"], ctx["spec"]
    plan = {
        "pipeline": "smoke",
        "simulated": True,
        "topic": spec.get("topic"),
        "mode": spec.get("mode"),
        "scenes": [{"id": 1, "duration_s": _SMOKE_SCENE_S},
                   {"id": 2, "duration_s": _SMOKE_SCENE_S}],
        "note": ("SIMULATED machinery walk — content is colour cards + tone "
                 "audio; proves the stage machine, ffmpeg render and ffprobe "
                 "validation end-to-end."),
    }
    (run_dir / "plan.json").write_text(json.dumps(plan, indent=1),
                                       encoding="utf-8")
    return {"plan": str(run_dir / "plan.json")}


def _smoke_script(ctx: dict) -> dict | None:
    run_dir, spec = ctx["run_dir"], ctx["spec"]
    script_dir = run_dir / "script"
    script_dir.mkdir(exist_ok=True)
    scenes = [{"id": 1, "text": f"[SIMULATED] Scene one about "
                                f"{spec.get('topic')}",
               "duration_s": _SMOKE_SCENE_S},
              {"id": 2, "text": "[SIMULATED] Scene two — closing card.",
               "duration_s": _SMOKE_SCENE_S}]
    (script_dir / "scenes.json").write_text(json.dumps(scenes, indent=1),
                                            encoding="utf-8")
    (script_dir / "script.txt").write_text(
        "\n\n".join(f"Scene {s['id']} ({s['duration_s']}s): {s['text']}"
                    for s in scenes) + "\n", encoding="utf-8")
    return {"scenes": len(scenes)}


def _smoke_assets(ctx: dict) -> dict | None:
    run_dir = ctx["run_dir"]
    assets = run_dir / "assets"
    assets.mkdir(exist_ok=True)
    for i, color in enumerate(_SMOKE_COLORS, start=1):
        _run([FFMPEG, "-y", "-f", "lavfi",
              "-i", f"color=c={color}:s={_SMOKE_W}x{_SMOKE_H}:d=1",
              "-frames:v", "1", assets / f"scene_{i}.png"],
             run_dir, ctx["log"], f"scene {i} card")
    # SIMULATED narration placeholder: a tone track, replaced by real TTS in
    # Phase 3+. Same total duration as the video.
    _run([FFMPEG, "-y", "-f", "lavfi",
          "-i", f"sine=frequency=330:duration={2 * _SMOKE_SCENE_S}",
          "-ar", "44100", "-ac", "2",
          assets / "narration_simulated.wav"],
         run_dir, ctx["log"], "narration placeholder")
    return {"assets": 2, "simulated": True}


def _smoke_generate(ctx: dict) -> dict | None:
    # No AI generation in smoke mode — this stage exists so the walk covers
    # the real order; Phase 4/7 plug genuine generation here.
    ctx["log"]("[smoke] generating stage: no AI generation (simulated mode)")
    return {"generated": 0, "simulated": True}


def _smoke_edit(ctx: dict) -> dict | None:
    run_dir = ctx["run_dir"]
    render = run_dir / "render"
    render.mkdir(exist_ok=True)
    out = render / "smoke.mp4"
    concat = run_dir / "concat.txt"
    concat.write_text(
        "\n".join(f"file 'assets/scene_{i}.png'\nduration {_SMOKE_SCENE_S}"
                  for i in range(1, len(_SMOKE_COLORS) + 1))
        + f"\nfile 'assets/scene_{len(_SMOKE_COLORS)}.png'\n",
        encoding="utf-8")
    fade_out_st = 2 * _SMOKE_SCENE_S - 0.5
    # output path RELATIVE to cwd (run_dir): passing out.name would drop the
    # file in run_dir and validating would find render/ empty. Atomic
    # (Phase 10): encode to .part, os.replace on success.
    from core.video_agent.resources import atomic_final, ffmpeg_threads
    tmp_final = run_dir / "render" / "smoke.mp4.part"
    _run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", concat.name,
          "-i", "assets/narration_simulated.wav",
          "-filter_complex",
          "[0:v]fps=25,format=yuv420p,"
          "fade=t=in:d=0.5,fade=t=out:st=" + str(fade_out_st) + ":d=0.5[v]",
          "-map", "[v]", "-map", "1:a",
          "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
          "-c:a", "aac", "-b:a", "96k",
          "-t", str(2 * _SMOKE_SCENE_S), "-f", "mp4", "-movflags", "+faststart",
          tmp_final.relative_to(run_dir)],
         run_dir, ctx["log"], "render smoke.mp4")
    atomic_final(tmp_final, out)
    return {"output": str(out), "duration_s": 2 * _SMOKE_SCENE_S,
            "resolution": f"{_SMOKE_W}x{_SMOKE_H}", "simulated": True}


def _smoke_validate(ctx: dict) -> dict | None:
    run_dir = ctx["run_dir"]
    out = run_dir / "render" / "smoke.mp4"
    if not out.is_file() or out.stat().st_size == 0:
        raise StageError("validated: render/smoke.mp4 missing or empty")
    facts = probe_video(out)
    expect = {"duration_s": (2 * _SMOKE_SCENE_S - 1.0, 2 * _SMOKE_SCENE_S + 2.0),
              "width": _SMOKE_W, "height": _SMOKE_H,
              "video_codec": "h264", "audio_codec": "aac"}
    lo, hi = expect["duration_s"]
    if not (lo <= facts["duration_s"] <= hi):
        raise StageError(f"validated: duration {facts['duration_s']:.2f}s "
                         f"outside expected {lo}–{hi}s")
    for key in ("width", "height", "video_codec", "audio_codec"):
        if facts[key] != expect[key]:
            raise StageError(f"validated: {key} is {facts[key]!r}, "
                             f"expected {expect[key]!r}")
    (run_dir / "validation.json").write_text(
        json.dumps({"ok": True, "ffprobe": facts, "simulated": True},
                   indent=1), encoding="utf-8")
    return {"validated": facts}


SMOKE_PIPELINE = {
    "planning": _smoke_plan,
    "scripting": _smoke_script,
    "asset_collection": _smoke_assets,
    "generating": _smoke_generate,
    "editing": _smoke_edit,
    "validating": _smoke_validate,
}

PIPELINES: dict[str, dict[str, Callable[[dict], dict | None]]] = {
    "smoke": SMOKE_PIPELINE,
}


def stages_for(name: str) -> dict[str, Callable[[dict], dict | None]] | None:
    """Stage dict for a pipeline, lazily resolving the heavy ones. The
    documentary pipeline lives in pipeline_doc.py, which imports THIS module
    for helpers — a module-level import here would be circular.

    Beyond the built-ins, engine-owned pipelines resolve through the
    Phase 2 engine registry: an engine may expose `stages()` and its
    pipeline id is its descriptor id (e.g. WAN_2_1). New engines therefore
    plug in WITHOUT touching this core file."""
    if name in PIPELINES:
        return PIPELINES[name]
    if name == "documentary":
        from core.video_agent import pipeline_doc
        return pipeline_doc.STAGES
    if name == "shorts":
        from core.video_agent import pipeline_shorts
        return pipeline_shorts.STAGES
    if name == "blender":
        from core.video_agent import pipeline_blender
        return pipeline_blender.STAGES
    try:
        engine = engine_registry.get(name)
        if engine is not None:
            stages = getattr(engine, "stages", None)
            if callable(stages):
                return stages()
    except Exception:                              # noqa: BLE001
        return None
    return None


def available_pipelines() -> dict[str, dict[str, list[str]]]:
    """Honest availability view for the action layer and RIO: which mode
    pipelines exist today and which stages they cover."""
    out = {name: {"stages": sorted(stages)}
           for name, stages in PIPELINES.items()}
    for lazy in ("documentary", "shorts", "blender"):
        stages = stages_for(lazy)
        if stages:
            out[lazy] = {"stages": sorted(stages)}
    for engine in engine_registry.all_engines():
        if callable(getattr(engine, "stages", None)):
            try:
                stages = engine.stages()
            except Exception:                      # noqa: BLE001
                continue
            if stages:
                out[engine.descriptor.id.lower()] = {"stages": sorted(stages)}
    return out


def missing_stages(pipeline: str) -> list[str]:
    stages = stages_for(pipeline) or {}
    return [s for s in VIDEO_STAGES if s not in stages]


# silence "unused" linters for the planned-phase placeholders
assert VIDEO_STAGES
