"""
core/video_agent/pipeline_hybrid.py — Phase 7: HYBRID scene routing.

One video, per-scene engines: the approved scene plan (Phase 6 binds it)
says which engine renders which scene. Mission rule: the COMPLETE scene
plan is shown and approved BEFORE anything runs — this pipeline only ever
executes an already-approved plan.

Supported scene engines today (measured readiness at run time):
  CANVAS          — text card + Ken Burns (free, local)
  BLENDER_3D      — the 3D scene builder rendered per scene (free, local)
  STOCK_FOOTAGE   — licensed real clips (needs a configured provider)
  VIMAX / WAN_2_1 / LTX_VIDEO — honestly refused per scene unless that
                    engine's backend measures ready (billing/worker)

The routing table rides in spec["scene_plan"] = [{scene: 1, method:
"CANVAS"}, …]. Every render is labelled with its engine in the validation
record — a viewer can always know which scenes are AI/3D/real footage.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from core.video_agent import pipeline_blender
from core.video_agent.encoder import run_encode
from core.video_agent.engines import registry as engine_registry
from core.video_agent.pipelines import FFMPEG, StageError, probe_video
from core.video_agent.pipeline_doc import _card, _scene_video
from core.video_agent.resources import atomic_final, cleanup_run_dir, \
    ensure_disk, ffmpeg_threads

_LOCAL_OK = ("READY_LOCAL",)


def _scene_plan(spec: dict) -> list[dict[str, Any]]:
    plan = spec.get("scene_plan") or []
    clean: list[dict[str, Any]] = []
    for i, entry in enumerate(plan[:16], start=1):
        if not isinstance(entry, dict):
            continue
        method = str(entry.get("method") or "").strip().upper()
        if method:
            clean.append({"scene": i, "method": method,
                          "prompt": str(entry.get("prompt")
                                        or entry.get("title") or "")[:300]})
    return clean


def _plan_hybrid(ctx: dict) -> dict | None:
    run_dir, spec, log = ctx["run_dir"], ctx["spec"], ctx["log"]
    ensure_disk(run_dir)
    plan = _scene_plan(spec)
    if not plan:
        raise StageError("hybrid: no approved scene plan — the method "
                         "approval must include the scene→engine table")
    problems: list[str] = []
    for entry in plan:
        engine = engine_registry.get(entry["method"])
        if engine is None:
            problems.append(f"scene {entry['scene']}: unknown engine "
                            f"{entry['method']}")
            continue
        status = engine.probe()
        if not status.is_runnable():
            problems.append(f"scene {entry['scene']}: "
                            f"{engine.descriptor.name} is {status.status}")
    if problems:
        raise StageError("hybrid: the approved scene plan cannot run — "
                         + "; ".join(problems)[:400])
    log(f"[hybrid] routing: " + ", ".join(
        f"s{e['scene']}={e['method']}" for e in plan))
    return {"scene_plan": plan, "routes": [e["method"] for e in plan]}


def _scripting(ctx: dict) -> dict | None:
    return {"scripted": "hybrid — prompts come from the approved scene plan"}


def _assets(ctx: dict) -> dict | None:
    return {"assets": "none upfront — scene engines fetch their own"}


def _gen_canvas(ctx: dict, scene_dir: Path, entry: dict) -> Path:
    """A text-card + Ken Burns scene via the proven documentary machinery."""
    run_dir, spec, log = ctx["run_dir"], ctx["spec"], ctx["log"]
    scene_dir.mkdir(parents=True, exist_ok=True)
    image = scene_dir / "card.png"
    title = entry.get("prompt") or f"Scene {entry['scene']}"
    _card(image, title[:60], f"scene {entry['scene']} · canvas",
          1280, 720)
    from core.video_agent import spec as vspec
    per_scene = dict(spec)
    per_scene["duration_s"] = int(spec.get("scene_duration_s") or 5)
    scene_ctx = {**ctx, "spec": per_scene, "run_dir": scene_dir}
    out = _scene_video(scene_ctx, scene_dir, image, None,
                       float(per_scene["duration_s"]), entry["scene"], log)
    return out


def _gen_blender(ctx: dict, scene_dir: Path, entry: dict) -> Path:
    """A Blender 3D scene rendered into this scene's sub-directory."""
    run_dir, spec = ctx["run_dir"], ctx["spec"]
    scene_dir.mkdir(parents=True, exist_ok=True)
    per_scene = dict(spec)
    per_scene["duration_s"] = max(5, int(spec.get("scene_duration_s") or 10))
    per_scene["resolution"] = "720p"
    scene_ctx = {**ctx, "spec": per_scene, "run_dir": scene_dir,
                 "log": ctx["log"]}
    pipeline_blender.plan_blender(scene_ctx)
    pipeline_blender.generate_blender(scene_ctx)
    out = scene_dir / "render" / "robot_adventure.mp4"
    if not out.is_file():
        raise StageError(f"hybrid: blender scene {entry['scene']} produced "
                         "no file")
    return out


def _gen_stock(ctx: dict, scene_dir: Path, entry: dict) -> Path:
    from core.video_agent import stock as stock_mod
    provider, reason = stock_mod.get_provider()
    if provider is None:
        raise StageError(f"hybrid scene {entry['scene']}: stock unavailable "
                         f"— {reason}")
    scene_dir.mkdir(parents=True, exist_ok=True)
    query = entry.get("prompt") or ctx["spec"].get("topic") or "nature"
    found = provider.search(query, per_page=2)
    if not found:
        raise StageError(f"hybrid scene {entry['scene']}: no clip for "
                         f"'{query[:60]}'")
    meta = stock_mod.download_clip(found[0], scene_dir / "clip.mp4")
    (scene_dir / "license.json").write_text(json.dumps(meta, indent=1),
                                            encoding="utf-8")
    return scene_dir / "clip.mp4"


def _generating(ctx: dict) -> dict | None:
    run_dir, spec, log = ctx["run_dir"], ctx["spec"], ctx["log"]
    plan = _scene_plan(spec)
    scenes_dir = run_dir / "scenes"
    routed: list[dict[str, Any]] = []
    for entry in plan:
        method = entry["method"]
        scene_dir = scenes_dir / f"scene_{entry['scene']:02d}"
        if method == "CANVAS":
            out = _gen_canvas(ctx, scene_dir, entry)
        elif method == "BLENDER_3D":
            out = _gen_blender(ctx, scene_dir, entry)
        elif method == "STOCK_FOOTAGE":
            out = _gen_stock(ctx, scene_dir, entry)
        else:
            engine = engine_registry.get(method)
            status = engine.probe().status if engine else "UNKNOWN"
            raise StageError(f"hybrid scene {entry['scene']}: {method} "
                             f"cannot render here ({status})")
        routed.append({"scene": entry["scene"], "method": method,
                       "file": str(out)})
        log(f"[hybrid] scene {entry['scene']} rendered by {method}")
    return {"scenes": routed, "ai_scenes": sum(
        1 for r in routed if r["method"] in ("VIMAX", "WAN_2_1",
                                             "LTX_VIDEO"))}


def _editing(ctx: dict) -> dict | None:
    """Concat the scene clips (same codec family) into the final MP4.
    Rendered scenes are re-derived from disk (the runner passes no stage
    notes between stages)."""
    run_dir, spec, log = ctx["run_dir"], ctx["spec"], ctx["log"]
    ensure_disk(run_dir)
    plan = _scene_plan(spec)
    files: list[Path] = []
    for entry in plan:
        scene_dir = run_dir / "scenes" / f"scene_{entry['scene']:02d}"
        for candidate in (scene_dir / f"scene_{entry['scene']:02d}.mp4",
                          scene_dir / "render" / "robot_adventure.mp4",
                          scene_dir / "clip.mp4"):
            if Path(candidate).is_file():
                files.append(Path(candidate))
                break
    if not files:
        raise StageError("hybrid: no rendered scenes to assemble")
    work = run_dir / "work"
    work.mkdir(parents=True, exist_ok=True)
    concat_list = work / "concat.txt"
    concat_list.write_text("\n".join(f"file '{p}'" for p in files),
                           encoding="utf-8")
    final = run_dir / "render" / "hybrid_video.mp4"
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.with_suffix(".mp4.part")
    w, h = ((720, 1280) if (spec.get("aspect_ratio") or "16:9") == "9:16"
            else (1280, 720))

    def build(enc):
        args = [FFMPEG, "-y", *enc.input_args(), "-f", "concat",
                "-safe", "0", "-i", str(concat_list)]
        args += ["-vf", enc.upload_filter(
            f"scale={w}:{h}:force_original_aspect_ratio="
            f"decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,fps=30")]
        args += enc.video_codec_args(threads=ffmpeg_threads())
        args += ["-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"]
        args += ["-f", "mp4", str(tmp)]
        return args, enc.env()

    result = run_encode(build, cwd=run_dir, log=log,
                        what="hybrid assembly",
                        prefer=str(spec.get("encoder") or "") or None)
    atomic_final(tmp, final)
    return {"output": str(final), "encoder": result["backend"],
            "scenes": len(files)}


def _validating(ctx: dict) -> dict | None:
    run_dir = ctx["run_dir"]
    final = run_dir / "render" / "hybrid_video.mp4"
    if not final.is_file() or final.stat().st_size == 0:
        raise StageError("validated: render/hybrid_video.mp4 missing")
    facts = probe_video(final)
    if facts["video_codec"] != "h264":
        raise StageError(f"validated: unexpected codecs {facts}")
    routes = [e["method"] for e in _scene_plan(ctx["spec"])]
    (run_dir / "validation.json").write_text(json.dumps(
        {"ok": True, "ffprobe": facts, "mode": "hybrid",
         "scene_routing": routes,
         "note": "per-scene engines — see scene_routing; "
                 "stock scenes are real footage, not AI"},
        indent=1), encoding="utf-8")
    cleanup_run_dir(run_dir)
    return {"validated": facts, "scene_routing": routes}


STAGES = {
    "planning": _plan_hybrid,
    "scripting": _scripting,
    "asset_collection": _assets,
    "generating": _generating,
    "editing": _editing,
    "validating": _validating,
}
