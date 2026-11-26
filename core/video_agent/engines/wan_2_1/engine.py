"""
core/video_agent/engines/wan_2_1/engine.py — the WAN_2_1 engine (adapter).

Wan 2.1 (Alibaba Tongyi-MAI, open weights) is a diffusion video model. On
this machine it is honestly INCOMPATIBLE locally: the GeForce 820M has no
working driver (nouveau, nvidia-smi fails), so CUDA is unavailable — and
Wan 2.1 needs a CUDA GPU (≥8 GB VRAM for the 1.3B T2V model).

The adapter is the mission's Phase 7 shape: local inference only when the
hardware MEASURES compatible (weights download separately, approval-gated);
remote inference as soon as a remote GPU worker is explicitly configured.
No weights are ever downloaded automatically.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from core.video_agent.engines.base import (INCOMPATIBLE, INSTALLABLE,
                                           NOT_CONFIGURED, READY_LOCAL,
                                           READY_REMOTE, EngineDescriptor,
                                           EngineStatus, VideoEngine)

WEIGHTS_DIR_NAME = "wan-2-1"
MIN_VRAM_MB = 8192          # 1.3B T2V model guidance


def weights_dir() -> Path:
    """Model weights live OUTSIDE Jarvis source (mission rule)."""
    return Path.home() / "JarvisTools" / "models" / WEIGHTS_DIR_NAME


def _hardware_ok(hw: dict[str, Any] | None) -> tuple[bool, str]:
    from core.video_agent import hardware as hw_mod
    return hw_mod.classify_requirements(
        {"needs_cuda": True, "min_vram_mb": MIN_VRAM_MB}, hw)


def _remote_configured() -> list[dict[str, Any]]:
    from core.video_agent import hardware as hw_mod
    return hw_mod.remote_workers()


class WanEngine(VideoEngine):
    descriptor = EngineDescriptor(
        id="WAN_2_1",
        name="Wan 2.1",
        version="2.1",
        summary=("Open-weight text/image-to-video diffusion model. Local "
                 "inference only on measured-compatible CUDA hardware; "
                 "remote inference via a configured GPU worker."),
        execution=("local", "remote"),
        video_types=("text_to_video", "image_to_video", "ai_cinematic"),
        resolutions=("1080p", "720p"),
        duration_range_s=(5, 30),
        hardware_requirements={"needs_cuda": True, "min_vram_mb": MIN_VRAM_MB,
                               "min_ram_mb": 16384, "min_free_disk_gb": 30},
        hw_requirements_text=(f"CUDA GPU with ≥{MIN_VRAM_MB // 1024} GB VRAM "
                              "(1.3B model), ≥16 GB system RAM, ~30 GB disk "
                              "for weights — or a configured remote GPU "
                              "worker"),
        estimated_resources={"ram_mb": 16384, "vram_mb": MIN_VRAM_MB,
                             "disk_mb": 30000, "gpu_minutes_per_5s": 6},
        estimated_cost="free weights; compute cost depends on local GPU or "
                       "remote worker provider",
        privacy=("local: nothing leaves the machine. remote: prompts upload "
                 "to your configured worker — consent at approval time"),
        install_supported=False,   # only after measured hardware compatibility
        install_source="https://github.com/Wan-Video/Wan2.1 (weights — "
                       "approval-gated, never automatic)",
        install_size_mb=28000.0,
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
                            f"installed ({weights}); an approved ~28 GB "
                            f"download would enable local inference")
        elif local_status == READY_LOCAL:
            local_detail = f"hardware compatible, weights present ({weights})"
        else:
            local_detail = f"local: {local_reason}"

        from core.video_agent import remote as remote_mod
        workers = _remote_configured()
        verified = [w for w in workers if remote_mod.is_verified(w)]
        names = ", ".join(str(w.get("name") or "?") for w in workers)
        if verified:
            remote_status, remote_detail = READY_REMOTE, (
                "verified remote worker configured: "
                + ", ".join(str(w.get("name") or "?") for w in verified))
        elif workers:
            remote_status, remote_detail = NOT_CONFIGURED, (
                "worker(s) configured but not verified — run a "
                f"connectivity check ({names})")
        else:
            remote_status, remote_detail = NOT_CONFIGURED, (
                "no remote GPU worker configured — add one under "
                "repo_agent.video_remote_workers to enable remote inference")

        overall = READY_REMOTE if READY_REMOTE in (local_status, remote_status) \
            else (INSTALLABLE if local_status == INSTALLABLE else
                  (NOT_CONFIGURED if remote_status == NOT_CONFIGURED
                   and local_status == INSTALLABLE else INCOMPATIBLE))
        detail = f"{local_detail}; {remote_detail}"
        return EngineStatus(overall, detail,
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
                "download weights into ~/JarvisTools/models/wan-2-1 "
                "(outside Jarvis source)",
                "run a small compatibility render before marking READY",
            ],
            "approval_required": True,
            "note": "never started automatically — explicit approval per "
                    "the installation policy",
        }

    def install(self, approve: bool = False, log=None) -> dict[str, Any]:
        ok, reason = _hardware_ok(None)
        if not ok:
            return {"ok": False, "status": INCOMPATIBLE,
                    "error": f"local install refused: {reason}",
                    "alternative": "configure a remote GPU worker instead"}
        if not approve:
            return {"ok": False, "approval_required": True,
                    "error": "downloading ~28 GB of weights requires "
                             "explicit approval",
                    "plan": self.install_plan()}
        return {"ok": False,
                "error": "weights installation arrives with the Phase 4 "
                         "installation manager (plan is ready; nothing was "
                         "downloaded)"}

    def stages(self) -> dict[str, Any]:
        """Engine-owned pipeline (Phase 7): resolved by
        pipelines.stages_for('wan_2_1') through the registry."""
        from core.video_agent.pipeline_remote import remote_stages
        return remote_stages(self.descriptor.id)

    def start_params(self, spec: dict[str, Any],
                     parameters: dict[str, Any] | None = None
                     ) -> dict[str, Any] | None:
        """A video job on this engine's own pipeline — but ONLY when a
        verified remote worker exists (local stays INCOMPATIBLE here). The
        approval dispatch re-checks probe() before queueing."""
        if not self.probe().is_runnable():
            return None
        if str(spec.get("pipeline") or "").lower() != "wan_2_1":
            return None
        return {"kind": "video", "ref": "wan_2_1",
                "params": {"spec": dict(spec),
                           "out_dir": str(spec.get("out_dir") or "")}}


ENGINE = WanEngine()
