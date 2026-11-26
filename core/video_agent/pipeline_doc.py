"""
core/video_agent/pipeline_doc.py — the DOCUMENTARY pipeline (Phase 4).

The mission's zero-cost workflow, one stage per registry entry:

    planning         research: LLM outline (via the house router) + web
                     sources (DuckDuckGo, free) → sources.json with URLs;
                     unverified claims stay flagged, never invented
    scripting        full structured script via script_writer (hook, chapters,
                     transitions, ending, timing estimates) grounded in the
                     gathered sources
    asset_collection visuals: PIL motion-graphic cards (title/chapters/ending)
                     + USER-PROVIDED images (matched in order) + optional user
                     music; every asset's source and license is recorded in
                     assets/manifest.json. AI image generation stays BLOCKED
                     (billing) — never silently substituted
    generating       honest no-op: no free AI generation exists on this
                     machine (measured, see providers.py)
    editing          narration (tts_runner, real per-segment audio) → per-scene
                     ffmpeg renders (Ken Burns, audio padded to scene) →
                     concat → subtitles burned (real per-segment SRT) →
                     optional music mix → final MP4. Resource-aware: 720p for
                     long renders on this 2C/4T CPU, -threads capped
    validating       ffprobe QC: duration window, codecs, dimensions, streams

Sample-first policy (mission): a documentary longer than 90 s renders as a
~30 s SAMPLE unless full=true — prove the pipeline, then commit to the long
render.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from core.video_agent import script_writer
from core.video_agent import tts_runner
from core.video_agent.pipelines import (FFMPEG, FFPROBE, StageError, probe_video)
from core.video_agent.resources import ffmpeg_threads
from core.video_agent import encoder as enc_mod

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
SCENE_FPS = 25
SCENE_TAIL_S = 0.7                 # breathing room after each narration clip
_RENDER_H = 720                    # hardware-constrained default (2C/4T CPU)
_SAMPLE_MAX_S = 30                 # the mission's 30-second test documentary
_LONG_DOC_S = 90                   # above this, sample-first applies


# ── planning: research + outline ─────────────────────────────────────────────

def _ask_llm(ctx: dict, prompt: str, system: str) -> tuple[str | None, str]:
    """One router call through the injectable LLM; (text|None, provider)."""
    llm = ctx.get("llm")
    if llm is None:
        llm = script_writer.HouseLLM()
        ctx["llm"] = llm
    out = llm.text(prompt, system=system)
    return out, getattr(llm, "name", "?")


def research_outline(ctx: dict) -> dict[str, Any]:
    run_dir, spec = ctx["run_dir"], ctx["spec"]
    research = run_dir / "research"
    research.mkdir(parents=True, exist_ok=True)
    system = ("You are a documentary researcher. Respond with ONE JSON "
              "object, nothing else. Never invent facts, statistics or "
              "sources — claims you cannot ground go to claims_needing_sources.")
    prompt = json.dumps({
        "task": "research outline for a short documentary",
        "topic": spec.get("topic"),
        "duration_s": spec.get("duration_s"),
        "audience": spec.get("audience") or "general",
        "output_format": {
            "working_title": "string",
            "angle": "one-sentence framing",
            "chapters": [{"title": "string", "key_points": ["string"],
                          "estimated_seconds": "integer"}],
            "search_queries": ["3-4 focused web search queries"],
            "claims_needing_sources": ["specific factual claims to verify"],
            "opinions": ["clearly interpretive points, kept separate"],
        },
    }, ensure_ascii=False)
    raw, provider = _ask_llm(ctx, prompt, system)
    data = script_writer.extract_json(raw)
    if not data or not (data.get("chapters") or data.get("working_title")):
        raise StageError(
            f"research outline failed: the model chain ({provider}) returned "
            "no parsable outline — nothing was fabricated")
    # web sources: free DuckDuckGo, real URLs, capped (3 queries × 4 hits)
    sources: list[dict] = []
    search_err = ""
    try:
        from actions.web_search import _ddg_search
        for query in (data.get("search_queries") or [])[:3]:
            query = str(query)[:200].strip()
            if not query:
                continue
            try:
                for hit in (_ddg_search(query, max_results=4) or [])[:4]:
                    url = str(hit.get("url") or "").strip()
                    if url.startswith("http"):
                        sources.append({"query": query,
                                        "title": str(hit.get("title") or "")[:200],
                                        "url": url,
                                        "snippet": str(hit.get("snippet") or "")[:400]})
            except Exception as exc:            # noqa: BLE001
                search_err = f"{type(exc).__name__}: {str(exc)[:120]}"
                break
    except ImportError:
        search_err = "ddgs package not installed"
    outline = {
        "provider": provider,
        "working_title": str(data.get("working_title") or "")[:200],
        "angle": str(data.get("angle") or "")[:400],
        "chapters": data.get("chapters") or [],
        "claims_needing_sources": list(data.get("claims_needing_sources") or []),
        "opinions": list(data.get("opinions") or []),
        "sources": sources,
        "sources_note": ("web sources gathered via DuckDuckGo (free)" if sources
                         else f"no web sources available ({search_err or 'no queries'})"
                         ) if True else "",
        "verified": bool(sources),
    }
    (research / "outline.json").write_text(json.dumps(outline, indent=1,
                                                       ensure_ascii=False),
                                           encoding="utf-8")
    if sources:
        (research / "sources.json").write_text(json.dumps(sources, indent=1,
                                                          ensure_ascii=False),
                                               encoding="utf-8")
    ctx["outline"] = outline
    return {"outline": str(research / "outline.json"),
            "sources": len(sources),
            "claims_flagged": len(outline["claims_needing_sources"]),
            "provider": provider}


# ── scripting ────────────────────────────────────────────────────────────────

def write_script(ctx: dict) -> dict | None:
    run_dir, spec = ctx["run_dir"], ctx["spec"]
    outline = ctx.get("outline") or {}
    # ground the script in the gathered sources: snippets travel with the ask
    grounded = dict(spec)
    if outline.get("sources"):
        grounded["sources"] = outline["sources"][:8]
    script_dir = run_dir / "script"
    script_dir.mkdir(parents=True, exist_ok=True)
    result = script_writer.generate_script(grounded)
    if not result.get("ok"):
        raise StageError(f"script failed: {result.get('error')}")
    script = result["script"]
    script["research_provider"] = outline.get("provider")
    script["sources_used"] = len(outline.get("sources") or [])
    script["claims_needing_sources"] = list(
        dict.fromkeys((script.get("claims_needing_sources") or [])
                      + list(outline.get("claims_needing_sources") or [])))
    (script_dir / "script.json").write_text(json.dumps(script, indent=1,
                                                       ensure_ascii=False),
                                            encoding="utf-8")
    (script_dir / "script.txt").write_text(
        f"{script['title']}\n\nHOOK\n{script['hook']}\n\n"
        + "\n\n".join(f"CHAPTER {i}: {c.get('title')}\n{c.get('narration')}"
                      for i, c in enumerate(script.get("chapters") or [], 1))
        + f"\n\nENDING\n{script['ending']}\n", encoding="utf-8")
    ctx["script"] = script
    return {"script": str(script_dir / "script.json"),
            "chapters": len(script.get("chapters") or []),
            "llm": result.get("provider"),
            "sources_used": script["sources_used"]}


# ── asset collection: cards + user assets + music ────────────────────────────

def _wrap(text: str, font, max_width: int, draw) -> list[str]:
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font) <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _card(path: Path, title: str, subtitle: str, w: int, h: int) -> None:
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (w, h))
    draw = ImageDraw.Draw(img)
    top, bottom = (18, 28, 46), (58, 46, 74)          # calm dark gradient
    for y in range(h):
        t = y / max(1, h - 1)
        draw.line([(0, y), (w, y)], fill=tuple(
            int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    f_title = ImageFont.truetype(FONT, size=max(28, w // 16))
    f_sub = ImageFont.truetype(FONT, size=max(18, w // 34))
    lines = _wrap(title, f_title, int(w * 0.86), draw)
    th = sum(draw.textlength(l, font=f_title) and f_title.size + 14
             for l in lines)
    y = (h - th) / 2 - (f_sub.size + 10 if subtitle else 0)
    for line in lines:
        lw = draw.textlength(line, font=f_title)
        draw.text(((w - lw) / 2, y), line, font=f_title, fill=(235, 238, 245))
        y += f_title.size + 14
    if subtitle:
        for line in _wrap(subtitle, f_sub, int(w * 0.8), draw)[:3]:
            lw = draw.textlength(line, font=f_sub)
            draw.text(((w - lw) / 2, y + 12), line, font=f_sub,
                      fill=(170, 178, 192))
            y += f_sub.size + 10
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


def collect_assets(ctx: dict) -> dict | None:
    run_dir, spec = ctx["run_dir"], ctx["spec"]
    script = ctx.get("script") or {}
    assets = run_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    w, h = 1280, _RENDER_H
    manifest: list[dict] = []
    cards = run_dir / "cards"
    _card(cards / "title.png", str(script.get("title") or spec.get("topic")),
          "a documentary", w, h)
    manifest.append({"asset": "cards/title.png", "source": "generated (PIL)",
                     "license": "in-house, no third-party content"})
    chapters = script.get("chapters") or []
    user_dir_raw = str(spec.get("user_assets_dir") or "").strip()
    user_dir = Path(user_dir_raw).expanduser() if user_dir_raw else None
    user_images = (
        [p for p in (list(user_dir.glob("*.jpg"))
                       + list(user_dir.glob("*.jpeg"))
                       + list(user_dir.glob("*.png")))
         if user_dir.is_dir()]
        if user_dir is not None else []
    )
    user_images = sorted(user_images)
    # permanent cast: when the default characters are requested and the user
    # supplied no assets of their own, the character reference sheet leads
    # the visuals (chapter 1) — the cast reveal
    characters_wanted = str(spec.get("characters") or "default") != "none"
    if characters_wanted and not user_images:
        from core.video_agent import characters as cast
        sheet = cast.image_reference()
        if sheet is not None:
            user_images = [sheet]
            manifest.append({
                "asset": "user/" + sheet.name,
                "source": f"permanent cast reference: {sheet}",
                "license": "user-provided (character design sheet)",
                "chapter": 1})
    for i, ch in enumerate(chapters, start=1):
        if i - 1 < len(user_images):
            manifest.append({"asset": f"user/{user_images[i-1].name}",
                             "source": f"user-provided: {user_images[i-1]}",
                             "license": "user-provided",
                             "chapter": i})
        else:
            _card(cards / f"chapter_{i}.png",
                  str(ch.get("title") or f"Chapter {i}"),
                  str(ch.get("visual_notes") or "")[:120], w, h)
            manifest.append({"asset": f"cards/chapter_{i}.png",
                             "source": "generated (PIL)",
                             "license": "in-house, no third-party content",
                             "chapter": i})
    _card(cards / "ending.png", "Thank you for watching",
          str(script.get("title") or "")[:80], w, h)
    manifest.append({"asset": "cards/ending.png", "source": "generated (PIL)",
                     "license": "in-house, no third-party content"})
    music = Path(str(spec.get("music_file") or "")).expanduser()
    if music.is_file():
        manifest.append({"asset": "music", "source": f"user-provided: {music}",
                         "license": "user-provided"})
    (assets / "manifest.json").write_text(json.dumps(
        {"assets": manifest,
         "ai_generated_visuals": 0,
         "note": ("no AI image/video generation: providers are billing-"
                  "blocked (measured); visuals are in-house motion graphics "
                  "+ user assets only")},
        indent=1), encoding="utf-8")
    ctx["user_images"] = user_images
    return {"assets": len(manifest), "user_images": len(user_images),
            "ai_generated_visuals": 0}


# ── generating: honest no-op ─────────────────────────────────────────────────

def no_free_generation(ctx: dict) -> dict | None:
    ctx["log"]("[documentary] generating stage: no free AI image/video "
               "provider exists (measured) — visuals stay motion graphics "
               "+ user assets; nothing is passed off as AI video")
    return {"ai_generated": 0, "honest_note": True}


# ── editing: narration → scenes → concat → subtitles → final ────────────────

def _ffmpeg(args: list[str], cwd: Path, log, what: str,
            timeout: int = 600) -> None:
    # Merge software-decode env so no ffmpeg call ever attempts VAAPI / DRM
    # hardware acceleration — that path asserts on this machine (exit 134).
    base_env = dict(os.environ)
    base_env.update({
        "VA_DISABLE_DRM": "1",
        "VA_NO_HARDWARE": "1",
        "LIBVA_MUTABLE_CONFIG": "0",
    })
    proc = subprocess.run([str(a) for a in args], cwd=str(cwd), timeout=timeout,
                          capture_output=True, text=True, env=base_env)
    if proc.returncode != 0:
        raise StageError(f"{what} failed (exit {proc.returncode}): "
                         f"{(proc.stderr or '')[-400:]}")
    log(f"[ffmpeg] {what}: ok")


def _scene_video(ctx: dict, work: Path, image: Path, audio: Path | None,
                 duration: float, index: int, log) -> Path:
    """One scene: still image + Ken Burns + narration padded to scene length.

    Encoded by the selected backend (Intel VAAPI H.264 when verified, CPU
    libx264 otherwise); a failed VAAPI encode automatically retries on CPU."""
    out = work / f"scene_{index:02d}.mp4"
    frames = max(int(math.ceil(duration * SCENE_FPS)) + 2, 25)
    w, h = 1280, _RENDER_H
    base = (f"[0:v]scale={w * 2}:{h * 2},"
            f"zoompan=z='min(zoom+0.0008,1.13)'"
            f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d={frames}:s={w}x{h}:fps={SCENE_FPS}")

    def build(enc):
        args = [FFMPEG, "-y", *enc.input_args(),
                "-loop", "1", "-i", str(image)]
        if audio is not None and Path(audio).is_file():
            args += ["-i", str(audio), "-filter_complex",
                     enc.upload_filter(base) + "[v]",
                     "-map", "[v]", "-map", "1:a",
                     "-af", f"apad=whole_dur={duration:.3f}",
                     "-t", f"{duration:.3f}"]
        else:
            args += ["-filter_complex", enc.upload_filter(base) + "[v]",
                     "-map", "[v]", "-t", f"{duration:.3f}"]
        args += enc.video_codec_args(threads=ffmpeg_threads())
        if audio is not None and Path(audio).is_file():
            args += ["-c:a", "aac", "-b:a", "128k", "-ar", "44100"]
        args += [out.name]
        return args, enc.env()

    result = enc_mod.run_encode(
        build, cwd=work, log=log,
        what=f"scene {index} ({duration:.1f}s)",
        prefer=str(ctx["spec"].get("encoder") or "") or None)
    ctx.setdefault("encoder_runs", []).append(result)
    return out


def _srt(segments: list[dict], starts: list[float]) -> str:
    def stamp(t: float) -> str:
        ms = int(round(t * 1000))
        return (f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:"
                f"{ms // 1000 % 60:02d},{ms % 1000:03d}")
    blocks = []
    for seg, start in zip(segments, starts):
        dur = float(seg.get("duration_s") or 0)
        text = " ".join(str(seg.get("text") or "").split())
        if not text or dur <= 0:
            continue
        blocks.append(f"{len(blocks) + 1}\n{stamp(start)} --> "
                      f"{stamp(start + min(dur, 6.0))}\n{text}\n")
    return "\n".join(blocks)


def edit_documentary(ctx: dict) -> dict | None:
    run_dir, spec = ctx["run_dir"], ctx["spec"]
    script = ctx.get("script")
    if script is None:
        loaded = run_dir / "script" / "script.json"
        script = json.loads(loaded.read_text(encoding="utf-8"))
        ctx["script"] = script
    work = run_dir / "work"
    work.mkdir(exist_ok=True)
    log = ctx["log"]

    # 1. narration — real audio per segment (tts_runner handles retries)
    timings = tts_runner.synthesize_narration(
        script, run_dir, language=str(spec.get("language") or "en"), log=log)
    segments = timings["segments"]
    if not segments:
        raise StageError("no narration segments were produced")

    # 2. scene plan: visuals + per-scene duration from REAL audio durations
    cards = run_dir / "cards"
    user_images = ctx.get("user_images") or []
    scenes: list[dict] = []
    for seg in segments:
        if seg["kind"] == "hook":
            image = cards / "title.png"
        elif seg["kind"] == "ending":
            image = cards / "ending.png"
        else:
            idx = int(seg.get("index") or 1)
            image = (user_images[idx - 1] if idx - 1 < len(user_images)
                     else cards / f"chapter_{idx}.png")
        if not Path(image).is_file():
            image = cards / "title.png"
        scenes.append({"segment": seg, "image": image,
                       "duration": float(seg["duration_s"]) + SCENE_TAIL_S})

    # 3. render scenes
    scene_files = []
    for i, scene in enumerate(scenes, start=1):
        scene_files.append(_scene_video(ctx, work, scene["image"],
                                        Path(scene["segment"]["path"]),
                                        scene["duration"], i, log))

    # 4. concat
    concat = work / "concat.txt"
    concat.write_text("".join(f"file '{f.name}'\n" for f in scene_files),
                      encoding="utf-8")
    joined = work / "joined.mp4"
    _ffmpeg([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", concat.name,
             "-c", "copy", joined.name], work, log, "concat scenes")

    # 5. subtitles from REAL segment timings (per-segment, never word-level)
    starts: list[float] = []
    t = 0.0
    for scene in scenes:
        starts.append(t)
        t += scene["duration"]
    srt = work / "subtitles.srt"
    srt.write_text(_srt(segments, starts), encoding="utf-8")

    # 6. final pass: burn subs + optional user music (mixed low). Paths are
    # relative to cwd (run_dir) — joined/subs live under work/. Encoded to a
    # .part temp then atomically renamed (Phase 10: no partial finals).
    from core.video_agent.resources import atomic_final, cleanup_run_dir, \
        ffmpeg_threads
    music = Path(str(spec.get("music_file") or "")).expanduser()
    final = run_dir / "render" / "documentary.mp4"
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp_final = run_dir / "render" / "documentary.mp4.part"

    def build(enc):
        args: list[str] = [FFMPEG, "-y", *enc.input_args(),
                           "-i", str(joined.relative_to(run_dir))]
        filters = f"[0:v]{enc.upload_filter('subtitles=work/subtitles.srt')}[v]"
        if music.is_file():
            args += ["-i", str(music)]
            filters += (";[0:a][1:a]amix=inputs=2:duration=first"
                        ":weights=1 0.18[a]")
            amap = ["-map", "[v]", "-map", "[a]"]
        else:
            amap = ["-map", "[v]", "-map", "0:a"]
        args += ["-filter_complex", filters] + amap + \
            enc.video_codec_args(threads=ffmpeg_threads()) + [
            "-c:a", "aac", "-b:a", "128k",
            "-f", "mp4", "-movflags", "+faststart",
            str(tmp_final.relative_to(run_dir))]
        return args, enc.env()

    final_run = enc_mod.run_encode(
        build, cwd=run_dir, log=log, what="final render (subs + mix)",
        prefer=str(spec.get("encoder") or "") or None)
    atomic_final(tmp_final, final)
    (run_dir / "render" / "encoder.json").write_text(json.dumps(
        {"final": final_run, "scenes": ctx.get("encoder_runs", []),
         "output_contract": {"video_codec": "h264", "audio_codec": "aac",
                             "fps": SCENE_FPS}}, indent=1),
        encoding="utf-8")

    # cleanup intermediates, keep sources + final (never delete user files)
    for f in scene_files:
        f.unlink(missing_ok=True)
    (work / "joined.mp4").unlink(missing_ok=True)
    cleanup_run_dir(run_dir)
    return {"output": str(final),
            "scenes": len(scene_files),
            "speech_s": round(timings["total_speech_s"], 1),
            "engine": timings.get("engine"),
            "encoder": final_run["backend"],
            "encoder_elapsed_s": final_run["elapsed_s"]}


# ── validating ───────────────────────────────────────────────────────────────

def validate_documentary(ctx: dict) -> dict | None:
    run_dir = ctx["run_dir"]
    final = run_dir / "render" / "documentary.mp4"
    if not final.is_file() or final.stat().st_size == 0:
        raise StageError("validated: render/documentary.mp4 missing or empty")
    facts = probe_video(final)
    if facts["video_codec"] != "h264" or facts["audio_codec"] != "aac":
        raise StageError(f"validated: unexpected codecs {facts}")
    if (facts["width"], facts["height"]) != (1280, _RENDER_H):
        raise StageError(f"validated: unexpected dimensions {facts}")
    if facts["duration_s"] < 3:
        raise StageError(f"validated: suspiciously short ({facts['duration_s']}s)")
    (run_dir / "validation.json").write_text(json.dumps(
        {"ok": True, "ffprobe": facts, "mode": "documentary",
         "ai_generated_visuals": 0}, indent=1), encoding="utf-8")
    return {"validated": facts}


# ── sample-first helper ──────────────────────────────────────────────────────

def apply_sample_policy(spec: dict) -> dict:
    """Mission rule: start with a 30-second test documentary before a long
    render. Long documentary requests default to a ~30 s SAMPLE unless
    full=true was explicit; the spec records the decision either way."""
    spec = dict(spec)
    duration = int(spec.get("duration_s") or 600)
    full = str(spec.get("full") or "").lower() in ("1", "true", "yes", "on")
    if duration > _LONG_DOC_S and not full:
        spec["sample"] = True
        spec.setdefault("notes", []).append(
            f"sample-first policy: {duration}s documentary request renders a "
            f"{_SAMPLE_MAX_S}s verification cut first; re-run with full=true "
            "for the complete piece")
        spec["duration_s"] = _SAMPLE_MAX_S
    elif duration > _LONG_DOC_S:
        spec.setdefault("notes", []).append(
            "full render requested (full=true) — long encode on a 2C/4T CPU "
            "takes a while; the render stage caps threads")
    return spec


# ── registry wiring ──────────────────────────────────────────────────────────

STAGES = {
    "planning": research_outline,
    "scripting": write_script,
    "asset_collection": collect_assets,
    "generating": no_free_generation,
    "editing": edit_documentary,
    "validating": validate_documentary,
}
