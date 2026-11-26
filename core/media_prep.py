"""Make a video file playable by the HUD's Qt/FFmpeg player.

WHY THIS EXISTS
    The Qt that ships with this project (PyQt6 6.12) carries its own FFmpeg
    build, and that build has **no software AV1 decoder**. Its `av1` decoder is
    the FFmpeg "native" one, which in FFmpeg is a hardware-only front end: when
    no hardware decoder accepts the frame it logs

        Your platform doesn't support hardware accelerated AV1 decoding.
        Failed to get pixel format.
        Get current frame error

    once per frame and produces no picture at all. The two decoders that *can*
    do AV1 in software — libdav1d and libaom-av1 — are absent from the bundle
    (checked: `avcodec_find_decoder_by_name("libdav1d")` is missing and the
    string "dav1d" does not occur in the bundled libavcodec). The stub
    libva libraries PyQt ships make the VAAPI device look usable, so Qt happily
    picks the hardware path and then fails every frame.

    No environment variable can fix that, because there is nothing to fall back
    to. `QT_FFMPEG_DECODING_HW_DEVICE_TYPES=""` only silences the hardware
    attempt; the decode still fails, just with "No HW decoder found".

WHAT THIS DOES
    Before a video is handed to Qt, ask ffprobe what is inside it. If the codec
    is one Qt can decode in software, hand it over untouched — zero added
    latency, hardware decoding still available for the codecs that support it.
    If it is not (AV1 today), transcode it once to H.264/AAC in a cache
    directory and hand *that* to Qt. H.264 software decoding is fast even on
    Haswell, the picture and sound end up in one stream (so they cannot drift),
    and the failed hardware path is never entered, so it is never retried.

    A missing ffmpeg/ffprobe is not fatal: `plan()` then says "play" and the
    player behaves exactly as it did before this module existed. The tools are
    looked for on PATH, in $JARVIS_FFMPEG/$JARVIS_FFPROBE, and in the Hermes
    tool directory this machine already has (`~/.hermes/tools/ffmpeg-*`).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# ── Where things are ────────────────────────────────────────────────────────

_IS_WINDOWS = os.name == "nt"


def _base_dir() -> Path:
    """Same relocation rule as the rest of the project: JARVIS_HOME wins."""
    home = os.environ.get("JARVIS_HOME", "").strip()
    if home:
        return Path(home).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def cache_dir() -> Path:
    d = _base_dir() / "cache" / "media"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _find_tool(env_var: str, name: str) -> str:
    """Absolute path to ffmpeg/ffprobe, or "" when this machine has neither.

    Order matters: an explicit choice first, then whatever is on PATH, then the
    bundled Hermes tools. Nothing here is required for the app to run — a video
    simply goes straight to Qt when the tool is absent.
    """
    explicit = os.environ.get(env_var, "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        if p.is_file():
            return str(p)

    found = shutil.which(name)
    if found:
        return found

    candidates = [
        Path.home() / ".hermes" / "tools",
        Path.home() / ".local" / "bin",
    ]
    for root in candidates:
        if not root.is_dir():
            continue
        try:
            # Newest ffmpeg-*/bin first: the directory name carries the version.
            for d in sorted(root.glob("ffmpeg-*"), reverse=True):
                exe = d / "bin" / (name + (".exe" if _IS_WINDOWS else ""))
                if exe.is_file():
                    return str(exe)
        except Exception:
            pass
    return ""


_FFMPEG = ""
_FFPROBE = ""
_FFPLAY = ""


def ffmpeg_bin() -> str:
    global _FFMPEG
    if not _FFMPEG:
        _FFMPEG = _find_tool("JARVIS_FFMPEG", "ffmpeg")
    return _FFMPEG


def ffprobe_bin() -> str:
    global _FFPROBE
    if not _FFPROBE:
        _FFPROBE = _find_tool("JARVIS_FFPROBE", "ffprobe")
    return _FFPROBE


def ffplay_bin() -> str:
    """ffplay, when this machine has it — used for local audio playback.

    It normally sits next to ffmpeg in the same bundle, which is why the same
    locator finds it even though nothing named ffplay is on PATH here.
    """
    global _FFPLAY
    if not _FFPLAY:
        _FFPLAY = _find_tool("JARVIS_FFPLAY", "ffplay")
    return _FFPLAY


# ── What Qt can actually decode ─────────────────────────────────────────────

# Codecs the bundled FFmpeg decodes in software. Deliberately a list of codecs
# we have seen work rather than "everything except AV1": an unknown codec is
# still handed to Qt, and if it fails the HUD now says so instead of freezing.
_QT_SOFTWARE_CODECS = {
    "h264", "hevc", "vp8", "vp9", "mpeg4", "mpeg2video", "mpeg1video",
    "mjpeg", "theora", "prores", "dnxhd", "vc1", "wmv3", "flv1", "h263",
    "rawvideo", "gif",
}

# Codecs that need a transcode when the bundle has no software decoder.
_SOFTWARE_ONLY_GAPS = {"av1"}

_dav1d_cache: bool | None = None


def qt_has_software_av1() -> bool:
    """True when the Qt FFmpeg bundle carries a real software AV1 decoder.

    Checked by looking for libdav1d next to Qt (the normal packaging) and for
    the string "dav1d" inside the bundled libavcodec (a static build). Cached:
    this touches a 15 MB file once per process.
    """
    global _dav1d_cache
    if _dav1d_cache is not None:
        return _dav1d_cache

    _dav1d_cache = False
    try:
        import importlib.util

        spec = importlib.util.find_spec("PyQt6")
        if not spec or not spec.submodule_search_locations:
            return _dav1d_cache
        qt_lib = Path(list(spec.submodule_search_locations)[0]) / "Qt6" / "lib"
        if not qt_lib.is_dir():
            return _dav1d_cache
        if list(qt_lib.glob("libdav1d*")):
            _dav1d_cache = True
            return _dav1d_cache
        for so in qt_lib.glob("libavcodec*"):
            try:
                with so.open("rb") as f:
                    while True:
                        chunk = f.read(1 << 20)
                        if not chunk:
                            break
                        if b"dav1d" in chunk:
                            _dav1d_cache = True
                            return _dav1d_cache
            except Exception:
                continue
    except Exception:
        pass
    return _dav1d_cache


# ── Probing ─────────────────────────────────────────────────────────────────

@dataclass
class MediaInfo:
    video: str = ""
    audio: str = ""
    container: str = ""
    duration: float = 0.0

    def as_dict(self) -> dict:
        return {
            "video": self.video,
            "audio": self.audio,
            "container": self.container,
            "duration": self.duration,
        }


def _run(cmd: list[str], timeout: float) -> tuple[int, str, str]:
    kwargs: dict = {}
    if _IS_WINDOWS:
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        p = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            **kwargs,
        )
        return (
            p.returncode,
            p.stdout.decode("utf-8", "replace"),
            p.stderr.decode("utf-8", "replace"),
        )
    except subprocess.TimeoutExpired:
        return -9, "", "timed out"
    except Exception as e:  # missing binary, permission, ...
        return -1, "", str(e)


def probe(source: str, timeout: float = 20.0) -> MediaInfo | None:
    """What is inside this file/URL, or None when ffprobe cannot say.

    None is not an error for a URL — the network may be slow — and callers
    treat it as "let the player try". For a local file it usually means the
    file is corrupt or not a media file at all.
    """
    tool = ffprobe_bin()
    if not tool:
        return None
    cmd = [
        tool, "-v", "error",
        "-show_entries", "stream=codec_type,codec_name",
        "-show_entries", "format=format_name,duration",
        "-of", "json", source,
    ]
    rc, out, _err = _run(cmd, timeout)
    if rc != 0 or not out.strip():
        return None
    try:
        data = json.loads(out)
    except Exception:
        return None

    info = MediaInfo()
    fmt = data.get("format") or {}
    info.container = str(fmt.get("format_name") or "")
    try:
        info.duration = float(fmt.get("duration") or 0.0)
    except Exception:
        info.duration = 0.0
    for s in data.get("streams") or []:
        kind = s.get("codec_type")
        name = str(s.get("codec_name") or "")
        if kind == "video" and not info.video:
            info.video = name
        elif kind == "audio" and not info.audio:
            info.audio = name
    return info


# ── The decision ────────────────────────────────────────────────────────────

@dataclass
class Plan:
    action: str          # "play" | "convert" | "error"
    reason: str = ""     # human-readable, for logs and the assistant
    codec: str = ""


def plan(source: str) -> Plan:
    """Decide whether Qt can play `source` as-is.

    Only two things cause a conversion:
      * a codec the bundle has no software decoder for (AV1 without dav1d), and
      * a local file that is unreadable, which is an error rather than a
        conversion — pretending otherwise would hide a corrupt download.
    """
    src = str(source or "").strip()
    if not src:
        return Plan("error", "no video was given")

    is_url = src.lower().startswith(("http://", "https://", "rtsp://"))
    if not is_url:
        p = Path(src).expanduser()
        if not p.exists() or not p.is_file():
            return Plan("error", f"'{Path(src).name}' does not exist")
        src = str(p)

    if not ffmpeg_bin() or not ffprobe_bin():
        # No tools: behave exactly as before this module existed.
        return Plan("play", "no ffmpeg on this machine; handing it straight to Qt")

    info = probe(src)
    if info is None:
        if is_url:
            return Plan("play", "could not inspect the stream; letting the player try")
        return Plan("error", "that file could not be read — it may be corrupt")

    codec = info.video.lower()
    if not codec:
        return Plan("error", "that file has no video stream")

    if codec in _SOFTWARE_ONLY_GAPS and not qt_has_software_av1():
        return Plan(
            "convert",
            f"{codec.upper()} has no software decoder in this Qt build; "
            f"converting to H.264",
            codec,
        )

    if codec not in _QT_SOFTWARE_CODECS and codec not in _SOFTWARE_ONLY_GAPS:
        # Unknown, not known-bad: Qt may well handle it. Do not pre-convert.
        return Plan("play", f"{codec} is not in the tested list; letting the player try", codec)

    return Plan("play", "", codec)


# ── Conversion ──────────────────────────────────────────────────────────────

class MediaError(RuntimeError):
    """The file cannot be made playable, with a sentence for the user."""


def _cache_key(src: str, info: MediaInfo | None) -> str:
    h = hashlib.sha1()
    h.update(b"media_prep_v1\x00")
    if src.lower().startswith(("http://", "https://", "rtsp://")):
        h.update(src.encode("utf-8", "replace"))
        return h.hexdigest()
    p = Path(src)
    try:
        st = p.stat()
        h.update(str(p.resolve()).encode("utf-8", "replace"))
        h.update(str(st.st_size).encode())
        h.update(str(st.st_mtime_ns).encode())
    except Exception:
        h.update(src.encode("utf-8", "replace"))
    if info:
        h.update(json.dumps(info.as_dict(), sort_keys=True).encode())
    return h.hexdigest()


def _prune_cache(max_age_s: float = 14 * 24 * 3600) -> None:
    """Old conversions are disposable; a 4K film is not."""
    try:
        now = __import__("time").time()
        for f in cache_dir().glob("*.mp4"):
            try:
                if now - f.stat().st_mtime > max_age_s:
                    f.unlink()
            except Exception:
                pass
    except Exception:
        pass


def prepare(source: str, timeout: float = 3600.0) -> tuple[str, str]:
    """Return (path Qt can play, note). Converts and caches when needed.

    Raises MediaError with a sentence the assistant can say when the file is
    unreadable or the conversion fails. The conversion result is cached by
    (path, size, mtime), so playing the same AV1 file twice converts once.
    """
    src = str(source or "").strip()
    decision = plan(src)
    if decision.action == "error":
        raise MediaError(decision.reason)
    if decision.action == "play":
        return src, ""

    tool = ffmpeg_bin()
    if not tool:
        raise MediaError("this build cannot decode that video and ffmpeg is not installed")

    info = probe(src)
    key = _cache_key(src, info)
    out = cache_dir() / f"{key}.mp4"
    if out.is_file() and out.stat().st_size > 0:
        return str(out), f"using the converted copy ({decision.reason})"

    _prune_cache()

    cmd = [
        tool, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", src,
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k",
        "-sn", "-dn",
        "-movflags", "+faststart",
        str(out),
    ]
    rc, _stdout, stderr = _run(cmd, timeout)
    if rc != 0 or not out.is_file() or out.stat().st_size == 0:
        try:
            out.unlink()
        except Exception:
            pass
        detail = (stderr or "").strip().splitlines()
        detail = detail[-1] if detail else "ffmpeg failed"
        raise MediaError(f"that video could not be converted for playback ({detail})")

    return str(out), decision.reason
