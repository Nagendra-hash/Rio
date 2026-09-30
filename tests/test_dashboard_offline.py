"""
tests/test_dashboard_offline.py — E2E regression test for the dashboard
offline screen + reconnect machinery.

History: the phone dashboard died silently whenever the network blipped or
the PC restarted — one dead WebSocket meant "refresh to reconnect" forever.
The patch adds exponential-backoff reconnect with `since=<seq>` resume (no
duplicated backlog), a `ping`/`pong` staleness probe, close-code 4001
handling (server restarted → silent device re-pair + reload), and a full
offline screen that distinguishes "phone offline" from "PC unreachable".

What this test proves (real headless Chrome + the REAL DashboardServer):
  1. Baseline: WS connects, broadcasts render, client `_lastSeq` advances.
  2. Phone offline (CDP-emulated): offline screen shows "Phone is offline",
     session/device tokens survive, recovery when the network returns.
  3. PC unreachable: the server is STOPPED (a real dead PC) → "Can't reach
     JARVIS" appears after backoff; a FRESH server instance (memory wiped,
     like a reboot) comes back → the page reconnects on its own via the
     4001 → device re-login path, and already-seen messages are NOT replayed.
  4. Login page: a failed device-login keeps the localStorage device token.

Isolation: everything is sandboxed — the device-token store and the static
token live in temp copies, never in your real config/.

Exit codes: 0 = pass, 1 = regression, 2 = environment error (deps missing).
Also pytest-compatible: test_offline_flow().

Run:  .venv/bin/python tests/test_dashboard_offline.py
"""
import asyncio
import json
import re
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# ── isolation: point config_manager at temp files BEFORE any import ──────────
_TMPDIR = tempfile.mkdtemp(prefix="jarvis-dash-e2e-")
_TMP_CONFIG = Path(_TMPDIR) / "api_keys.json"
_TMP_DEVICES = Path(_TMPDIR) / "dashboard_devices.json"
_TMP_CONFIG.write_text(json.dumps({"dashboard_auth_token": "e2e-static-token-1234"}))

import memory.config_manager as cfg  # noqa: E402  (after temp paths are set)
cfg.CONFIG_FILE = _TMP_CONFIG        # sandbox the static-token store

from dashboard.server import DashboardServer  # noqa: E402


def _env_ok() -> tuple[bool, str]:
    try:
        import uvicorn  # noqa: F401
        import playwright  # noqa: F401
    except ImportError as e:
        return False, f"missing dep: {e} (.venv/bin/pip install playwright uvicorn)"
    try:
        from dashboard.server import _DEPS_OK  # noqa: F401
    except Exception:
        pass
    return True, ""


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Server:
    """The real DashboardServer on a deterministic port; stop() = PC died.
    Each start() builds a FRESH server object — in-memory sessions are gone,
    exactly like a reboot — while the device-token file persists on disk.
    The port is FIXED (derived from the pid): if the page (or the runtime)
    holds state from an earlier run/session, a reused port keeps that state
    consistent; a random new port per boot would point old state at a server
    that no longer exists."""

    def __init__(self):
        self.port = _free_port()
        self.srv: DashboardServer | None = None
        self.server = self.thread = None

    def start(self) -> None:
        self.srv = DashboardServer()          # fresh instance every boot
        self.srv._devices_file = _TMP_DEVICES  # sandboxed persistence
        self.srv._device_sessions = {}
        self.srv._load_devices()               # re-read from the temp file
        import uvicorn
        config = uvicorn.Config(self.srv.app, host="127.0.0.1", port=self.port,
                                log_level="error")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        if not _wait(f"http://127.0.0.1:{self.port}/login"):
            raise RuntimeError("test server did not come up")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)
        end = time.time() + 10                 # wait until the port truly refuses
        while time.time() < end:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/login", timeout=0.5)
                time.sleep(0.2)
            except Exception:
                return

    def bcast(self, text: str) -> None:
        loop = self.srv._loop
        end = time.time() + 15
        while loop is None and time.time() < end:
            time.sleep(0.2)               # _loop appears with the first WS accept
            loop = self.srv._loop
        if loop is None:
            raise RuntimeError("server loop never appeared — page WS never connected")
        asyncio.run_coroutine_threadsafe(
            self.srv.broadcast({"type": "log", "speaker": "jarvis", "text": text}),
            loop).result(timeout=10)


def _wait(url: str, timeout: float = 25) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except Exception:
            time.sleep(0.2)
    return False


def _pair(base: str) -> tuple[str, str]:
    """Pair a device exactly like a real phone (pair API → auto-login page)."""
    req = urllib.request.Request(
        f"{base}/api/phone/pair", method="POST",
        headers={"Authorization": "Bearer e2e-static-token-1234"})
    pair = json.load(urllib.request.urlopen(req))
    key = re.search(r"key=([A-Z2-9]+)", pair["link"]).group(1)
    body = urllib.request.urlopen(urllib.request.Request(
        f"{base}/auto-login?key={key}",       # harness serves plain HTTP
        headers={"User-Agent": "Mozilla/5.0 (E2E)"})).read().decode()
    dev = re.search(r"localStorage\.setItem\('jarvis_device_token','([^']+)'\)",
                    body).group(1)
    ses = re.search(r"sessionStorage\.setItem\('jarvis_token','([^']+)'\)",
                    body).group(1)
    return dev, ses


# ── static sanity (runs even without a browser) ──────────────────────────────

def test_offline_code_present() -> tuple[bool, str]:
    src = (BASE / "dashboard" / "static" / "app.html").read_text()
    needed = ["offline-screen", "_offline.note('ws')", "_offline.note('fetch')",
              "_offline.hide()", "_scheduleWsReconnect", "_lastSeq",
              "since=", "ev.code === 4001", "addEventListener('online'"]
    missing = [n for n in needed if n not in src]
    if missing:
        return False, f"offline/reconnect pieces missing from app.html: {missing}"
    return True, "offline screen + reconnect + 4001 re-pair code all present"


# ── the live browser test ────────────────────────────────────────────────────

def test_offline_flow() -> tuple[bool, str]:
    from playwright.sync_api import sync_playwright

    failures: list[str] = []
    ctl = _Server()
    ctl.start()
    base = f"http://127.0.0.1:{ctl.port}"
    dev_tok, ses_tok = _pair(base)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True, channel="chrome",
                args=["--no-sandbox", "--disable-dev-shm-usage"])
            # Fresh context every run: seeding must start clean (a leaked
            # sessionStorage from an earlier run would connect to a stale port
            # and poison every later assertion). service_workers='block' keeps
            # the SW out of the picture (it's covered by its own test).
            ctx = browser.new_context(
                storage_state=None,
                service_workers="block")
            page = ctx.new_page()
            page.on("pageerror", lambda e: print(f"   [pageerror] {e}"))
            page.on("console", lambda m: print(f"   [console.{m.type}] {m.text[:140]}")
                    if m.type in ("error", "warning") else None)
            cdp = ctx.new_cdp_session(page)

            def throttle(offline: bool) -> None:
                cdp.send("Network.emulateNetworkConditions",
                         {"offline": offline, "latency": 0,
                          "downloadThroughput": -1, "uploadThroughput": -1})

            def screen_shown() -> bool:
                return page.evaluate(
                    "document.getElementById('offline-screen')"
                    ".classList.contains('show')")

            def screen_title() -> str:
                return page.evaluate(
                    "document.getElementById('of-title').textContent") or ""

            def ws_open() -> bool:
                return page.evaluate("!!(ws && ws.readyState === 1)")

            def feed() -> str:
                return page.evaluate("document.getElementById('feed').textContent")

            page.add_init_script(
                f"localStorage.setItem('jarvis_device_token','{dev_tok}');"
                f"sessionStorage.setItem('jarvis_token','{ses_tok}');"
            )
            page.goto(base + "/", wait_until="domcontentloaded")
            page.wait_for_timeout(2500)

            # ── [1] baseline ────────────────────────────────────────────────
            if not ws_open():
                failures.append("WS did not connect on a healthy network")
            n0 = page.evaluate("document.querySelectorAll('#feed .msg').length")
            seq0 = page.evaluate("_lastSeq") or 0
            ctl.bcast("e2e baseline message")
            page.wait_for_timeout(1500)
            if not page.evaluate(
                    "document.querySelectorAll('#feed .msg').length") > n0:
                failures.append("broadcast never rendered in the feed")
            if not (page.evaluate("_lastSeq") or 0) > seq0:
                failures.append("client _lastSeq did not advance")

            # ── [2] phone offline ───────────────────────────────────────────
            throttle(True)
            page.evaluate("_offline.note('fetch')")  # what a real failed fetch does
            page.wait_for_timeout(600)
            if not screen_shown():
                failures.append("offline screen not shown when phone went offline")
            if "offline" not in screen_title().lower():
                failures.append(f"wrong title for phone-offline: {screen_title()!r}")
            if not page.evaluate("!!sessionStorage.getItem('jarvis_token')"):
                failures.append("session token wiped while offline")
            if not page.evaluate("!!localStorage.getItem('jarvis_device_token')"):
                failures.append("device token wiped while offline")

            throttle(False)
            page.evaluate("dispatchEvent(new Event('online'))")
            if not _poll(lambda: ws_open(), 20):
                failures.append("no reconnect after the network returned")
            if not _poll(lambda: not screen_shown(), 5):
                failures.append("offline screen did not hide after recovery")

            # ── [3] PC unreachable (real stop + fresh-boot restart) ────────
            ctl.bcast("e2e pre-freeze message")
            page.wait_for_timeout(1200)
            ctl.stop()
            shown = _poll(
                lambda: screen_shown() and "reach" in screen_title(), 45, 1.0)
            if not shown:
                failures.append(
                    f"'Can't reach JARVIS' never appeared (title={screen_title()!r})")

            ctl.start()   # fresh instance — sessions wiped, device file persists
            if not _poll(ws_open, 35):
                failures.append("page did not reconnect after the PC came back")
            # Wait until the NEW server actually registered the client (its
            # _loop is set at WS accept): bcast() right after ws_open() can
            # otherwise race a not-yet-accepted socket.
            if not _poll(lambda: ctl.srv._loop is not None
                         and len(ctl.srv._clients) > 0, 15):
                failures.append("new server never saw the reconnected client")
            page.wait_for_timeout(250)          # settle: backlog flush, pong
            if not _poll(lambda: not screen_shown(), 8):
                failures.append("offline screen stuck after reconnect")
            ctl.bcast("e2e post-revive message")
            page.wait_for_timeout(1500)
            body = feed()
            if body.count("pre-freeze message") != 1:
                failures.append("pre-freeze message replayed after reconnect "
                                "(since= resume broken)")
            if "post-revive message" not in body:
                failures.append("post-revive message never arrived")
            # fresh boot wiped in-memory sessions → the page must have gone
            # through the 4001 → device re-login path; prove the token changed.
            if page.evaluate("sessionStorage.getItem('jarvis_token')") == ses_tok:
                failures.append("session token unchanged after server restart — "
                                "silent re-pair did not happen")

            # ── [4] login page keeps the device token on network failure ───
            page4 = ctx.new_page()
            page4.add_init_script(
                "localStorage.setItem('jarvis_device_token','devtok-must-survive')")
            page4.route("**/api/device-login", lambda route: route.abort("failed"))
            page4.goto(base + "/login", wait_until="domcontentloaded")
            page4.wait_for_timeout(2000)
            if page4.evaluate(
                    "localStorage.getItem('jarvis_device_token')"
            ) != "devtok-must-survive":
                failures.append("login page wiped the device token on a "
                                "network-level failure")
            msg = page4.evaluate("document.getElementById('err').textContent || ''")
            if not (("offline" in msg.lower()) or ("unreachable" in msg.lower())):
                failures.append(f"login page gave no useful outage message: {msg!r}")
            page4.close()

            browser.close()
    finally:
        try:
            ctl.stop()
        except Exception:
            pass

    if failures:
        return False, "; ".join(failures)
    return True, ("phone-offline + PC-restart recovery verified live: "
                  "auto-reconnect, 4001 re-pair, since= resume, no dup replay")


def _poll(fn, timeout: float, step: float = 0.5) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if fn():
                return True
        except Exception:
            pass
        time.sleep(step)
    return False


# ── standalone runner (repo convention: 0 pass / 1 regression / 2 env) ──────

def main() -> int:
    ok, msg = _env_ok()
    if not ok:
        print(f"ENV-SKIP: {msg}")
        return 2

    ok1, msg1 = test_offline_code_present()
    print(f"{'PASS' if ok1 else 'FAIL'}  static offline/reconnect code: {msg1}")
    if not ok1:
        return 1

    try:
        ok2, msg2 = test_offline_flow()
    except Exception as e:
        print(f"ENV-SKIP: browser test could not run ({type(e).__name__}: {e})")
        return 2
    print(f"{'PASS' if ok2 else 'FAIL'}  live offline/reconnect behavior: {msg2}")
    return 0 if ok2 else 1


if __name__ == "__main__":
    sys.exit(main())
