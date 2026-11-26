"""
core/video_agent/engines/character_animation/engine.py — the
CHARACTER_ANIMATION engine (Phase 10 of the video-engine upgrade).

Production mode for original children's-style videos with REAL articulated
3D characters (the permanent cast, stylized low-poly), directed multi-shot
camera work, beat-locked original music and local TTS narration.

Honest capability: characters are joint-pivot rigs (per-limb keyframes) —
NOT armature/skinning and NOT viseme lip-sync. The stage machine refuses
static-image substitution (motion proof required).
"""
from __future__ import annotations

from typing import Any

from core.video_agent.engines.base import (READY_LOCAL, UNAVAILABLE,
                                           EngineDescriptor, EngineStatus,
                                           VideoEngine)


class CharacterAnimationEngine(VideoEngine):
    descriptor = EngineDescriptor(
        id="CHARACTER_ANIMATION",
        name="3D Character Animation",
        version="1.0",
        summary=("Original children's-style videos with two articulated "
                 "low-poly 3D characters (the permanent cast, stylized), "
                 "choreographed dance/story actions, a directed 5-shot "
                 "camera, beat-locked original music, local TTS narration "
                 "and burned subtitles."),
        execution=("local",),
        video_types=("3d_animation", "children_dance", "story"),
        resolutions=("720p",),
        duration_range_s=(15, 20),
        hardware_requirements={"needs_blender": True, "min_ram_mb": 2000,
                               "min_free_disk_gb": 2},
        hw_requirements_text=("CPU render through Blender Eevee (verified "
                              "on this 2C/4T laptop); ~1.5 GB free per "
                              "render"),
        estimated_resources={"ram_mb": 1500, "disk_mb": 400,
                             "cpu_minutes_per_18s": 10},
        estimated_cost="free (Blender + local TTS + original synthesized "
                       "music)",
        privacy="runs locally — no cloud, no uploads",
        install_supported=False,
        install_source="",
        install_size_mb=0.0,
    )

    def probe(self, hw: dict[str, Any] | None = None) -> EngineStatus:
        try:
            from core.video_agent.engines.blender_3d.engine import (
                _version_probe)
            verified, detail = _version_probe()
        except Exception as exc:                    # noqa: BLE001
            return EngineStatus(UNAVAILABLE,
                                f"blender probe failed: {exc}",
                                {"local": UNAVAILABLE})
        if verified:
            return EngineStatus(
                READY_LOCAL,
                "Blender verified — articulated two-character rigs, "
                "choreography, 5-shot camera, original beat-locked music",
                {"local": READY_LOCAL})
        return EngineStatus(UNAVAILABLE, detail, {"local": UNAVAILABLE})

    def stages(self) -> dict[str, Any]:
        """Engine-owned pipeline: resolved by
        pipelines.stages_for('character_animation')."""
        from core.video_agent import pipeline_character_animation as pca
        return {
            "planning": lambda ctx: {
                "mode": "3d_character_animation",
                "cast": "psychology_expert + little_boy (stylized "
                        "low-poly, joint-pivot rigs)",
                "honest_limits": ("no armature/skinning, no viseme "
                                  "lip-sync; motion proof required"),
            },
            "scripting": lambda ctx: {
                "scripted": "narration provided by the request (LLM "
                            "optional, not required)"
            },
            "asset_collection": lambda ctx: {
                "assets": "procedural characters + playground + "
                          "synthesized music (nothing downloaded)"
            },
            "generating": lambda ctx: pca.run_character_animation(
                str(ctx["spec"].get("narration") or ctx["spec"].get("topic")
                    or "RIO character dance"),
                # the REQUESTED save_dir wins (the job's run_dir parent is
                # only a fallback): writing next to it dropped every video
                # into ~/Videos/JARVIS instead of the folder the user named
                str(ctx["spec"].get("out_dir") or ctx["run_dir"].parent),
                log=ctx["log"]),
            "editing": lambda ctx: {
                "edited": "assembled in the generating stage (mux + "
                          "subtitles)"
            },
            "validating": lambda ctx: {
                "validated": "motion proof + ffprobe contract enforced "
                             "inside the pipeline"
            },
        }

    def start_params(self, spec: dict[str, Any],
                     parameters: dict[str, Any] | None = None
                     ) -> dict[str, Any] | None:
        if self.probe().status != READY_LOCAL:
            return None
        if str(spec.get("pipeline") or "").lower() != "character_animation":
            return None
        return {"kind": "video", "ref": "character_animation",
                "params": {"spec": dict(spec),
                           "out_dir": str(spec.get("out_dir") or "")}}


ENGINE = CharacterAnimationEngine()
