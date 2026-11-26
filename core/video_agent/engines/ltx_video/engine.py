"""
core/video_agent/engines/ltx_video/engine.py — the LTX_VIDEO engine (adapter).

LTX-Video (Lightricks, open weights) — same adapter shape as WAN_2_1: local
inference only on measured-compatible CUDA hardware (the 2B distilled model
needs ~12 GB VRAM; the 13B needs far more), remote inference via an
explicitly configured GPU worker. Weights are never downloaded automatically.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from core.video_agent.engines.base import (INCOMPATIBLE, INSTALLABLE,
                                           NOT_CONFIGURED, READY_LOCAL,
                                           READY_REMOTE, EngineDescriptor,
                                           EngineStatus, VideoEngine)

WEIGHTS_DIR_NAME = "ltx-video"
MIN_VRAM_MB = 12288         # 2B distilled model guidance


def weights_dir() -> Path:
    """Model weights live OUTSIDE Jarvis source (mission rule)."""
    return Path.home() / "JarvisTools" / "models" / WEIGHTS_DIR_NAME


def _hardware_ok(hw: dict[str, Any] | None) -> tuple[bool, str]:
    from core.video_agent import hardware as hw_mod
    return hw_mod.classify_requirements(
        {"needs_cuda": True, "min_vram_mb": MIN_VRAM_MB}, hw)


class LtxEngine(VideoEngine):
    descriptor = EngineDescriptor(
        id="LTX_VIDEO",
        name="LTX-Video",
        version="0.9",
        summary=("Open-weight, fast text/image-to-video diffusion model. "
                 "Local inference only on measured-compatible CUDA "
                 "hardware; remote inference via a configured worker."),
        execution=("local", "remote"),
        video_types=("text_to_video", "image_to_video", "ai_cinematic"),
        resolutions=("1080p", "720p"),
        duration_range_s=(5, 15),
        hardware_requirements={"needs_cuda": True, "min_vram_mb": MIN_VRAM_MB,
                               "min_ram_mb": 16384, "min_free_disk_gb": 25},
        hw_requirements_text=(f"CUDA GPU with ≥{MIN_VRAM_MB // 1024} GB VRAM "
                              "(2B distilled model), ≥16 GB system RAM, "
                              "~25 GB disk for weights — or a configured "
                              "remote GPU worker"),
        estimated_resources={"ram_mb": 16384, "vram_mb": MIN_VRAM_MB,
                             "disk_mb": 25000, "gpu_minutes_per_5s": 3},
        estimated_cost="free weights; compute cost depends on local GPU or "
                       "remote worker provider",
        privacy=("local: nothing leaves the machine. remote: prompts upload "
                 "to your configured worker — consent at approval time"),
        install_supported=False,
        install_source="https://github.com/Lightricks/LTX-Video (weights — "
                       "approval-gated, never automatic)",
        install_size_mb=24000.0,
    )

    def probe(self, hw: dict[str, Any] | None = None) -> EngineStatus:
        local_ok, local_reason = _hardware_ok(hw)
        weights = weights_dir()
        have_weights = weights.is_dir() and any(weights.iterdir()) \
            if weights.is_dir() else False

        local_status = (
            READY_LOCAL if (local_ok and have_weights) else
            INSTALLABLE if local_ok and not have_weights else
            INCOMPATIBLE)
        if local_status == INSTALLABLE:
            local_detail = (f"hardware measured compatible — weights not "
                            f"installed ({weights}); an approved ~24 GB "
                            f"download would enable local inference")
        elif local_status == READY_LOCAL:
            local_detail = f"hardware compatible, weights present ({weights})"
        else:
            local_detail = f"local: {local_reason}"

        from core.video_agent import hardware as hw_mod
        from core.video_agent import remote as remote_mod
        workers = hw_mod.remote_workers()
        if any(remote_mod.is_verified(w) for w in workers):
            remote_status, remote_detail = READY_REMOTE, (
                f"verified remote worker configured: "
                f"{', '.join(w.get('name', '?') for w in workers
                             if remote_mod.is_verified(w))}")
        elif workers:
            remote_status, remote_detail = NOT_CONFIGURED, (
                "worker(s) configured but not verified — run a "
                "connectivity check")
        else:
            remote_status, remote_detail = NOT_CONFIGURED, (
                "no remote GPU worker configured — add one under "
                "repo_agent.video_remote_workers")

        overall = READY_REMOTE if READY_REMOTE in (local_status, remote_status) \
            else (INSTALLABLE if local_status == INSTALLABLE else
                  (NOT_CONFIGURED if remote_status == NOT_CONFIGURED
                   and local_status == INSTALLABLE else INCOMPATIBLE))
        return EngineStatus(overall, f"{local_detail}; {remote_detail}",
                            {"local": local_status, "remote": remote_status})

    def install_plan(self) -> dict[str, Any] | None:
        ok, reason = _hardware_ok(None)
        if not ok:
            return None
        return {
            "engine": self.descriptor.id,
            "needs_download": True,
            "source": self.descriptor.install_source,
            "download_size_mb": self.descriptor.install_size_mb,
            "disk_use_mb": self.descriptor.install_size_mb * 1.2,
            "steps": [
                "measure hardware again immediately before the download",
                "download weights into ~/JarvisTools/models/ltx-video",
                "run a small compatibility render before marking READY",
            ],
            "approval_required": True,
            "note": "never started automatically",
        }

    def install(self, approve: bool = False, log=None) -> dict[str, Any]:
        ok, reason = _hardware_ok(None)
        if not ok:
            return {"ok": False, "status": INCOMPATIBLE,
                    "error": f"local install refused: {reason}",
                    "alternative": "configure a remote GPU worker instead"}
        if not approve:
            return {"ok": False, "approval_required": True,
                    "error": "downloading ~24 GB of weights requires "
                             "explicit approval",
                    "plan": self.install_plan()}
        return {"ok": False,
                "error": "weights installation arrives with the Phase 4 "
                         "installation manager (plan is ready; nothing was "
                         "downloaded)"}

    def stages(self) -> dict[str, Any]:
        """Engine-owned pipeline (Phase 7): resolved by
        pipelines.stages_for('ltx_video')."""
        from core.video_agent.pipeline_remote import remote_stages
        return remote_stages(self.descriptor.id)

    def start_params(self, spec: dict[str, Any],
                     parameters: dict[str, Any] | None = None
                     ) -> dict[str, Any] | None:
        if not self.probe().is_runnable():
            return None
        if str(spec.get("pipeline") or "").lower() != "ltx_video":
            return None
        return {"kind": "video", "ref": "ltx_video",
                "params": {"spec": dict(spec),
                           "out_dir": str(spec.get("out_dir") or "")}}


ENGINE = LtxEngine()
