"""camera_feed.py — shared camera frame bus for the RIO dashboard.

Why a bus: the webcam is a single-consumer device. The RIO Home tab wants a
live preview (continuous frames) while JARVIS's own vision path
(actions/screen_processor.py) opens the camera for short captures (palm
reading, screen_process angle='camera'). Two independent OpenCV captures on
one V4L2 device fight each other — the second open fails with EBUSY and the
palm reading dies. This module makes ONE owner of the device at a time:

- The dashboard's MJPEG endpoint (rio_api.py) streams from `latest_jpeg()`
  and calls ensure_stream()/release() as browser clients come and go; the
  capture thread here owns the device while a preview is up.
- screen_processor calls `is_streaming()` first: when the bus owns the
  camera, it reuses `latest_jpeg()` instead of opening its own capture.
- When JARVIS captures normally (bus idle), screen_processor publishes its
  encoded JPEG with `publish()` — the Home tab's camera card shows the real
  frame JARVIS just saw (the "camera opened → visible on the UI" flow).

Everything is thread-safe and best-effort: a camera that cannot open reports
honestly through `state`, never a fake frame.
"""
from __future__ import annotations

import glob
import io
import json
import threading
import time
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _BASE / "config" / "api_keys.json"

_JPEG_Q = 82
_STREAM_FPS = 15.0        # preview + vision both read this; 640x480 MJPG is light
_STALE_S = 2.5            # a published frame older than this = camera idle

_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop_flag = threading.Event()
_clients = 0              # MJPEG readers; the device is held while > 0

_last_jpeg: bytes | None = None
_last_ts = 0.0
_source = "idle"          # idle | stream | external
_error = ""


def _camera_index() -> int:
    """The cached auto-detected index (screen_processor persists it)."""
    try:
        cfg = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        return int(cfg.get("camera_index") or 0)
    except Exception:
        return 0


def _encode_jpeg(frame_bgr) -> bytes | None:
    try:
        import cv2
        ok, buf = cv2.imencode(".jpg", frame_bgr,
                               [int(cv2.IMWRITE_JPEG_QUALITY), _JPEG_Q])
        return buf.tobytes() if ok else None
    except Exception:
        return None


def publish(jpeg: bytes) -> None:
    """An external capture (screen_processor) contributed a frame. Marks the
    camera active for _STALE_S so the UI can show what JARVIS sees."""
    global _last_jpeg, _last_ts, _source
    with _lock:
        _last_jpeg = jpeg
        _last_ts = time.time()
        _source = "external"


def is_streaming() -> bool:
    """True while the bus's own capture thread holds the device."""
    t = _thread
    return bool(t and t.is_alive() and not _stop_flag.is_set())


def latest_jpeg(max_age_s: float = _STALE_S) -> bytes | None:
    with _lock:
        if _last_jpeg and (time.time() - _last_ts) <= max_age_s:
            return _last_jpeg
    return None


def state() -> dict:
    with _lock:
        fresh = bool(_last_jpeg and (time.time() - _last_ts) <= _STALE_S)
        out = {
            "streaming": is_streaming(),
            "active": bool(is_streaming() or fresh),
            "source": _source if (is_streaming() or fresh) else "idle",
            "clients": _clients,
            "error": _error if not fresh and not is_streaming() else "",
            "has_frame": fresh,
        }
    # No attempt made yet (fresh boot, nobody pressed the button)? Say WHY the
    # camera is unavailable anyway — the card must be honest at a glance, not
    # only after the user clicks and waits. Passive probe, cached below.
    if not out["streaming"] and not out["active"] and not out["error"]:
        out["error"] = _absence_error()
    return out


def ensure_stream() -> None:
    """Start the capture thread (idempotent). Used by the MJPEG endpoint when
    a browser client connects."""
    global _thread, _error
    with _lock:
        _stop_flag.clear()
        if is_streaming():
            return
        _error = ""
        _thread = threading.Thread(target=_run, daemon=True,
                                   name="rio-camera-stream")
        _thread.start()


def release() -> None:
    """Stop the capture thread and free the device (last MJPEG client left,
    or the UI toggled the camera off)."""
    global _thread
    with _lock:
        _stop_flag.set()
    t = _thread
    if t and t.is_alive():
        t.join(timeout=3.0)
    with _lock:
        _thread = None
        _source = "idle"


def client_arrived() -> None:
    global _clients
    with _lock:
        _clients += 1
    ensure_stream()


def client_left() -> None:
    global _clients
    with _lock:
        _clients = max(0, _clients - 1)
    if _clients == 0:
        release()


_presence_cache = {"ts": 0.0, "err": ""}


def _absence_error() -> str:
    """The honest reason the camera is unavailable when NO attempt has been
    made yet — so the card can say it at a glance, before the user clicks.
    Probed passively (no device open) and cached 10 s: the failing case runs
    journalctl, and Home polls this often."""
    now = time.time()
    if _presence_cache["err"] and (now - _presence_cache["ts"]) < 10.0:
        return _presence_cache["err"]
    err = ""
    if not glob.glob("/dev/video*"):
        err = ("webcam is failing USB enumeration (hardware) — power the "
               "laptop off fully, then check the camera cable or the BIOS "
               "camera setting" if _usb_enum_failing() else
               "no camera device found — the webcam is not on the USB bus")
    _presence_cache.update(ts=now, err=err)
    return err


def _usb_enum_failing() -> bool:
    """True while the kernel is failing to enumerate a USB device ("device
    not accepting address" / "descriptor read … error -32" in the ring
    buffer). For this laptop's webcam that means the camera is physically
    flapping on the port — a reboot was already tried (2026-10-17 00:55,
    after an earlier drop at 00:24) and changed nothing, so telling the user
    to reboot again would be a lie. The honest report is the hardware one."""
    try:
        import subprocess
        out = subprocess.run(["journalctl", "-k", "-n", "120", "--no-pager"],
                             capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return False
    for line in out.splitlines()[-60:]:
        low = line.lower()
        if "usb" in low and ("not accepting address" in low
                             or "descriptor read" in low
                             or "error -32" in low):
            return True
    return False


def _run() -> None:
    """The capture loop — owns the device while streaming. Exits promptly on
    stop_flag; a device that will not open is reported and retried a few
    times before giving up (the UI shows the honest error)."""
    global _last_jpeg, _last_ts, _source, _error
    import cv2                                   # lazy — optional dependency

    idx = _camera_index()
    # If the cached index is gone, the camera may simply have enumerated at a
    # different index (hot-plug, or a first run that never auto-detected).
    # Ask screen_processor to scan and persist the right one before declaring
    # the device absent — otherwise a preview would fail forever on index 0.
    if not Path(f"/dev/video{idx}").exists():
        try:
            from actions.screen_processor import _detect_camera_index
            found = _detect_camera_index()
            if found != idx and Path(f"/dev/video{found}").exists():
                idx = found
        except Exception:
            pass
    # Presence first: a missing /dev/video node is a hard fact, and the EBUSY
    # wording ("JARVIS's own capture may hold it") would mislead. The card
    # must name the real problem: not connected, or failing to enumerate.
    if not Path(f"/dev/video{idx}").exists():
        nodes = sorted(glob.glob("/dev/video*"))
        failing = (not nodes) and _usb_enum_failing()
        with _lock:
            if nodes:
                _error = (f"/dev/video{idx} not found — camera devices "
                          f"present: {', '.join(nodes)}")
            elif failing:
                _error = ("webcam is failing USB enumeration (hardware) — "
                          "power the laptop off fully, then check the camera "
                          "cable or the BIOS camera setting")
            else:
                _error = ("no camera device found — the webcam is not on "
                          "the USB bus")
        return
    cap = None
    try:
        # brief retries absorb EBUSY from a capture that is just finishing;
        # CAP_V4L2 on Linux keeps each miss to one warning — a bare index
        # makes OpenCV probe every backend, printing a V4L2 warning, a full
        # FFMPEG exception and an Orbbec error per attempt, which read like
        # a crash when the webcam had dropped off the USB bus (2026-10-16).
        for attempt in range(3):
            cap = (cv2.VideoCapture(idx, cv2.CAP_V4L2)
                   if hasattr(cv2, "CAP_V4L2") else cv2.VideoCapture(idx))
            if cap.isOpened():
                break
            cap.release()
            cap = None
            if _stop_flag.wait(0.6):
                return
        if cap is None or not cap.isOpened():
            # The cached index exists but will not open as a capture node —
            # a UVC webcam commonly exposes /dev/video0 (capture) and
            # /dev/video1 (metadata only), and a stale cache of "1" fails
            # here forever with "can't open camera by index". Rescan for the
            # real capture node (this also persists the corrected index), then
            # open it once.
            try:
                from actions.screen_processor import _detect_camera_index
                found = _detect_camera_index()
            except Exception:
                found = idx
            if found != idx:
                idx = found
                for _attempt in range(2):
                    cap = (cv2.VideoCapture(idx, cv2.CAP_V4L2)
                           if hasattr(cv2, "CAP_V4L2") else cv2.VideoCapture(idx))
                    if cap.isOpened():
                        break
                    cap.release()
                    cap = None
                    if _stop_flag.wait(0.4):
                        return
        if cap is None or not cap.isOpened():
            with _lock:
                _error = (f"/dev/video{idx} exists but would not open — the "
                          f"device is busy (another capture holds it) or "
                          f"faulty")
            return

        # Modest MJPG stream. This laptop's webcam sits behind an EHCI hub
        # that resets the device when asked for a large uncompressed stream
        # (kernel: "Failed to set UVC probe control -71" then "USB
        # disconnect"). 640x480 MJPG at 15 fps is stable, and it is plenty
        # for both the HUD preview and a 43 KB vision frame.
        try:
            if hasattr(cv2, "VideoWriter_fourcc"):
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            cap.set(cv2.CAP_PROP_FPS, _STREAM_FPS)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass

        interval = 1.0 / _STREAM_FPS
        misses = 0
        while not _stop_flag.is_set():
            t0 = time.time()
            ok, frame = cap.read()
            if not ok or frame is None:
                # A camera waking from USB autosuspend often drops the first
                # few reads. Retry briefly instead of declaring it dead and
                # forcing the caller to reopen (which is what flapped the
                # device); only a sustained failure ends the stream.
                misses += 1
                if misses <= 15:
                    if _stop_flag.wait(0.1):
                        break
                    continue
                with _lock:
                    _error = "camera returned no frame"
                break
            misses = 0
            jpeg = _encode_jpeg(frame)
            if jpeg:
                with _lock:
                    _last_jpeg = jpeg
                    _last_ts = time.time()
                    _source = "stream"
                    _error = ""
            wait = interval - (time.time() - t0)
            if wait > 0 and _stop_flag.wait(wait):
                break
    except Exception as exc:
        with _lock:
            _error = f"{type(exc).__name__}: {exc}"[:160]
    finally:
        try:
            if cap is not None:
                cap.release()
        except Exception:
            pass
        with _lock:
            _source = "idle"
