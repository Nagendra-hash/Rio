"""
core/video_agent/encoder.py — hardware video encoder selection (VAAPI → CPU).

The Video Agent has always encoded with libx264 on the CPU. This module adds
the verified Intel VAAPI H.264 path and keeps CPU libx264 as an automatic,
honest fallback:

  * detection uses the STABLE PCI by-path device
    /dev/dri/by-path/pci-0000:00:02.0-render (the renderD* number moves
    between boots; the by-path symlink does not)
  * LIBVA_DRIVER_NAME=i965 is set for the ffmpeg process only
  * availability is proven by a REAL tiny encode (not by "the file exists"),
    cached for a few minutes
  * a failed VAAPI encode is retried once with libx264; the job records
    which backend actually produced the file, the elapsed time and the error
  * output contract: H.264 + AAC, 30 fps-capable, 1080x1920 vertical
  * RAM gate: renders refuse to start when MemAvailable is below the
    configured floor (video_min_free_mb)

No CUDA, no NVENC, no driver/kernel changes. Every probe is local and cheap.

Config keys (repo_agent section / JARVIS_REPO_AGENT_* env, see jobs.py):
    video_encoder          auto | vaapi | libx264   (default auto)
    video_vaapi_device     override the by-path device
    video_vaapi_driver     default i965
    video_vaapi_bitrate    default 6M
    video_vaapi_maxrate    default 8M
    video_min_free_mb      default 700
    video_x264_crf         default 26
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from core.repo_agent.jobs import config as jobs_config
from core.video_agent.pipelines import FFMPEG, StageError

# The Intel iGPU's PCI address is stable; renderD128/129 is not.
INTEL_PCI = "0000:00:02.0"
DEFAULT_DEVICE = f"/dev/dri/by-path/pci-{INTEL_PCI}-render"
DEFAULT_DRIVER = "i965"
_PROBE_TTL_S = 300.0

_lock = threading.Lock()
_probe_cache: dict[str, Any] = {}
_encoders_cache: tuple[float, set[str]] | None = None
# After a REAL VAAPI encode failure, stop retrying it in this process: the
# first scene should not pay the same broken-encoder cost six times.
_runtime_error: str = ""


# ── configuration helpers ────────────────────────────────────────────────────

def _cfg(key: str, default: Any = "") -> Any:
    try:
        value = jobs_config().get(key)
    except Exception:                              # noqa: BLE001
        value = None
    return default if value in (None, "") else value


def vaapi_device() -> Path:
    return Path(str(_cfg("video_vaapi_device", DEFAULT_DEVICE))).expanduser()


def vaapi_driver() -> str:
    return str(_cfg("video_vaapi_driver", DEFAULT_DRIVER)).strip() or \
        DEFAULT_DRIVER


def _encoder_names() -> set[str]:
    """ffmpeg's encoder list (cached 5 min; no encode is run)."""
    global _encoders_cache
    now = time.time()
    if _encoders_cache and now - _encoders_cache[0] < _PROBE_TTL_S:
        return _encoders_cache[1]
    names: set[str] = set()
    try:
        out = subprocess.run([FFMPEG, "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=20)
        for line in (out.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 2 and len(parts[0]) == 6:
                names.add(parts[1])
    except Exception:                              # noqa: BLE001
        names = set()
    _encoders_cache = (now, names)
    return names


def _driver_file() -> Path | None:
    """The i965 VA driver Mesa actually loads (no install, no download)."""
    for directory in ("/usr/lib/x86_64-linux-gnu/dri", "/usr/lib/dri",
                      "/usr/lib64/dri"):
        candidate = Path(directory) / f"{vaapi_driver()}_drv_video.so"
        if candidate.is_file():
            return candidate
    return None


def _device_state(device: Path) -> dict[str, Any]:
    state: dict[str, Any] = {"path": str(device), "exists": False,
                             "resolved": "", "is_char_device": False}
    try:
        state["exists"] = device.exists()
        if state["exists"]:
            state["resolved"] = str(device.resolve())
            state["is_char_device"] = device.resolve().is_char_device()
    except OSError:
        pass
    return state


# ── real VAAPI probe ────────────────────────────────────────────────────────

def _probe_encode(device: Path, driver: str) -> tuple[bool, str]:
    """Prove the encoder end-to-end with a 0.4 s 320x240 encode.

    This is deliberately a REAL encode: a present /dev/dri node and a present
    ffmpeg encoder do not guarantee that i965 can create an H.264 context on
    this kernel (the exact failure we must not discover mid-render)."""
    env = dict(os.environ)
    env["LIBVA_DRIVER_NAME"] = driver
    # Never attempt hardware decoding during the probe — the probe encodes a
    # synthetic test pattern, so no decode is needed; but some libva builds
    # still try to initialise DRM/VAAPI contexts on import and assert-fail
    # with exit 134 when the GPU cannot drive them. These vars block that.
    env.update({
        "VA_DISABLE_DRM": "1",
        "VA_NO_HARDWARE": "1",
        "LIBVA_MUTABLE_CONFIG": "0",
    })
    args = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-vaapi_device", str(device),
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=0.4",
        "-vf", "format=nv12,hwupload",
        "-c:v", "h264_vaapi", "-profile:v", "high",
        "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(args, capture_output=True, text=True,
                              timeout=30, env=env)
    except Exception as exc:                       # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:200]}"
    if proc.returncode == 0:
        return True, "real 320x240 h264_vaapi encode succeeded"
    tail = " ".join((proc.stderr or "").strip().splitlines()[-2:])[:300]
    return False, f"probe encode failed (exit {proc.returncode}): {tail}"


def probe_vaapi(force: bool = False) -> dict[str, Any]:
    """Measured VAAPI capability. Cached for _PROBE_TTL_S unless forced."""
    device = vaapi_device()
    driver = vaapi_driver()
    key = f"{device}|{driver}"
    now = time.time()
    with _lock:
        cached = _probe_cache.get(key)
        if cached and not force and now - cached["_ts"] < _PROBE_TTL_S:
            return {k: v for k, v in cached.items() if k != "_ts"}

    state = _device_state(device)
    driver_file = _driver_file()
    has_encoder = "h264_vaapi" in _encoder_names()
    checks = {
        "device_by_path_exists": state["exists"],
        "device_is_char": state["is_char_device"],
        "ffmpeg_h264_vaapi": has_encoder,
        "i965_driver_present": driver_file is not None,
    }
    result: dict[str, Any] = {
        "available": False,
        "backend": "vaapi",
        "device": str(device),
        "device_resolved": state["resolved"],
        "driver": driver,
        "driver_file": str(driver_file or ""),
        "checks": checks,
        "detail": "",
        "last_probe": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "last_probe_epoch": now,
    }
    if not all(checks.values()):
        missing = [k for k, v in checks.items() if not v]
        result["detail"] = "VAAPI unavailable: " + ", ".join(missing)
    else:
        ok, detail = _probe_encode(device, driver)
        result["available"] = ok
        result["detail"] = detail
    with _lock:
        _probe_cache[key] = {**result, "_ts": now}
    return result


# ── encoder choice + ffmpeg argument builders ───────────────────────────────

@dataclass(frozen=True)
class Encoder:
    backend: str                 # "vaapi" | "libx264"
    device: str = ""
    driver: str = ""
    reason: str = ""

    @property
    def hardware(self) -> bool:
        return self.backend == "vaapi"

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.hardware:
            env["LIBVA_DRIVER_NAME"] = self.driver
        return env

    def input_args(self) -> list[str]:
        """Global/input options (must come BEFORE -i)."""
        return ["-vaapi_device", self.device] if self.hardware else []

    def video_codec_args(self, *, threads: int | None = None,
                         bitrate: str | None = None) -> list[str]:
        if self.hardware:
            rate = bitrate or str(_cfg("video_vaapi_bitrate", "5M"))
            maxrate = str(_cfg("video_vaapi_maxrate", "7M"))
            return ["-c:v", "h264_vaapi", "-profile:v", "high",
                    "-b:v", rate, "-maxrate", maxrate,
                    "-bufsize", str(_cfg("video_vaapi_bufsize", "10M"))]
        crf = int(_cfg("video_x264_crf", 26))
        args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf)]
        if threads:
            args += ["-threads", str(threads)]
        return args

    def upload_filter(self, chain: str) -> str:
        """Finish a CPU filter chain for the selected encoder."""
        if self.hardware:
            return f"{chain},format=nv12,hwupload"
        return f"{chain},format=yuv420p"

    def as_dict(self) -> dict[str, Any]:
        return {"backend": self.backend, "hardware": self.hardware,
                "device": self.device, "driver": self.driver,
                "reason": self.reason}


def select_encoder(prefer: str | None = None) -> Encoder:
    """Pick the encoder for a render. `prefer` = auto | vaapi | libx264.

    'vaapi' is still allowed to fall back at RUN time (run_encode); this
    selection only decides what to try first."""
    mode = str(prefer or _cfg("video_encoder", "auto")).strip().lower()
    if _runtime_error and mode != "libx264":
        return Encoder("libx264",
                       reason=f"VAAPI failed earlier in this process: "
                              f"{_runtime_error[:160]}")
    probe = probe_vaapi()
    if mode == "libx264":
        return Encoder("libx264", reason="configured CPU libx264")
    if mode == "vaapi" and not probe["available"]:
        return Encoder("libx264",
                       reason=f"VAAPI requested but unavailable: "
                              f"{probe['detail']}")
    if probe["available"]:
        return Encoder("vaapi", device=probe["device"],
                       driver=probe["driver"],
                       reason="Intel VAAPI verified by real probe")
    return Encoder("libx264", reason=f"VAAPI unavailable: {probe['detail']}")


# ── RAM guard ───────────────────────────────────────────────────────────────

def mem_available_mb() -> float:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return round(int(line.split()[1]) / 1024, 1)
    except Exception:                              # noqa: BLE001
        pass
    return -1.0


def ensure_memory(needed_mb: float | None = None) -> float:
    """Refuse a render when free RAM is below the configured floor.

    The RenderLock already serializes heavy renders; this gate covers the
    other side of the mission rule — a single render must not start into a
    machine that is already swapping."""
    minimum = float(needed_mb if needed_mb is not None
                    else _cfg("video_min_free_mb", 700))
    available = mem_available_mb()
    if available >= 0 and available < minimum:
        raise StageError(
            f"not enough free RAM to render: {available:.0f} MB available, "
            f"need {minimum:.0f} MB — close other heavy apps and retry "
            "(nothing was started)")
    return available


# ── encode runner with automatic fallback ───────────────────────────────────

def run_encode(build: Callable[[Encoder], tuple[list[str], dict[str, str]]],
               *, cwd: Path, log, what: str, timeout: int = 900,
               prefer: str | None = None) -> dict[str, Any]:
    """Run an ffmpeg encode with automatic VAAPI → libx264 fallback.

    `build(encoder)` returns (args, env). It is called once per attempt so the
    filter chain and codec flags always match the backend that runs."""
    global _runtime_error
    first = select_encoder(prefer)
    order = [first]
    if first.hardware:
        order.append(Encoder("libx264", reason="automatic fallback"))
    attempts: list[dict[str, Any]] = []
    for encoder in order:
        args, env = build(encoder)
        started = time.monotonic()
        log(f"[encoder] {what}: trying {encoder.backend}"
            + (f" device={encoder.device} driver={encoder.driver}"
               if encoder.hardware else ""))
        try:
            proc = subprocess.run([str(a) for a in args], cwd=str(cwd),
                                  timeout=timeout, env=env,
                                  capture_output=True, text=True)
        except subprocess.TimeoutExpired:
            elapsed = round(time.monotonic() - started, 2)
            attempts.append({"backend": encoder.backend, "ok": False,
                             "elapsed_s": elapsed,
                             "error": f"timeout after {timeout}s"})
            if encoder.hardware:
                _runtime_error = f"vaapi timeout after {timeout}s"
            log(f"[encoder] {what}: {encoder.backend} TIMEOUT after "
                f"{timeout}s")
            continue
        elapsed = round(time.monotonic() - started, 2)
        if proc.returncode == 0:
            attempts.append({"backend": encoder.backend, "ok": True,
                             "elapsed_s": elapsed, "error": ""})
            log(f"[encoder] {what}: {encoder.backend} ok in {elapsed:.2f}s")
            return {"backend": encoder.backend, "encoder": encoder.as_dict(),
                    "elapsed_s": elapsed, "attempts": attempts,
                    "fell_back": len(attempts) > 1}
        err = " ".join((proc.stderr or "").strip().splitlines()[-3:])[:500]
        attempts.append({"backend": encoder.backend, "ok": False,
                         "elapsed_s": elapsed, "error": err})
        if encoder.hardware:
            _runtime_error = err or "vaapi encode failed"
        log(f"[encoder] {what}: {encoder.backend} FAILED in {elapsed:.2f}s — "
            f"{err[:200]}")
    raise StageError(f"{what} failed on every encoder "
                     f"({', '.join(a['backend'] for a in attempts)}): "
                     f"{attempts[-1]['error'][:400]}")


def encoder_status() -> dict[str, Any]:
    """JSON-able status for RIO / voice. Cached probe; never starts a render."""
    from core.video_agent.resources import ffmpeg_threads
    probe = probe_vaapi()
    chosen = select_encoder()
    return {
        "configured": str(_cfg("video_encoder", "auto")),
        "selected": chosen.backend,
        "hardware_accelerated": chosen.hardware,
        "reason": chosen.reason,
        "vaapi": probe,
        "cpu": {"backend": "libx264", "preset": "veryfast",
                "crf": int(_cfg("video_x264_crf", 26)),
                "threads": ffmpeg_threads()},
        "output": {"video_codec": "h264", "audio_codec": "aac",
                   "fps": 30, "vertical": "1080x1920"},
        "memory": {"available_mb": mem_available_mb(),
                   "minimum_mb": float(_cfg("video_min_free_mb", 700))},
    }


def status_text() -> str:
    st = encoder_status()
    v = st["vaapi"]
    lines = [
        f"Encoder: {st['selected']}"
        + (" (Intel VAAPI hardware H.264)" if st["hardware_accelerated"]
           else " (CPU libx264)"),
        f"Reason: {st['reason']}",
        f"VAAPI device: {v['device']}"
        + (f" → {v['device_resolved']}" if v.get("device_resolved") else ""),
        f"VAAPI driver: {v['driver']}"
        + (" present" if v["checks"].get("i965_driver_present") else " missing"),
        f"VAAPI probe: {'PASS' if v['available'] else 'FAIL'} — "
        f"{v['detail'][:180]}",
        f"Output: H.264 + AAC, 30 fps, 1080x1920",
        f"RAM: {st['memory']['available_mb']:.0f} MB available "
        f"(floor {st['memory']['minimum_mb']:.0f} MB)",
    ]
    return "\n".join(lines)
