"""
core/video_agent/gpu_effects.py — honest NVIDIA / OpenGL capability probe.

The mission allows the NVIDIA GPU (Nouveau, no CUDA, no NVENC) to run OPTIONAL
visual effects — but only when a real accelerated processing path exists.
This module measures exactly that and refuses to pretend:

  * the NVIDIA PCI function and its bound kernel driver are read from sysfs
  * `glxinfo -B` is run normally and with DRI_PRIME=1 (PRIME offload) to see
    whether an accelerated NVIDIA OpenGL context can actually be created
  * ffmpeg's own accelerated filter path is probed (`-init_hw_device opencl`)
    because ffmpeg 4.4 has no generic OpenGL filter — the OpenCL filters are
    the only non-CUDA, non-NVENC effect path available
  * if any link is missing, `accelerated_effects_available` stays false and
    the renderer keeps every effect on the CPU, labelled as CPU

Nothing here installs drivers, loads kernel modules or changes the kernel.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

NVIDIA_PCI = "0000:08:00.0"
_SYSFS = Path("/sys/bus/pci/devices") / NVIDIA_PCI


def _read_text(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _pci_state() -> dict[str, Any]:
    driver_link = _SYSFS / "driver"
    driver = ""
    try:
        driver = driver_link.resolve().name
    except OSError:
        pass
    return {
        "pci": NVIDIA_PCI,
        "present": _SYSFS.exists(),
        "driver": driver,
        "vendor": _read_text(_SYSFS / "vendor"),
        "device": _read_text(_SYSFS / "device"),
    }


def _parse_glx(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {"renderer": "", "vendor": "", "version": "",
                           "accelerated": False}
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith("OpenGL renderer string:"):
            out["renderer"] = s.split(":", 1)[1].strip()
        elif s.startswith("OpenGL vendor string:"):
            out["vendor"] = s.split(":", 1)[1].strip()
        elif s.startswith("OpenGL version string:"):
            out["version"] = s.split(":", 1)[1].strip()
        elif s.startswith("Accelerated:"):
            out["accelerated"] = s.split(":", 1)[1].strip().lower() == "yes"
    return out


def _glxinfo(extra_env: dict[str, str] | None = None) -> dict[str, Any]:
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    try:
        proc = subprocess.run(["glxinfo", "-B"], capture_output=True,
                              text=True, timeout=20, env=env)
    except Exception as exc:                       # noqa: BLE001
        return {"available": False, "error": f"{type(exc).__name__}: "
                                             f"{str(exc)[:160]}"}
    if proc.returncode != 0:
        return {"available": False,
                "error": (proc.stderr or "").strip()[:200]}
    parsed = _parse_glx(proc.stdout)
    parsed["available"] = bool(parsed["renderer"])
    return parsed


def _opencl_available() -> tuple[bool, str]:
    """ffmpeg's OpenCL filter device — the only non-CUDA effect path here."""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-init_hw_device", "opencl=ocl", "-f", "lavfi",
             "-i", "testsrc2=size=64x64:duration=0.1", "-f", "null", "-"],
            capture_output=True, text=True, timeout=20)
    except Exception as exc:                       # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:120]}"
    if proc.returncode == 0:
        return True, "ffmpeg OpenCL device initialised"
    tail = " ".join((proc.stderr or "").strip().splitlines()[-2:])[:240]
    return False, f"ffmpeg OpenCL unavailable: {tail}"


def probe_nvidia() -> dict[str, Any]:
    """Measured NVIDIA Nouveau OpenGL + effect-path capability."""
    pci = _pci_state()
    default_gl = _glxinfo()
    prime_gl = _glxinfo({"DRI_PRIME": "1"})
    renderer = str(prime_gl.get("renderer") or "")
    nvidia_context = bool(
        re.search(r"nvd|nouveau|nvidia", renderer, re.IGNORECASE)
        or pci.get("vendor") == "0x10de" and prime_gl.get("available"))
    accelerated = bool(prime_gl.get("accelerated")) and nvidia_context
    opencl_ok, opencl_detail = _opencl_available()
    available = bool(accelerated and opencl_ok)
    if available:
        reason = "NVIDIA OpenGL offload + ffmpeg OpenCL filter path verified"
    elif not pci.get("present"):
        reason = "NVIDIA PCI function not present"
    elif pci.get("driver") != "nouveau":
        reason = f"NVIDIA driver is {pci.get('driver') or 'unbound'}, not nouveau"
    elif not accelerated:
        reason = ("no accelerated NVIDIA OpenGL context via DRI_PRIME=1 — "
                  "effects stay on the CPU")
    else:
        reason = ("NVIDIA OpenGL offload works, but ffmpeg 4.4 exposes no "
                  "OpenGL filter and its OpenCL device is unavailable — "
                  "effects stay on the CPU")
    return {
        "pci": pci,
        "opengl_default": default_gl,
        "opengl_prime": prime_gl,
        "opencl": {"available": opencl_ok, "detail": opencl_detail},
        "nvidia_context": nvidia_context,
        "accelerated_opengl": accelerated,
        "accelerated_effects_available": available,
        "effects": ["ffmpeg OpenCL filters"] if available else [],
        "cpu_effects": True,
        "reason": reason,
        "cuda_used": False,
        "nvenc_used": False,
        "last_probe": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def effects_for_render() -> dict[str, Any]:
    """What the renderer may actually use. CPU effects are always available;
    GPU effects are returned only when the measured path exists."""
    probe = probe_nvidia()
    return {
        "backend": "nvidia-opencl" if probe["accelerated_effects_available"]
                   else "cpu",
        "gpu_effects": probe["effects"],
        "cpu_effects": True,
        "reason": probe["reason"],
    }


def status_text() -> str:
    p = probe_nvidia()
    prime = p["opengl_prime"]
    return "\n".join([
        f"NVIDIA: {p['pci']['pci']} driver={p['pci']['driver'] or '?'} "
        f"vendor={p['pci']['vendor'] or '?'}",
        f"OpenGL (DRI_PRIME=1): {prime.get('renderer') or 'unavailable'}"
        + (f" — {prime.get('version')}" if prime.get("version") else ""),
        f"Accelerated OpenGL: {'yes' if p['accelerated_opengl'] else 'no'}",
        f"OpenCL effect path: {'yes' if p['opencl']['available'] else 'no'}",
        f"GPU effects: {'available' if p['accelerated_effects_available'] else 'NOT USED — CPU effects only'}",
        f"Reason: {p['reason']}",
        "CUDA: not used. NVENC: not used.",
    ])
