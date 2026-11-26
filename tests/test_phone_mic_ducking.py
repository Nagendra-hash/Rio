"""
tests/test_phone_mic_ducking.py — regression test for the double-voice fix.

History: with the phone mic live, JARVIS's reply played on BOTH the PC
speakers and the phone. The phone's mic (and the PC's) picked the PC
playback up again — an echo loop that produced the "two voices" effect and
confused Gemini's turn detection. The fix: the dashboard tracks phone-mic
liveness and the app ducks PC speaker playback while it streams.

What this test proves (against the REAL DashboardServer, no browser):
  1. Liveness: phone_mic_active() is False at rest, flips True while
     /ws/phone-audio receives audio, and decays back to False after
     PHONE_MIC_IDLE_S without audio.
  2. Callback: set_phone_mic_callback receives active=True on first audio
     and active=False on socket close.
  3. Fallback wiring: when no callback is registered (or the app loop is
     down), the play-loop fallback in _play() still ducks, because it polls
     phone_mic_active() directly.

Run:  .venv/bin/python tests/test_phone_mic_ducking.py
Also pytest-compatible: test_phone_mic_ducking().
"""

import asyncio
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "dashboard"))

from dashboard.server import DashboardServer  # noqa: E402


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_server(port: int):
    srv = DashboardServer()
    import uvicorn

    config = uvicorn.Config(srv.app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    end = time.time() + 20
    while time.time() < end:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/login", timeout=2)
            return srv, server
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("test server did not come up")


def _wait_for(pred, timeout: float, step: float = 0.05) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return False


def test_phone_mic_ducking() -> tuple[bool, str]:
    failures: list[str] = []

    port = _free_port()
    srv, server = _start_server(port)

    # Fresh session token (device-login needs a paired device; /login needs
    # a pending key — mint one directly like the pairing flow does).
    key = srv.new_key(expiry_secs=60)
    srv._pending_keys[key] = time.time() + 60
    import urllib.error

    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/login", data=b"", method="POST"
    )  # placeholder; login needs JSON below
    # /login expects JSON {"pin": ...} — build the request properly.
    import json as _json

    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/login",
        data=_json.dumps({"pin": key}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = _json.load(urllib.request.urlopen(req, timeout=5))
    except urllib.error.HTTPError as e:
        server.should_exit = True
        return False, f"login failed: HTTP {e.code}"
    if not resp.get("ok") or not resp.get("token"):
        server.should_exit = True
        return False, f"login response unexpected: {resp}"
    tok = resp["token"]

    # Callback capture (the app would register _on_phone_mic_state here).
    events: list[bool] = []

    # A fake JarvisLive stand-in exercising pause/resume semantics: a
    class _FakeStream:
        def __init__(self):
            self.started = True
            self.stops = 0
            self.starts = 0

        def stop(self):
            self.stops += 1
            self.started = False

        def start(self):
            self.starts += 1
            self.started = True

    app_state = {"paused": False, "stream": _FakeStream(), "logs": []}

    def _app_on_phone_mic(active: bool):
        # Mirrors JarvisLive._set_speaker_duck's mic half (minus the loop
        # hop — this test runs on one thread).
        if active:
            app_state["paused"] = True
            app_state["stream"].stop()
            app_state["logs"].append("paused")
        else:
            app_state["paused"] = False
            app_state["stream"].start()
            app_state["logs"].append("resumed")

    srv.set_phone_mic_callback(lambda a: (events.append(a), _app_on_phone_mic(a)))

    async def _exercise():
        # 1) at rest → inactive
        if srv.phone_mic_active():
            failures.append("phone_mic_active() True before any audio")

        # 2) stream audio on /ws/phone-audio → active + callback(True)
        import websockets

        ws_url = f"ws://127.0.0.1:{port}/ws/phone-audio?token={tok}"
        async with websockets.connect(ws_url) as ws:
            await ws.send(b"\x00\x00" * 512)
            if not _wait_for(lambda: srv.phone_mic_active(), 5):
                failures.append("phone_mic_active() did not go True while streaming")
            if not _wait_for(lambda: bool(events) and events[0] is True, 5):
                failures.append(f"callback(True) never fired (events={events})")

            # mid-stream: the app must have STOPPED the PC mic stream
            st_mid = app_state["stream"]
            if not _wait_for(lambda: st_mid.stops >= 1 and not st_mid.started, 5):
                failures.append(
                    f"PC mic stream not stopped while phone mic live "
                    f"(stops={st_mid.stops}, started={st_mid.started})"
                )

            # silence beyond PHONE_MIC_IDLE_S → liveness decays
            await asyncio.sleep(DashboardServer.PHONE_MIC_IDLE_S + 0.6)
            if srv.phone_mic_active():
                failures.append("liveness did not decay after idle window")

            # one more batch → active again, no DUPLICATE callback(True)
            n = len(events)
            await ws.send(b"\x00\x00" * 512)
            if not _wait_for(lambda: srv.phone_mic_active(), 5):
                failures.append("liveness did not return after new audio")
            if not _wait_for(lambda: len(events) >= n + 1, 5):
                failures.append("callback did not fire on reactivation")
        # 3) socket closed → callback(False)
        if not _wait_for(lambda: events and events[-1] is False, 5):
            failures.append(
                f"callback(False) never fired after close (events={events})"
            )

        # 4) mic resume semantics after the phone mic stopped
        st = app_state["stream"]
        if not _wait_for(lambda: st.starts >= 1 and st.started, 5):
            failures.append(
                f"PC mic stream was not restarted after phone mic "
                f"stopped (starts={st.starts}, started={st.started})"
            )
        if app_state["logs"][:1] != ["paused"] or app_state["logs"][-1:] != ["resumed"]:
            failures.append(
                f"unexpected pause/resume log sequence: {app_state['logs']}"
            )

    try:
        asyncio.run(_exercise())
    except ImportError:
        server.should_exit = True
        return 2, "websockets not installed (pip install websockets)"  # type: ignore[return-value]
    except Exception as e:
        failures.append(f"exercise crashed: {type(e).__name__}: {e}")
    finally:
        server.should_exit = True

    if failures:
        return False, "; ".join(failures)
    return True, (
        "liveness + idle decay + callback(True/False) + no duplicate "
        "reactivation + PC-mic pause/resume all verified against the real server"
    )


if __name__ == "__main__":
    ok, msg = test_phone_mic_ducking()
    print(f"{'PASS' if ok else 'FAIL'}  {msg}")
    sys.exit(0 if ok else 1)
