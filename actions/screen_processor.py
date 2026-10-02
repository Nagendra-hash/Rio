"""
Screen & webcam capture for JARVIS vision.

Provides the two capture entry points main.py uses — `_capture_screen()` and
`_capture_camera()` — plus their helpers (compression, camera auto-detection,
config access). main.py grabs a frame here on demand, then injects it into the
main Gemini Live session; there is no separate vision session here.

Screen capture has two paths:
  • X11 — mss (fast, silent).
  • Wayland — the xdg-desktop-portal Screenshot API over D-Bus, spoken with
    jeepney (tiny pure-Python D-Bus client). PyGObject is NOT used: the project
    venv does not ship it, and mss captures pure black frames on Wayland, so
    the portal is the only reliable way to see the screen.
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
import uuid
from collections import deque
from pathlib import Path

import numpy as np

try:
    import cv2
    _CV2 = True
except ImportError:
    _CV2 = False

try:
    import mss
    import mss.tools
    _MSS = True
except ImportError:
    _MSS = False

try:
    import PIL.Image
    _PIL = True
except ImportError:
    _PIL = False


def _base_dir() -> Path:
    # JARVIS_HOME (pendrive mode) — same contract as memory_manager.get_base_dir.
    import os
    home = os.environ.get("JARVIS_HOME", "").strip()
    if home:
        return Path(home).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_BASE        = _base_dir()
_CONFIG_PATH = _BASE / "config" / "api_keys.json"


def _load_config() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_config_key(key: str, value) -> None:
    try:
        cfg = _load_config()
        cfg[key] = value
        _CONFIG_PATH.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
    except Exception as e:
        print(f"[Vision] ⚠️  Could not save config key '{key}': {e}")


def _clear_config_key(key: str) -> None:
    """Remove a config key. Used to un-cache a stale camera index so the next
    capture re-runs detection instead of trusting a value that no longer works
    (device unplugged, or a webcam added later at a different index)."""
    try:
        cfg = _load_config()
        if key in cfg:
            del cfg[key]
            _CONFIG_PATH.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
    except Exception as e:
        print(f"[Vision] ⚠️  Could not clear config key '{key}': {e}")


def _get_os() -> str:
    return _load_config().get("os_system", "windows").lower()


_IMG_MAX_W = 1280
_IMG_MAX_H = 720
_JPEG_Q    = 82


def _compress(img_bytes: bytes, source_format: str = "PNG") -> tuple[bytes, str]:
    if not _PIL:
        return img_bytes, f"image/{source_format.lower()}"

    try:
        img = PIL.Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img.thumbnail((_IMG_MAX_W, _IMG_MAX_H), PIL.Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_JPEG_Q, optimize=False)
        return buf.getvalue(), "image/jpeg"
    except Exception as e:
        print(f"[Vision] ⚠️  Image compress failed: {e}")
        return img_bytes, f"image/{source_format.lower()}"


def _capture_screen_mss() -> tuple[bytes, str]:

    if not _MSS:
        raise RuntimeError("mss is not installed. Run: pip install mss")

    # mss 10.x renamed the factory (mss.mss → mss.MSS) and deprecates the old
    # name; prefer the new one but stay compatible with older installs.
    with (mss.MSS if hasattr(mss, "MSS") else mss.mss)() as sct:
        monitors = sct.monitors          # [0] = all combined, [1..n] = real screens
        target   = monitors[1] if len(monitors) > 1 else monitors[0]
        shot     = sct.grab(target)
        png      = mss.tools.to_png(shot.rgb, shot.size)

    return _compress(png, "PNG")


def _is_wayland() -> bool:
    """True when running in a Wayland session. mss/X11 captures black frames
    there, so detection alone is not enough — see _capture_is_black."""
    return (os.environ.get("WAYLAND_DISPLAY") or "").strip() != ""


def _capture_is_black(png_bytes: bytes) -> bool:
    """Cheap black-frame detector. On Wayland an X11-based capture (mss)
    returns a uniformly black image instead of failing — catch that so the
    caller can retry with the portal instead of sending Gemini a black frame."""
    if not _PIL:
        return False
    try:
        img = PIL.Image.open(io.BytesIO(png_bytes)).convert("L")
        img.thumbnail((320, 320))
        import numpy as _np
        return float(_np.asarray(img).max()) <= 2
    except Exception:
        return False


_PORTAL_DEST  = "org.freedesktop.portal.Desktop"
_PORTAL_PATH  = "/org/freedesktop/portal/desktop"
_PORTAL_IFACE = "org.freedesktop.portal.Screenshot"


def _capture_screen_portal(interactive: bool, timeout_s: int = 120) -> tuple[bytes, str]:
    """Screen capture via the xdg-desktop-portal Screenshot API (the only
    sanctioned way to grab the screen under Wayland).

    Called first with interactive=False (GNOME 42-era backends take the shot
    silently); if that is refused, the caller retries with interactive=True,
    which pops a GNOME consent dialog — the user presses Allow and the capture
    goes through.

    Implementation notes (jeepney, pure Python):
      • The calling process must STAY on the bus while the portal works —
        the portal cancels requests whose caller disconnects. That is exactly
        what broke the naive `gdbus call` approach.
      • The Screenshot method reply only carries the request handle path; the
        actual result arrives later as an org.freedesktop.portal.Request
        Response signal on that handle path. A MatchRule for our own unique
        sender + handle path catches it (rules include sender by default —
        nothing else on the bus can cross into our queue).
      • Values inside the results a{sv} dict are raw Variants; unwrap .value.

    Returns (jpeg_bytes, mime) exactly like the mss path.
    """
    try:
        from jeepney import MatchRule, new_method_call
        from jeepney.io.blocking import Proxy, open_dbus_connection
        from jeepney.wrappers import DBusAddress
        from jeepney.bus_messages import message_bus
    except ImportError:
        raise RuntimeError("jeepney is not installed. Run: pip install jeepney")

    conn = open_dbus_connection()
    try:
        token = f"jarvis_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        # Response signals arrive on /org/freedesktop/portal/desktop/request/
        #   <sender with ':' and '.' replaced by '_'>/<handle_token>
        sender_suffix = conn.unique_name[1:].replace(".", "_")
        req_path = f"/org/freedesktop/portal/desktop/request/{sender_suffix}/{token}"

        rule = MatchRule(
            interface="org.freedesktop.portal.Request",
            path=req_path,
            member="Response",
        )
        Proxy(message_bus, conn).AddMatch(rule)

        portal = DBusAddress(_PORTAL_PATH, bus_name=_PORTAL_DEST, interface=_PORTAL_IFACE)
        # jeepney serialises D-Bus variants ('v') from (signature, value) tuples.
        options = {
            "handle_token": ("s", token),
            "interactive":  ("b", interactive),
        }
        # Fire the request. The reply (request handle path) is of no interest —
        # send without waiting so the Response signal is what we consume.
        conn.send_message(new_method_call(portal, "Screenshot", "sa{sv}", ("", options)))

        queue = deque(maxlen=1)
        try:
            resp = conn.recv_until_filtered(queue, timeout=timeout_s)
        except TimeoutError:
            raise RuntimeError(f"portal gave no response within {timeout_s}s")

        code, results = resp.body
        if code != 0:
            raise RuntimeError(f"portal screenshot refused (code {code})")

        # jeepney parses variants back out as (signature, value) tuples.
        uri_v = results.get("uri")
        uri = uri_v[1] if isinstance(uri_v, tuple) else getattr(uri_v, "value", None)
        if not uri:
            raise RuntimeError("portal response carried no uri")

        path = uri.removeprefix("file://")
        from urllib.parse import unquote
        path = unquote(path)
        try:
            with open(path, "rb") as f:
                png_bytes = f.read()
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

        return _compress(png_bytes, "PNG")
    finally:
        conn.close()


def _capture_screen_shell() -> tuple[bytes, str]:
    """Capture via GNOME Shell's native screenshot API — silent, fast, native
    Wayland (compositor-side, so it always shows the real screen).

    Shell only answers this API for an allowlist of callers. Owning the
    org.gnome.Screenshot well-known name puts us on it: that is the exact
    name the gnome-screenshot tool registers when installed, and the same
    trick Flameshot uses on GNOME Wayland. We claim it for the duration of
    the call and release it afterwards, so real screenshot tools keep working.

    Returns (jpeg_bytes, mime) exactly like the mss path.
    """
    try:
        from jeepney import new_method_call
        from jeepney.io.blocking import open_dbus_connection
        from jeepney.wrappers import DBusAddress
        from jeepney.bus_messages import message_bus
    except ImportError:
        raise RuntimeError("jeepney is not installed. Run: pip install jeepney")

    conn = open_dbus_connection()
    try:
        # ALLOW_REPLACEMENT|REPLACE: if the real gnome-screenshot tool is
        # running, we don't fight it — we just won't get the name, and Shell
        # will refuse us (caller falls back to the portal path).
        r = conn.send_and_get_reply(new_method_call(
            message_bus, "RequestName", "su",
            ("org.gnome.Screenshot", 6)), timeout=10)
        if r.body[0] not in (1, 4):     # not primary owner / already owned
            raise RuntimeError("org.gnome.Screenshot name unavailable")

        out = f"/tmp/jarvis_shot_{os.getpid()}_{uuid.uuid4().hex[:6]}.png"
        shell = DBusAddress("/org/gnome/Shell/Screenshot",
                            bus_name="org.gnome.Shell.Screenshot",
                            interface="org.gnome.Shell.Screenshot")
        # (include_cursor=False, flash=False, absolute output path)
        reply = conn.send_and_get_reply(new_method_call(
            shell, "Screenshot", "bbs", (False, False, out)), timeout=20)
        ok, used = reply.body[0], reply.body[1]
        if not ok:
            raise RuntimeError("gnome-shell screenshot returned failure")
        path = used if isinstance(used, str) and used else out
        try:
            with open(path, "rb") as f:
                png_bytes = f.read()
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        return _compress(png_bytes, "PNG")
    finally:
        conn.close()


def _capture_screen() -> tuple[bytes, str]:
    """Capture the whole screen, OS-appropriately.

    Wayland: mss (X11) returns black frames there, so go straight to the portal
    — silently first, and with a consent dialog if the silent variant is
    refused.
    X11: use mss, but verify the frame is not black — a locked session or a
    compositor change can make even X11 capture black, and the portal is the
    more honest path then.
    """
    if not _is_wayland():
        img, mime = _capture_screen_mss()
        if _capture_is_black(img):
            return _capture_screen_portal(interactive=True)
        return img, mime

    # Wayland: the native Shell API is the best path — silent, instant, and
    # it never shows the black frames mss produces under Wayland. The portal
    # stays as fallback (needed on non-GNOME Wayland, or if Shell ever
    # tightens its allowlist): silently first, then with a consent dialog.
    try:
        return _capture_screen_shell()
    except Exception as e:
        print(f"[Vision] Shell capture unavailable ({e}) — trying portal")

    try:
        return _capture_screen_portal(interactive=False, timeout_s=15)
    except Exception:
        return _capture_screen_portal(interactive=True, timeout_s=120)


def _cv2_backend() -> int:
    """Return the best OpenCV camera backend for the current OS."""
    if not _CV2:
        return 0
    os_name = _get_os()
    if os_name == "windows":
        return cv2.CAP_DSHOW
    if os_name == "mac":
        return cv2.CAP_AVFOUNDATION
    return cv2.CAP_ANY


def _probe_camera(index: int, backend: int, warmup: int = 5) -> bool:

    if not _CV2:
        return False
    cap = cv2.VideoCapture(index, backend)
    if not cap.isOpened():
        cap.release()
        return False
    for _ in range(warmup):
        cap.read()
    ret, frame = cap.read()
    cap.release()
    if not ret or frame is None:
        return False
    return bool(np.mean(frame) > 8)


def _detect_camera_index() -> int:

    backend = _cv2_backend()
    print("[Vision] 🔍 Auto-detecting camera...")
    for idx in range(6):
        if _probe_camera(idx, backend):
            print(f"[Vision] ✅ Camera found at index {idx}")
            _save_config_key("camera_index", idx)
            return idx
        print(f"[Vision] ⚠️  Camera index {idx}: no usable frame")

    print("[Vision] ⚠️  No camera found — clearing cached index so a later hot-plug is detected")
    _clear_config_key("camera_index")
    return 0


def _get_camera_index() -> int:
    cfg = _load_config()
    if "camera_index" in cfg:
        return int(cfg["camera_index"])
    return _detect_camera_index()


def _capture_camera() -> tuple[bytes, str]:
    if not _CV2:
        raise RuntimeError("OpenCV (cv2) is not installed. Run: pip install opencv-python")

    index   = _get_camera_index()
    backend = _cv2_backend()
    cap     = cv2.VideoCapture(index, backend)

    if not cap.isOpened():
        # The cached index no longer works — the device was unplugged, or a
        # webcam was added later at a different index. Re-scan once so a
        # hot-plugged camera is picked up without a restart.
        cap.release()
        index = _detect_camera_index()
        cap   = cv2.VideoCapture(index, backend)

    if not cap.isOpened():
        cap.release()
        # The generic message would have the user checking a plug that does
        # not exist: an internal webcam cannot be replugged. When the machine
        # is KNOWN to have one, say the actual diagnosis — camera wedged at
        # the USB level, fixable only by a power drain — instead.
        try:
            from actions.camera_watch import _camera_status_text
            hint = _camera_status_text()
            if "No webcam is available" in hint:      # wedged / driver diagnosis
                raise RuntimeError(hint)
        except RuntimeError:
            raise
        except Exception:
            pass
        raise RuntimeError("No camera could be opened (cached index + full re-scan failed). Is a webcam connected?")

    for _ in range(10):
        cap.read()

    ret, frame = cap.read()
    cap.release()

    if not ret or frame is None:
        raise RuntimeError("Camera returned no frame.")

    if _PIL:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = PIL.Image.fromarray(rgb)
        img.thumbnail((_IMG_MAX_W, _IMG_MAX_H), PIL.Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_JPEG_Q)
        return buf.getvalue(), "image/jpeg"

    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, _JPEG_Q])
    return buf.tobytes(), "image/jpeg"
