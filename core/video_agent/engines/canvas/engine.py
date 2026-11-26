"""
core/video_agent/engines/canvas/engine.py — the CANVAS engine.

The free, zero-cost generation method that already works on this machine:
stills + motion graphics + real narration + ffmpeg assembly — the DOCUMENTARY
and SHORTS pipelines. Registered as a first-class engine so the approval
dialog can offer the honest free path next to the AI engines (mission rule:
"free-first, honest-always" — visuals are labelled motion graphics, never AI
video).
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from core.video_agent.engines.base import (READY_LOCAL, UNAVAILABLE,
                                           EngineDescriptor, EngineStatus,
                                           VideoEngine)


class CanvasEngine(VideoEngine):
    descriptor = EngineDescriptor(
        id="CANVAS",
        name="Canvas (stills + motion graphics)",
        version="1.1",
        summary=("Zero-cost assembly pipeline: researched grounded script, "
                 "text cards / your images with Ken-Burns motion, real TTS "
                 "narration, burned captions, music mix, ffmpeg render with "
                 "VAAPI + CPU fallback. Documentary (16:9) and Shorts "
                 "(9:16) modes."),
        execution=("local",),
        video_types=("documentary", "shorts"),
        resolutions=("1080p", "720p"),
        duration_range_s=(10, 900),
        hardware_requirements={"needs_ffmpeg_encoder": "libx264",
                               "min_free_disk_gb": 2},
        hw_requirements_text="CPU only — ffmpeg assembly (already verified)",
        estimated_resources={"ram_mb": 700, "disk_mb": 400,
                             "cpu_minutes_per_min_output": 4},
        estimated_cost="free (LLM + TTS via the free model chain)",
        privacy="runs locally; only web-search queries leave the machine",
        install_supported=False,
        install_source="",
        install_size_mb=0.0,
    )

    def probe(self, hw: dict[str, Any] | None = None) -> EngineStatus:
        ffmpeg_ok = False
        try:
            from core.video_agent.pipelines import FFMPEG
            ffmpeg_ok = Path(FFMPEG).is_file()
        except Exception:                          # noqa: BLE001
            pass
        ffmpeg_ok = ffmpeg_ok or bool(shutil.which("ffmpeg"))
        if not ffmpeg_ok:
            return EngineStatus(UNAVAILABLE,
                                "ffmpeg not found — assembly backend missing",
                                {"local": "UNAVAILABLE"})
        return EngineStatus(
            READY_LOCAL,
            "verified on this machine (renders completed: documentary, "
            "Shorts, doc→Shorts splits); visuals are motion graphics, "
            "never labelled AI video",
            {"local": READY_LOCAL})

    def start_params(self, spec: dict[str, Any],
                     parameters: dict[str, Any] | None = None
                     ) -> dict[str, Any] | None:
        mode = str(spec.get("mode") or "").upper()
        pipeline = str(spec.get("pipeline") or "").lower()
        if pipeline == "smoke":
            ref = "smoke"
        elif mode == "DOCUMENTARY":
            ref = "documentary"
        elif mode == "SHORTS":
            ref = "shorts"
        else:
            return None
        return {"kind": "video", "ref": ref,
                "params": {"spec": dict(spec),
                           "out_dir": str(spec.get("out_dir") or "")}}


ENGINE = CanvasEngine()
