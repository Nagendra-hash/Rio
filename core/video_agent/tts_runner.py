"""
core/video_agent/tts_runner.py — VOICE_GENERATOR role for the video agent.

Segmented narration synthesis with per-segment retry and REAL measured
durations. Two honesty rules from the mission:

  * word/phrase-level caption timing may only come from real alignment —
    this module records the REAL duration of every synthesized segment
    (measured from the written audio file, never estimated) and marks the
    output `timing_granularity: "per-segment"`; Phase 7 must not invent
    word-level timestamps from these numbers
  * a failed segment is retried, then reported honestly — narration is never
    silently skipped, repeated or truncated

Engines (the mission's "Piper TTS, existing local TTS if appropriate"):
  the default ladder is whatever is INSTALLED on this machine — gTTS today
  (free cloud, proven in actions/youtube_agent.py), Kokoro once its deps +
  ~330 MB model are approved and installed (engine + wav writer already in
  core/tts.py), Piper as a future CLI adapter. Any engine can be injected
  for tests. Availability truth lives in core/video_agent/providers.py.
"""
from __future__ import annotations

import json
import re
import subprocess
import wave as _wave
from pathlib import Path
from typing import Any


class TTSError(RuntimeError):
    """Narration could not be produced; message is user-safe."""


LANGUAGE_CODES = {
    "hindi": "hi", "hi": "hi", "हिंदी": "hi", "हिन्दी": "hi",
    "english": "en", "en": "en",
    "spanish": "es", "es": "es", "french": "fr", "fr": "fr",
    "german": "de", "de": "de", "arabic": "ar", "ar": "ar",
    "bengali": "bn", "bn": "bn", "tamil": "ta", "ta": "ta",
    "telugu": "te", "te": "te", "marathi": "mr", "mr": "mr",
    "urdu": "ur", "ur": "ur", "portuguese": "pt", "pt": "pt",
}


def language_code(language: str | None) -> str:
    """'Hindi' / 'hi' / 'हिंदी' / 'simple English' → the gTTS code. Unknown →
    first two ASCII letters, defaulting to English only when nothing given."""
    raw = str(language or "").strip().lower()
    if not raw:
        return "en"
    if raw in LANGUAGE_CODES:
        return LANGUAGE_CODES[raw]
    # "simple English", "calm Hindi narration" → match a known name as a word
    for word in re.findall(r"[a-z]+", raw):
        if word in LANGUAGE_CODES:
            return LANGUAGE_CODES[word]
    for key, code in LANGUAGE_CODES.items():   # non-ASCII names (हिंदी …)
        if not key.isascii() and key in raw:
            return code
    first = raw.replace("-", " ").split()[0]
    return first[:2] if first.isascii() and len(first) >= 2 else "en"


class GttsEngine:
    """gTTS — free cloud voice, no API key, installed today."""

    name = "gtts"
    free_or_paid = "free (cloud, no key)"

    def __init__(self, lang: str = "en") -> None:
        self.lang = lang

    def synth_to_file(self, path: Path, text: str) -> dict[str, Any]:
        try:
            from gtts import gTTS
            path.parent.mkdir(parents=True, exist_ok=True)
            tts = gTTS(text=text, lang=self.lang)
            tts.save(str(path))
        except Exception as exc:                   # noqa: BLE001
            raise TTSError(f"gtts failed: {type(exc).__name__}: "
                           f"{str(exc)[:160]}") from None
        return {"path": str(path)}


class EdgeEngine:
    """Edge TTS — free cloud neural voices (engine exists in core/tts.py;
    the edge-tts package is not installed yet, so this engine only becomes
    reachable once that is approved/installed)."""

    name = "edge-tts"
    free_or_paid = "free (cloud)"

    def __init__(self, voice: str = "", lang: str = "en") -> None:
        self.voice = voice or ("hi-IN-MadhurNeural" if lang == "hi"
                               else "en-US-GuyNeural")

    def synth_to_file(self, path: Path, text: str) -> dict[str, Any]:
        try:
            from core.tts import EdgeTTSEngine
            data = EdgeTTSEngine(voice=self.voice).synth_bytes(text)
            if not data:
                raise RuntimeError("empty audio")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except TTSError:
            raise
        except Exception as exc:                   # noqa: BLE001
            raise TTSError(f"edge-tts failed: {type(exc).__name__}: "
                           f"{str(exc)[:160]}") from None
        return {"path": str(path)}


class KokoroEngine:
    """Kokoro — the house LOCAL neural TTS (offline after a one-time ~330 MB
    model download that requires user approval; see providers.probe_voice)."""

    name = "kokoro"
    free_or_paid = "free (open source, offline)"

    def __init__(self, voice: str = "af_heart") -> None:
        self.voice = voice

    def synth_to_file(self, path: Path, text: str) -> dict[str, Any]:
        try:
            from core.tts import KokoroTTSEngine
            info = KokoroTTSEngine(voice=self.voice).synth_to_wav(path, text)
        except Exception as exc:                   # noqa: BLE001
            raise TTSError(f"kokoro failed: {type(exc).__name__}: "
                           f"{str(exc)[:160]}") from None
        return {"path": str(info["path"]),
                "duration_s": info.get("duration_s")}


def default_engines(language: str | None = "en") -> list[Any]:
    """Ladder in preference order — LOCAL first once it exists, cloud free
    as the working fallback today. Availability truth: providers.probe_voice.

    Kokoro is English-only today, so a non-English request skips it (using it
    would produce mispronounced audio, not narration)."""
    import importlib.util
    code = language_code(language)
    engines: list[Any] = []
    if code == "en" and importlib.util.find_spec("kokoro") is not None:
        engines.append(KokoroEngine())             # local, offline
    if importlib.util.find_spec("edge_tts") is not None:
        engines.append(EdgeEngine(lang=code))      # free cloud neural
    if importlib.util.find_spec("gtts") is not None:
        engines.append(GttsEngine(lang=code))      # free cloud, proven
    return engines


def _wav_duration(path: Path) -> float:
    """REAL duration measured from the file on disk — never estimated."""
    try:
        with _wave.open(str(path), "rb") as wf:
            return round(wf.getnframes() / float(wf.getframerate() or 1), 3)
    except Exception:                              # noqa: BLE001
        pass
    # non-wav (mp3) — ffprobe, the house validator
    ffprobe = "/usr/bin/ffprobe"
    try:
        out = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json",
             "-show_format", str(path)],
            capture_output=True, text=True, timeout=30)
        data = json.loads(out.stdout or "{}")
        return round(float((data.get("format") or {}).get("duration") or 0), 3)
    except Exception:                              # noqa: BLE001
        return 0.0


def segment_script(script: dict, max_chars: int = 600) -> list[dict]:
    """Split a generated script into narration segments (hook, per-chapter
    narration, ending). Segments are the unit of synthesis, retry and —
    later — of scene timing."""
    segments: list[dict] = []
    order = 0
    for kind in ("hook", "chapters", "ending"):
        if kind == "chapters":
            for i, ch in enumerate(script.get("chapters") or [], start=1):
                text = str(ch.get("narration") or "").strip()
                if text:
                    order += 1
                    segments.append({
                        "id": order, "kind": "chapter", "index": i,
                        "title": str(ch.get("title") or f"Chapter {i}"),
                        "text": text,
                        "visual_notes": str(ch.get("visual_notes") or ""),
                        "estimated_seconds": ch.get("estimated_seconds"),
                    })
        else:
            text = str(script.get(kind) or "").strip()
            if text:
                order += 1
                segments.append({"id": order, "kind": kind,
                                 "title": kind.capitalize(),
                                 "text": text, "visual_notes": "",
                                 "estimated_seconds": None})
    # hard cap: one segment per narration block; a chapter longer than
    # max_chars is NOT split mid-thought — it is flagged for the editor stage
    for seg in segments:
        seg["too_long"] = len(seg["text"]) > max_chars
    return segments


def synthesize_narration(script: dict, out_dir: Path, *,
                         engines: list | None = None,
                         retries: int = 2,
                         language: str | None = "en",
                         log=None) -> dict[str, Any]:
    """Synthesize every segment. Per-segment engine fallback + retry; real
    durations measured from disk. Raises TTSError only when a segment has
    NO possible narration after all engines × retries (the stage then fails
    honestly — partial audio is never presented as complete narration)."""
    log = log or (lambda _m: None)
    out_dir = Path(out_dir)
    narr_dir = out_dir / "narration"
    narr_dir.mkdir(parents=True, exist_ok=True)
    engines = engines if engines is not None else default_engines(language)
    if not engines:
        raise TTSError(
            "no TTS engine is installed — install kokoro (offline, ~330 MB "
            "model, needs approval) or gtts (free cloud) first")

    segments = segment_script(script)
    results: list[dict[str, Any]] = []
    used_engine: str | None = None

    for seg in segments:
        wav = narr_dir / f"segment_{seg['id']:02d}.mp3"
        last_err = ""
        done = False
        for attempt in range(1, max(1, retries) + 1):
            if done:
                break
            for engine in engines:
                try:
                    log(f"[tts] segment {seg['id']} ({seg['kind']}) via "
                        f"{engine.name} attempt {attempt}")
                    engine.synth_to_file(wav, seg["text"])
                    duration = _wav_duration(wav)
                    if duration <= 0.05:
                        raise TTSError("produced audio is empty")
                    results.append({
                        "id": seg["id"], "kind": seg["kind"],
                        "index": seg.get("index"),
                        "title": seg["title"], "text": seg["text"],
                        "path": str(wav), "duration_s": duration,
                        "engine": engine.name,
                        "attempts": attempt,
                        "estimated_seconds": seg.get("estimated_seconds"),
                        "timing_granularity": "per-segment",
                    })
                    used_engine = used_engine or engine.name
                    done = True
                    break
                except TTSError as exc:
                    last_err = str(exc)
                    log(f"[tts] segment {seg['id']} failed via "
                        f"{engine.name}: {last_err[:120]}")
        if not done:
            raise TTSError(
                f"narration segment {seg['id']} ({seg['kind']}: "
                f"{seg['title']}) failed on every engine after {retries} "
                f"attempt(s): {last_err[:200]} — narration is incomplete, "
                "nothing was faked")

    total = round(sum(r["duration_s"] for r in results), 3)
    timings = {
        "engine": used_engine,
        "language": str(language or "en"),
        "language_code": language_code(language),
        "timing_granularity": "per-segment",
        "word_level_available": False,      # only real alignment may claim it
        "total_speech_s": total,
        "segments": results,
    }
    (narr_dir / "timings.json").write_text(json.dumps(timings, indent=1),
                                           encoding="utf-8")
    return timings
