"""
core/video_agent/cinematic.py — provider-independent cinematic interface.

The mission's three cinematic operations, mapped to the executors that
genuinely exist on this machine (measured, not assumed):

    TEXT_TO_VIDEO   → ViMax idea2video     (idea → story → storyboard → shots)
    SCENE_TO_VIDEO  → ViMax script2video   (user scenes/script → shots)
    IMAGE_TO_VIDEO  → ViMax i2v            (user image + prompt, added Phase 6)

Honest status contract:
  * the INTERFACES are implemented and tested; the GENERATION providers are
    billing-blocked on this machine (OpenRouter 402 never-purchased,
    Google free tier 429 limit: 0 — measured 2026-10-24/25). Jobs queue and
    stop honestly at the provider gate with the error in the job row.
  * local Wan / LTX open-weight models are BLOCKED on this hardware
    (GeForce 820M has no working driver); a remote GPU service would need
    explicit user approval + credentials (NOT IMPLEMENTED).
  * if no free AI video backend exists, the documentary pipeline (Phase 4)
    is the offered zero-cost alternative — stills/narration/ffmpeg, always
    labelled, never passed off as AI video.
  * scene continuity: ViMax maintains character registries + reference
    images across shots natively (character_portraits_registry,
    characters.json, storyboard.json — verified live 2026-10-25). Perfect
    visual consistency is NOT claimed.
"""
from __future__ import annotations

from typing import Any

TEXT_TO_VIDEO = "TEXT_TO_VIDEO"
IMAGE_TO_VIDEO = "IMAGE_TO_VIDEO"
SCENE_TO_VIDEO = "SCENE_TO_VIDEO"
CINEMATIC_OPS = (TEXT_TO_VIDEO, IMAGE_TO_VIDEO, SCENE_TO_VIDEO)

# op → (vimax operation, what it needs from the request)
OP_ROUTES: dict[str, dict[str, str]] = {
    TEXT_TO_VIDEO: {"vimax_op": "idea2video",
                    "needs": "topic (an idea or premise)"},
    IMAGE_TO_VIDEO: {"vimax_op": "i2v",
                     "needs": "image (path to the user's picture) + prompt"},
    SCENE_TO_VIDEO: {"vimax_op": "script2video",
                     "needs": "script or scene descriptions"},
}


def capability_matrix() -> list[dict[str, Any]]:
    """Per-operation capability view: interface, executor, provider status.
    Composes the measured provider truth (providers.probe_video_generator)."""
    from core.video_agent import providers
    gen = providers.probe_video_generator()
    blocked = [p for p in gen["providers"] if str(p["health"]).startswith("BLOCKED")]
    gpu_blocked = any("hardware" in str(p["health"]).lower()
                      for p in gen["providers"])
    rows: list[dict[str, Any]] = []
    for op in CINEMATIC_OPS:
        route = OP_ROUTES[op]
        rows.append({
            "op": op,
            "interface": "implemented",
            "executor": f"ViMax {route['vimax_op']}",
            "needs": route["needs"],
            "provider_status": ("BLOCKED (billing) — jobs queue and stop "
                                "honestly at the provider gate" if blocked
                                else "available"),
            "free_alternative": ("documentary pipeline (stills + narration + "
                                 "ffmpeg, clearly labelled)"),
        })
    if gpu_blocked:
        rows.append({
            "op": "LOCAL_WAN_LTX",
            "interface": "NOT IMPLEMENTED",
            "executor": "none",
            "needs": "CUDA GPU (8-24 GB VRAM) or an approved remote GPU "
                     "service with credentials",
            "provider_status": "BLOCKED (hardware) on this laptop",
            "free_alternative": "documentary pipeline",
        })
    return rows


def matrix_text() -> str:
    """Voice/RIO rendering of the cinematic capability matrix."""
    lines = ["Cinematic capabilities (honest status):"]
    for row in capability_matrix():
        lines.append(f"  • {row['op']} — {row['interface']} via "
                     f"{row['executor']}; needs {row['needs']}. "
                     f"Provider: {row['provider_status']}.")
    lines.append("Scene continuity: ViMax keeps character reference images "
                 "and a storyboard across shots (not pixel-perfect — never "
                 "claimed to be).")
    lines.append("Zero-cost alternative: a documentary-style render "
                 "(stills + narration + ffmpeg), always labelled as such.")
    return "\n".join(lines)


def scenes_to_script(scenes: str | list[str]) -> str:
    """SCENE_TO_VIDEO input: user scene descriptions (a text block or a list)
    → a script-style text ViMax's script2video consumes. No LLM involved —
    the user's scene text IS the content."""
    if isinstance(scenes, str):
        return scenes.strip()[:8000]
    parts = []
    for i, scene in enumerate(scenes, start=1):
        text = str(scene or "").strip()
        if text:
            parts.append(f"SCENE {i}: {text}")
    return "\n\n".join(parts)[:8000]
