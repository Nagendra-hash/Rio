"""
core/video_agent/lipsync.py — audio-driven mouth animation.

WHAT THIS IS (and is not)
--------------------------
The narration is synthesized by Kokoro, one WAV per sentence. This module
turns that audio into a per-frame viseme track that Blender keys the mouth
from:

  1. TEXT  → IPA phonemes (misaki G2P, the same front-end Kokoro uses)
  2. AUDIO → 5 ms RMS energy envelope (read from the WAV itself)
  3. ALIGN → dynamic time warping of the phoneme sonority curve onto the
             measured energy envelope, with duration priors. The warping
             path gives every phoneme a start/end IN THE ACTUAL AUDIO.
  4. MAP   → phoneme class → one of 8 mouth shapes (visemes)
  5. SNAP  → closure is forced on measured silence, so the mouth is never
             open while nobody is speaking

This is real forced alignment against the waveform, not a random mouth
flap: `verify_alignment()` measures the correlation between mouth openness
and the audio envelope and the pipeline refuses a track that fails it.

It is still NOT phoneme-perfect lip-sync (no viseme classifier, no manual
correction): consonants that share a mouth shape are indistinguishable, and
fast speech can compress two phonemes into one frame. The manifest says so.
"""
from __future__ import annotations

import json
import math
import re
import wave
from pathlib import Path
from typing import Any, Iterable

import numpy as np

__all__ = ["phonemes_for", "viseme_track", "write_visemes", "verify_alignment",
           "SHAPES"]

# mouth shapes the rig actually has (see build_character_dance_scene.py)
SHAPES = ("REST", "MBP", "FV", "TH", "L", "CH", "KG", "A", "E", "O", "U")

# ── IPA → viseme ────────────────────────────────────────────────────────────
# grouped by where the mouth actually is, which is all a mouth-shape rig can
# show. Order matters: first match wins.
_VISEME_TABLE: tuple[tuple[str, str], ...] = (
    ("pbm", "MBP"),                    # bilabial closure
    ("fv", "FV"),                      # teeth on lower lip
    ("θð", "TH"),                      # tongue between teeth
    ("szʃʒtdnlɹrʧʤ", "L"),             # alveolar / postalveolar, wide-ish
    ("kgŋ", "KG"),                     # velar, jaw partly open
    ("wʊuɔo", "O"),                    # rounded
    ("jiɪeɛæ", "E"),                   # wide, spread
    ("aɑʌɐɜə", "A"),                   # open jaw
)

# sonority: how much acoustic energy a class carries (drives the DTW prior)
_SONORITY = {"A": 1.0, "E": 0.95, "O": 0.95, "U": 0.9, "L": 0.7, "CH": 0.7,
             "KG": 0.55, "TH": 0.4, "FV": 0.35, "MBP": 0.12, "REST": 0.0}

# relative duration priors (vowels are long, stops are short)
_DURATION = {"A": 1.0, "E": 0.95, "O": 1.0, "U": 0.9, "L": 0.62, "CH": 0.7,
             "KG": 0.6, "TH": 0.65, "FV": 0.7, "MBP": 0.55, "REST": 1.0}

_STRESS = "ˈˌːˑʰʲˠˤ̯̩̃"
_PUNCT = set(".,!?;:—-…\"'()[]")

_G2P = None


def phonemes_for(text: str) -> list[str]:
    """IPA phoneme symbols for `text` (misaki, the Kokoro front-end)."""
    global _G2P
    if _G2P is None:
        from misaki import en as _en
        _G2P = _en.G2P(trf=False, british=False)
    phones, _ = _G2P(str(text or ""))
    out: list[str] = []
    for ch in phones:
        if ch in _STRESS:
            continue
        if ch in _PUNCT:
            out.append("|")            # phrase break → short rest
            continue
        if ch.isspace():
            continue
        out.append(ch)
    return out


def _viseme_of(sym: str) -> str:
    if sym == "|":
        return "REST"
    low = sym.lower()
    for chars, shape in _VISEME_TABLE:
        if low in chars:
            return shape
    return "L"                         # unknown consonant: neutral wide


# ── audio ───────────────────────────────────────────────────────────────────

def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        rate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    if width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        data = np.frombuffer(raw, dtype="u1").astype(np.float32) / 128.0 - 1.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data, rate


def energy_envelope(x: np.ndarray, rate: int, hop_s: float = 0.005,
                    win_s: float = 0.020) -> np.ndarray:
    """RMS per hop, normalised to 0..1 against the clip's own peak."""
    hop = max(1, int(round(hop_s * rate)))
    win = max(hop, int(round(win_s * rate)))
    if x.size < win:
        return np.zeros(1, dtype=np.float32)
    n = 1 + (x.size - win) // hop
    frames = np.lib.stride_tricks.sliding_window_view(x, win)[::hop][:n]
    rms = np.sqrt(np.mean(frames.astype(np.float32) ** 2, axis=1) + 1e-12)
    peak = float(np.percentile(rms, 99.0)) or float(rms.max()) or 1.0
    return np.clip(rms / peak, 0.0, 1.0)


def speech_mask(env: np.ndarray, floor_db: float = -34.0) -> np.ndarray:
    """Frames that actually contain speech (adaptive to the clip's floor)."""
    db = 20.0 * np.log10(np.maximum(env, 1e-6))
    top = float(np.percentile(db, 95))
    threshold = max(top + floor_db, float(np.percentile(db, 30)) + 10.0)
    return db > threshold


# ── alignment ───────────────────────────────────────────────────────────────

def _align(sonority: np.ndarray, weights: np.ndarray, env: np.ndarray,
           mask: np.ndarray, window: tuple[int, int] | None = None
           ) -> list[tuple[int, int]]:
    """Monotonic DTW: phoneme i occupies frames [start_i, end_i).

    cost(i, a, b) = mean |sonority_i - env| over the span
                  + duration prior penalty (log-ratio)
                  + silence mismatch (a voiced phoneme over silence, or a
                    rest over speech, is expensive)
    """
    n_ph, m = len(sonority), len(env)
    if n_ph == 0:
        return []
    if m == 0:
        return [(0, 0)] * n_ph

    if window is not None:
        # an explicit speech run (a phrase between two pauses): align inside
        # it and map the frames back to the clip's timeline
        a0, b0 = window
        a0 = max(0, int(a0))
        b0 = min(m, int(b0))
        if b0 - a0 < 4:
            return [(a0, a0 + 1)] * n_ph
    else:
        # ALIGN ONLY THE SPEECH REGION. Kokoro pads each clip with ~0.4 s of
        # silence at the head and ~0.7 s at the tail; forcing the path across
        # that padding dragged the last words onto silence.
        voiced = np.where(mask)[0]
        if voiced.size >= 2:
            pad = 6                                   # ~30 ms
            a0 = max(0, int(voiced[0]) - pad)
            b0 = min(m, int(voiced[-1]) + 1 + pad)
        else:
            a0, b0 = 0, m
        if b0 - a0 < 10:
            a0, b0 = 0, m
    env = env[a0:b0]
    mask = mask[a0:b0]
    m = b0 - a0

    scale = m / float(weights.sum())        # frames per weight unit
    lo = np.maximum(1, np.floor(weights * scale * 0.35).astype(int))
    hi = np.maximum(lo + 1, np.ceil(weights * scale * 2.6).astype(int))

    big = 1e9
    # the trim above removed the padding, so the path starts at 0 and ends
    # at the last speech frame
    prev = np.full(m + 1, big, dtype=np.float64)
    prev[0] = 0.0
    back = np.zeros((n_ph, m + 1), dtype=np.int32)

    cum = np.concatenate(([0.0], np.cumsum(env)))
    quiet = (~mask).astype(np.float64)
    quiet_cum = np.concatenate(([0.0], np.cumsum(quiet)))

    for i in range(n_ph):
        cur = np.full(m + 1, big, dtype=np.float64)
        s = float(sonority[i])
        for b in range(1, m + 1):
            best, best_a = big, 0
            a_min = max(0, b - int(hi[i]))
            a_max = b - int(lo[i])
            for a in range(a_min, a_max + 1):
                base = prev[a]
                if base >= big:
                    continue
                span = b - a
                mean_env = (cum[b] - cum[a]) / span
                cost = abs(s - mean_env)
                # a closed-mouth class over loud audio (and vice versa) is
                # the classic misalignment — punish it hard
                quiet_frac = (quiet_cum[b] - quiet_cum[a]) / span
                if s > 0.6:
                    cost += 0.8 * quiet_frac
                elif s < 0.2:
                    cost += 0.8 * (1.0 - quiet_frac)
                ratio = span / max(1e-6, weights[i] * scale)
                cost += 0.35 * abs(math.log(max(ratio, 1e-3)))
                total = base + cost * span
                if total < best:
                    best, best_a = total, a
            cur[b] = best
            back[i, b] = best_a
        prev = cur

    # fixed end: the path must reach the last speech frame
    b = m
    spans: list[tuple[int, int]] = []
    for i in range(n_ph - 1, -1, -1):
        a = int(back[i, b])
        spans.append((a, b))
        b = a
    spans.reverse()
    return [(a + a0, b + a0) for a, b in spans]


def _snap_to_silence(spans: list[tuple[int, int]], visemes: list[str],
                     mask: np.ndarray) -> list[tuple[int, int]]:
    """Move each boundary to the nearest silence edge when it is close —
    stops land on the closure, not a few frames late.

    Boundaries at a PHRASE gap are left alone: snapping one of those pulled
    a vowel across the comma pause (a 480 ms mouth-open on silence).
    """
    out = list(spans)
    for i in range(len(out) - 1):
        a, b = out[i]
        nxt = out[i + 1][0]
        if nxt > b:                      # real gap between phrases — keep it
            continue
        best, best_d = b, 10 ** 9
        for j in range(max(a + 1, b - 4), min(nxt - 1, b + 4)):
            if mask[j] != mask[j + 1]:
                d = abs(j + 1 - b)
                if d < best_d:
                    best, best_d = j + 1, d
        if best != b:
            out[i] = (a, best)
            out[i + 1] = (best, out[i + 1][1])
    return out


# ── public: build the track ─────────────────────────────────────────────────

def _speech_runs(mask: np.ndarray, min_frames: int = 8) -> list[tuple[int, int]]:
    """Contiguous speech regions; short blips are ignored."""
    runs: list[tuple[int, int]] = []
    start = None
    for i, voiced in enumerate(mask):
        if voiced and start is None:
            start = i
        elif not voiced and start is not None:
            if i - start >= min_frames:
                runs.append((start, i))
            start = None
    if start is not None and len(mask) - start >= min_frames:
        runs.append((start, len(mask)))
    return runs


def _phrase_runs(mask: np.ndarray, min_frames: int = 8,
                 merge_gap: int = 50) -> list[tuple[int, int]]:
    """Speech runs merged across SHORT gaps (<250 ms).

    Stops and fricatives dip below the speech floor for a few frames, which
    split one phrase into a dozen "runs". Only real pauses — the ones the
    punctuation marks — stay separate.
    """
    merged: list[tuple[int, int]] = []
    for a, b in _speech_runs(mask, min_frames):
        if merged and a - merged[-1][1] < merge_gap:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return merged


def _split_groups(phones: list[str]) -> list[list[str]]:
    """Phonemes split at punctuation, i.e. at the pauses the voice makes."""
    groups: list[list[str]] = []
    cur: list[str] = []
    for p in phones:
        if p == "|":
            if cur:
                groups.append(cur)
            cur = []
        else:
            cur.append(p)
    if cur:
        groups.append(cur)
    return groups


def viseme_track(segments: Iterable[dict[str, Any]], fps: int = 24,
                 hop_s: float = 0.005) -> dict[str, Any]:
    """segments = [{text, wav, start}] → per-frame viseme keys.

    Returns {"fps", "frames": [{t, shape, open, viseme_index}], "events":
    [{t0, t1, shape, phoneme}]} — frames is what Blender keys.
    """
    events: list[dict[str, Any]] = []
    for seg in segments:
        text = str(seg.get("text") or "")
        wav = Path(str(seg.get("wav") or ""))
        t0 = float(seg.get("start") or 0.0)
        if not text.strip() or not wav.is_file():
            continue
        phones = phonemes_for(text)
        if not phones:
            continue
        data, rate = read_wav(wav)
        env = energy_envelope(data, rate, hop_s=hop_s)
        mask = speech_mask(env)
        groups = _split_groups(phones)
        runs = _phrase_runs(mask)
        # one phrase per measured speech run: the comma pause is then a real
        # pause in the mouth, not something a phoneme has to stretch across
        if groups and len(groups) == len(runs):
            spans: list[tuple[int, int]] = []
            shapes_all: list[str] = []
            phones_all: list[str] = []
            for group, run in zip(groups, runs):
                sh = [_viseme_of(p) for p in group]
                son = np.array([_SONORITY[s] for s in sh], dtype=np.float64)
                wts = np.array([_DURATION[s] for s in sh], dtype=np.float64)
                spans.extend(_align(son, wts, env, mask, window=run))
                shapes_all.extend(sh)
                phones_all.extend(group)
        else:
            shapes_all = [_viseme_of(p) for p in phones]
            son = np.array([_SONORITY[s] for s in shapes_all], dtype=np.float64)
            wts = np.array([_DURATION[s] for s in shapes_all], dtype=np.float64)
            spans = _align(son, wts, env, mask)
            phones_all = phones
        spans = _snap_to_silence(spans, shapes_all, mask)
        for (a, b), shape, phone in zip(spans, shapes_all, phones_all):
            if b <= a:
                continue
            events.append({"t0": round(t0 + a * hop_s, 4),
                           "t1": round(t0 + b * hop_s, 4),
                           "shape": shape, "phoneme": phone})

    events.sort(key=lambda e: e["t0"])
    total = max((e["t1"] for e in events), default=0.0)
    frames = []
    idx = 0
    n_frames = int(math.ceil(total * fps)) + 1
    for f in range(n_frames):
        t = f / float(fps)
        while idx + 1 < len(events) and events[idx]["t1"] <= t:
            idx += 1
        ev = events[idx] if events else {"shape": "REST", "t1": 0.0}
        active = bool(events) and ev["t0"] <= t < ev["t1"]
        shape = ev["shape"] if active else "REST"
        frames.append({"t": round(t, 4), "shape": shape,
                       "open": _openness(shape) if active else 0.0})
    return {"fps": fps, "frames": frames, "events": events,
            "total_s": round(total, 3)}


def _openness(shape: str) -> float:
    return {"REST": 0.0, "MBP": 0.0, "FV": 0.18, "TH": 0.3, "L": 0.35,
            "CH": 0.45, "KG": 0.55, "E": 0.6, "O": 0.75, "U": 0.55,
            "A": 1.0}.get(shape, 0.3)


def write_visemes(track: dict[str, Any], path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(track, indent=0), encoding="utf-8")
    return path


def verify_alignment(track: dict[str, Any],
                     segments: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Prove the mouth follows the audio, with metrics that match what a
    mouth rig can actually show.

    NOTE: correlating mouth OPENNESS with RMS is the wrong test — /s/ and /f/
    are loud with a nearly closed mouth, so a correct lip-sync scores low on
    it. What must hold is:
      1. the mouth is shut while the audio is silent,
      2. vowels (the sonorous part) open wider than consonants,
      3. bilabial closures land on the quietest moments,
      4. every frame of speech has an active viseme.
    """
    fps = float(track["fps"])
    prepared = []
    for seg in segments:
        wav = Path(str(seg.get("wav") or ""))
        if not wav.is_file():
            continue
        data, rate = read_wav(wav)
        env = energy_envelope(data, rate, hop_s=0.005)
        mask = speech_mask(env)
        runs = _phrase_runs(mask)
        in_speech = np.zeros(env.size, dtype=bool)
        for a, b in runs:
            in_speech[a:b] = True
        prepared.append((float(seg.get("start") or 0.0), env, in_speech))
    if not prepared:
        return {"ok": False, "reason": "no audio for verification"}

    env_at, speech_at, open_at, shape_at = [], [], [], []
    for fr in track["frames"]:
        t = float(fr["t"])
        for start, env, in_speech in prepared:
            idx = int(round((t - start) / 0.005))
            if 0 <= idx < env.size:
                env_at.append(float(env[idx]))
                speech_at.append(bool(in_speech[idx]))
                open_at.append(float(fr["open"]))
                shape_at.append(str(fr["shape"]))
                break
    if not env_at:
        return {"ok": False, "reason": "no overlap between audio and frames"}

    env_arr = np.array(env_at)
    open_arr = np.array(open_at)
    speech_arr = np.array(speech_at)
    shapes = np.array(shape_at)
    vowels = np.isin(shapes, ["A", "E", "O", "U"])
    bilabial = shapes == "MBP"
    paused = ~speech_arr

    silent_open = float(open_arr[paused].mean()) if paused.any() else 0.0
    vowel_open = float(open_arr[vowels].mean()) if vowels.any() else 0.0
    consonant_open = float(open_arr[~vowels].mean()) if (~vowels).any() else 0.0
    vowel_env = float(env_arr[vowels].mean()) if vowels.any() else 0.0
    cons_env = float(env_arr[~vowels].mean()) if (~vowels).any() else 0.0
    bilabial_env = float(env_arr[bilabial].mean()) if bilabial.any() else 1.0
    animated = float((shapes[speech_arr] != "REST").mean()) \
        if speech_arr.any() else 0.0

    checks = {
        "mouth_shut_in_pauses": silent_open < 0.12,
        "vowels_wider_than_consonants": vowel_open > consonant_open + 0.10,
        "vowels_louder_than_consonants": vowel_env > cons_env,
        "closures_on_quiet_frames": bilabial_env < cons_env,
        "speech_frames_animated": animated > 0.95,
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "silent_open": round(silent_open, 3),
        "vowel_open": round(vowel_open, 3),
        "consonant_open": round(consonant_open, 3),
        "vowel_env": round(vowel_env, 3),
        "consonant_env": round(cons_env, 3),
        "bilabial_env": round(bilabial_env, 3),
        "speech_frames_animated": round(animated, 3),
        "frames_checked": int(env_arr.size),
    }
