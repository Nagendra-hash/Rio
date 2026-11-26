"""
core/video_agent/engines/_installer.py — small, guarded Blender installer.

Only used after EXPLICIT user approval (engines/blender_3d install(approve=
True)). Rules from the mission's installation policy:

  * reuse an existing official tarball when present (no unnecessary downloads)
  * download ONLY from the official blender.org release URL, streamed with a
    hard size cap, into a `.part` file renamed atomically
  * extract with zip-slip protection (no absolute paths, no `..` members)
  * disk gate before starting; verify `blender --version` before READY
  * never touches drivers, CUDA, the kernel or system configuration
"""
from __future__ import annotations

import shutil
import tarfile
import time
import urllib.request
from pathlib import Path
from typing import Any

from core.video_agent.engines.blender_3d.engine import blender_bin

BLENDER_VERSION = "3.6.23"
TARBALL_NAME = f"blender-{BLENDER_VERSION}-linux-x64.tar.xz"
OFFICIAL_URL = (f"https://download.blender.org/release/Blender3.6/"
                f"{TARBALL_NAME}")
MAX_DOWNLOAD_MB = 450
MIN_FREE_GB = 3.0


def _tools_root() -> Path:
    return Path.home() / "JarvisTools"


def extract_blender(log=None) -> dict[str, Any]:
    def _log(msg: str) -> None:
        if log:
            log(f"[engine-install] {msg}")

    root = _tools_root()
    root.mkdir(parents=True, exist_ok=True)
    tarball = root / TARBALL_NAME

    free_gb = shutil.disk_usage(root).free / 2**30
    if free_gb < MIN_FREE_GB:
        return {"ok": False,
                "error": f"only {free_gb:.1f} GB free — need ≥{MIN_FREE_GB} "
                         "GB for Blender (nothing was downloaded)"}

    if not tarball.is_file():
        _log(f"downloading official tarball: {OFFICIAL_URL}")
        try:
            _download(OFFICIAL_URL, tarball)
        except Exception as exc:                    # noqa: BLE001
            tarball.unlink(missing_ok=True)
            return {"ok": False,
                    "error": f"download failed: {type(exc).__name__}: "
                             f"{str(exc)[:200]}"}
    else:
        _log(f"reusing existing tarball {tarball} (no download)")

    target = root / f"blender-{BLENDER_VERSION}-linux-x64"
    if target.exists():
        _log(f"removing previous extraction at {target}")
        shutil.rmtree(target, ignore_errors=True)
    _log(f"extracting {tarball.name} → {root}")
    try:
        _safe_extract(tarball, root)
    except Exception as exc:                        # noqa: BLE001
        return {"ok": False, "error": f"extraction failed: "
                                      f"{type(exc).__name__}: {str(exc)[:200]}"}
    return {"ok": True, "installed_path": str(target),
            "downloaded": False, "message": "extracted — verify with "
            "`blender --version`"}


def _download(url: str, dest: Path) -> None:
    part = dest.with_suffix(dest.suffix + ".part")
    limit = MAX_DOWNLOAD_MB * 1024 * 1024
    with urllib.request.urlopen(url, timeout=60) as resp, \
            open(part, "wb") as out:
        remaining = limit
        while True:
            chunk = resp.read(1024 * 512)
            if not chunk:
                break
            remaining -= len(chunk)
            if remaining < 0:
                raise ValueError(f"download exceeds {MAX_DOWNLOAD_MB} MB cap")
            out.write(chunk)
    if part.stat().st_size < 10 * 1024 * 1024:
        raise ValueError("downloaded file suspiciously small")
    part.replace(dest)


def _safe_extract(tarball: Path, dest: Path) -> None:
    """Zip-slip-safe tarfile extraction (rejects absolute/.. members)."""
    dest = dest.resolve()
    with tarfile.open(tarball, "r:xz") as tar:
        members = []
        for member in tar.getmembers():
            candidate = (dest / member.name).resolve()
            if not str(candidate).startswith(str(dest)):
                raise ValueError(f"refusing unsafe archive member "
                                 f"{member.name!r}")
            members.append(member)
        tar.extractall(dest, members=members)      # noqa: S202 (checked)
