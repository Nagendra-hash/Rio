"""
gui/chromium_player.py — External Chromium manager for YouTube playback.

Why not embedded? This machine has PyQt6 QtMultimedia but NOT QtWebEngine —
QWebEngineView would crash at import time. An external Playwright-driven
Chromium window sidesteps that entirely while giving us native YouTube
playback (no AV1/FFmpeg decode path inside RIO).

Architecture
------------
  * One persistent Chromium browser process per RIO session (launched lazily
    on first YouTube request, reused for subsequent requests).
  * All Chromium operations run on a single dedicated thread to avoid
    Playwright sync API thread-safety issues.
  * Commands are sent via a thread-safe queue and results returned via futures.
  * Each "video" is one tab/page. The controller manages page lifecycle.

Routing decision
-----------------
  actions/video_player.py checks this module before falling back to yt-dlp:

    from gui.chromium_player import resolve_youtube_for_chromium

    url, title, error = resolve_youtube_for_chromium(source)

  If url is non-empty, video_player.py opens it in Chromium via the controller.
  If it returns "", video_player.py falls back to the old yt-dlp path (preserving
  backward compatibility when Playwright/Chromium is unavailable).

Voice integration
-----------------
  Gemini must never hear the Chromium window. Chromium runs fully headless
  (no audio capture, no microphone access). If the user asks for sound, they
  listen through the system speakers — RIO does not know about it, and the
  mic callback sees only what the physical microphone picks up. No echo
  cancellation problem, no feedback loop.
"""
from __future__ import annotations

import queue
import re
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote_plus

# Lazy-load playwright to avoid ImportError at module level.
_playwright: Any = None
_browser = None          # single persistent Browser across the session
_pw_ctx = None           # persistent playwright context manager
_lock = threading.Lock()

_YT_URL = re.compile(r"(youtube\.com/|youtu\.be/)", re.I)

# Cache the last resolved result so repeated calls with the same query return
# instantly without another network round-trip.
_cache: dict[str, tuple[str, str]] = {}
_CACHE_TTL_S = 600.0
_cache_ts: dict[str, float] = {}


# ── Command definitions ──────────────────────────────────────────────────────

@dataclass
class _Command:
    """A command to be executed on the Chromium thread."""
    kind: str          # "open", "stop", "pause", "mute", "seek", "close", "fullscreen", "debug"
    future: Future     # future to set with result
    url: str = ""
    title: str = ""
    paused: bool = False
    muted: bool = False
    seconds: float = 0.0
    relative: bool = True
    fullscreen: bool = False


# ── Chromium controller thread ───────────────────────────────────────────────

class _ChromiumController:
    """Runs on a dedicated thread, owns all Playwright objects."""

    def __init__(self):
        self._cmd_queue: queue.Queue[_Command] = queue.Queue()
        self._page = None
        self._tab_active = False
        self._last_url = ""
        self._error_msg = ""
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="chromium-controller")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        # Send a sentinel to unblock the queue
        self._cmd_queue.put(None)  # type: ignore
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._thread = None

    def _run(self) -> None:
        """Main loop - processes commands on this thread."""
        global _browser, _pw_ctx
        while not self._stop_event.is_set():
            try:
                cmd = self._cmd_queue.get(timeout=0.5)
                if cmd is None:  # sentinel
                    break
                self._handle(cmd)
            except queue.Empty:
                continue
            except Exception as e:
                print(f"[ChromiumController] Unexpected error: {e}")

        # Cleanup on exit
        if self._page is not None:
            try:
                self._page.close()
            except Exception:
                pass
            self._page = None

    def _ensure_browser_on_thread(self) -> Any:
        """Ensure browser is launched on this thread."""
        global _browser, _pw_ctx
        sync_playwright = _get_playwright()
        if sync_playwright is None:
            return None
        with _lock:
            if _browser is not None:
                return _browser
            try:
                _pw_ctx = sync_playwright().__enter__()
                ctx = _pw_ctx.chromium.launch_persistent_context(
                    user_data_dir=str(Path.home() / ".jarvis_chrome_profile"),
                    headless=False,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                    ],
                    ignore_https_errors=True,
                )
                _browser = ctx.browser
                return _browser
            except Exception as exc:
                print(f"[ChromiumPlayer] browser launch failed: {exc}")
                return None

    def _handle(self, cmd: _Command) -> None:
        try:
            if cmd.kind == "open":
                self._handle_open(cmd)
            elif cmd.kind == "stop":
                self._handle_stop(cmd)
            elif cmd.kind == "pause":
                self._handle_pause(cmd)
            elif cmd.kind == "mute":
                self._handle_mute(cmd)
            elif cmd.kind == "seek":
                self._handle_seek(cmd)
            elif cmd.kind == "close":
                self._handle_close(cmd)
            elif cmd.kind == "fullscreen":
                self._handle_fullscreen(cmd)
        except Exception as e:
            cmd.future.set_exception(e)

    def _handle_open(self, cmd: _Command) -> None:
        bw = self._ensure_browser_on_thread()
        if bw is None:
            cmd.future.set_result((False, "Playwright Chromium is not available."))
            return

        try:
            # Close previous page if it exists
            if self._page is not None:
                try:
                    self._page.close()
                except Exception:
                    pass

            # Create a new page in the persistent context.
            ctx = bw.contexts[0] if bw.contexts else None
            if ctx is None:
                self._page = bw.new_page()
            else:
                self._page = ctx.new_page()
            page = self._page

            page.goto(cmd.url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)
            # Auto-play: YouTube requires user interaction, so trigger play programmatically
            page.evaluate("() => { const v = document.querySelector('video'); if (v) v.play().catch(() => {}); }")
            self._tab_active = True
            self._last_url = cmd.url
            cmd.future.set_result((True, ""))
        except Exception as exc:
            self._error_msg = f"Chromium failed to open video: {exc}"
            print(f"[ChromiumPlayer] {self._error_msg}")
            if self._page is not None:
                try:
                    self._page.close()
                except Exception:
                    pass
                self._page = None
            cmd.future.set_result((False, self._error_msg))

    def _handle_stop(self, cmd: _Command) -> None:
        if self._page is not None:
            try:
                self._page.close()
            except Exception:
                pass
            self._page = None
        self._tab_active = False
        self._last_url = ""
        cmd.future.set_result(True)

    def _handle_pause(self, cmd: _Command) -> None:
        if self._page is not None:
            try:
                self._page.evaluate(
                    "() => { const v = document.querySelector('video'); if (v) v." +
                    ("pause()" if cmd.paused else "play()") + "; }"
                )
                cmd.future.set_result(True)
            except Exception:
                cmd.future.set_result(False)
        else:
            cmd.future.set_result(False)

    def _handle_mute(self, cmd: _Command) -> None:
        if self._page is not None:
            try:
                self._page.evaluate(
                    f"() => {{ const v = document.querySelector('video'); if (v) v.muted = {str(cmd.muted).lower()}; }}"
                )
                cmd.future.set_result(True)
            except Exception:
                cmd.future.set_result(False)
        else:
            cmd.future.set_result(False)

    def _handle_seek(self, cmd: _Command) -> None:
        if self._page is not None:
            try:
                if cmd.relative:
                    self._page.evaluate(f"() => {{ const v = document.querySelector('video'); if (v) v.currentTime += {cmd.seconds}; }}")
                else:
                    self._page.evaluate(f"() => {{ const v = document.querySelector('video'); if (v) v.currentTime = {cmd.seconds}; }}")
                cmd.future.set_result(True)
            except Exception:
                cmd.future.set_result(False)
        else:
            cmd.future.set_result(False)

    def _handle_close(self, cmd: _Command) -> None:
        if self._page is not None:
            try:
                self._page.close()
            except Exception:
                pass
            self._page = None
        self._tab_active = False
        cmd.future.set_result(True)

    def _handle_fullscreen(self, cmd: _Command) -> None:
        if self._page is not None:
            try:
                # Use setTimeout to avoid blocking on requestFullscreen which needs user gesture
                self._page.evaluate(
                    "() => { const v = document.querySelector('video'); "
                    "if (v) { "
                    "  setTimeout(() => { "
                    "    if (!document.fullscreenElement) { v.requestFullscreen().catch(() => {}); } "
                    "    else { document.exitFullscreen().catch(() => {}); } "
                    "  }, 0); "
                    "} }"
                )
                cmd.future.set_result(True)
            except Exception:
                cmd.future.set_result(False)
        else:
            cmd.future.set_result(False)

    # Public API (thread-safe, called from other threads)

    def open_video(self, url: str, title: str = "") -> tuple[bool, str]:
        future: Future = Future()
        self._cmd_queue.put(_Command(kind="open", future=future, url=url, title=title))
        return future.result(timeout=40.0)

    def stop(self) -> bool:
        future: Future = Future()
        self._cmd_queue.put(_Command(kind="stop", future=future))
        return future.result(timeout=10.0)

    def pause(self, paused: bool) -> bool:
        future: Future = Future()
        self._cmd_queue.put(_Command(kind="pause", future=future, paused=paused))
        return future.result(timeout=10.0)

    def mute(self, muted: bool) -> bool:
        future: Future = Future()
        self._cmd_queue.put(_Command(kind="mute", future=future, muted=muted))
        return future.result(timeout=10.0)

    def seek(self, seconds: float, relative: bool) -> bool:
        future: Future = Future()
        self._cmd_queue.put(_Command(kind="seek", future=future, seconds=seconds, relative=relative))
        return future.result(timeout=10.0)

    def fullscreen(self, enter: bool = True) -> bool:
        future: Future = Future()
        self._cmd_queue.put(_Command(kind="fullscreen", future=future, fullscreen=enter))
        return future.result(timeout=10.0)

    def close_page(self) -> bool:
        """Close the current page without stopping the controller."""
        future: Future = Future()
        self._cmd_queue.put(_Command(kind="close", future=future))
        return future.result(timeout=10.0)

    def close(self) -> bool:
        """Alias for close_page for backward compatibility."""
        return self.close_page()

    @property
    def is_playing(self) -> bool:
        return self._tab_active

    @property
    def last_url(self) -> str:
        return self._last_url


# Module-level singleton controller
_controller: Optional[_ChromiumController] = None
_controller_lock = threading.Lock()


def _get_controller() -> _ChromiumController:
    global _controller
    with _controller_lock:
        if _controller is None:
            _controller = _ChromiumController()
            _controller.start()
        return _controller


# ── Public API ───────────────────────────────────────────────────────────────

def _get_playwright() -> Any:
    """Import playwright's sync_api once and cache it."""
    global _playwright
    if _playwright is not None:
        return _playwright
    try:
        from playwright.sync_api import sync_playwright
        _playwright = sync_playwright
    except ImportError:
        pass
    return _playwright


def _search_youtube(query: str) -> list[dict]:
    """Search YouTube and return a list of (video_id, title) dicts."""
    try:
        import yt_dlp
    except ImportError:
        return []

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query}", download=False)
        if info and info.get("entries"):
            entries = info["entries"]
        elif info:
            entries = [info]
        else:
            entries = []
        results = []
        for e in entries[:5]:
            vid = e.get("id") or ""
            title = e.get("title") or ""
            if vid and title:
                results.append({"id": vid, "title": title})
        return results
    except Exception:
        return []


def _resolve_youtube_url(source: str) -> tuple[str, str]:
    """Given a YouTube URL or search query, return (watch_url, title)."""
    source = (source or "").strip()
    if not source:
        return "", ""

    if _YT_URL.search(source):
        m = re.search(r"v=([A-Za-z0-9_-]{11})", source)
        if m:
            return f"https://www.youtube.com/watch?v={m.group(1)}", ""
        m = re.search(r"youtu\.be/([A-Za-z0-9_-]{11})", source)
        if m:
            return f"https://www.youtube.com/watch?v={m.group(1)}", ""
        return source, ""

    if source.lower().startswith(("play ", "search ", "find ")):
        query = source.split(None, 2)[-1] if len(source.split()) > 1 else source
    else:
        query = source

    results = _search_youtube(query)
    if not results:
        return "", ""
    best = results[0]
    return f"https://www.youtube.com/watch?v={best['id']}", best["title"]


def resolve_youtube_for_chromium(source: str) -> tuple[str, str, str]:
    """Resolve a YouTube source to a Chromium-playable URL.

    Returns (url, title, error). On success, url is non-empty and error is "".
    On failure, url is "" and error explains why.
    """
    key = source.strip()
    now = time.time()
    cached_url = _cache.get(key)
    cached_ts = _cache_ts.get(key, 0)
    if cached_url and (now - cached_ts) < _CACHE_TTL_S:
        return cached_url, key, ""

    url, title = _resolve_youtube_url(source)
    if url:
        _cache[key] = (url, title)
        _cache_ts[key] = now
        return url, title, ""
    return "", "", "YouTube source could not be resolved."


# ── Session-like interface for backward compatibility ────────────────────────

class ChromiumSession:
    """Backward-compatible session interface that delegates to the controller."""

    def __init__(self) -> None:
        self._ctrl = _get_controller()

    @property
    def is_playing(self) -> bool:
        return self._ctrl.is_playing

    @property
    def last_url(self) -> str:
        return self._ctrl.last_url

    def open_video(self, url: str, title: str = "") -> tuple[bool, str]:
        return self._ctrl.open_video(url, title)

    def stop(self) -> None:
        self._ctrl.stop()

    def close(self) -> None:
        self._ctrl.close()


# Module-level singleton for simple access patterns.
_session: Optional[ChromiumSession] = None


def get_session() -> Optional[ChromiumSession]:
    global _session
    with _lock:
        if _session is None:
            _session = ChromiumSession()
        return _session


def set_session(session: Optional[ChromiumSession]) -> None:
    global _session
    with _lock:
        _session = session


def destroy_session() -> None:
    global _session, _controller
    with _lock:
        if _session is not None:
            _session.close()
            _session = None
        # Do NOT stop the controller — keep the thread and browser alive to avoid
        # greenlet/thread context issues. Just close the current page.
        if _controller is not None:
            _controller.close_page()
        # The browser closes when the Python process exits.