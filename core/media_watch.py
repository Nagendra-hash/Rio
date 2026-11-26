"""Watching for sound the assistant did not make.

WHY THIS EXISTS
    The HUD closes the microphone for a video or a song IT is playing — its own
    `_sync_mic_for_video` rule. Everything else makes the same noise with no
    way for the app to know: Spotify's desktop app, a browser tab, VLC, a video
    that fell back to `webbrowser.open`, music on the RIO dashboard. The echo
    guard in core/echo.py cannot help there, because it subtracts *our own*
    output and this is somebody else's. So the assistant answers the film, the
    song, or the advert between them.

    This is the missing half: a small watcher over the operating system's media
    sessions that reports when another application is actually playing.

HOW
    It asks the same source that "what is playing" is answered from — MPRIS on
    Linux (jeepney, in-process, no subprocess), the System Media Transport
    Controls on Windows, AppleScript on macOS — through actions/music_player.py,
    which already carries the three probes and their fallbacks. It also asks the
    dashboard's media bus, because a song playing from the phone's browser is
    sound in the same room.

WHAT IT IS NOT
    Not a level test on the microphone. core/echo.py explains at length why a
    loudness threshold is the wrong instrument; this does not look at the
    microphone at all.
    It does not mute anything either. It reports a state change and the HUD
    applies the rule it already applies to its own video, so a microphone the
    user muted stays muted, and pressing F4 during playback hands the decision
    back to them for good.

COST
    One D-Bus round trip every two seconds on Linux (a few milliseconds, in
    process). On Windows and macOS each reading is a subprocess, so the interval
    is longer there. Nothing runs while `stop()` has been called.
"""
from __future__ import annotations

import platform
import threading

_POLL_LINUX = 2.0
_POLL_OTHER = 6.0
_PLAYING = "playing"


def default_interval() -> float:
    """How often to ask, in seconds. Windows/macOS pay a subprocess per read."""
    return _POLL_LINUX if platform.system() == "Linux" else _POLL_OTHER


def _dashboard_playing() -> tuple[bool, str] | None:
    """Music pushed to the RIO dashboard's own player, if the bus is reachable.

    Kept for callers that want it; `query_media` does not use it (see there).
    """
    try:
        from dashboard import media_bus
        st = media_bus.state() or {}
    except Exception:
        return None
    status = str(st.get("status", "")).strip().lower()
    if status == _PLAYING:
        return True, "the RIO dashboard"
    return False, ""


def query_media() -> tuple[bool, str] | None:
    """(playing, who) from the OS media sessions, or None when nobody answered.

    An empty answer is not the same as "nothing is playing": a machine with no
    media session to ask (no D-Bus, no playerctl, no permission) must not be
    read as silence, or the microphone would never be given back.

    The dashboard's media bus is deliberately NOT asked. That card exists for
    the phone, so its sound usually comes out of a phone in someone's hand,
    which this microphone does not hear — muting for it would lock the user out
    of their assistant for no reason. When the same card is played by a browser
    on this machine, the browser registers a media session of its own and the
    OS probe below reports it anyway.
    """
    sessions = None
    try:
        # The HUD's own Qt player registers no media session, so anything found
        # here belongs to another application. In-HUD music is the HUD's own
        # business and is muted by its own rule, without a second owner.
        from actions.music_player import _query_media_sessions
        sessions = _query_media_sessions()
    except Exception:
        return None

    for s in sessions or []:
        if str(s.get("status", "")).strip().lower() != _PLAYING:
            continue
        player = str(s.get("player") or "").strip()
        title = str(s.get("title") or "").strip()
        who = f"{player.title()}" if player else "Another application"
        if title:
            who += f" — {title[:60]}"
        return True, who

    return False, ""


class MediaWatch:
    """Polls the OS media sessions and reports transitions, never levels."""

    def __init__(self, on_change, query=None, interval: float | None = None):
        self._on_change = on_change
        self._query = query or query_media
        self._interval = float(interval if interval is not None else default_interval())
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._reported = False

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="media-watch",
                                        daemon=True)
        self._thread.start()

    def stop(self, wait: float = 1.5) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=wait)
        self._thread = None

    # ── one reading ──────────────────────────────────────────────────────────
    def poll_once(self) -> bool:
        """Ask once. True when the reported state changed (a callback ran)."""
        try:
            answer = self._query()
        except Exception:
            return False
        if answer is None:            # nobody could be asked — say nothing
            return False
        playing, who = answer
        playing = bool(playing)
        if playing == self._reported:
            return False
        self._reported = playing
        try:
            self._on_change(playing, who if playing else "")
        except Exception:
            pass
        return True

    def _loop(self) -> None:
        self.poll_once()              # a song already playing at startup counts
        while not self._stop.wait(self._interval):
            self.poll_once()
