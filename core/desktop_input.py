"""desktop_input.py — pyautogui, imported when it is first USED, not when the
module holding an action is imported.

WHY THIS EXISTS
    `pyautogui` connects to the X server during `import` (through `mouseinfo`).
    On a machine with a broken or missing X cookie that raised

        Xlib.error.DisplayConnectionError: Can't connect to display ":0":
        Authorization required

    at module-import time — which is where `core/action_loader.py` imports every
    `actions/*.py`. The exception is not an ImportError, so it escaped the
    `try: import pyautogui` guard those files had, the module failed to load,
    and the action was not registered at all. One broken display therefore
    removed computer control AND left error noise on every launch, while every
    other action was fine.

    Two things are fixed here:

    1. `LazyPyAutoGUI` defers the import to the first attribute access, so an
       action module always imports cleanly and stays discoverable. The failure,
       if any, now happens where the tool is actually used — and comes back as
       the handler's own error string instead of a startup traceback.
    2. `_load()` installs `core.x11_compat` first, which teaches python-xlib the
       libXau wildcard rule Mutter's XWayland cookie needs.

    Nothing here grants access the session did not already have: `x11_compat`
    only selects a different entry from the auth file `$XAUTHORITY` points at.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()


class LazyPyAutoGUI:
    """A stand-in for the `pyautogui` module.

    Every attribute access (`pyautogui.click`, `pyautogui.size`, ...) resolves
    through `__getattr__`, which imports the real module on first use and then
    hands back the real attribute. `available()` says whether that will work,
    without raising — it is what the actions use in place of the old
    `_PYAUTOGUI` boolean.
    """

    def __init__(self, *, pause: float = 0.05, failsafe: bool = True):
        self._mod = None
        self._error: BaseException | None = None
        self._pause = float(pause)
        self._failsafe = bool(failsafe)

    # ── loading ──────────────────────────────────────────────────────────────
    def _load(self):
        if self._mod is not None:
            return self._mod
        with _lock:
            if self._mod is not None:
                return self._mod
            # Before the import, never after: pyautogui's import connects.
            from core import x11_compat

            x11_compat.install()
            import pyautogui as pg

            pg.FAILSAFE = self._failsafe
            pg.PAUSE = self._pause
            self._mod = pg
            return pg

    def available(self) -> bool:
        """True if the real module can be imported in this environment."""
        try:
            self._load()
            return True
        except BaseException as e:            # noqa: BLE001 — reported, not raised
            self._error = e
            return False

    def fail_reason(self) -> str:
        """A one-line explanation for the activity log. Empty when it works."""
        if self.available():
            return ""
        e = self._error
        return f"{type(e).__name__}: {e}" if e is not None else "unavailable"

    # ── module behaviour ─────────────────────────────────────────────────────
    def __getattr__(self, name: str):
        # Only reached for attributes this class does not define, so `_mod`
        # and friends above are never shadowed.
        return getattr(self._load(), name)

    def __bool__(self) -> bool:
        """`if pyautogui:` used to be true whenever the import succeeded."""
        return self.available()


def unavailable_message(proxy: LazyPyAutoGUI, what: str = "control the desktop") -> str:
    """The sentence an action returns when it cannot reach the display."""
    return (
        f"I can't {what} right now — {proxy.fail_reason()}. "
        "On Wayland, desktop input needs ydotool; on X11 the session needs a "
        "valid $XAUTHORITY (see the X11 line in the startup log)."
    )
