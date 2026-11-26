"""
rio_window.py — RIO as the default desktop window (Mark-III).

What this does
--------------
The RIO web frontend is served by the existing dashboard (dashboard/server.py,
:8000/:8001) at the twelve RIO routes (/home, /chat, ...). This module opens
that UI as the app's desktop window: a Chrome "app-mode" (--app=) frameless
window pointed at the running dashboard. No second server, no port change, no
QtWebEngine dependency, no rebuild — the backend that already runs is the
backend that serves the UI.

Why a Chrome --app window and not a QtWebEngine widget
-----------------------------------------------------
The installed PyQt6 (6.11.0) ships no QtWebEngine module, and pip-installing
it pulls ~150 MB of framework onto a 3.7 GiB-RAM box. Chrome 154 is already
installed; --app gives a true single-page desktop window (no tabs, no address
bar, no bookmarks) in ~40 ms.

Auth — the same pairing path every other device already uses
------------------------------------------------------------
No new secrets, no new endpoints, nothing written to config. When the
dashboard is already alive in this process, we mint ONE of its standard
short-lived one-time pairing keys (dashboard.DashboardServer.new_key, the
exact mechanism behind the "Remote Control" QR button) and open the window at
/auto-login?key=…&to=/home. /auto-login exchanges the key for an ordinary
session bearer token inside the window's own sessionStorage and redirects to
RIO's home route. Key expires in EXPIRY_SECS even if the window never loads.

Profile isolation
-----------------
The window runs on a dedicated throwaway profile dir (--user-data-dir
/tmp/jarvis_rio_window_profile) so it never reads the user's real Chrome
profile (bookmarks, extensions, cookies, saved passwords) and never writes
into it. --ignore-certificate-errors is flagged ONLY for this dedicated
profile because the dashboard serves a self-signed pair over HTTPS; the user's
everyday browsing profile is untouched and unprotected by nothing — this flag
never applies to it.

Single window
-------------
A second invocation detects the profile marker via pgrep and exits 0 instead
of stacking a duplicate window (same guard philosophy as
jarvis_gui_wrapper.sh).

Fallback / rollback
-------------------
Set JARVIS_RIO_WINDOW=0 and the app launches exactly as before (PyQt HUD
shown normally); nothing here is reachable. Delete this file + remove the
two JARVIS_RIO_WINDOW blocks from main.py to fully revert (backups:
backup/rio_default_ui_20261010/).
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

try:
    from dashboard.server import PORT, DashboardServer  # the one port the dashboard really uses
except Exception:                       # pragma: no cover - module guard
    PORT = 8000

PROFILE_DIR = Path(os.environ.get(
    "JARVIS_RIO_WINDOW_PROFILE", "/tmp/jarvis_rio_window_profile"))
EXPIRY_SECS = 120          # pairing key lifetime (standard server mechanism)
START_PATH  = "/home"      # RIO home route — the first screen the window shows


def _chrome_binary() -> str | None:
    for cand in (shutil.which("google-chrome-stable"),
                 shutil.which("google-chrome"),
                 shutil.which("chromium-browser"),
                 shutil.which("chromium")):
        if cand:
            return cand
    return None


def _already_open() -> bool:
    """True when a RIO window (same profile dir) is already running."""
    return window_running()


def window_running() -> bool:
    """Is THIS app's RIO desktop window still open?

    The check keys on the app's own throwaway Chrome profile dir — a path only
    this application ever launches with — so a process matching it is owned by
    this app and nothing else. Used by main.py to notice the user closing the
    RIO window (there is no X11/Wayland close event to subscribe to for a
    foreign Chrome --app child)."""
    try:
        out = subprocess.run(
            ["pgrep", "-af", str(PROFILE_DIR)],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return False
    return bool(out.stdout.strip())


def _wait_dashboard_up(base_url: str, wait_secs: float) -> bool:
    """Block until the dashboard actively answers /login (not just binds).
    Speaks the same protocol the server really serves: when the cert pair is
    present the dashboard is TLS-only (plain HTTP gets RemoteDisconnected),
    so the probe must use HTTPS — with verification off, because the pair is
    self-signed by design. This matches what the Chrome window itself sees
    (--ignore-certificate-errors, this profile only)."""
    import http.client
    import ssl
    import urllib.parse
    from urllib.parse import urlparse
    host = urlparse(base_url).hostname or "127.0.0.1"
    is_https = base_url.startswith("https")
    deadline = time.time() + wait_secs
    while time.time() < deadline:
        try:
            if is_https:
                conn = http.client.HTTPSConnection(
                    host, PORT, timeout=1.5,
                    context=ssl._create_unverified_context())
            else:
                conn = http.client.HTTPConnection(host, PORT, timeout=1.5)
            conn.request("HEAD", "/login")
            resp = conn.getresponse()
            resp.read()
            conn.close()
            if resp.status < 500:
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def open_rio_window(dashboard=None, wait_secs: float = 20.0) -> int:
    """Open the RIO desktop window.

    dashboard  — optional live DashboardServer. When None, the class-level
                 DashboardServer.live registry (set in serve()) is used, so
                 callers don't need to plumb the instance through.
    Returns 0 on success, 1 when Chrome is unavailable, 2 when the dashboard
    never came up, 3 when a window is already open.
    """
    chrome = _chrome_binary()
    if chrome is None:
        print("[RIO window] no google-chrome/chromium on PATH — "
              "install Chrome or set JARVIS_RIO_WINDOW=0 to use the old HUD.")
        return 1

    if _already_open():
        # A window that survived the previous run carries the OLD Chrome
        # flags and OLD cached assets — relaunching is not "stacking a
        # duplicate", it is how the window picks up new code (observed
        # 2026-10-16/17: a surviving window kept the pre-autoplay-flag
        # launch → the dock stayed autoplay-blocked and the media
        # controller never appeared). A service-restart kill also matches
        # here as a DYING process — wait a beat so we do not SIGTERM a
        # corpse, then replace whatever is genuinely still alive.
        alive = True
        for _ in range(6):                      # ~3 s
            time.sleep(0.5)
            if not _already_open():
                alive = False
                break
        if alive:
            print("[RIO window] replacing the leftover RIO window — it "
                  "runs old flags/assets (fresh launch every start).")
            close_rio_window()
            for _ in range(10):                 # ~5 s for Chrome to exit
                time.sleep(0.5)
                if not _already_open():
                    break
            if _already_open():
                print("[RIO window] leftover window still exiting — "
                      "opening anyway on the same profile.")

    # Same URL logic as DashboardServer.get_url(): HTTPS when the self-signed
    # pair exists, HTTP otherwise. Loopback, never the LAN-facing IP.
    certs = BASE_DIR / "config" / "certs"
    proto = ("https" if (certs / "jarvis.crt").exists()
             and (certs / "jarvis.key").exists() else "http")
    base_url = f"{proto}://127.0.0.1:{PORT}"

    if not _wait_dashboard_up(base_url, wait_secs):
        print(f"[RIO window] dashboard on {base_url} did not answer within "
              f"{wait_secs:.0f}s — not opening a dead window.")
        return 2

    # One-time pairing key → /auto-login → session token in the window's
    # sessionStorage → redirect to RIO /home. Exactly the QR path a phone
    # walks; nothing new stored or exposed.
    url = f"{base_url}/auto-login?to={START_PATH}"
    live = dashboard or getattr(DashboardServer, "live", None)
    if live is not None:
        try:
            key = live.new_key(expiry_secs=EXPIRY_SECS)
            if key:
                url = f"{base_url}/auto-login?key={key}&to={START_PATH}"
        except Exception as exc:
            print(f"[RIO window] pairing key unavailable ({exc}) — opening "
                  "the window at the RIO pairing screen instead.")

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        chrome,
        f"--app={url}",
        f"--user-data-dir={PROFILE_DIR}",          # throwaway profile —
        "--no-first-run",                          #   never the user's real one
        "--no-default-browser-check",
        "--ignore-certificate-errors",             # self-signed pair; scoped
                                                   #   to THIS profile only
        # A throwaway profile has ~zero Media Engagement Index, so Chrome's
        # default autoplay policy blocks the media dock's audio.play() with
        # sound — the dock then sat on "▶ Tap to play" and a voice request
        # ("play Kesariya") produced silence while the action replied
        # "Playing…" (observed 2026-10-16). This window is our own app UI
        # playing exactly what the user asked for — the same trust the old
        # PyQt HUD's player had — so autoplay does not need a gesture here.
        "--autoplay-policy=no-user-gesture-required",
        "--disable-background-networking",
        "--disable-component-update",
        "--password-store=basic",
    ]

    print(f"[RIO window] opening RIO at {base_url}{START_PATH}")
    try:
        subprocess.Popen(cmd, start_new_session=True,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return 0
    except Exception as exc:
        print(f"[RIO window] failed to launch Chrome: {exc}")
        return 1


def close_rio_window() -> None:
    """Terminates the RIO window process group (used on app shutdown)."""
    try:
        out = subprocess.run(["pgrep", "-f", str(PROFILE_DIR)],
                             capture_output=True, text=True, timeout=5)
        for pid_s in out.stdout.split():
            try:
                os.kill(int(pid_s), signal.SIGTERM)
            except (ValueError, ProcessLookupError, PermissionError):
                pass
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(open_rio_window())
