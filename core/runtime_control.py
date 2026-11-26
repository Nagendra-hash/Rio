"""
core/runtime_control.py — the small bridge between file-backed actions and the
running JARVIS session.

WHY THIS EXISTS
    Every actions/*.py file is discovered by core.action_loader *inside*
    JarvisLive. An action therefore runs in the same process as the assistant,
    but it cannot `import main` to reach the live object: importing main.py
    would execute the application a second time.

    Some actions genuinely need one thing from the live session — "the settings
    changed, rebuild the conversation so the new engine takes over". This module
    is the one place JarvisLive registers itself for that, the same way a panel
    would emit a Qt signal. Keeping it to one weak reference and one thread-safe
    call keeps the dependency pointing in the safe direction (action -> bridge)
    instead of the unsafe one (action -> main).

The reconnect is deliberately delayed by a couple of seconds: the action's
answer has to be spoken by the CURRENT engine before the session it is running
inside is torn down. The delay is re-armed rather than stacked if a second
switch lands while the first is still pending.
"""

from __future__ import annotations

import logging
import threading
import weakref
from typing import Any

log = logging.getLogger(__name__)

_lock = threading.RLock()
_live_ref: weakref.ref | None = None
_pending: threading.Timer | None = None


def register_live(controller: Any) -> None:
    """Called once by JarvisLive.__init__ (or main() right after it exists)."""
    global _live_ref
    if controller is None:
        return
    with _lock:
        try:
            _live_ref = weakref.ref(controller)
        except TypeError:
            # Not weak-referenceable (tests, exotic proxies): keep a plain
            # reference so the bridge still works. The process holds the live
            # object for its whole life anyway.
            _live_ref = lambda: controller  # type: ignore[assignment]


def unregister_live() -> None:
    global _live_ref
    with _lock:
        _live_ref = None


def live_controller() -> Any:
    """The registered JarvisLive, or None when the app is not running."""
    ref = _live_ref
    if ref is None:
        return None
    try:
        return ref()
    except Exception:
        return None


def is_available() -> bool:
    return live_controller() is not None


def _fire(reason: str, keep_context: bool) -> None:
    live = live_controller()
    if live is None:
        return
    try:
        live.request_reconnect(keep_context=keep_context, reason=reason)
    except Exception as e:  # a closing app must never turn this into a crash
        log.warning("engine reconnect request failed: %s", e)


def request_engine_reconnect(
    reason: str = "model switch",
    keep_context: bool = True,
    delay_s: float = 2.0,
) -> bool:
    """Ask the live session to rebuild itself. Thread-safe, never raises.

    Returns False when no live session is registered (headless run, tests) —
    the caller can still say the setting was saved and will apply on next
    start. `delay_s` lets the current engine finish speaking its confirmation.
    """
    global _pending
    live = live_controller()
    if live is None:
        return False

    with _lock:
        if _pending is not None:
            try:
                _pending.cancel()
            except Exception:
                pass
            _pending = None
        if delay_s and delay_s > 0:
            t = threading.Timer(float(delay_s), _fire, args=(reason, keep_context))
            t.daemon = True
            _pending = t
            t.start()
        else:
            _fire(reason, keep_context)
    return True
