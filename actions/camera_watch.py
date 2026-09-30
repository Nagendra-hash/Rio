"""
Webcam connect/disconnect watcher for JARVIS vision.

`CameraWatcher` watches /dev/video* and, when a webcam is plugged in or
unplugged, produces short human-readable event messages ("Webcam connected:
USB Camera (/dev/video0)"). main.py runs it as a background task and speaks
those events through the Live session — exactly like system-monitor and
background-monitor alerts — so the user learns the moment vision becomes
available, without touching a terminal.

Why polling instead of pyudev: a 0.25 s poll of /dev/video* costs one glob
call and detects hot-plug within about a second — fast enough to feel
instantaneous — while needing no extra dependency and behaving identically
on every Linux setup. Events are debounced: when a webcam appears the kernel
often exposes several nodes in quick succession (capture + metadata), so we
wait for the device list to stay stable before reporting.

Also self-describes a `camera_status` TOOL (same shape as every other
bundled action) so the user can ask "do I have a webcam?" by voice — and
when no camera exists on a machine that is KNOWN to have an internal one,
it diagnoses why (wedged USB controller vs. driver problem) and says the
fix that actually clears it.
"""
from __future__ import annotations

import glob
import re
import subprocess
import threading
import time
from pathlib import Path


def _friendly_name(node: str) -> str:
    """Best-effort human name for a /dev/videoX node (never raises)."""
    base = Path(node).name
    for candidate in (
        f"/sys/class/video4linux/{base}/device/name",
        f"/sys/class/video4linux/{base}/name",
    ):
        try:
            txt = Path(candidate).read_text(encoding="utf-8", errors="ignore").strip()
            if txt:
                return txt.splitlines()[0][:60]
        except Exception:
            pass
    return "camera"


def _snapshot(dev_root: str = "/dev") -> dict[str, str]:
    """Current cameras as {device_node: friendly_name}. Never raises."""
    try:
        return {p: _friendly_name(p)
                for p in sorted(glob.glob(f"{dev_root}/video*"))}
    except Exception:
        return {}


class CameraWatcher:
    """
    Detects webcam connect/disconnect by watching /dev/video*.

    `watch_debounced()` blocks until the device list changes AND then stays
    stable for `quiet` seconds, then returns the event messages. Runs in a
    worker thread (main.py calls it via asyncio.to_thread), so it must never
    touch Qt or asyncio — it only returns strings.
    """

    def __init__(self, poll_interval: float = 0.25, dev_root: str = "/dev",
                 logger=print):
        """
        dev_root is injectable so the watcher can be integration-tested
        against a fake device tree (tests pass a temp dir); production uses
        the default /dev, and _snapshot() globs dev_root/video* there.
        """
        self._poll = poll_interval
        self._dev_root = dev_root
        self._logger = logger
        self._stop = threading.Event()
        self._baseline = _snapshot(dev_root)

    # ── lifecycle ────────────────────────────────────────────────────────────
    def stop(self) -> None:
        """Unblock a waiting watch_debounced() call (returns [])."""
        self._stop.set()

    def resume(self) -> None:
        self._stop.clear()

    # ── the blocking wait ────────────────────────────────────────────────────
    def watch_debounced(self, quiet: float = 2.0) -> list[str] | None:
        """
        Block until a change occurs and settles. Returns the accumulated event
        messages, or None if stopped — the caller simply calls again.

        A webcam often exposes several device nodes in quick succession
        (capture + metadata), so after the first change we keep extending the
        event list until the device list has stayed stable for `quiet` seconds.
        """
        while True:
            if self._stop.is_set():
                return None
            time.sleep(self._poll)

            current = _snapshot(self._dev_root)
            if current == self._baseline:
                continue                      # nothing happening — keep waiting

            events = self.diff_messages(self._baseline, current)
            self._baseline = current

            # Change seen — hold until the list stops flapping.
            deadline = time.monotonic() + quiet
            while time.monotonic() < deadline:
                if self._stop.is_set():
                    return None
                time.sleep(self._poll)
                newest = _snapshot(self._dev_root)
                if newest != current:
                    events.extend(self.diff_messages(current, newest))
                    current = newest
                    self._baseline = newest
                    deadline = time.monotonic() + quiet   # still settling — wait more
            return events

    # ── helpers ──────────────────────────────────────────────────────────────
    def diff_messages(self, before: dict[str, str], after: dict[str, str]) -> list[str]:
        msgs: list[str] = []
        for node, name in after.items():
            if node not in before:
                msgs.append(f"Webcam connected: {name} ({node})")
        for node, name in before.items():
            if node not in after:
                msgs.append(f"Webcam disconnected: {name} ({node})")
        return msgs


def list_cameras(dev_root: str = "/dev") -> dict[str, str]:
    """Public helper: current cameras as {device_node: friendly_name}."""
    return _snapshot(dev_root)


# ── Failure diagnosis: the wedged-webcam case ─────────────────────────────────
# Internal UVC cameras (e.g. the Inspiron 3542's Integrated_Webcam_HD) can wedge
# at the USB level while in use: kernel logs "device descriptor read/64, error
# -32" until it gives up. The camera then answers electrical presence but
# completes no transaction — no /dev/video node, nothing in lsusb, and NO sysfs
# entry — so "camera wedged" and "no camera installed" look identical to a glob
# of /dev/video*. A USB-controller unbind/bind does NOT clear it (verified live:
# the re-probe fails with the same -32); only removing power does. These helpers
# let camera_status tell the cases apart and give the fix that actually works.
_INTERNAL_CAM_VIDPID = "0c45:670b"   # Integrated_Webcam_HD (this Dell)

# Product strings internal camera modules report over USB, by vendor. The
# journal grep is the 'this machine HAS an internal webcam' signal — kept to
# internal-module names so a once-plugged EXTERNAL cam can never make a
# desktop-without-camera report a wedged internal module.
_INTERNAL_CAM_NAMES = re.compile(
    r"Integrated_Webcam"        # Dell
    r"|Integrated Camera"       # Lenovo
    r"|HP TrueVision"           # HP
    r"|WebCam_HD|HD WebCam"     # generic laptop modules
)

_MSG_HALF_ENUMERATED = (
    "The internal webcam is detected on USB but produced no video device — "
    "try: sudo modprobe uvcvideo, and reboot if that doesn't help."
)
_MSG_WEDGED = (
    "Your internal webcam has dropped off the USB bus — the laptop keeps it "
    "powered, so a normal reboot may not clear it. Do a full shutdown, unplug "
    "the charger, hold the power button for 30 seconds, then boot. If it keeps "
    "dropping, the camera module or its cable may be failing — an external USB "
    "webcam works as a workaround."
)

_KNOWN_INTERNAL_CAM: bool | None = None   # computed once per process


def _usb_controllers_present(sys_root: str) -> bool:
    """True when at least one root USB hub (host controller) is visible.
    VMs often hide USB topology entirely — then there is nothing to diagnose."""
    return bool(glob.glob(f"{sys_root}/bus/usb/devices/usb*"))


def _find_usb_device(vid_pid: str, sys_root: str) -> bool:
    """True when a USB device with this 'vid:pid' is (at least half-)enumerated.
    Reads the kernel-generated modalias of every device on the bus; matches
    case-insensitively because modalias upper-cases both fields."""
    want = vid_pid.lower()
    for mpath in glob.glob(f"{sys_root}/bus/usb/devices/*/modalias"):
        try:
            m = re.match(r"usb:v([0-9A-Fa-f]{4})p([0-9A-Fa-f]{4})",
                         Path(mpath).read_text(errors="ignore").strip())
        except Exception:
            continue
        if m and f"{m.group(1).lower()}:{m.group(2).lower()}" == want:
            return True
    return False


def _known_internal_camera() -> bool:
    """True when the kernel journal shows this machine has an internal webcam —
    the distinction between 'wedged controller' and 'never had one'. Cached:
    the journal is scanned at most once per process; any failure means False
    (generic advice is always safe)."""
    global _KNOWN_INTERNAL_CAM
    if _KNOWN_INTERNAL_CAM is None:
        _KNOWN_INTERNAL_CAM = False
        try:
            out = subprocess.run(
                ["journalctl", "-k", "--no-pager"],
                capture_output=True, text=True, timeout=8)
            _KNOWN_INTERNAL_CAM = bool(_INTERNAL_CAM_NAMES.search(out.stdout))
        except Exception:
            pass
    return _KNOWN_INTERNAL_CAM


def diagnose_camera(dev_root: str = "/dev", sys_root: str = "/sys",
                    known_internal: bool | None = None) -> str:
    """Best-effort explanation for 'no cameras found'. Returns '' when cameras
    exist, when there is nothing to diagnose (no internal webcam on record),
    or when the platform hides what we need (VMs). Never raises.

    dev_root/sys_root are injectable for offline tests; known_internal can be
    forced to skip the journal scan in tests."""
    try:
        if _snapshot(dev_root):
            return ""                              # camera(s) present — all good
        if known_internal is None:
            if not _usb_controllers_present(sys_root):
                return ""
            known_internal = _known_internal_camera()
        if not known_internal:
            return ""
        if _find_usb_device(_INTERNAL_CAM_VIDPID, sys_root):
            return _MSG_HALF_ENUMERATED            # enumerated, no driver node
        return _MSG_WEDGED                         # controller alive, camera gone
    except Exception:
        return ""


# ── Voice tool: camera_status ─────────────────────────────────────────────────

def _camera_status_text(dev_root: str = "/dev", sys_root: str = "/sys",
                        known_internal: bool | None = None) -> str:
    cams = _snapshot(dev_root)
    if cams:
        lines = [f"{i + 1}. {name} ({node})" for i, (node, name) in enumerate(cams.items())]
        return "Connected camera(s):\n" + "\n".join(lines)
    hint = diagnose_camera(dev_root, sys_root, known_internal)
    if hint:
        return f"No webcam is available right now. {hint}"
    return "No webcam is connected right now. Plug one in and I'll detect it automatically."


def camera_status(parameters: dict = None, **_) -> str:
    return _camera_status_text()


TOOL = {
    "name": "camera_status",
    "description": (
        "Report which webcams are currently connected to this computer. "
        "Use when the user asks about camera availability, whether a webcam "
        "is plugged in, or why the camera isn't working. Diagnoses a webcam "
        "that dropped off the USB bus (says the power-drain fix) instead of "
        "just reporting 'no camera found'."
    ),
    "parameters": {"type": "OBJECT", "properties": {}},
    "handler": camera_status,
}
