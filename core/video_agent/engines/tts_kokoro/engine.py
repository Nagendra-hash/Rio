"""
core/video_agent/engines/tts_kokoro/engine.py — local neural TTS engine.

Not a video generator — the Studio's VOICE engine. kokoro is the only local,
offline neural narration option; gTTS (cloud, free) remains the default
narration path. Installing it = one small pip package into the Jarvis venv
(~5 MB); the ~330 MB model downloads on first use and is cached under
~/.cache/huggingface (existing core/tts.py behavior, unchanged).
"""
from __future__ import annotations

from importlib.util import find_spec
from typing import Any

from core.video_agent.engines.base import (INSTALLABLE, READY_LOCAL,
                                           EngineDescriptor, EngineStatus,
                                           VideoEngine)


class KokoroTtsEngine(VideoEngine):
    descriptor = EngineDescriptor(
        id="TTS_KOKORO",
        name="Kokoro TTS (local neural voice)",
        version="0.9",
        summary=("Offline neural narration (Hindi-capable voices) running "
                 "on CPU inside the Jarvis venv; the ~330 MB model is "
                 "cached on first use."),
        execution=("local",),
        video_types=("narration",),
        resolutions=("audio",),
        duration_range_s=(1, 3600),
        hardware_requirements={"min_ram_mb": 2000},
        hw_requirements_text="CPU + ~2 GB RAM during synthesis",
        estimated_resources={"ram_mb": 2000, "disk_mb": 340},
        estimated_cost="free (model weights are open)",
        privacy="runs locally — narration never leaves the machine",
        install_supported=True,
        install_source="PyPI `kokoro>=0.9` (verified by import after "
                       "install)",
        install_size_mb=5.0,
    )

    def probe(self, hw: dict[str, Any] | None = None) -> EngineStatus:
        try:
            ok = find_spec("kokoro") is not None
        except (ImportError, ValueError):
            ok = False
        if ok:
            return EngineStatus(READY_LOCAL,
                                "kokoro importable — model downloads on "
                                "first use (~330 MB, cached)",
                                {"local": READY_LOCAL})
        return EngineStatus(INSTALLABLE,
                            "not installed — one pip package "
                            "(approval-gated), model cached on first use",
                            {"local": INSTALLABLE})

    def install_plan(self) -> dict[str, Any] | None:
        if self.probe().status == READY_LOCAL:
            return None
        return {
            "engine": self.descriptor.id,
            "needs_download": True,
            "source": self.descriptor.install_source,
            "download_size_mb": 5.0,
            "disk_use_mb": 345.0,       # package + first-use model cache
            "steps": [
                "pip install kokoro>=0.9 into the Jarvis venv",
                "verify with an import (a FAILED verify is never READY)",
                "model (~330 MB) downloads on first synthesis, cached",
            ],
            "approval_required": True,
        }

    def start_params(self, spec: dict[str, Any],
                     parameters: dict[str, Any] | None = None) -> None:
        return None


ENGINE = KokoroTtsEngine()
