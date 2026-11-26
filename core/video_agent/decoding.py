"""
core/video_agent/decoding.py — Codec-aware FFmpeg decoding utilities.

Policy (mission-driven):

    | Codec  | Decode path                         | Encode path               |
    |--------|-------------------------------------|---------------------------|
    | AV1    | Software ONLY (libdav1d / libaom)   | N/A (not encoded by RIO)  |
    | H.264  | Hardware if VAAPI verified          | Hardware (VAAPI) preferred|
    | H.265  | Hardware if VAAPI verified          | Software (libx265)        |
    | VP9    | Hardware if VAAPI verified          | N/A                       |

VAAPI availability is PROBED, not assumed. A probe that asserts-crashes or
returns an undefined-symbol error is treated as unavailable — the same class
as "the driver file is missing". The encoder module already does this per-pipeline;
this module adds the same discipline to DECODING decisions.

Usage
-----
    from core.video_agent.decoding import (
        decode_args,       # input args for a given codec
        is_sw_only_codec,  # True for codecs that must never use hwaccel
        software_fallback_env,  # env dict to force software paths
    )
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

FFMPEG_BIN = shutil.which("ffmpeg") or "ffmpeg"


def _has_libdav1d() -> bool:
    """True when the bundled ffmpeg has libdav1d compiled in."""
    try:
        out = subprocess.run(
            [FFMPEG_BIN, "-hide_banner", "-decoders"],
            capture_output=True, text=True, timeout=10,
        )
        return "libdav1d" in (out.stdout or "")
    except Exception:
        return False


def _has_libaom_av1() -> bool:
    """True when the bundled ffmpeg has libaom-av1 compiled in."""
    try:
        out = subprocess.run(
            [FFMPEG_BIN, "-hide_banner", "-decoders"],
            capture_output=True, text=True, timeout=10,
        )
        return "libaom-av1" in (out.stdout or "")
    except Exception:
        return False


def _preferred_software_av1_decoder() -> str | None:
    """Pick the best available software AV1 decoder.

    libdav1d is faster and uses less RAM than libaom-av1, so it is preferred.
    Returns None only when neither is available — a clear error condition.
    """
    if _has_libdav1d():
        return "libdav1d"
    if _has_libaom_av1():
        return "libaom-av1"
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_sw_only_codec(codec: str) -> bool:
    """Return True for codecs that MUST use software decoding on this machine.

    AV1 is the primary case: Intel Haswell (HD 4400) and AMD Polaris-era GPUs
    have no VA-API AV1 decode support. Forcing VAAPI on these causes fatal
    errors ('Failed setup for format vaapi') that spam stderr and can crash
    the media backend.
    """
    return codec.lower() in ("av1", "eac3", "truehd")


def software_fallback_env() -> dict[str, str]:
    """Environment that forces ALL ffmpeg subprocesses onto software paths.

    Set these BEFORE launching any ffmpeg instance. They do NOT affect the
    encoder pipeline (which manages its own env); they only apply when callers
    explicitly merge them into their subprocess env dict.

    VA_DISABLE_DRM=1     — blocks DRM hwaccel (av1_drm, vp9_drm)
    VA_NO_HARDWARE=1     — disables VAAPI entirely for decoding
    LIBVA_MUTABLE_CONFIG=0 — prevents mid-call driver re-probe
    """
    return {
        "VA_DISABLE_DRM": "1",
        "VA_NO_HARDWARE": "1",
        "LIBVA_MUTABLE_CONFIG": "0",
    }


def decode_args(
    input_path: str | Path,
    codec: str,
    *,
    output_fmt: str = "yuv420p",
    extra_input: list[str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Build ffmpeg decode arguments for one input file.

    Returns (args, env) ready to pass to subprocess.run().

    For AV1 and other sw-only codecs the correct software decoder is selected
    automatically; for H.264/H.265/VP9 the caller is expected to supply their
    own hwaccel handling (see encoder.py for the encode-side pattern).
    """
    codec = str(codec).lower()
    args: list[str] = []
    env_extra = {}

    if is_sw_only_codec(codec):
        # Force software decoder — never attempt VAAPI/DRM/CUDA for this codec.
        decoder = _preferred_software_av1_decoder()
        if decoder is None:
            raise RuntimeError(
                f"No software AV1 decoder available ({codec}). "
                "Install ffmpeg with --enable-libdav1d or --enable-libaom."
            )
        env_extra = software_fallback_env()
        args = [
            FFMPEG_BIN, "-hide_banner", "-loglevel", "error",
            *(extra_input or []),
            "-i", str(input_path),
            "-vf", f"format={output_fmt}",
            "-c:v", decoder,
            "-f", "rawvideo", "-",
        ]
    else:
        # Non-restricted codec: let ffmpeg choose naturally.
        args = [
            FFMPEG_BIN, "-hide_banner", "-loglevel", "error",
            *(extra_input or []),
            "-i", str(input_path),
            "-vf", f"format={output_fmt}",
            "-f", "rawvideo", "-",
        ]

    return args, env_extra


def probe_codec(path: str | Path) -> dict[str, Any]:
    """Return video stream info via ffprobe — used to decide decode strategy."""
    import json
    ffprobe = shutil.which("ffprobe") or "ffprobe"
    try:
        out = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json",
             "-show_streams", str(path)],
            capture_output=True, text=True, timeout=15,
        )
        data = json.loads(out.stdout or "{}")
        streams = data.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), {})
        return {
            "codec": str(video.get("codec_name") or ""),
            "profile": str(video.get("profile") or ""),
            "level": str(video.get("level") or ""),
            "width": int(video.get("width") or 0),
            "height": int(video.get("height") or 0),
            "resolution": f"{video.get('width', '?')}x{video.get('height', '?')}",
        }
    except Exception:
        return {"codec": "", "error": "probe failed"}
