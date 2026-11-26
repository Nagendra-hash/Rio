"""
core/video_agent/pipeline_lesson.py — narrated 3D LESSON production mode.

Script in → finished, subtitled, lip-synced vertical MP4 out. Fully local.

Stages (each one reports into the manifest):
  1. TTS      Kokoro, one WAV per sentence, two voices (expert + boy),
              measured durations — the timeline is never guessed
  2. LIPSYNC  misaki phonemes forced-aligned (DTW) onto the measured RMS
              envelope of each sentence, per speaker → viseme track, then
              verify_alignment() must pass or the run stops
  3. DIRECTION scenes / shots / performance cues as data (direction.json)
  4. RENDER   Blender headless: two articulated characters, two sets,
              mouth + brows keyed from the viseme track, blinks by squashing
              the eyeball
  5. ASSEMBLE narration + burned subtitles, loudnorm to -16 LUFS
  6. VALIDATE ffprobe contract, 24 fps, audio level, and a motion proof

Honest limits (written into every manifest):
  * characters are articulated JOINT-PIVOT rigs — parented empties at
    shoulders/elbows/hips/knees/neck with per-limb keyframes. They are NOT
    armature/skinning rigs and NOT blend-shape face rigs.
  * the mouth is an 11-shape viseme set driven by the REAL audio; it is not
    phoneme-perfect (consonants sharing a shape look identical)
  * blink, brow and head motion are procedural, not mocap
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import time
import wave
from pathlib import Path
from typing import Any, Callable

import numpy as np

from core.video_agent import encoder as enc_mod
from core.video_agent import lipsync as ls
from core.video_agent.avatar_pipeline import (_ass_escape, build_srt,
                                              split_sentences)
from core.video_agent.pipelines import FFMPEG, StageError, probe_video

__all__ = ["run_lesson", "build_direction", "synthesize_script"]

BLENDER_BIN_ENV = "JARVIS_BLENDER_BIN"
_SCENE_SCRIPT = (Path(__file__).parent / "blender" / "build_lesson_scene.py")
FPS = 24
RES = (720, 1280)
SENTENCE_GAP_S = 0.30
TAIL_S = 0.9                      # beat after the last word of the film
SCENE_GAP_S = 0.15                # silence between a scene's last word and
                                  # the cut to the next scene


def _blender_bin() -> str:
    """Same resolution order as the dance pipeline: env override, then the
    locally installed Blender (it is not on PATH)."""
    return os.environ.get(BLENDER_BIN_ENV) or str(
        Path.home() / "JarvisTools" / "blender-3.6.23-linux-x64" / "blender")


def _log_default(msg: str) -> None:
    print(msg, flush=True)


# ── stage 1: speech ─────────────────────────────────────────────────────────

def synthesize_script(scenes: list[dict], out_dir: Path,
                      voices: dict[str, str],
                      log: Callable[[str], None] = _log_default) -> dict:
    """One WAV per sentence, in order, with a real measured timeline."""
    from core.tts import KokoroTTSEngine

    out_dir = Path(out_dir)
    wav_dir = out_dir / "speech"
    wav_dir.mkdir(parents=True, exist_ok=True)

    flat: list[dict] = []
    for si, scene in enumerate(scenes):
        for li, (who, text) in enumerate(scene["lines"]):
            flat.append({"scene": si, "line": li, "speaker": who,
                         "text": text.strip()})
    if not flat:
        raise StageError("lesson: the script has no lines")

    engine = KokoroTTSEngine(voice=voices.get(flat[0]["speaker"],
                                              "am_michael"))
    cursor = 0.0
    for i, item in enumerate(flat):
        engine.voice = voices.get(item["speaker"], engine.voice)
        wav = wav_dir / f"line_{i:03d}.wav"
        if not wav.is_file():
            result = engine.synth_to_wav(wav, item["text"])
            dur = float(result["duration_s"])
        else:                                   # resume without re-synth
            dur = _wav_seconds(wav)
        item.update({"wav": str(wav), "start": round(cursor, 3),
                     "dur": round(dur, 3),
                     "end": round(cursor + dur, 3)})
        cursor += dur + SENTENCE_GAP_S

    total = round(cursor - SENTENCE_GAP_S + TAIL_S, 3)
    narration = out_dir / "narration.wav"
    _concat_wavs(flat, narration, total)
    log(f"[lesson-tts] {len(flat)} lines, {len(scenes)} scenes, "
        f"{total:.1f}s")
    return {"lines": flat, "wav": str(narration), "total_s": total}


def _wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as wf:
        return wf.getnframes() / float(wf.getframerate())


def _concat_wavs(lines: list[dict], out: Path, total_s: float) -> None:
    """Place every measured sentence on one timeline at 24 kHz mono."""
    rate = 24000
    buf = np.zeros(int(total_s * rate) + rate, dtype=np.float32)
    for item in lines:
        data, r = ls.read_wav(Path(item["wav"]))
        if r != rate:
            idx = np.linspace(0, data.size, int(data.size * rate / r))
            data = np.interp(idx, np.arange(data.size), data).astype(
                np.float32)
        a = int(item["start"] * rate)
        buf[a:a + data.size] += data
    buf = buf[:int(total_s * rate)]
    peak = float(np.max(np.abs(buf))) or 1.0
    if peak > 0.98:                              # headroom, never clip
        buf *= 0.98 / peak
    with wave.open(str(out), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes((np.clip(buf, -1, 1) * 32767).astype("<i2").tobytes())


# ── stage 2: lip-sync ───────────────────────────────────────────────────────

def build_viseme_tracks(lines: list[dict],
                        log: Callable[[str], None] = _log_default
                        ) -> tuple[dict[str, dict], dict[str, dict]]:
    """One viseme track per speaker + the audio proof for each."""
    by_speaker: dict[str, list[dict]] = {}
    for item in lines:
        by_speaker.setdefault(item["speaker"], []).append(
            {"text": item["text"], "wav": item["wav"],
             "start": item["start"]})
    tracks, proofs = {}, {}
    for who, segs in by_speaker.items():
        track = ls.viseme_track(segs, fps=FPS)
        proof = ls.verify_alignment(track, segs)
        tracks[who] = track
        proofs[who] = proof
        log(f"[lesson-lipsync] {who}: {len(track['events'])} visemes, "
            f"ok={proof.get('ok')} {proof.get('checks')}")
        if not proof.get("ok"):
            raise StageError(
                f"lesson: lip-sync failed the audio proof for {who}: {proof}")
    return tracks, proofs


# ── stage 3: direction ──────────────────────────────────────────────────────

def build_direction(scenes: list[dict], lines: list[dict],
                    total_s: float) -> dict:
    """Turn the content module into the Blender direction document."""
    # Scene boundaries are CONTIGUOUS: t1 of a scene is t0 of the next one.
    # The first version ended a scene 0.9 s after its last word while the next
    # scene began at its first word, so the two overlapped by 0.6 s — the
    # camera then had keys out of order and, at the cut, interpolated from the
    # classroom setup to the playground one, landing on the chalkboard.
    owned = []
    for si, scene in enumerate(scenes):
        own = [x for x in lines if x["scene"] == si]
        if own:
            owned.append((si, scene, own))

    bounds = []
    for i, (si, scene, own) in enumerate(owned):
        start = 0.0 if i == 0 else round(owned[i - 1][2][-1]["end"]
                                         + SCENE_GAP_S, 3)
        end = (round(own[-1]["end"] + SCENE_GAP_S, 3)
               if i + 1 < len(owned) else round(total_s, 3))
        bounds.append((start, end))

    out_scenes = []
    for i, (si, scene, own) in enumerate(owned):
        t0, t1 = bounds[i]
        span = max(0.001, t1 - t0)

        shots = []
        for shot in scene["shots"]:
            s = dict(shot)
            if "lines" in shot:
                # pin the cut to the dialogue instead of to a fraction of
                # the scene: a reaction shot has to land on the reaction
                a = max(0, min(int(shot["lines"][0]), len(own) - 1))
                b = max(a, min(int(shot["lines"][1]), len(own) - 1))
                s["t0"] = round(own[a]["start"], 3)
                hold = 0.25 if b == len(own) - 1 else 0.0
                s["t1"] = round(own[b]["end"] + hold, 3)
            else:
                s["t0"] = round(t0 + span * float(shot["from"]), 3)
                s["t1"] = round(t0 + span * float(shot["to"]), 3)
            shots.append(s)

        cues = []
        for cue in scene["cues"]:
            a, b = int(cue["from"]), int(cue["to"])
            a = min(a, len(own) - 1)
            b = min(b, len(own) - 1)
            item = dict(cue)
            item["t0"] = round(own[a]["start"], 3)
            item["t1"] = round(own[b]["end"], 3)
            cues.append(item)

        # Shots must TILE the scene: contiguous, no overlap, no gap. A gap
        # turns the cut into a slow drift, and two shots at the same time
        # leave duplicate keys on the same frame.
        shots.sort(key=lambda x: float(x["t0"]))
        keep = [x for x in shots
                if float(x["t1"]) - float(x["t0"]) >= 2.0 / FPS]
        for k, shot in enumerate(keep):
            shot["t1"] = (round(float(keep[k + 1]["t0"]) - 1.0 / FPS, 3)
                          if k + 1 < len(keep) else round(t1, 3))
        # a shot whose line range collapsed onto another shot's start would
        # now be zero-length: drop it and re-tile
        keep = [x for x in keep if float(x["t1"]) - float(x["t0"]) >= 1.0 / FPS]
        for k, shot in enumerate(keep):
            shot["t1"] = (round(float(keep[k + 1]["t0"]) - 1.0 / FPS, 3)
                          if k + 1 < len(keep) else round(t1, 3))
        if keep:
            # the first shot starts AT the scene start (the camera and the
            # cast teleport on the same frame = a hard cut), and the last
            # shot stops one frame short so the next scene's first key is not
            # written onto the same frame
            keep[0]["t0"] = round(t0, 3)
            if i + 1 < len(owned):
                keep[-1]["t1"] = round(t1 - 1.0 / FPS, 3)
        out_scenes.append({"env": scene["env"], "t0": t0, "t1": t1,
                           "cast": scene["cast"], "shots": keep,
                           "cues": cues})

    return {"fps": FPS, "res": f"{RES[0]}x{RES[1]}",
            "duration": round(total_s, 3),
            "speakers": sorted({x["speaker"] for x in lines}),
            "scenes": out_scenes}


# ── stage 4: render ─────────────────────────────────────────────────────────

def render_lesson(out_dir: Path, direction: dict, tracks: dict[str, dict],
                  log: Callable[[str], None] = _log_default) -> dict:
    out_dir = Path(out_dir)
    run_dir = out_dir / "render"
    run_dir.mkdir(parents=True, exist_ok=True)
    frames_dir = run_dir / "frames"
    frames_dir.mkdir(exist_ok=True)
    blend = run_dir / "lesson.blend"
    direction_path = run_dir / "direction.json"
    visemes_path = run_dir / "visemes.json"
    direction_path.write_text(json.dumps(direction, indent=1),
                              encoding="utf-8")
    visemes_path.write_text(json.dumps({"tracks": tracks}),
                            encoding="utf-8")

    duration = float(direction["duration"])
    end_frame = int(round(FPS * duration))

    def blender(args: list[str], what: str, timeout: int = 3600) -> str:
        proc = subprocess.run(
            [_blender_bin(), "-b", "--factory-startup"] + args,
            capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip()[-600:]
            raise StageError(f"blender {what} failed (exit "
                             f"{proc.returncode}): {tail}")
        return proc.stdout or ""

    reuse = os.environ.get("JARVIS_LESSON_REUSE_FRAMES", "") == "1"
    have = len(list(frames_dir.glob("frame_*.png")))
    if reuse and have == end_frame:
        log(f"[lesson] reusing {have} frames")
        render_s = 0.0
    else:
        for old in frames_dir.glob("frame_*.png"):
            old.unlink()
        out = blender(["--python", str(_SCENE_SCRIPT), "--",
                       "--out", str(blend),
                       "--direction", str(direction_path),
                       "--visemes", str(visemes_path),
                       "--res", f"{RES[0]}x{RES[1]}", "--fps", str(FPS)],
                      "scene build", 600)
        for line in out.splitlines():
            if line.startswith("SCENE_BUILT") or line.startswith("[lesson]"):
                log(f"[blender] {line}")
        t0 = time.time()
        blender(["-b", str(blend), "-o", str(frames_dir / "frame_"),
                 "-F", "PNG", "-s", "1", "-e", str(end_frame),
                 "-f", f"1..{end_frame}"],
                f"render {end_frame} frames", 3600 * 4)
        have = len(list(frames_dir.glob("frame_*.png")))
        if have != end_frame:
            raise StageError(f"lesson: expected {end_frame} frames, "
                             f"got {have}")
        render_s = round(time.time() - t0, 1)
        log(f"[lesson] {have} frames in {render_s}s "
            f"({render_s / max(1, have):.2f}s/frame)")

    video = run_dir / "lesson_silent.mp4"
    tmp = video.with_suffix(".mp4.part")

    def build(enc):
        args = [FFMPEG, "-y", *enc.input_args(),
                "-framerate", str(FPS),
                "-i", str(frames_dir / "frame_%04d.png"),
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-t", f"{duration:.3f}"]
        args += enc.video_codec_args(threads=2)
        args += ["-c:a", "aac", "-b:a", "128k", "-r", str(FPS),
                 "-map", "0:v", "-map", "1:a",
                 "-f", "mp4", "-movflags", "+faststart", str(tmp)]
        return args, enc.env()

    result = enc_mod.run_encode(build, cwd=run_dir, log=log,
                                what="lesson encode")
    tmp.replace(video)
    return {"video": str(video), "frames": have, "render_s": render_s,
            "encoder": result["backend"], "fps": FPS}


# ── stage 5: assemble ───────────────────────────────────────────────────────

def assemble_lesson(video: Path, narration: Path, srt: Path, out_dir: Path,
                    duration: float,
                    log: Callable[[str], None] = _log_default) -> dict:
    out_dir = Path(out_dir)
    final = out_dir / "lesson_video.mp4"
    tmp = final.with_suffix(".mp4.part")

    def build(enc):
        args = [FFMPEG, "-y", *enc.input_args(), "-i", str(video),
                "-i", str(narration)]
        vf = enc.upload_filter(f"subtitles='{_ass_escape(Path(srt))}'")
        args += ["-vf", vf]
        # -16 LUFS / -1.5 dBTP: the standard web delivery level
        args += ["-filter_complex",
                 "[1:a]apad,loudnorm=I=-16:TP=-1.5:LRA=11[a]"]
        args += ["-map", "0:v", "-map", "[a]", "-t", f"{duration:.3f}"]
        args += enc.video_codec_args(threads=2)
        args += ["-c:a", "aac", "-b:a", "160k", "-ar", "44100",
                 "-f", "mp4", "-movflags", "+faststart", str(tmp)]
        return args, enc.env()

    result = enc_mod.run_encode(build, cwd=out_dir, log=log,
                                what="lesson final mux")
    tmp.replace(final)
    return {"output": str(final), "encoder": result["backend"]}


# ── stage 6: validate ───────────────────────────────────────────────────────

def validate_lesson(final: Path, expected_s: float,
                    log: Callable[[str], None] = _log_default) -> dict:
    facts = probe_video(final)
    if facts["video_codec"] != "h264" or facts["audio_codec"] != "aac":
        raise StageError(f"lesson: unexpected codecs {facts}")
    fps = _fps(final)
    if abs(fps - FPS) > 0.05:
        raise StageError(f"lesson: fps {fps} != {FPS}")
    if abs(facts["duration_s"] - expected_s) > 1.5:
        raise StageError(f"lesson: duration {facts['duration_s']}s vs "
                         f"expected {expected_s}s")
    if facts["width"] != RES[0] or facts["height"] != RES[1]:
        raise StageError(f"lesson: resolution {facts['width']}x"
                         f"{facts['height']}")
    level = _mean_volume_db(final)
    if level < -26.0:
        raise StageError(f"lesson: narration is too quiet ({level} dB)")

    # motion proof: two frames far apart must differ (never a still)
    a = final.parent / "proof_a.png"
    b = final.parent / "proof_b.png"
    _grab(final, expected_s * 0.30, a)
    _grab(final, expected_s * 0.62, b)
    diff = _image_delta(a, b)
    if diff["changed_fraction"] < 0.02:
        raise StageError(f"lesson: frames look static: {diff}")

    log(f"[lesson] validated {facts['width']}x{facts['height']} "
        f"{fps:.2f}fps {facts['duration_s']:.2f}s "
        f"mean {level}dB motion {diff['changed_fraction']:.3f}")
    return {"fps": fps, "duration_s": facts["duration_s"],
            "mean_volume_db": level, "motion": diff}


def _fps(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "0", "-select_streams", "v:0", "-show_entries",
         "stream=r_frame_rate", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True).stdout.strip()
    try:
        num, den = out.split("/")
        return float(num) / float(den or 1)
    except Exception:
        return 0.0


def _mean_volume_db(path: Path) -> float:
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
         "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True)
    for line in (proc.stderr or "").splitlines():
        if "mean_volume:" in line:
            return float(line.split("mean_volume:")[1].split("dB")[0])
    return -99.0


def _grab(video: Path, t: float, out: Path) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "0", "-ss", f"{t:.3f}",
                    "-i", str(video), "-frames:v", "1", str(out)],
                   check=True)


def _image_delta(a: Path, b: Path) -> dict:
    from PIL import Image
    import numpy as _np
    x = _np.asarray(Image.open(a).convert("L"), dtype=_np.int16)
    y = _np.asarray(Image.open(b).convert("L"), dtype=_np.int16)
    d = _np.abs(x - y)
    return {"changed_fraction": round(float((d > 12).mean()), 4),
            "mean_delta": round(float(d.mean()), 2)}


# ── orchestration ───────────────────────────────────────────────────────────

def run_lesson(scenes: list[dict], out_dir: str | Path, *,
               voices: dict[str, str],
               title: str = "RIO lesson",
               log: Callable[[str], None] = _log_default) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()

    speech = synthesize_script(scenes, out_dir, voices, log=log)
    tracks, proofs = build_viseme_tracks(speech["lines"], log=log)
    direction = build_direction(scenes, speech["lines"], speech["total_s"])
    (out_dir / "direction.json").write_text(json.dumps(direction, indent=1),
                                            encoding="utf-8")
    (out_dir / "visemes.json").write_text(json.dumps({"tracks": tracks}),
                                          encoding="utf-8")

    timings = [{"text": x["text"], "start": x["start"], "end": x["end"]}
               for x in speech["lines"]]
    srt = build_srt(timings, out_dir / "lesson.srt")

    render = render_lesson(out_dir, direction, tracks, log=log)
    final = assemble_lesson(Path(render["video"]), Path(speech["wav"]), srt,
                            out_dir, float(direction["duration"]), log=log)
    checks = validate_lesson(Path(final["output"]),
                             float(direction["duration"]), log=log)

    manifest = {
        "title": title,
        "status": "COMPLETED",
        "mode": "lesson_3d",
        "output": final["output"],
        "duration_s": checks["duration_s"],
        "fps": checks["fps"],
        "resolution": list(RES),
        "lines": len(speech["lines"]),
        "scenes": len(direction["scenes"]),
        "shots": sum(len(s["shots"]) for s in direction["scenes"]),
        "words": sum(len(x["text"].split()) for x in speech["lines"]),
        "voices": voices,
        "lip_sync": {
            "method": "misaki IPA phonemes + DTW forced alignment on the "
                      "measured RMS envelope of the real TTS audio",
            "speakers": proofs,
        },
        "capabilities": {
            "rig": "articulated joint-pivot rig (parented empties at "
                   "shoulders/elbows/hips/knees/neck) — not armature/skinning",
            "face": "11-shape viseme mouth + eyeball-squash blinks + brow "
                    "lift + head nods, procedural; no blend-shape face rig, "
                    "no mocap",
            "camera": "directed shots with cuts; movement inside a shot is "
                      "a dolly, not a zoom",
            "music": "none (narration only)",
        },
        "checks": checks,
        "render": render,
        "elapsed_s": round(time.time() - started, 1),
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1),
                                           encoding="utf-8")
    log(f"[lesson] done → {final['output']}")
    return manifest
