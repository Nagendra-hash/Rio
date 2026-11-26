"""
core/video_agent/engines/blender_3d/engine.py — the BLENDER_3D engine.

Wraps the existing, proven Blender pipeline (core/video_agent/pipeline_blender.py,
Blender 3.6.23 at ~/JarvisTools) as a first-class registry engine. Readiness is
a REAL probe: the binary must exist AND answer `--version` — files on disk
alone are never readiness.
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from core.video_agent.engines.base import (INSTALLABLE, READY_LOCAL,
                                           EngineDescriptor, EngineStatus,
                                           VideoEngine)

_TTL_S = 300.0
_lock = threading.Lock()
_probe_cache: dict[str, Any] = {}


def blender_bin() -> str:
    """Resolve at call time (env override + HOME relocation safe)."""
    import os
    return os.environ.get("JARVIS_BLENDER_BIN") or \
        str(Path.home() / "JarvisTools"
            / "blender-3.6.23-linux-x64" / "blender")


def _version_probe() -> tuple[bool, str]:
    """(verified, detail) — verified means the binary RAN and reported."""
    binary = blender_bin()
    if not binary or not Path(binary).is_file():
        return False, f"Blender binary not found at {binary}"
    try:
        proc = subprocess.run([binary, "--version"], capture_output=True,
                              text=True, timeout=90)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Blender probe failed to run: {exc}"
    first = (proc.stdout or "").strip().splitlines()[:1]
    version = first[0].strip() if first else ""
    if proc.returncode == 0 and version.lower().startswith("blender"):
        return True, f"{version} verified (binary runs)"
    return False, f"binary exists but did not report a version " \
                  f"(exit {proc.returncode})"


class BlenderEngine(VideoEngine):
    descriptor = EngineDescriptor(
        id="BLENDER_3D",
        name="Blender 3D",
        version="3.6.23",
        summary=("Real 3D geometry, procedural characters and keyframe "
                 "animation rendered headlessly with Eevee; fully offline, "
                 "no paid APIs."),
        execution=("local",),
        video_types=("3d_animation",),
        resolutions=("720p", "1080p"),
        duration_range_s=(5, 30),
        hardware_requirements={"needs_binaries": [], "needs_blender": True,
                               "min_ram_mb": 2000, "min_free_disk_gb": 3},
        hw_requirements_text=("CPU+RAM render (no GPU needed); Blender "
                              "standalone in ~/JarvisTools; ~200 MB "
                              "headroom per render"),
        estimated_resources={"ram_mb": 1500, "disk_mb": 250,
                             "cpu_minutes_per_10s": 8},
        estimated_cost="free",
        privacy="runs locally — no user data leaves the machine",
        install_supported=True,
        install_source="https://download.blender.org/release/"
                       "Blender3.6/ (official tarball, verified by "
                       "--version before READY)",
        install_size_mb=272.0,
    )

    def probe(self, hw: dict[str, Any] | None = None) -> EngineStatus:
        now = time.time()
        binary = blender_bin()
        with _lock:
            cached = _probe_cache.get(binary)
        if cached and now - cached[0] < _TTL_S:
            verified, detail = cached[1], cached[2]
        else:
            # probe OUTSIDE the lock (non-reentrant lock; a slow real
            # `blender --version` must not block or deadlock other callers).
            # Cache is keyed by binary path so a changed install re-probes.
            verified, detail = _version_probe()
            with _lock:
                _probe_cache[binary] = (now, verified, detail)
        if verified:
            return EngineStatus(READY_LOCAL, detail,
                                {"local": READY_LOCAL})
        # Distinguish "never installed" (installable) from "installed but
        # broken" (unavailable): a directory with a dead binary is broken.
        if Path(binary).is_file():
            return EngineStatus("UNAVAILABLE", detail,
                                {"local": "UNAVAILABLE"})
        return EngineStatus(INSTALLABLE,
                            f"{detail} — an approved install can fix this",
                            {"local": INSTALLABLE})

    def install_plan(self) -> dict[str, Any] | None:
        binary = blender_bin()
        if Path(binary).is_file():
            return None
        tarball = Path.home() / "JarvisTools" / \
            "blender-3.6.23-linux-x64.tar.xz"
        return {
            "engine": self.descriptor.id,
            "needs_download": not tarball.is_file(),
            "source": self.descriptor.install_source,
            "download_size_mb": 0.0 if tarball.is_file()
            else self.descriptor.install_size_mb,
            "disk_use_mb": self.descriptor.install_size_mb * 2.2,
            "steps": [
                "verify an official blender.org tarball (existing local "
                "tarball reused when present — no unnecessary downloads)",
                "extract into ~/JarvisTools/ (isolated from Jarvis source)",
                "verify with `blender --version` before marking READY",
            ],
            "approval_required": True,
        }

    def install(self, approve: bool = False, log=None) -> dict[str, Any]:
        plan = self.install_plan()
        if plan is None:
            verified, detail = _version_probe()
            return {"ok": verified, "message": detail,
                    "already_installed": True}
        if not approve:
            return {"ok": False,
                    "error": "install requires explicit approval",
                    "approval_required": True, "plan": plan}
        from core.video_agent.engines._installer import extract_blender
        result = extract_blender(log=log)
        if result.get("ok"):
            with _lock:
                _probe_cache.clear()
            verified, detail = _version_probe()
            result["verified"] = verified
            result["detail"] = detail
        return result

    def uninstall(self, approve: bool = False) -> dict[str, Any]:
        binary = blender_bin()
        target = Path(binary).parent.parent
        if not Path(binary).is_file():
            return {"ok": False, "error": "Blender is not installed"}
        if not approve:
            return {"ok": False, "approval_required": True,
                    "error": f"would remove {target} — approve to proceed"}
        import shutil
        shutil.rmtree(target, ignore_errors=True)
        with _lock:
            _probe_cache.clear()
        return {"ok": True, "removed": str(target)}

    def start_params(self, spec: dict[str, Any],
                     parameters: dict[str, Any] | None = None
                     ) -> dict[str, Any] | None:
        if str(spec.get("mode") or "").upper() != "BLENDER_3D":
            return None
        return {"kind": "video", "ref": "blender",
                "params": {"spec": dict(spec),
                           "out_dir": str(spec.get("out_dir") or "")}}


ENGINE = BlenderEngine()
