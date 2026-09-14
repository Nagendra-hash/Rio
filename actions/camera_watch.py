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
bundled action) so the user can ask "do I have a webcam?" by voice.
"""
from __future__ import annotations

import glob
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


# ── Voice tool: camera_status ─────────────────────────────────────────────────

def camera_status(parameters: dict = None, **_) -> str:
    cams = _snapshot()
    if not cams:
        return "No webcam is connected right now. Plug one in and I'll detect it automatically."
    lines = [f"{i + 1}. {name} ({node})" for i, (node, name) in enumerate(cams.items())]
    return f"Connected camera(s):\n" + "\n".join(lines)


TOOL = {
    "name": "camera_status",
    "description": (
        "Report which webcams are currently connected to this computer. "
        "Use when the user asks about camera availability, whether a webcam "
        "is plugged in, or why the camera isn't working."
    ),
    "parameters": {"type": "OBJECT", "properties": {}},
    "handler": camera_status,
}
