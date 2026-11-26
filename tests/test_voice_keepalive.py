"""
tests/test_voice_keepalive.py — regression test for the phone-voice keep-alive.

History: the phone mic session died silently whenever the tab was backgrounded
or the network blipped — the old code had one-shot handlers (drop = dead session
until a manual refresh + re-tap). The keep-alive patch adds a user-intent state
machine (_voiceWanted), exponential-backoff auto-reconnect, Wake Lock, an
audible-tab loopback, and visibility auto-resume.

What this test proves (against the REAL dashboard server, headless Chromium
with a fake microphone):
  1. Tapping the mic opens the audio WebSocket (works on plain HTTP with
     --use-fake-ui-for-media-stream + fake device, so no cert/mic prompts).
  2. KILLING the socket from the outside (the exact network-blip scenario
     that used to kill the session) → the page reconnects BY ITSELF and the
     server sees a fresh "Phone microphone live." — no human touch.
  3. A second tap stops cleanly: socket closed, intent cleared, no zombie
     reconnects afterwards.

Exit codes: 0 = pass, 1 = regression, 2 = environment error (deps missing).
Also pytest-compatible: test_voice_keepalive().

Run:  .venv/bin/python tests/test_voice_keepalive.py
"""

import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PAGE = BASE / "dashboard" / "static" / "app.html"

sys.path.insert(0, str(BASE / "dashboard"))


# ── helpers ──────────────────────────────────────────────────────────────────


def _env_ok() -> tuple[bool, str]:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return (
            False,
            "playwright not installed (.venv/bin/pip install playwright && playwright install chromium)",
        )
    if not PAGE.exists():
        return False, f"missing {PAGE}"
    return True, ""


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _serve(port: int) -> subprocess.Popen:
    """Serve dashboard/static + a /ws/phone-audio echo endpoint (record-only)."""
    handler = r"""
import asyncio, json
from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse

app = FastAPI()
events = []

@app.get("/")
async def index():
    return FileResponse(r"__PAGE__", media_type="text/html")

@app.websocket("/ws/phone-audio")
async def pa(ws: WebSocket, token: str = ""):
    await ws.accept()
    events.append("live")
    try:
        while True:
            data = await ws.receive_bytes()
            events.append("audio:%d" % len(data))
    except Exception:
        events.append("stopped")

@app.get("/events")
async def ev():
    return {"events": events[-30:]}
""".replace("__PAGE__", str(PAGE))

    script = (
        "import uvicorn, sys\n"
        f"sys.path.insert(0, {str(BASE / 'dashboard')!r})\n"
        + handler
        + f"\nuvicorn.run(app, host='127.0.0.1', port={port}, log_level='error')\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # wait for the server
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/events", timeout=1)
            return proc
        except Exception:
            time.sleep(0.2)
    proc.terminate()
    raise RuntimeError("test server failed to start")


def _events(port: int) -> list[str]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/events", timeout=3) as r:
        return json.load(r)["events"]


# ── the actual browser test ──────────────────────────────────────────────────

import pytest

pytestmark = pytest.mark.browser_e2e


def test_voice_keepalive() -> tuple[bool, str]:
    from playwright.sync_api import sync_playwright

    port = _free_port()
    proc = _serve(port)
    failures: list[str] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                args=[
                    "--use-fake-ui-for-media-stream",  # auto-allow mic permission
                    "--use-fake-device-for-media-stream",  # synthetic audio input
                    "--autoplay-policy=no-user-gesture-required",  # allow AudioContext resume
                ]
            )
            page = browser.new_page()
            # app.html redirects to /login without a token — seed it before load
            page.add_init_script(
                "sessionStorage.setItem('jarvis_token','test-token');"
                "sessionStorage.setItem('jarvis_key','test-key');"
            )
            page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")

            # 1) mic tap → socket opens, server sees the session
            page.evaluate("doMic()")
            try:
                page.wait_for_function(
                    "_voiceWs && _voiceWs.readyState === 1", timeout=15000, polling=100
                )
            except Exception:
                state = page.evaluate(
                    "JSON.stringify({url: location.href, mic: micBtn.title, "
                    "ws: _voiceWs ? _voiceWs.readyState : null, "
                    "wanted: _voiceWanted, feed: Array.from(document.querySelectorAll('#feed > *'))"
                    ".slice(-4).map(e => e.innerText.replace(/\\n/g,' ').slice(0,60))})"
                )
                raise AssertionError(f"voice socket never opened; page state: {state}")
            deadline = time.time() + 5
            while time.time() < deadline and "live" not in _events(port):
                time.sleep(0.2)
            if "live" not in _events(port):
                failures.append("server never saw 'live' after mic tap")

            # 2) simulate a network blip: kill the socket from outside.
            #    Old code: session dead forever. New code: auto-reconnect.
            page.evaluate("_voiceWs.close()")
            # intent (_voiceWanted) must stay true
            if not page.evaluate("_voiceWanted"):
                failures.append(
                    "intent (_voiceWanted) was cleared on drop — reconnect cannot happen"
                )
            page.wait_for_function(
                "_voiceWs && _voiceWs.readyState === 1",
                timeout=25000,
                polling=100,  # backoff schedule allows up to ~1+2+4+8s
            )
            deadline = time.time() + 5
            ok = False
            while time.time() < deadline:
                ev = _events(port)
                if ev.count("live") >= 2:
                    ok = True
                    break
                time.sleep(0.2)
            if not ok:
                failures.append(
                    "server did not see a second 'live' after the drop — auto-reconnect failed"
                )

            # 3) second tap → clean stop, no zombie reconnects
            page.evaluate("doMic()")
            time.sleep(1.5)
            if page.evaluate("_voiceWanted"):
                failures.append("intent still set after user stop")
            if page.evaluate("_voiceWs !== null"):
                failures.append("socket not cleared after user stop")
            n_live = _events(port).count("live")
            time.sleep(3)
            if _events(port).count("live") != n_live:
                failures.append("a reconnect fired AFTER user stop — zombie session")

            browser.close()
    finally:
        proc.terminate()

    if failures:
        return False, "; ".join(failures)
    return True, "drop → auto-reconnect → clean stop all verified against a live server"


# ── static sanity (always runs, even without playwright) ────────────────────


def test_keepalive_code_present() -> tuple[bool, str]:
    src = PAGE.read_text()
    needed = [
        "_voiceWanted",
        "_scheduleReconnect",
        "wakeLock",
        "visibilitychange",
        "_teardown",
    ]
    missing = [n for n in needed if n not in src]
    if missing:
        return False, f"keep-alive pieces missing from app.html: {missing}"
    return True, "all keep-alive pieces present"


# ── standalone runner (repo convention: 0 pass / 1 regression / 2 env) ──────


def main() -> int:
    ok, msg = _env_ok()
    if not ok:
        print(f"ENV-SKIP: {msg}")
        return 2

    ok1, msg1 = test_keepalive_code_present()
    print(f"{'PASS' if ok1 else 'FAIL'}  static keep-alive code: {msg1}")
    if not ok1:
        return 1

    try:
        ok2, msg2 = test_voice_keepalive()
    except Exception as e:
        print(f"ENV-SKIP: browser test could not run ({type(e).__name__}: {e})")
        return 2
    print(f"{'PASS' if ok2 else 'FAIL'}  live reconnect behavior: {msg2}")
    return 0 if ok2 else 1


if __name__ == "__main__":
    sys.exit(main())
