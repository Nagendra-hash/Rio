"""
core/video_agent/hardware.py — Phase 3 (Universal Video Studio): ONE measured
hardware report shared by every video engine.

Rules encoded here (mission Phase 3):

  * a detected GPU label never implies a capability. CUDA means nvidia-smi
    actually WORKS on this machine — the GeForce 820M sits on nouveau with no
    driver: present, but CUDA/NVENC unavailable (measured 2026-10-26)
  * VAAPI capability is the REAL probe encode from encoder.py, never a
    "the file exists" guess
  * ROCm is only claimed when /dev/kfd exists AND rocm-smi answers
  * OpenGL facts come from the honest gpu_effects probe (default + DRI_PRIME)
  * remote GPU workers exist ONLY as explicit user configuration in
    config/api_keys.json → repo_agent.video_remote_workers — nothing is
    auto-discovered, auto-connected or probed without the user asking
  * reports are cached for a short TTL and refreshable on demand; a stable
    `fingerprint` lets the Phase 11 hardware-refresh flow detect new kit

No driver, kernel or CUDA operations anywhere. Every probe is local, cheap
and read-only (plus encoder.py's existing 0.4 s VAAPI encode probe).
"""
from __future__ import annotations

import hashlib
import json
import platform
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from core.video_agent import gpu_effects
from core.video_agent import encoder as enc_mod

__all__ = ["probe", "refresh", "fingerprint", "summary_text",
           "classify_requirements", "remote_workers"]

_TTL_S = 60.0
_lock = threading.Lock()
_cache: dict[str, Any] = {}
_FFMPEG_ENCODERS_OF_INTEREST = (
    "h264_vaapi", "hevc_vaapi", "libx264", "libx265",
    "h264_nvenc", "hevc_nvenc", "aac",
)


# ── CPU / RAM / disk ─────────────────────────────────────────────────────────

def cpu_info() -> dict[str, Any]:
    model = ""
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                model = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    return {
        "architecture": platform.machine() or "unknown",
        "model": model or platform.processor() or "unknown",
        "threads": os_cpu_count(),
    }


def os_cpu_count() -> int:
    try:
        return os_cpu_count_raw() or 1
    except Exception:                              # noqa: BLE001
        return 1


def os_cpu_count_raw() -> int | None:
    import os
    return os.cpu_count()


def ram_info() -> dict[str, Any]:
    total = avail = -1.0
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                total = round(int(line.split()[1]) / 1024, 1)
            elif line.startswith("MemAvailable:"):
                avail = round(int(line.split()[1]) / 1024, 1)
    except (OSError, ValueError, IndexError):
        pass
    return {"total_mb": total, "available_mb": avail}


def disk_info() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for label, path in (("home", Path.home()), ("repo", Path.cwd())):
        try:
            usage = shutil.disk_usage(path)
            out[label] = {
                "path": str(path),
                "free_gb": round(usage.free / 2**30, 1),
                "total_gb": round(usage.total / 2**30, 1),
            }
        except Exception:                          # noqa: BLE001
            out[label] = {"path": str(path), "free_gb": -1, "total_gb": -1}
    return out


# ── GPUs ─────────────────────────────────────────────────────────────────────

def _nvidia_smi() -> dict[str, Any] | None:
    """None = nvidia-smi unusable (no driver) — a PRESENT gpu is not a
    WORKING one. This is the anti-assumption the mission demands."""
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
             "--format=csv,noheader"], capture_output=True, text=True,
            timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return None
    parts = [p.strip() for p in proc.stdout.strip().splitlines()[0].split(",")]
    if len(parts) < 3:
        return None
    try:
        vram_mb = int(str(parts[1]).lower().replace("mib", "").strip())
    except ValueError:
        vram_mb = -1
    return {"name": parts[0], "vram_mb": vram_mb,
            "driver_version": parts[2]}


def gpu_info(force: bool = False) -> dict[str, Any]:
    nvidia_pci = gpu_effects._pci_state()
    smi = _nvidia_smi() if nvidia_pci.get("present") else None
    cuda_available = bool(smi)          # driver must actually answer
    vaapi = enc_mod.probe_vaapi(force=force)
    rocm = _rocm_state()
    gl = gpu_effects.probe_nvidia()
    intel = {
        "present": vaapi.get("device_resolved") or vaapi.get("device") or "",
        "driver": "i915",
        "vaapi": vaapi,
    }
    nvidia = {
        "pci": nvidia_pci,
        "present": bool(nvidia_pci.get("present")),
        "kernel_driver": nvidia_pci.get("driver") or "",
        "driver_userspace": bool(smi),
        "cuda_available": cuda_available,
        "cuda_reason": ("nvidia-smi answered — driver present"
                        if cuda_available else
                        "NVIDIA GPU present but nvidia-smi fails — no usable "
                        "driver, CUDA/NVENC unavailable (never assume CUDA "
                        "from a GPU label)"),
        "smi": smi,
        "vram_mb": (smi or {}).get("vram_mb", -1),
        "opengl_prime": gl.get("opengl_prime") or {},
        "accelerated_opengl": bool(gl.get("accelerated_opengl")),
    }
    return {
        "intel": intel,
        "nvidia": nvidia,
        "rocm": rocm,
        "opengl": {
            "default": gl.get("opengl_default") or {},
            "prime": gl.get("opengl_prime") or {},
            "opencl": gl.get("opencl") or {},
        },
        "vaapi": vaapi,
    }


def _rocm_state() -> dict[str, Any]:
    kfd = Path("/dev/kfd").exists()
    binary = shutil.which("rocm-smi")
    version = ""
    if kfd and binary:
        try:
            proc = subprocess.run([binary, "--showproduct"],
                                  capture_output=True, text=True, timeout=10)
            if proc.returncode == 0:
                version = (proc.stdout or "").strip()[:120]
        except (OSError, subprocess.SubprocessError):
            pass
    return {"present": kfd, "available": bool(kfd and binary and version),
            "detail": version or ("kernel device present, rocm-smi missing"
                                  if kfd else "no ROCm kernel device")}


# ── ffmpeg / remote workers ──────────────────────────────────────────────────

def ffmpeg_info() -> dict[str, Any]:
    version = ""
    try:
        proc = subprocess.run(["ffmpeg", "-version"], capture_output=True,
                              text=True, timeout=15)
        if proc.returncode == 0:
            version = (proc.stdout or "").splitlines()[0][:120]
    except (OSError, subprocess.SubprocessError):
        pass
    names = enc_mod._encoder_names()
    encoders = {name: (name in names) for name in _FFMPEG_ENCODERS_OF_INTEREST}
    nvenc_usable = bool(encoders.get("h264_nvenc")) and \
        bool(gpu_info().get("nvidia", {}).get("cuda_available"))
    return {
        "version": version,
        "ffprobe": bool(shutil.which("ffprobe")),
        "encoders": encoders,
        "nvenc_usable": nvenc_usable,
        "nvenc_reason": ("usable" if nvenc_usable else
                         "encoder exists in ffmpeg but no usable NVIDIA "
                         "driver — never auto-selected"),
    }


def remote_workers() -> list[dict[str, Any]]:
    """Explicit user configuration ONLY. Never probed, never auto-created.
    Shape: repo_agent.video_remote_workers = [{name, host, gpu, notes}, …]."""
    try:
        from memory.config_manager import load_api_keys
        section = (load_api_keys() or {}).get("repo_agent") or {}
        raw = section.get("video_remote_workers") or []
        if isinstance(raw, list):
            return [w for w in raw if isinstance(w, dict) and w.get("name")]
    except Exception:                              # noqa: BLE001
        pass
    return []


# ── the report ───────────────────────────────────────────────────────────────

def probe(force: bool = False) -> dict[str, Any]:
    """The full measured report. Cached TTL_S unless forced."""
    now = time.time()
    with _lock:
        cached = _cache.get("report")
        if cached and not force and now - cached.get("_ts", 0) < _TTL_S:
            return {k: v for k, v in cached.items() if not k.startswith("_")}

    report: dict[str, Any] = {
        "cpu": cpu_info(),
        "ram": ram_info(),
        "disk": disk_info(),
        "gpus": gpu_info(force=force),
        "ffmpeg": ffmpeg_info(),
        "remote_workers": remote_workers(),
        "last_probe": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    report["fingerprint"] = fingerprint(report)
    with _lock:
        _cache["report"] = {**report, "_ts": now}
    return report


def refresh() -> dict[str, Any]:
    """User-requested / startup re-probe (Phase 11 hook). Forces the VAAPI
    probe too, so a hardware change is measured, not remembered."""
    return probe(force=True)


def fingerprint(report: dict[str, Any]) -> str:
    """Stable identity of the hardware that matters to engine selection.
    Deliberately EXCLUDES volatile values (available RAM, free disk)."""
    gpus = report.get("gpus", {})
    stable = {
        "cpu": report.get("cpu", {}),
        "ram_total_mb": report.get("ram", {}).get("total_mb"),
        "nvidia": {
            "present": gpus.get("nvidia", {}).get("present"),
            "driver": gpus.get("nvidia", {}).get("kernel_driver"),
            "cuda": gpus.get("nvidia", {}).get("cuda_available"),
            "vram_mb": gpus.get("nvidia", {}).get("vram_mb"),
        },
        "vaapi": gpus.get("vaapi", {}).get("available"),
        "rocm": gpus.get("rocm", {}).get("available"),
        "ffmpeg_version": report.get("ffmpeg", {}).get("version"),
        "encoders": sorted(k for k, v in
                           report.get("ffmpeg", {}).get("encoders", {}).items()
                           if v),
        "remote_workers": sorted(w.get("name", "")
                                 for w in report.get("remote_workers", [])),
    }
    blob = json.dumps(stable, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


# ── generic requirement check (engines build on this) ────────────────────────

def classify_requirements(reqs: dict[str, Any],
                          report: dict[str, Any] | None = None
                          ) -> tuple[bool, str]:
    """Check an engine's machine-readable requirements against the measured
    report. Returns (ok, reason). Keys understood:
        min_ram_mb, min_free_disk_gb, needs_cuda, min_vram_mb,
        needs_vaapi, needs_ffmpeg_encoder, needs_binaries, needs_blender.
    Unknown keys are ignored (forward compatibility)."""
    hw = report or probe()
    missing: list[str] = []

    min_ram = reqs.get("min_ram_mb")
    if min_ram and (hw["ram"]["total_mb"] >= 0 and
                    hw["ram"]["total_mb"] < float(min_ram)):
        missing.append(f"needs ≥{int(min_ram)} MB RAM "
                       f"(has {int(hw['ram']['total_mb'])} MB)")

    min_disk = reqs.get("min_free_disk_gb")
    if min_disk:
        free = hw["disk"]["home"]["free_gb"]
        if free >= 0 and free < float(min_disk):
            missing.append(f"needs ≥{min_disk} GB free disk (has {free} GB)")

    nvidia = hw["gpus"]["nvidia"]
    if reqs.get("needs_cuda") and not nvidia.get("cuda_available"):
        missing.append("needs a working NVIDIA CUDA driver "
                       + (f"(GPU present, driver={nvidia.get('kernel_driver')
                                          or 'unbound'})"
                          if nvidia.get("present") else "(no NVIDIA GPU)"))
    min_vram = reqs.get("min_vram_mb")
    if min_vram and nvidia.get("cuda_available"):
        vram = nvidia.get("vram_mb") or -1
        if vram >= 0 and vram < float(min_vram):
            missing.append(f"needs ≥{int(min_vram)} MB VRAM "
                           f"(has {int(vram)} MB)")

    if reqs.get("needs_vaapi") and not hw["gpus"]["vaapi"].get("available"):
        missing.append("needs the verified VAAPI encode path")

    need_enc = reqs.get("needs_ffmpeg_encoder")
    if need_enc and not hw["ffmpeg"]["encoders"].get(str(need_enc)):
        missing.append(f"needs ffmpeg encoder {need_enc}")

    for binary in (reqs.get("needs_binaries") or []):
        if not shutil.which(str(binary)):
            missing.append(f"needs the '{binary}' binary")

    if reqs.get("needs_blender") and not _blender_found():
        missing.append("needs the Blender standalone (~/JarvisTools)")

    if missing:
        return False, "; ".join(missing)
    return True, "all measured requirements satisfied"


def _blender_found() -> bool:
    try:
        from core.video_agent.pipeline_blender import BLENDER_BIN
        return Path(BLENDER_BIN).is_file()
    except Exception:                              # noqa: BLE001
        return False


# ── human view ───────────────────────────────────────────────────────────────

def summary_text(report: dict[str, Any] | None = None) -> str:
    hw = report or probe()
    cpu, ram, gpus = hw["cpu"], hw["ram"], hw["gpus"]
    ff, nvidia, vaapi = hw["ffmpeg"], gpus["nvidia"], gpus["vaapi"]
    workers = hw["remote_workers"]
    lines = [
        f"CPU: {cpu['model']} ({cpu['architecture']}, "
        f"{cpu['threads']} threads)",
        f"RAM: {ram['total_mb']:.0f} MB total, "
        f"{ram['available_mb']:.0f} MB available",
        f"Disk: {hw['disk']['home']['free_gb']} GB free (home), "
        f"{hw['disk']['repo']['free_gb']} GB free (repo)",
        f"GPU Intel: VAAPI {'VERIFIED (real probe encode)' if vaapi.get('available') else 'unavailable — ' + str(vaapi.get('detail', ''))[:100]}",
        f"GPU NVIDIA: {'present, driver ' + str(nvidia.get('kernel_driver')) if nvidia.get('present') else 'not present'} — "
        f"CUDA {'available' if nvidia.get('cuda_available') else 'NOT available'} "
        f"({nvidia.get('cuda_reason', '')[:90]})",
        f"GPU ROCm: {gpus['rocm']['detail']}",
        f"OpenGL: {gpus['opengl']['prime'].get('renderer') or gpus['opengl']['default'].get('renderer') or 'unavailable'}",
        f"FFmpeg: {ff['version'] or 'not found'} — "
        f"h264_vaapi {'yes' if ff['encoders'].get('h264_vaapi') else 'no'}, "
        f"libx264 {'yes' if ff['encoders'].get('libx264') else 'no'}, "
        f"NVENC {'usable' if ff['nvenc_usable'] else 'not used (' + ff['nvenc_reason'][:60] + ')'}",
        f"Remote GPU workers: {len(workers)} configured"
        + (": " + ", ".join(w.get("name", "?") for w in workers)
           if workers else " (none — configure in repo_agent.video_remote_workers)"),
        f"Fingerprint: {hw['fingerprint']} (probed {hw['last_probe']})",
    ]
    return "\n".join(lines)
