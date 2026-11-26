"""
core/video_agent/spec.py — typed video job specification + stage machine.

Phase 2 of the video upgrade: modes, defaults, request detection, validation
and the stage machine. Pure stdlib + regex; no LLM, no network — every
function here is deterministic and unit-testable.

The stage machine rides the existing RepoJobStore columns (`phase`, `progress`)
while keeping the store's coarse `status` contract untouched; `job_state()`
exposes the mission-named states (QUEUED → PLANNING → … → COMPLETED) as a
first-class view.
"""
from __future__ import annotations

import re
from typing import Any

# ── modes ────────────────────────────────────────────────────────────────────

DOCUMENTARY = "DOCUMENTARY"
AI_CINEMATIC = "AI_CINEMATIC"
SHORTS = "SHORTS"
BLENDER_3D = "BLENDER_3D"
CHARACTER_ANIMATION = "CHARACTER_ANIMATION"
MODES = (DOCUMENTARY, AI_CINEMATIC, SHORTS, BLENDER_3D, CHARACTER_ANIMATION)

# pipeline executor that will carry each mode ("smoke" walks the same stage
# machine with simulated content to prove the machinery; the real documentary
# and shorts pipelines arrive in Phases 4 and 7)
MODE_PIPELINE = {
    DOCUMENTARY: "documentary",
    AI_CINEMATIC: "vimax",        # existing ViMax idea2video path (unchanged)
    SHORTS: "shorts",
    BLENDER_3D: "blender",
    CHARACTER_ANIMATION: "character_animation",
}

# ── stage machine (mission order) ────────────────────────────────────────────

VIDEO_STAGES = ("planning", "scripting", "asset_collection",
                "generating", "editing", "validating")

# monotonic progress each stage reports when it STARTS; validating ends at 1.0
STAGE_PROGRESS = {
    "planning": 0.08,
    "scripting": 0.25,
    "asset_collection": 0.45,
    "generating": 0.65,
    "editing": 0.85,
    "validating": 0.95,
}

# store statuses that pass through unchanged in job_state()
PASS_THROUGH_STATES = ("QUEUED", "WAITING_FOR_APPROVAL", "NEEDS_CONFIGURATION",
                       "COMPLETED", "FAILED", "CANCELLED")


def job_state(job: dict | None) -> str:
    """Mission-named state of a job row: RUNNING maps to the current stage
    (SCRIPTING…), everything else passes through (QUEUED/COMPLETED/…)."""
    status = str((job or {}).get("status") or "QUEUED").upper()
    phase = str((job or {}).get("phase") or "").lower()
    if status == "RUNNING" and phase in VIDEO_STAGES:
        return phase.upper()
    return status if status in PASS_THROUGH_STATES else "RUNNING"


# ── per-mode defaults (mission contract) ─────────────────────────────────────

MODE_DEFAULTS: dict[str, dict[str, Any]] = {
    DOCUMENTARY: {
        "duration_s": 600, "min_duration_s": 60, "max_duration_s": 900,
        "aspect_ratio": "16:9", "resolution": "1080p",
        "style": "calm professional documentary",
        "language": "simple English", "music": "ambient, low",
        "subtitles": True, "target_platform": "local",
        # 1080p target is subject to hardware: the render stage may step down
        # to 720p on this 2C/4T CPU (decided in Phase 4, recorded in the job).
    },
    AI_CINEMATIC: {
        "duration_s": 10, "min_duration_s": 10, "max_duration_s": 600,
        "aspect_ratio": "16:9", "resolution": "1080p",
        "style": "cinematic", "language": "simple English",
        "music": "cinematic score, low", "subtitles": False,
        "target_platform": "local",
        # min 10 / max 600 mirror the ViMax skill's input schema exactly —
        # a cinematic spec outside this range can never pass run-skill
        # validation, so it is clamped here instead (no doomed jobs)
    },
    BLENDER_3D: {
        "duration_s": 15, "min_duration_s": 5, "max_duration_s": 30,
        "aspect_ratio": "9:16", "resolution": "720p",
        "style": "cute expressive robot", "language": "simple English",
        "music": "ambient sci-fi, low", "subtitles": False,
        "target_platform": "local",
    },
    SHORTS: {
        "duration_s": 45, "min_duration_s": 15, "max_duration_s": 90,
        "aspect_ratio": "9:16", "resolution": "1080p",
        "style": "energetic, clear", "language": "simple English",
        "music": "upbeat, low", "subtitles": True,
        "target_platform": "youtube",
    },
    CHARACTER_ANIMATION: {
        "duration_s": 18, "min_duration_s": 15, "max_duration_s": 20,
        "aspect_ratio": "9:16", "resolution": "720p",
        "style": "cheerful children's animation",
        "language": "simple English",
        "music": "original beat-locked tune", "subtitles": True,
        "target_platform": "youtube",
    },
}

ASPECT_RATIOS = ("16:9", "9:16", "1:1")
RESOLUTIONS = ("1080p", "720p")

_MODE_KEYWORDS = (
    (CHARACTER_ANIMATION, ("dance video", "3d character animation",
                           "character animation", "animated characters "
                           "dancing", "kids dance", "children dance")),
    (SHORTS, ("short", "reel", "tiktok", "vertical", "9:16")),
    (AI_CINEMATIC, ("cinematic", "ai video", "film", "trailer", "movie scene")),
    (BLENDER_3D, ("3d", "3d animation", "blender", "robot", "animated robot",
                   "little robot", "3d animated")),
    (DOCUMENTARY, ("documentary", "explained", "history of", "deep dive",
                   "essay", "analysis of")),
)

_DURATION_RE = re.compile(
    r"(\d+)\s*[- ]?\s*(hours?|hrs?|h\b|minutes?|mins?|m\b|seconds?|secs?|s\b)",
    re.IGNORECASE)
_UNIT_SECONDS = {"h": 3600, "hour": 3600, "hours": 3600, "hrs": 3600,
                 "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
                 "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1}


class SpecError(ValueError):
    """A request cannot become a spec. `question` is the ONE clarifying
    question to ask the user (mission contract: ask one, not an interrogation)."""

    def __init__(self, question: str) -> None:
        self.question = question
        super().__init__(question)


def parse_duration_s(text: str) -> int | None:
    """'10-minute' / '45 seconds' / '2 min' → seconds. None when absent."""
    m = _DURATION_RE.search(str(text or ""))
    if not m:
        return None
    value, unit = int(m.group(1)), m.group(2).lower().rstrip(".")
    for key, seconds in _UNIT_SECONDS.items():
        if unit == key or unit.startswith(key):
            return value * seconds
    return None


def detect_mode(text: str) -> tuple[str, str]:
    """(mode, reason) from the user's request. Keyword matches win (checked
    SHORTS → CINEMATIC → DOCUMENTARY so 'vertical short about…' wins over
    'documentary' when both appear); then duration heuristics; then the
    documentary default — 'create a video about X' means a narrated piece."""
    t = str(text or "").lower()
    for mode, keywords in _MODE_KEYWORDS:
        for kw in keywords:
            if kw in t:
                return mode, f"keyword '{kw}'"
    duration = parse_duration_s(t)
    if duration is not None:
        if duration <= 90:
            return SHORTS, f"duration {duration}s ≤ 90s"
        return DOCUMENTARY, f"duration {duration}s > 90s"
    return DOCUMENTARY, "no mode keywords — documentary default"


def build_spec(params: dict | None, request_text: str = "") -> dict[str, Any]:
    """Resolve a user request into a complete, validated, JSON-able job spec.

    Raises SpecError(question) when the one essential setting (topic) is
    missing. Everything else has a per-mode default; out-of-range durations
    are clamped to the mode range (recorded in `notes`, never silently)."""
    p = dict(params or {})
    text = str(request_text or "")
    topic = str(p.get("topic") or "").strip()
    if not topic:
        raise SpecError(
            "What should the video be about? Give me a topic or idea and I "
            "will start the pipeline.")

    mode = str(p.get("mode") or "").strip().upper()
    detection = "explicit"
    if mode not in MODES:
        mode, detection = detect_mode(f"{topic} {text}")

    d = MODE_DEFAULTS[mode]
    notes: list[str] = []

    duration = p.get("duration_s")
    if duration in (None, ""):
        duration = parse_duration_s(text) or d["duration_s"]
    try:
        duration = int(duration)
    except (TypeError, ValueError):
        raise SpecError(f"How long should the video be? I could not read "
                        f"duration '{duration}'.") from None
    if duration < d["min_duration_s"]:
        notes.append(f"duration {duration}s below {mode} minimum — clamped "
                     f"to {d['min_duration_s']}s")
        duration = d["min_duration_s"]
    elif duration > d["max_duration_s"]:
        notes.append(f"duration {duration}s above {mode} maximum — clamped "
                     f"to {d['max_duration_s']}s")
        duration = d["max_duration_s"]

    aspect = str(p.get("aspect_ratio") or d["aspect_ratio"]).strip()
    if aspect not in ASPECT_RATIOS:
        notes.append(f"aspect ratio '{aspect}' unsupported — using "
                     f"{d['aspect_ratio']}")
        aspect = d["aspect_ratio"]

    resolution = str(p.get("resolution") or d["resolution"]).strip().lower()
    if resolution not in RESOLUTIONS:
        notes.append(f"resolution '{resolution}' unsupported — using "
                     f"{d['resolution']}")
        resolution = d["resolution"]

    pipeline = str(p.get("pipeline") or "").strip().lower() or MODE_PIPELINE[mode]
    if str(p.get("smoke") or "").strip().lower() in ("1", "true", "yes", "on"):
        pipeline = "smoke"

    def _s(key: str, default: str = "") -> str:
        return str(p.get(key) or d.get(key) or default).strip()

    return {
        "mode": mode,
        "topic": topic[:2000],
        "duration_s": duration,
        "language": _s("language", "simple English"),
        "voice": _s("voice"),                    # resolved by TTS in Phase 3
        "style": _s("style"),
        "aspect_ratio": aspect,
        "target_platform": _s("target_platform", "local"),
        "resolution": resolution,
        "music": _s("music"),
        "subtitles": bool(p.get("subtitles", d["subtitles"])),
        # Phase 3 of the hardware-encoder upgrade: fade transitions between
        # scene clips and an optional per-job encoder override (auto|vaapi|
        # libx264). Defaults preserve existing behaviour.
        "transitions": str(p.get("transitions") or "").strip().lower()
                       in ("1", "true", "yes", "on", "fade"),
        "encoder": _s("encoder"),
        "out_dir": "",                            # filled by the action layer
        "pipeline": pipeline,
        "detection": detection,
        "notes": notes,
        "request_text": text[:1000],
        # pass-throughs used by later pipeline stages (documentary Phase 4)
        "user_assets_dir": _s("user_assets_dir"),
        "music_file": _s("music_file"),
        "full": p.get("full") in (True, 1, "1", "true", "yes", "on"),
        "branding": _s("branding"),
        # Phase 7: documentary → Shorts reuse
        "reuse_narration_from": _s("reuse_narration_from"),
        "segment_ids": p.get("segment_ids") or [],
        # Permanent cast: "default" resolves to the user's character sheet
        # profiles (Psychology Expert + Little Boy) unless explicitly
        # overridden (e.g. characters="none")
        "characters": _s("characters") or "default",
        # 3D character animation: the narration text drives the video
        "narration": _s("narration"),
        "scene_plan": p.get("scene_plan") or [],
        "scene_prompts": p.get("scene_prompts") or [],
        "scene_duration_s": p.get("scene_duration_s") or "",
    }
