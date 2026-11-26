"""
core/video_agent/pipeline_shorts.py — the SHORTS pipeline (Phase 7).

Vertical 9:16 (1080×1920), 30–60 s, MP4 with narration and burned captions —
the zero-cost path: script via the free model chain, narration via free TTS
(gTTS today; Kokoro when approved), visuals = vertical motion-graphic cards +
user images (center-cropped to 9:16), music = the user's own file.

Caption honesty (mission rule): captions are PER-SEGMENT with REAL measured
durations (timings.json, word_level_available: False). Proportional
phrase-level timing would be invented alignment and is never produced.
Segments are kept short by design (the script prompt asks for punchy beats)
so caption blocks stay readable without inventing timestamps.

Documentary → Shorts: `split_documentary()` reuses an existing documentary
run's REAL narration audio (and its measured durations) — no re-synthesis,
no re-timing.
"""
from __future__ import annotations

import json
import math
import shutil
from pathlib import Path
from typing import Any

from core.video_agent import script_writer
from core.video_agent import tts_runner
from core.video_agent.pipelines import FFMPEG, StageError, probe_video
from core.video_agent.pipeline_doc import FONT, _srt, _ffmpeg
from core.video_agent.resources import atomic_final, cleanup_run_dir, \
    ffmpeg_threads
from core.video_agent import encoder as enc_mod

V_W, V_H = 1080, 1920
SCENE_FPS = 30                    # verified VAAPI contract: 1080x1920 @ 30
SCENE_TAIL_S = 0.5
TRANSITION_S = 0.35               # fade-through-black between scene clips
_CAPTION_SAFE_BOTTOM = 420        # px reserved for platform UI overlays
_DEVANAGARI_FONT = "/usr/share/fonts/truetype/lohit-devanagari/Lohit-Devanagari.ttf"


def _font_for(language: str | None) -> str:
    """PIL font that actually contains the script's glyphs. DejaVu has no
    Devanagari; Lohit Devanagari is installed on this system."""
    code = tts_runner.language_code(language)
    if code in ("hi", "mr", "ne", "sa") and Path(_DEVANAGARI_FONT).is_file():
        return _DEVANAGARI_FONT
    return FONT


# ── planning: the short script ───────────────────────────────────────────────

def _ask_llm(ctx: dict, prompt: str, system: str) -> tuple[str | None, str]:
    llm = ctx.get("llm")
    if llm is None:
        llm = script_writer.HouseLLM()
        ctx["llm"] = llm
    return llm.text(prompt, system=system), getattr(llm, "name", "?")


def plan_short(ctx: dict) -> dict | None:
    run_dir, spec = ctx["run_dir"], ctx["spec"]
    research = run_dir / "research"
    research.mkdir(parents=True, exist_ok=True)

    # Documentary → Shorts: the narration (and caption text) comes from the
    # source segments — NO LLM call, no failure mode, no re-timing.
    reuse_dir = Path(str(spec.get("reuse_narration_from") or "")).expanduser()
    if reuse_dir.is_dir() and (reuse_dir / "narration" / "timings.json") \
            .is_file():
        src = json.loads((reuse_dir / "narration" / "timings.json")
                         .read_text())
        wanted = {int(i) for i in (spec.get("segment_ids") or [])}
        segs = [s for s in src["segments"]
                if not wanted or int(s["id"]) in wanted]
        texts = [str(s.get("text") or "").strip() for s in segs]
        texts = [t for t in texts if t]
        script = {
            "title": str(spec.get("topic") or "Short from my documentary"),
            "description": "Reused narration from my documentary "
                           "(real timing, no re-synthesis).",
            "hook": texts[0] if texts else "",
            "beats": texts[1:],
            "closing": "",
            "claims_needing_sources": [],
            "provider": "reused-narration",
        }
        ctx["script"] = script
        (research / "short_script.json").write_text(
            json.dumps(script, indent=1, ensure_ascii=False),
            encoding="utf-8")
        return {"script": str(research / "short_script.json"),
                "beats": len(script["beats"]), "provider": "reused-narration",
                "llm_skipped": True}

    system = (
        "You write punchy vertical-video shorts. Respond with ONE JSON "
        "object, nothing else. The hook must land in the first 3 seconds. "
        "Beats are short spoken sentences. Never invent statistics or "
        "sources — mark uncertain claims [NEEDS SOURCE].")
    # Spoken pace is measured, not guessed: English gTTS ~1.6 words/s
    # (Phase 7 correction), Hindi gTTS ~2.1 words/s (measured job
    # rt-20261025-30a00a53e1: 89 words → 42.9 s). The 3 s reserve covers the
    # per-scene tails so the FINAL file lands near the requested duration.
    code = tts_runner.language_code(spec.get("language"))
    ask_s = int(spec.get("duration_s") or 45)
    if code == "hi":
        length = (f"roughly {int(max(20, (ask_s - 3) * 2.1))} words total "
                  "(Hindi spoken pace ~2.1 words/s, measured)")
    else:
        length = f"roughly {int(ask_s * 1.6)} words total (spoken pace ~1.6 words/s)"
    prompt = json.dumps({
        "task": "write a vertical short (youtube shorts / reels)",
        "topic": spec.get("topic"),
        "duration_s": spec.get("duration_s"),
        "language": spec.get("language") or "simple English",
        "structure": {
            "hook": "one punchy opening line (lands in 3 seconds)",
            "beats": ["3-5 short spoken beats that deliver the value"],
            "closing": "one closing line (call to action or takeaway)",
        },
        "length": length,
        "output_format": {
            "title": "string (short video title)",
            "description": "string (1-2 sentence platform description)",
            "hook": "string",
            "beats": ["string (each one short spoken sentence)"],
            "closing": "string",
            "claims_needing_sources": ["string"],
        },
    }, ensure_ascii=False)
    raw, provider = _ask_llm(ctx, prompt, system)
    data = script_writer.extract_json(raw)
    if not data or not (data.get("hook") or data.get("beats")):
        raise StageError(
            f"short script failed: the model chain ({provider}) returned no "
            "parsable script — nothing was fabricated")
    script = {
        "title": str(data.get("title") or spec.get("topic") or "")[:200],
        "description": str(data.get("description") or "")[:500],
        "hook": str(data.get("hook") or ""),
        "beats": [str(b) for b in (data.get("beats") or [])][:6],
        "closing": str(data.get("closing") or ""),
        "claims_needing_sources": list(data.get("claims_needing_sources")
                                       or []),
        "provider": provider,
    }
    ctx["script"] = script
    (research / "short_script.json").write_text(json.dumps(script, indent=1,
                                                           ensure_ascii=False),
                                                encoding="utf-8")
    return {"script": str(research / "short_script.json"),
            "beats": len(script["beats"]), "provider": provider}


def segment_short(ctx: dict) -> dict | None:
    """SCRIPTING: caption segments — hook, each beat, closing. One segment
    per spoken block keeps captions readable without invented timing."""
    run_dir = ctx["run_dir"]
    script = ctx.get("script") or {}
    segs: list[dict[str, Any]] = []
    order = 0
    for kind, text in (("hook", script.get("hook")),
                       * [("beat", b) for b in script.get("beats") or []],
                       ("closing", script.get("closing"))):
        text = str(text or "").strip()
        if not text:
            continue
        order += 1
        segs.append({"id": order, "kind": kind, "title": kind.capitalize(),
                     "text": text, "visual_notes": "",
                     "estimated_seconds": None, "too_long": len(text) > 200})
    script_dir = run_dir / "script"
    script_dir.mkdir(parents=True, exist_ok=True)
    (script_dir / "segments.json").write_text(json.dumps(segs, indent=1,
                                                         ensure_ascii=False),
                                              encoding="utf-8")
    ctx["segments"] = segs
    return {"segments": len(segs),
            "too_long": sum(1 for s in segs if s["too_long"])}


# ── asset collection: vertical cards + user images + music ──────────────────

def _wrap_v(text: str, font, max_width: int, draw) -> list[str]:
    words, lines, cur = str(text).split(), [], ""
    for word in words:
        trial = (cur + " " + word).strip()
        if draw.textlength(trial, font=font) <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines or [""]


def _vcard(path: Path, title: str, subtitle: str = "",
           branding: str = "", font: str = FONT) -> None:
    from PIL import Image, ImageDraw, ImageFont
    w, h = V_W, V_H
    img = Image.new("RGB", (w, h))
    draw = ImageDraw.Draw(img)
    top, bottom = (14, 22, 40), (52, 40, 68)
    for y in range(h):
        t = y / (h - 1)
        draw.line([(0, y), (w, y)], fill=tuple(
            int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    f_title = ImageFont.truetype(font, size=max(52, w // 11))
    f_sub = ImageFont.truetype(font, size=max(30, w // 22))
    f_brand = ImageFont.truetype(font, size=max(24, w // 30))
    lines = _wrap_v(title, f_title, int(w * 0.86), draw)
    th = len(lines) * (f_title.size + 18)
    y = (h - _CAPTION_SAFE_BOTTOM) / 2 - th / 2
    for line in lines:
        lw = draw.textlength(line, font=f_title)
        draw.text(((w - lw) / 2, y), line, font=f_title, fill=(238, 241, 247))
        y += f_title.size + 18
    if subtitle:
        for line in _wrap_v(subtitle, f_sub, int(w * 0.8), draw)[:4]:
            lw = draw.textlength(line, font=f_sub)
            draw.text(((w - lw) / 2, y + 16), line, font=f_sub,
                      fill=(172, 180, 196))
            y += f_sub.size + 12
    if branding:
        lw = draw.textlength(branding, font=f_brand)
        draw.text(((w - lw) / 2, h - 160), branding, font=f_brand,
                  fill=(150, 158, 172))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


def collect_assets_short(ctx: dict) -> dict | None:
    run_dir, spec = ctx["run_dir"], ctx["spec"]
    script = ctx.get("script") or {}
    branding = str(spec.get("branding") or "").strip()
    font = _font_for(spec.get("language"))
    cards = run_dir / "cards"
    _vcard(cards / "title.png", str(script.get("hook") or spec.get("topic")
                                    or "")[:120], branding=branding,
           font=font)
    user_dir = Path(str(spec.get("user_assets_dir") or "")).expanduser()
    user_images = sorted([p for p in (list(user_dir.glob("*.jpg"))
                                      + list(user_dir.glob("*.jpeg"))
                                      + list(user_dir.glob("*.png")))
                          if user_dir.is_dir()]) if user_dir.is_dir() else []
    manifest: list[dict] = [{"asset": "cards/title.png",
                             "source": "generated (PIL)",
                             "license": "in-house, no third-party content"}]
    beats = script.get("beats") or []
    for i in range(1, len(beats) + 1):
        if i - 1 < len(user_images):
            manifest.append({"asset": f"user/{user_images[i-1].name}",
                             "source": f"user-provided: {user_images[i-1]}",
                             "license": "user-provided", "beat": i})
        else:
            _vcard(cards / f"beat_{i}.png", str(beats[i - 1])[:120],
                   branding=branding, font=font)
            manifest.append({"asset": f"cards/beat_{i}.png",
                             "source": "generated (PIL)",
                             "license": "in-house, no third-party content",
                             "beat": i})
    _vcard(cards / "closing.png", str(script.get("closing") or "")[:120],
           branding=branding, font=font)
    manifest.append({"asset": "cards/closing.png",
                     "source": "generated (PIL)",
                     "license": "in-house, no third-party content"})
    music = Path(str(spec.get("music_file") or "")).expanduser()
    if music.is_file():
        manifest.append({"asset": "music",
                         "source": f"user-provided: {music}",
                         "license": "user-provided"})
    (run_dir / "assets").mkdir(parents=True, exist_ok=True)
    (run_dir / "assets" / "manifest.json").write_text(json.dumps(
        {"assets": manifest, "ai_generated_visuals": 0,
         "note": "no AI image/video generation (providers billing-blocked, "
                 "measured); visuals are in-house vertical cards + user "
                 "assets"},
        indent=1), encoding="utf-8")
    ctx["user_images"] = user_images
    return {"assets": len(manifest), "user_images": len(user_images)}


def no_free_generation_short(ctx: dict) -> dict | None:
    ctx["log"]("[shorts] generating stage: no free AI generation (measured) "
               "— vertical cards + user assets only")
    return {"ai_generated": 0, "honest_note": True}


# ── editing: narration → vertical scenes → captions → final ─────────────────

def _vertical_scene(work: Path, image: Path, audio: Path | None,
                    duration: float, index: int, log,
                    transition: bool = False,
                    prefer: str | None = None,
                    enc_runs: list | None = None) -> Path:
    """One vertical clip. Fade-through-black transitions are applied at the
    clip edges (video fade + matching audio fade), so the concatenated result
    keeps exact per-segment timing — no invented crossfade offsets."""
    out = work / f"scene_{index:02d}.mp4"
    frames = max(int(math.ceil(duration * SCENE_FPS)) + 2, SCENE_FPS)
    base = (f"scale={V_W}:{V_H}:force_original_aspect_ratio=increase,"
            f"crop={V_W}:{V_H},"
            f"zoompan=z='min(zoom+0.0006,1.12)'"
            f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d={frames}:s={V_W}x{V_H}:fps={SCENE_FPS}")
    fade_out = max(0.0, duration - TRANSITION_S)
    af = f"apad=whole_dur={duration:.3f}"
    if transition:
        base += (f",fade=t=in:st=0:d={TRANSITION_S}"
                 f",fade=t=out:st={fade_out:.3f}:d={TRANSITION_S}")
        af += (f",afade=t=in:st=0:d={TRANSITION_S}"
               f",afade=t=out:st={fade_out:.3f}:d={TRANSITION_S}")

    def build(enc):
        args = [FFMPEG, "-y", *enc.input_args(), "-i", str(image)]
        if audio is not None and Path(audio).is_file():
            args += ["-i", str(audio), "-filter_complex",
                     enc.upload_filter(base) + "[v]",
                     "-map", "[v]", "-map", "1:a", "-af", af,
                     "-t", f"{duration:.3f}"]
        else:
            args += ["-filter_complex", enc.upload_filter(base) + "[v]",
                     "-map", "[v]", "-t", f"{duration:.3f}"]
        args += enc.video_codec_args(threads=ffmpeg_threads())
        if audio is not None and Path(audio).is_file():
            args += ["-c:a", "aac", "-b:a", "128k", "-ar", "44100"]
        args += [out.name]
        return args, enc.env()

    result = enc_mod.run_encode(build, cwd=work, log=log,
                                what=f"vertical scene {index} "
                                     f"({duration:.1f}s)",
                                prefer=prefer)
    if enc_runs is not None:
        enc_runs.append({"index": index, "backend": result["backend"],
                         "elapsed_s": result["elapsed_s"],
                         "fell_back": result["fell_back"]})
    return out


def _load_short_script(run_dir: Path) -> dict:
    path = run_dir / "research" / "short_script.json"
    if not path.is_file():
        raise StageError("editing: no short script found in the run dir")
    return json.loads(path.read_text(encoding="utf-8"))


def _beats_to_script(script: dict) -> dict:
    """Map the short script onto tts_runner's expected shape
    (hook / chapters / ending)."""
    return {"title": script.get("title") or "",
            "hook": script.get("hook") or "",
            "chapters": [{"title": f"Beat {i}", "narration": beat,
                          "visual_notes": "", "estimated_seconds": None}
                         for i, beat in enumerate(script.get("beats") or [],
                                                  start=1)],
            "ending": script.get("closing") or ""}


def _topup_segments(ctx: dict, spec: dict, script: dict,
                    timings: dict, log) -> None:
    """Close a real duration shortfall with extra LLM beats (bounded at 2).

    The script LLM sometimes returns fewer words than the budget asked for
    (measured: a 56 s ask rendered 37 s). This tops up using the REAL
    measured speech total — never by stretching audio or inventing timing —
    and records every added segment in timings.json (topup: true)."""
    code = tts_runner.language_code(spec.get("language"))
    pace = 2.1 if code == "hi" else 1.6     # measured gTTS pace, see plan_short
    target = float(spec.get("duration_s") or 0)
    if not target or str(timings.get("engine") or "").startswith("reused:"):
        return
    segments = timings["segments"]
    run_dir = ctx["run_dir"]
    cards = run_dir / "cards"
    narr_dir = run_dir / "narration"
    eng = None
    for _ in range(2):
        speech = sum(float(s["duration_s"]) for s in segments)
        gap = target - speech - SCENE_TAIL_S * (len(segments) + 1)
        if gap < 5.0:
            break
        if eng is None:
            engines = tts_runner.default_engines(spec.get("language"))
            if not engines:
                log("[shorts] top-up skipped: no TTS engine available")
                break
            eng = engines[0]
        ask = {
            "task": "write ONE extra spoken beat for this short",
            "topic": spec.get("topic"),
            "language": spec.get("language") or "simple English",
            "max_words": int(gap * pace),
            "existing_hook": str(script.get("hook") or "")[:120],
            "existing_beats": [str(b)[:80] for b in
                               (script.get("beats") or [])][-4:],
            "output_format": {"beat": "string (one short spoken sentence)"},
        }
        raw, provider = _ask_llm(ctx, json.dumps(ask, ensure_ascii=False),
                                 "You write punchy vertical-video shorts. "
                                 "Respond with ONE JSON object only.")
        data = script_writer.extract_json(raw)
        text = str((data or {}).get("beat") or "").strip()
        if not text:
            log(f"[shorts] top-up beat not produced by {provider} — stopping")
            break
        seg_id = max(int(s["id"]) for s in segments) + 1
        wav = narr_dir / f"segment_{seg_id:02d}.mp3"
        try:
            eng.synth_to_file(wav, text)
            duration = tts_runner._wav_duration(wav)
            if duration <= 0.05:
                raise tts_runner.TTSError("empty audio")
        except tts_runner.TTSError as exc:
            log(f"[shorts] top-up narration failed honestly: {exc}")
            break
        beat_index = len(script.get("beats") or []) + 1
        seg = {"id": seg_id, "kind": "beat", "index": beat_index,
               "title": f"Beat {beat_index}", "text": text,
               "visual_notes": "", "estimated_seconds": None,
               "path": str(wav), "duration_s": duration,
               "engine": getattr(eng, "name", "?"), "attempts": 1,
               "topup": True, "timing_granularity": "per-segment"}
        # insert before the closing segment so the piece still ends on it
        insert_at = (len(segments) - 1 if segments
                     and segments[-1].get("kind") == "closing"
                     else len(segments))
        segments.insert(insert_at, seg)
        _vcard(cards / f"beat_{beat_index}.png", text[:120],
               branding=str(spec.get("branding") or ""),
               font=_font_for(spec.get("language")))
        script.setdefault("beats", []).insert(beat_index - 1, text)
        log(f"[shorts] top-up beat {beat_index} ({duration:.1f}s) closes a "
            f"{gap:.1f}s gap")
    timings["total_speech_s"] = round(sum(float(s["duration_s"])
                                          for s in segments), 3)
    timings["duration_topup"] = True
    (run_dir / "narration").mkdir(parents=True, exist_ok=True)
    (run_dir / "narration" / "timings.json").write_text(
        json.dumps(timings, indent=1, ensure_ascii=False), encoding="utf-8")
    ctx["script"] = script
    research = run_dir / "research" / "short_script.json"
    if research.is_file():
        research.write_text(json.dumps(script, indent=1, ensure_ascii=False),
                            encoding="utf-8")
    # keep the asset manifest honest about the added cards
    manifest_path = run_dir / "assets" / "manifest.json"
    if manifest_path.is_file():
        try:
            mf = json.loads(manifest_path.read_text())
            have = {a.get("asset") for a in mf.get("assets", [])}
            for s in segments:
                if s.get("topup"):
                    key = f"cards/beat_{s['index']}.png"
                    if key not in have:
                        mf.setdefault("assets", []).append(
                            {"asset": key, "source": "generated (PIL)",
                             "license": "in-house, no third-party content",
                             "topup": True})
            manifest_path.write_text(json.dumps(mf, indent=1,
                                                ensure_ascii=False),
                                     encoding="utf-8")
        except Exception:                          # noqa: BLE001
            pass


def _reused_narration(spec: dict, run_dir: Path,
                      log) -> tuple[dict | None, list[dict]]:
    """Documentary → Shorts: copy the selected REAL narration segments (and
    their measured durations) from a completed documentary run."""
    reuse_dir = Path(str(spec.get("reuse_narration_from") or "")).expanduser()
    if not reuse_dir.is_dir() or not (reuse_dir / "narration" /
                                      "timings.json").is_file():
        return None, []
    src = json.loads((reuse_dir / "narration" / "timings.json").read_text())
    wanted = {int(i) for i in (spec.get("segment_ids") or [])}
    segments = [dict(s) for s in src["segments"]
                if not wanted or int(s["id"]) in wanted]
    if not segments:
        return None, []
    narr_dir = run_dir / "narration"
    narr_dir.mkdir(parents=True, exist_ok=True)
    for seg in segments:
        dst = narr_dir / f"segment_{seg['id']:02d}.mp3"
        shutil.copyfile(seg["path"], dst)
        seg["path"] = str(dst)
        seg["reused"] = True
    timings = {"engine": f"reused:{src.get('engine')}",
               "timing_granularity": "per-segment",
               "word_level_available": False,
               "total_speech_s": round(sum(s["duration_s"]
                                           for s in segments), 3),
               "segments": segments}
    (narr_dir / "timings.json").write_text(json.dumps(timings, indent=1),
                                           encoding="utf-8")
    log(f"[shorts] reusing {len(segments)} narration segment(s) from "
        f"{reuse_dir}")
    return timings, segments


def edit_short(ctx: dict) -> dict | None:
    run_dir, spec = ctx["run_dir"], ctx["spec"]
    work = run_dir / "work"
    work.mkdir(parents=True, exist_ok=True)
    log = ctx["log"]

    # 1. narration — real audio per segment; OR reuse an existing
    #    documentary's narration (documentary → Shorts)
    language = str(spec.get("language") or "en")
    transitions = bool(spec.get("transitions"))
    timings, segments = _reused_narration(spec, run_dir, log)
    if not segments:
        script = ctx.get("script") or _load_short_script(run_dir)
        timings = tts_runner.synthesize_narration(
            _beats_to_script(script), run_dir, language=language, log=log)
        segments = timings["segments"]
    if not segments:
        raise StageError("no narration segments were produced")
    # duration honesty: close a real shortfall with extra beats (bounded),
    # measured against the REAL speech total — never stretched audio
    _topup_segments(ctx, spec, ctx.get("script") or
                    _load_short_script(run_dir), timings, log)
    segments = timings["segments"]

    # 2. scene plan (hook card → beat cards / user images → closing card)
    cards = run_dir / "cards"
    user_images = ctx.get("user_images") or []
    scenes = []
    for seg in segments:
        if seg["kind"] == "hook":
            image = cards / "title.png"
        elif seg["kind"] == "closing":
            image = cards / "closing.png"
        else:
            idx = int(seg.get("index") or 1) if seg.get("index") else seg["id"]
            image = (user_images[idx - 1] if user_images
                     and idx - 1 < len(user_images)
                     else cards / f"beat_{idx}.png")
        if not Path(image).is_file():
            image = cards / "title.png"
        scenes.append({"segment": seg, "image": image,
                       "duration": float(seg["duration_s"]) + SCENE_TAIL_S})

    # 3. render vertical scenes + concat
    scene_files, enc_runs = [], []
    for i, scene in enumerate(scenes, start=1):
        scene_files.append(_vertical_scene(work, scene["image"],
                                           Path(scene["segment"]["path"]),
                                           scene["duration"], i, log,
                                           transition=transitions,
                                           prefer=str(spec.get("encoder")
                                                      or "") or None,
                                           enc_runs=enc_runs))
    concat = work / "concat.txt"
    concat.write_text("".join(f"file '{f.name}'\n" for f in scene_files),
                      encoding="utf-8")
    joined = work / "joined.mp4"
    _ffmpeg([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", concat.name,
             "-c", "copy", joined.name], work, log, "concat scenes")

    # 4. captions — per-segment SRT with REAL timings
    starts, t = [], 0.0
    for scene in scenes:
        starts.append(t)
        t += scene["duration"]
    (work / "subtitles.srt").write_text(_srt(segments, starts),
                                        encoding="utf-8")

    # 5. final pass: burn captions (safe-area margin) + optional user music
    music = Path(str(spec.get("music_file") or "")).expanduser()
    final = run_dir / "render" / "short.mp4"
    final.parent.mkdir(parents=True, exist_ok=True)
    code = tts_runner.language_code(language)
    font_name = "Lohit Devanagari" if code in ("hi", "mr", "ne", "sa") else ""
    # libass margins are in the SRT→ASS script's PlayRes units (288 high by
    # default in ffmpeg), NOT pixels: MarginV=420 scaled to 2800 px and pushed
    # every caption off-screen (found by frame inspection 2026-10-26). The
    # 420 px safe-area target converts to 420 * 288 / 1920 = 63 units.
    margin_units = max(1, round(_CAPTION_SAFE_BOTTOM * 288 / V_H))
    style = (f"FontSize=13,PrimaryColour=&H00F5F7FA,"
             f"OutlineColour=&H00000000,Borderstyle=1,Outline=2,Shadow=1,"
             f"MarginV={margin_units}"
             + (f",FontName={font_name}" if font_name else ""))
    tmp_final = run_dir / "render" / "short.mp4.part"
    subtitle_chain = (f"subtitles=work/subtitles.srt:"
                      f"force_style='{style}'")

    def build(enc):
        args: list[str] = [FFMPEG, "-y", *enc.input_args(),
                           "-i", str(joined.relative_to(run_dir))]
        filters = f"[0:v]{enc.upload_filter(subtitle_chain)}[v]"
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
        build, cwd=run_dir, log=log, what="final render (captions + mix)",
        prefer=str(spec.get("encoder") or "") or None)
    atomic_final(tmp_final, final)
    (run_dir / "render" / "encoder.json").write_text(json.dumps(
        {"final": final_run, "scenes": enc_runs,
         "output_contract": {"video_codec": "h264", "audio_codec": "aac",
                             "fps": SCENE_FPS, "size": [V_W, V_H]},
         "transitions": "fade" if transitions else "none",
         "language": language, "language_code": code}, indent=1),
        encoding="utf-8")

    for f in scene_files:
        f.unlink(missing_ok=True)
    (work / "joined.mp4").unlink(missing_ok=True)
    cleanup_run_dir(run_dir)
    # optional platform metadata (mission: optional title/description)
    script = ctx.get("script") or _load_short_script(run_dir)
    (run_dir / "render" / "metadata.txt").write_text(
        f"{script.get('title') or ''}\n\n{script.get('description') or ''}\n",
        encoding="utf-8")
    return {"output": str(final), "scenes": len(scene_files),
            "speech_s": round(timings["total_speech_s"], 1),
            "engine": timings.get("engine"),
            "language": language, "transitions":
                "fade" if transitions else "none",
            "encoder": final_run["backend"],
            "encoder_elapsed_s": final_run["elapsed_s"],
            "reused": bool(timings.get("engine", "").startswith("reused:"))}


# ── validating ───────────────────────────────────────────────────────────────

def validate_short(ctx: dict) -> dict | None:
    run_dir = ctx["run_dir"]
    final = run_dir / "render" / "short.mp4"
    if not final.is_file() or final.stat().st_size == 0:
        raise StageError("validated: render/short.mp4 missing or empty")
    facts = probe_video(final)
    if facts["video_codec"] != "h264" or facts["audio_codec"] != "aac":
        raise StageError(f"validated: unexpected codecs {facts}")
    if (facts["width"], facts["height"]) != (V_W, V_H):
        raise StageError(f"validated: not vertical {facts['width']}x"
                         f"{facts['height']}")
    if facts["duration_s"] < 3:
        raise StageError(f"validated: suspiciously short "
                         f"({facts['duration_s']}s)")
    (run_dir / "validation.json").write_text(json.dumps(
        {"ok": True, "ffprobe": facts, "mode": "shorts",
         "ai_generated_visuals": 0,
         "timing_granularity": "per-segment"}, indent=1), encoding="utf-8")
    return {"validated": facts}


# ── documentary → Shorts splitting ───────────────────────────────────────────

def split_documentary(doc_dir: str | Path, count: int = 3
                      ) -> list[dict[str, Any]]:
    """Pick `count` narration segments from an existing documentary run for
    Shorts reuse — evenly spaced across its chapter segments, with their REAL
    measured durations."""
    doc = Path(doc_dir).expanduser()
    timings_path = doc / "narration" / "timings.json"
    if not timings_path.is_file():
        raise StageError(
            f"no narration timings found in {doc} — Shorts splitting needs a "
            "completed documentary run (its narration audio is reused)")
    timings = json.loads(timings_path.read_text(encoding="utf-8"))
    chapters = [s for s in timings["segments"]
                if s.get("kind") == "chapter"] or timings["segments"]
    count = max(1, min(int(count), len(chapters)))
    if len(chapters) <= count:
        picks = chapters
    else:
        step = len(chapters) / count
        picks = [chapters[min(int(i * step), len(chapters) - 1)]
                 for i in range(count)]
    return [{"segment_ids": [s["id"]], "duration_s": s["duration_s"],
             "text": str(s.get("text") or "")[:120], "path": s["path"]}
            for s in picks]


# ── registry wiring ──────────────────────────────────────────────────────────

STAGES = {
    "planning": plan_short,
    "scripting": segment_short,
    "asset_collection": collect_assets_short,
    "generating": no_free_generation_short,
    "editing": edit_short,
    "validating": validate_short,
}
