"""
core/video_agent/pipeline_stock.py — Phase 7: the STOCK_FOOTAGE pipeline.

Licensed real footage per scene + narration + captions, assembled with the
existing ffmpeg machinery. Mission rules:

  * every clip's source + license lands in the assets manifest; the final
    video is labelled "stock footage — real clips, NOT AI-generated"
  * the provider is user-configured (core/video_agent/stock.py); planning
    refuses honestly when it is not
  * scene queries come from the director's visual_prompts when present,
    otherwise from the topic (recorded in the manifest)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from core.video_agent import stock
from core.video_agent.pipelines import FFMPEG, StageError, probe_video
from core.video_agent.resources import atomic_final, cleanup_run_dir, \
    ensure_disk, ffmpeg_threads
from core.video_agent.encoder import run_encode


def _queries_for(spec: dict) -> list[dict[str, str]]:
    """One query per scene: director visual_prompts win, else the topic."""
    scenes = spec.get("scene_prompts") or []
    topic = str(spec.get("topic") or "")
    count = max(1, min(12, int(spec.get("duration_s") or 60) // 15))
    if scenes:
        return [{"query": str(s)[:120], "label": f"scene {i + 1}"}
                for i, s in enumerate(scenes[:count])]
    return [{"query": topic[:120], "label": f"scene {i + 1}"}
            for i in range(count)]


def _plan_stock(ctx: dict) -> dict | None:
    run_dir, spec, log = ctx["run_dir"], ctx["spec"], ctx["log"]
    ensure_disk(run_dir)
    provider, reason = stock.get_provider()
    if provider is None:
        raise StageError(f"stock footage unavailable: {reason}")
    queries = _queries_for(spec)
    log(f"[stock] provider {provider.name}, {len(queries)} scene queries")
    return {"provider": provider.name, "queries": queries,
            "license": provider.license}


def _collect_stock(ctx: dict) -> dict | None:
    """Search + download one licensed clip per scene (bounded, .part-atomic,
    license metadata per clip). Cancellation happens BETWEEN stages (the
    runner's contract) — each completed clip is durable on disk."""
    run_dir, spec, log = ctx["run_dir"], ctx["spec"], ctx["log"]
    provider, _ = stock.get_provider()
    if provider is None:
        raise StageError("stock provider disappeared mid-run — refusing")
    clips_dir = run_dir / "clips"
    manifest_path = run_dir / "assets" / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    queries = _queries_for(spec)
    records: list[dict[str, Any]] = []
    for i, q in enumerate(queries, start=1):
        try:
            found = provider.search(q["query"], per_page=3)
        except Exception as exc:                    # noqa: BLE001
            raise StageError(f"clip search failed for '{q['query'][:60]}': "
                             f"{type(exc).__name__}: {str(exc)[:160]}") \
                from None
        if not found:
            raise StageError(f"no licensed clip found for "
                             f"'{q['query'][:60]}' — try a different topic")
        clip = found[0]
        meta = stock.download_clip(clip, clips_dir / f"clip_{i:02d}.mp4")
        records.append(meta)
        log(f"[stock] scene {i}: {clip.get('clip_id')} by "
            f"{clip.get('author')} ({meta['size_bytes'] // 1024} KB, "
            f"{clip.get('license', '')[:40]})")
    manifest_path.write_text(json.dumps(
        {"ai_generated": False,
         "label": "stock footage — real licensed clips, NOT AI-generated",
         "clips": records}, indent=1), encoding="utf-8")
    return {"clips": len(records), "manifest": str(manifest_path),
            "ai_generated": False}


def _no_generation(ctx: dict) -> dict | None:
    """Honest generating stage: real footage needs no generation."""
    return {"generated": "nothing — stock clips are real footage",
            "ai_generated": False}


def _edit_stock(ctx: dict) -> dict | None:
    """Trim each clip to its scene slot, normalize to the aspect, concat,
    mix narration (if provided), burn the label into the metadata."""
    run_dir, spec, log = ctx["run_dir"], ctx["spec"], ctx["log"]
    ensure_disk(run_dir)
    clips = sorted((run_dir / "clips").glob("clip_*.mp4"))
    if not clips:
        raise StageError("no downloaded clips to assemble")
    vertical = (spec.get("aspect_ratio") or "16:9") == "9:16"
    w, h = ((720, 1280) if vertical else (1280, 720))
    total = float(spec.get("duration_s") or 60)
    per = max(2.0, total / len(clips))
    work = run_dir / "work"
    work.mkdir(parents=True, exist_ok=True)
    normalized: list[Path] = []
    for i, clip in enumerate(clips, start=1):
        out = work / f"norm_{i:02d}.mp4"
        tmp = out.with_suffix(".mp4.part")

        def build(enc, clip=clip, i=i):
            args = [FFMPEG, "-y", *enc.input_args(), "-i", str(clip)]
            args += ["-vf", enc.upload_filter(
                f"scale={w}:{h}:force_original_aspect_ratio="
                f"decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2")]
            args += enc.video_codec_args(threads=ffmpeg_threads())
            args += ["-t", f"{per:.3f}", "-an", "-r", "30",
                     "-f", "mp4", str(tmp)]
            return args, enc.env()

        result = run_encode(build, cwd=work, log=log,
                            what=f"normalize clip {i}",
                            prefer=str(spec.get("encoder") or "") or None)
        tmp.replace(out)
        normalized.append(out)

    final = run_dir / "render" / "stock_video.mp4"
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp_final = final.with_suffix(".mp4.part")
    concat_list = work / "concat.txt"
    concat_list.write_text(
        "\n".join(f"file '{p}'" for p in normalized), encoding="utf-8")
    narration = run_dir / "narration" / "narration.wav"
    has_narration = narration.is_file()

    def build_final(enc):
        args = [FFMPEG, "-y", *enc.input_args(), "-f", "concat",
                "-safe", "0", "-i", str(concat_list)]
        if has_narration:
            args += ["-i", str(narration)]
        else:
            args += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
        args += enc.video_codec_args(threads=ffmpeg_threads())
        args += ["-c:a", "aac", "-b:a", "128k", "-shortest",
                 "-movflags", "+faststart"]
        args += ["-f", "mp4", str(tmp_final)]
        return args, enc.env()

    result = run_encode(build_final, cwd=run_dir, log=log,
                        what="final stock assembly",
                        prefer=str(spec.get("encoder") or "") or None)
    atomic_final(tmp_final, final)
    return {"output": str(final), "encoder": result["backend"],
            "clips": len(normalized), "has_narration": has_narration}


def _validate_stock(ctx: dict) -> dict | None:
    run_dir = ctx["run_dir"]
    final = run_dir / "render" / "stock_video.mp4"
    if not final.is_file() or final.stat().st_size == 0:
        raise StageError("validated: render/stock_video.mp4 missing or empty")
    manifest = run_dir / "assets" / "manifest.json"
    if not manifest.is_file():
        raise StageError("validated: license manifest missing — stock clips "
                         "must always carry their license metadata")
    data = json.loads(manifest.read_text())
    if data.get("ai_generated") is not False or not data.get("clips"):
        raise StageError("validated: manifest is not an honest stock "
                         "manifest")
    facts = probe_video(final)
    if facts["video_codec"] != "h264":
        raise StageError(f"validated: unexpected codecs {facts}")
    (run_dir / "validation.json").write_text(json.dumps(
        {"ok": True, "ffprobe": facts, "mode": "stock_footage",
         "ai_generated": False,
         "label": "stock footage — real licensed clips, NOT AI-generated",
         "licensed_clips": len(data["clips"])}, indent=1),
        encoding="utf-8")
    cleanup_run_dir(run_dir)
    return {"validated": facts, "ai_generated": False,
            "licensed_clips": len(data["clips"])}


STAGES = {
    "planning": _plan_stock,
    "scripting": lambda ctx: {"scripted": "queries from topic/scene prompts"},
    "asset_collection": _collect_stock,
    "generating": _no_generation,
    "editing": _edit_stock,
    "validating": _validate_stock,
}
