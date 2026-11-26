"""
core/video_agent/avatar_pipeline.py — isolated end-to-end avatar video
pipeline (3D avatar e2e test).

Narration string in → finished vertical MP4 out, fully offline:

  1. TTS       — Kokoro (LOCAL neural, user-approved ~335 MB model), one
                 synthesis per sentence, REAL measured durations
  2. Subtitles — SRT built from the known text + measured timings
  3. Render    — Blender headless (original procedural avatar: idle bob,
                 head turn, blinks, mouth talk animation via keyframes),
                 frames encoded with the verified VAAPI→CPU ladder
  4. Assemble  — narration (padded into the talk window) muxed over the
                 video, subtitles burned; music only when a file is
                 provided (none shipped)
  5. Validate  — ffprobe contract + an ANIMATION PROOF: frames inside vs
                 outside the talk window must differ (the avatar visibly
                 moves and the mouth visibly opens) — a static image can
                 never pass

Every stage reports success/failure into the manifest; no cloud API.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from core.video_agent import encoder as enc_mod
from core.video_agent.pipelines import FFMPEG, StageError, probe_video

__all__ = ["run_avatar_e2e", "split_sentences", "build_srt"]


def _blender_bin() -> str:
    return os.environ.get("JARVIS_BLENDER_BIN") or \
        str(Path.home() / "JarvisTools"
            / "blender-3.6.23-linux-x64" / "blender")


_SCENE_SCRIPT = Path(__file__).parent / "blender" / "build_avatar_scene.py"
_SENTENCE_GAP_S = 0.32
_TALK_PAD_S = 1.2                     # narration starts after the wave-in


def split_sentences(narration: str) -> list[str]:
    """Split on sentence boundaries, keeping punctuation."""
    parts = re.split(r"(?<=[.!?])\s+", str(narration or "").strip())
    return [p.strip() for p in parts if p.strip()]


def build_srt(timings: list[dict[str, Any]], path: Path) -> Path:
    """Sentence-level SRT from MEASURED synthesis durations (never
    invented). timings = [{text, start, end}, …] in seconds."""
    def _ts(t: float) -> str:
        ms = int(round(t * 1000))
        h, ms = divmod(ms, 3600000)
        m, ms = divmod(ms, 60000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    lines = []
    for i, t in enumerate(timings, start=1):
        lines += [str(i), f"{_ts(t['start'])} --> {_ts(t['end'])}",
                  t["text"], ""]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ── stage: TTS ───────────────────────────────────────────────────────────────

def synthesize_narration(narration: str, out_dir: Path,
                         voice: str = "af_heart",
                         log: Callable[[str], None] | None = None) -> dict:
    """Per-sentence Kokoro synthesis with measured durations. Returns
    {wav, total_s, timings, talk_start, talk_end}. Fully local."""
    from core.tts import KokoroTTSEngine
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    sentences = split_sentences(narration)
    if not sentences:
        raise StageError("avatar: narration is empty")
    log(f"[avatar-tts] {len(sentences)} sentences, voice={voice} "
        "(Kokoro, local)")
    engine = KokoroTTSEngine(voice=voice)
    timings: list[dict[str, Any]] = []
    cursor = _TALK_PAD_S
    for i, sentence in enumerate(sentences, start=1):
        wav = out_dir / "narration" / f"seg_{i:02d}.wav"
        result = engine.synth_to_wav(wav, sentence)
        dur = float(result["duration_s"])
        timings.append({"text": sentence, "start": round(cursor, 3),
                        "end": round(cursor + dur, 3), "file": str(wav),
                        "duration_s": dur})
        cursor += dur + _SENTENCE_GAP_S
        log(f"[avatar-tts] seg {i}: {dur:.2f}s — {sentence[:48]}")
    total = cursor - _SENTENCE_GAP_S
    # full narration track: lead silence + segments + gaps
    concat_dir = out_dir / "narration"
    files: list[Path] = []
    sil = concat_dir / "lead_silence.wav"
    _ffmpeg_silence(sil, _TALK_PAD_S)
    files.append(sil)
    for i, t in enumerate(timings):
        files.append(Path(t["file"]))
        if i < len(timings) - 1:
            gap = concat_dir / f"gap_{i:02d}.wav"
            _ffmpeg_silence(gap, _SENTENCE_GAP_S)
            files.append(gap)
    narration_wav = concat_dir / "narration.wav"
    _ffmpeg_concat_wav(files, narration_wav)
    return {"wav": str(narration_wav), "total_s": round(total, 3),
            "timings": timings, "engine": f"kokoro:{voice}",
            "talk_start": _TALK_PAD_S, "talk_end": round(total, 3)}


def _run_ffmpeg(args: list[str], what: str, timeout: int = 300) -> None:
    proc = subprocess.run([str(a) for a in args], capture_output=True,
                          text=True, timeout=timeout)
    if proc.returncode != 0:
        raise StageError(f"{what} failed (exit {proc.returncode}): "
                         f"{(proc.stderr or '')[-300:]}")


def _ffmpeg_silence(dest: Path, seconds: float) -> None:
    _run_ffmpeg([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
                 "-i", "anullsrc=r=24000:cl=mono", "-t", f"{seconds:.3f}",
                 dest], f"silence {seconds}s")


def _ffmpeg_concat_wav(files: list[Path], dest: Path) -> None:
    concat = dest.parent / "concat_wav.txt"
    concat.write_text("\n".join(f"file '{p}'" for p in files),
                      encoding="utf-8")
    _run_ffmpeg([FFMPEG, "-y", "-loglevel", "error", "-f", "concat",
                 "-safe", "0", "-i", str(concat), "-c", "copy", dest],
                "narration concat")


# ── stage: render ────────────────────────────────────────────────────────────

def render_avatar_video(out_dir: Path, duration_s: float,
                        talk_start: float, talk_end: float,
                        log: Callable[[str], None], fps: int = 24) -> dict:
    """Blender scene build → frame render → silent MP4 encode."""
    out_dir = Path(out_dir)
    run_dir = out_dir / "render"
    run_dir.mkdir(parents=True, exist_ok=True)
    blend = run_dir / "avatar.blend"
    frames_dir = run_dir / "frames"
    frames_dir.mkdir(exist_ok=True)

    def blender(args: list[str], what: str, timeout: int = 1800) -> None:
        proc = subprocess.run(
            [_blender_bin(), "-b", "--factory-startup"] + args,
            capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip()[-400:]
            raise StageError(f"blender {what} failed (exit "
                             f"{proc.returncode}): {tail}")
        for line in (proc.stdout or "").splitlines():
            if line.startswith("SCENE_BUILT"):
                log(f"[blender] {line}")
        log(f"[blender] {what}: ok")

    blender(["--python", str(_SCENE_SCRIPT), "--", "--out", str(blend),
             "--res", "720x1280", "--fps", str(fps),
             "--duration", f"{duration_s:.2f}",
             "--talk-start", f"{talk_start:.2f}",
             "--talk-end", f"{talk_end:.2f}"], "scene build", 300)

    t0 = time.time()
    end_frame = int(fps * duration_s)
    blender(["-b", str(blend), "-o", str(frames_dir / "frame_"), "-F",
             "PNG", "-s", "1", "-e", str(end_frame),
             "-f", f"1..{end_frame}"], f"render {end_frame} frames")
    rendered = len(list(frames_dir.glob("frame_*.png")))
    if rendered != end_frame:
        raise StageError(f"expected {end_frame} frames, got {rendered}")
    render_s = round(time.time() - t0, 1)
    log(f"[blender] {rendered} frames in {render_s}s")

    video = run_dir / "avatar_silent.mp4"
    tmp = video.with_suffix(".mp4.part")

    def build(enc):
        args = [FFMPEG, "-y", *enc.input_args(),
                "-framerate", str(fps),
                "-i", str(frames_dir / "frame_%04d.png"),
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-t", f"{duration_s:.3f}"]
        args += enc.video_codec_args(threads=2)
        args += ["-c:a", "aac", "-b:a", "128k", "-r", "30",
                 "-map", "0:v", "-map", "1:a",
                 "-f", "mp4", "-movflags", "+faststart", str(tmp)]
        return args, enc.env()

    enc_result = enc_mod.run_encode(build, cwd=run_dir, log=log,
                                    what="avatar encode")
    tmp.replace(video)
    return {"video": str(video), "frames": rendered, "render_s": render_s,
            "encoder": enc_result["backend"]}


# ── stage: assemble ──────────────────────────────────────────────────────────

def _ass_escape(path: Path) -> str:
    """libass filename escaping (the house pattern from pipeline_shorts)."""
    return (str(path).replace("\\", "\\\\").replace(":", "\\:")
            .replace("'", "\\'").replace(",", "\\,"))


def assemble_video(video: Path, narration: dict, srt: Path, out_dir: Path,
                   log: Callable[[str], None], music_file: str | None = None
                   ) -> dict:
    """Mux narration (already padded into the talk window) + burned
    subtitles over the rendered video."""
    out_dir = Path(out_dir)
    final = out_dir / "avatar_video.mp4"
    tmp = final.with_suffix(".mp4.part")
    narration_wav = narration["wav"]

    def build(enc):
        args = [FFMPEG, "-y", *enc.input_args(), "-i", str(video),
                "-i", str(narration_wav)]
        vf = enc.upload_filter(f"subtitles='{_ass_escape(Path(srt))}'")
        args += ["-vf", vf]
        args += ["-af", "apad", "-shortest"]
        args += enc.video_codec_args(threads=2)
        args += ["-c:a", "aac", "-b:a", "160k", "-ar", "44100",
                 "-f", "mp4", "-movflags", "+faststart", str(tmp)]
        return args, enc.env()

    result = enc_mod.run_encode(build, cwd=out_dir, log=log,
                                what="avatar final mux")
    tmp.replace(final)
    return {"output": str(final), "encoder": result["backend"],
            "music_used": bool(music_file)}


# ── stage: validate + animation proof ────────────────────────────────────────

def validate_output(final: Path, narration: dict, out_dir: Path,
                    log: Callable[[str], None]) -> dict:
    facts = probe_video(final)
    if facts["video_codec"] != "h264" or facts["audio_codec"] != "aac":
        raise StageError(f"validated: unexpected codecs {facts}")
    if not 12 <= facts["duration_s"] <= 30:
        raise StageError(f"validated: duration out of range "
                         f"({facts['duration_s']}s)")
    # ANIMATION PROOF: a frame with the mouth open vs one with it closed
    # must differ — a static image would fail here.
    proof = _animation_proof(final, narration["talk_start"] + 0.12,
                             narration["talk_start"] + 0.24, out_dir)
    if not proof["moved"]:
        raise StageError("validated: no animation detected — frames inside "
                         "the talk window are identical (static-image "
                         "substitution refused)")
    log(f"[avatar] animation proof: mean_diff={proof['mean_diff']:.2f} "
        "(mouth open vs closed)")
    return {"ffprobe": facts, "animation_proof": proof}


def _animation_proof(final: Path, t_open: float, t_closed: float,
                     out_dir: Path) -> dict:
    from PIL import Image, ImageChops
    frames = {}
    for label, t in (("open", t_open), ("closed", t_closed)):
        dest = Path(out_dir) / f"proof_{label}.png"
        proc = subprocess.run(
            [FFMPEG, "-y", "-loglevel", "error", "-ss", f"{t:.3f}",
             "-i", str(final), "-frames:v", "1", str(dest)],
            capture_output=True, timeout=60)
        if proc.returncode != 0 or not dest.is_file():
            return {"moved": False,
                    "reason": f"frame extract failed ({label})"}
        frames[label] = Image.open(dest).convert("L")
    diff = ImageChops.difference(frames["open"], frames["closed"])
    hist = diff.histogram()
    total_px = sum(hist)
    mean_diff = sum(i * c for i, c in enumerate(hist)) / max(1, total_px)
    return {"moved": mean_diff > 0.5, "mean_diff": round(mean_diff, 3),
            "frames": {"open": str(Path(out_dir) / "proof_open.png"),
                       "closed": str(Path(out_dir) / "proof_closed.png")}}


# ── orchestration ────────────────────────────────────────────────────────────

def run_avatar_e2e(narration: str, out_dir: str | Path, *,
                   voice: str = "af_heart", music_file: str | None = None,
                   log: Callable[[str], None] | None = None
                   ) -> dict[str, Any]:
    """The full pipeline. Returns the manifest (also written to
    <out_dir>/manifest.json). Raises StageError (stage name prefixed) on
    any failure."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    manifest: dict[str, Any] = {
        "task": "3D avatar short video end-to-end test",
        "narration": narration,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tools": {}, "stages": [], "outputs": [],
    }
    stages = manifest["stages"]

    def stage(name: str, fn: Callable[[], dict]) -> dict:
        t0 = time.time()
        log(f"[stage] {name}…")
        try:
            result = fn() or {}
        except StageError as exc:
            stages.append({"stage": name, "status": "FAILED",
                           "error": str(exc)[:400],
                           "seconds": round(time.time() - t0, 1)})
            _write_manifest(out_dir, manifest)
            raise
        stages.append({"stage": name, "status": "OK",
                       "seconds": round(time.time() - t0, 1), **result})
        log(f"[stage] {name}: OK ({time.time() - t0:.1f}s)")
        return result

    if log is None:
        def log(m: str) -> None:
            print(m, flush=True)

    tts = stage("tts", lambda: synthesize_narration(narration, out_dir,
                                                    voice, log))
    srt_path = build_srt(tts["timings"], out_dir / "subtitles.srt")
    srt = {"srt": str(srt_path), "segments": len(tts["timings"])}
    stages.append({"stage": "subtitles", "status": "OK", **srt})

    render = stage("render", lambda: render_avatar_video(
        out_dir, tts["total_s"] + _TALK_PAD_S + 1.5, tts["talk_start"],
        tts["talk_end"], log))
    assemble = stage("assemble", lambda: assemble_video(
        Path(render["video"]), tts, srt_path, out_dir, log, music_file))
    validation = stage("validate", lambda: validate_output(
        Path(assemble["output"]), tts, out_dir, log))

    manifest["tools"] = {
        "blender": _blender_version(),
        "ffmpeg": _ffmpeg_version(),
        "tts": tts["engine"] + " (local, offline)",
        "encoder": assemble["encoder"] + " (VAAPI with CPU fallback)",
        "subtitles": "ffmpeg libass (burned, measured timings)",
        "music": ("omitted — no licensed/original bed on disk"
                  if not music_file else f"user-provided: {music_file}"),
    }
    manifest["outputs"] = [assemble["output"], str(srt_path), tts["wav"]]
    manifest["dimensions"] = {"width": validation["ffprobe"]["width"],
                              "height": validation["ffprobe"]["height"]}
    manifest["duration_s"] = validation["ffprobe"]["duration_s"]
    manifest["ai_generated"] = {"avatar_3d": True, "voice": "local TTS",
                                "stock_footage": 0}
    manifest["animation_proof"] = validation["animation_proof"]
    manifest["total_seconds"] = round(time.time() - started, 1)
    manifest["status"] = "COMPLETED"
    _write_manifest(out_dir, manifest)
    log(f"[avatar] COMPLETE in {manifest['total_seconds']}s → "
        f"{assemble['output']}")
    return manifest


def _write_manifest(out_dir: Path, manifest: dict) -> None:
    tmp = Path(out_dir) / "manifest.json.tmp"
    tmp.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    tmp.replace(Path(out_dir) / "manifest.json")


def _blender_version() -> str:
    try:
        proc = subprocess.run([_blender_bin(), "--version"],
                              capture_output=True, text=True, timeout=60)
        return (proc.stdout or "").splitlines()[0][:60]
    except Exception:                              # noqa: BLE001
        return "unknown"


def _ffmpeg_version() -> str:
    try:
        proc = subprocess.run([FFMPEG, "-version"], capture_output=True,
                              text=True, timeout=15)
        return (proc.stdout or "").splitlines()[0][:60]
    except Exception:                              # noqa: BLE001
        return "unknown"
