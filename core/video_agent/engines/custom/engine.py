"""
core/video_agent/engines/custom/engine.py — the CUSTOM_VIDEO_MODEL adapter.

The documented adapter interface for any future video model or remote GPU
server. To register a new engine WITHOUT touching the core orchestration
system (mission Phase 2 / Phase 7):

    1. create  core/video_agent/engines/<your_id>/engine.py
    2. export  ENGINE = MyEngine()
    3. subclass core.video_agent.engines.base.VideoEngine:
         - descriptor : EngineDescriptor (all mission Phase 2 fields)
         - probe(hw)  -> EngineStatus   measured truth; READY only after a
                         REAL backend verification (a run must have worked)
         - start_params(spec, parameters) -> {"kind", "ref", "params"} so the
                         detached job store can launch your backend
    4. done — the registry auto-discovers the folder; RIO, voice and the
       approval flow pick it up with no further wiring

Nothing is registered here by default → honest UNAVAILABLE.
"""
from __future__ import annotations

from typing import Any

from core.video_agent.engines.base import (UNAVAILABLE, EngineDescriptor,
                                           EngineStatus, VideoEngine)


class CustomEngine(VideoEngine):
    descriptor = EngineDescriptor(
        id="CUSTOM_VIDEO_MODEL",
        name="Custom video model",
        version="1.0",
        summary=("Documented adapter interface for any additional video "
                 "model or remote GPU server. Register an engine module and "
                 "it appears everywhere — no core changes."),
        execution=("local", "remote"),
        video_types=("any",),
        resolutions=("any",),
        duration_range_s=(1, 3600),
        hardware_requirements={},
        hw_requirements_text="defined by the adapter you register",
        estimated_resources={},
        estimated_cost="defined by the adapter you register",
        privacy="defined by the adapter you register",
        install_supported=False,
        install_source="",
        install_size_mb=0.0,
    )

    def probe(self, hw: dict[str, Any] | None = None) -> EngineStatus:
        # A custom engine becomes READY the moment a real adapter module
        # registers with the same id and verifies its backend.
        return EngineStatus(
            UNAVAILABLE,
            "no custom engine registered yet — see this module's docstring "
            "for the adapter contract (drop engines/<id>/engine.py, export "
            "ENGINE, verify the backend, go READY)",
            {"local": UNAVAILABLE})

    def start_params(self, spec: dict[str, Any],
                     parameters: dict[str, Any] | None = None) -> None:
        return None


ENGINE = CustomEngine()
