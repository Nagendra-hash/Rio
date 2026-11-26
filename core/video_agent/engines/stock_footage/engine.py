"""
core/video_agent/engines/stock_footage/engine.py — the STOCK_FOOTAGE engine.

Licensed stock clips assembled with the existing ffmpeg machinery. Mission
rules encoded here:

  * stock footage is NEVER presented as newly AI-generated video — every
    clip carries source + license metadata in the run's assets manifest
  * no provider is configured by default → honest NOT_CONFIGURED
  * no bulk downloads: clips are fetched per-project, small and bounded,
    only after the method is approved
  * the assembly backend (ffmpeg) is already verified on this machine
"""
from __future__ import annotations

import shutil
from typing import Any

from core.video_agent.engines.base import (NOT_CONFIGURED, UNAVAILABLE,
                                           READY_LOCAL,
                                           EngineDescriptor, EngineStatus,
                                           VideoEngine)

SUPPORTED_PROVIDERS = ("pexels", "pixabay", "coverr")


def _provider_config() -> dict[str, Any]:
    """Explicit user configuration only: repo_agent.stock = {provider, key}."""
    try:
        from memory.config_manager import load_api_keys
        section = ((load_api_keys() or {}).get("repo_agent") or {})
        stock = section.get("stock") or {}
        if isinstance(stock, dict) and stock.get("provider") \
                and stock.get("api_key_ref", "stock_api_key"):
            return stock
    except Exception:                              # noqa: BLE001
        pass
    return {}


class StockFootageEngine(VideoEngine):
    descriptor = EngineDescriptor(
        id="STOCK_FOOTAGE",
        name="Stock footage",
        version="1.0",
        summary=("Licensed stock clips researched per scene and assembled "
                 "with ffmpeg (narration, captions, music) — real footage, "
                 "never labelled AI-generated."),
        execution=("local",),               # assembly is local; clips come
        # from the licensed provider API
        video_types=("documentary", "broll", "ai_cinematic"),
        resolutions=("1080p", "720p"),
        duration_range_s=(15, 900),
        hardware_requirements={"needs_ffmpeg_encoder": "libx264"},
        hw_requirements_text="CPU only — ffmpeg assembly (already verified)",
        estimated_resources={"ram_mb": 700, "disk_mb": 500},
        estimated_cost="free tiers exist; per-clip licensing depends on the "
                       "configured provider",
        privacy=("search queries go to the configured footage provider; "
                 "clips are downloaded with source + license metadata"),
        install_supported=False,            # no bulk tool downloads needed
        install_source="",
        install_size_mb=0.0,
    )

    def probe(self, hw: dict[str, Any] | None = None) -> EngineStatus:
        config = _provider_config()
        ffmpeg_ok = bool(shutil.which("ffmpeg"))
        if not ffmpeg_ok:
            return EngineStatus(UNAVAILABLE, "ffmpeg not found — assembly "
                                             "backend missing",
                                {"local": "UNAVAILABLE"})
        if not config.get("provider"):
            return EngineStatus(
                NOT_CONFIGURED,
                "assembly backend verified (ffmpeg); no footage provider "
                f"configured — set repo_agent.stock = {{provider: one of "
                f"{', '.join(SUPPORTED_PROVIDERS)}, api_key_ref: …}} to "
                "enable (approval-gated per project)",
                {"local": NOT_CONFIGURED})
        provider = str(config.get("provider"))
        if provider not in SUPPORTED_PROVIDERS:
            return EngineStatus(
                NOT_CONFIGURED,
                f"provider '{provider}' not supported yet "
                f"({', '.join(SUPPORTED_PROVIDERS)})",
                {"local": NOT_CONFIGURED})
        return EngineStatus(
            READY_LOCAL,
            f"provider '{provider}' configured — clip downloads are "
            "per-project, licensed and metadata-tracked; each project is "
            "approval-gated",
            {"local": READY_LOCAL})

    def stages(self) -> dict[str, Any]:
        """Engine-owned pipeline (Phase 7): resolved by
        pipelines.stages_for('stock_footage')."""
        from core.video_agent.pipeline_stock import STAGES
        return STAGES

    def start_params(self, spec: dict[str, Any],
                     parameters: dict[str, Any] | None = None
                     ) -> dict[str, Any] | None:
        if self.probe().status != READY_LOCAL:
            return None
        if str(spec.get("pipeline") or "").lower() != "stock_footage":
            return None
        return {"kind": "video", "ref": "stock_footage",
                "params": {"spec": dict(spec),
                           "out_dir": str(spec.get("out_dir") or "")}}


ENGINE = StockFootageEngine()
