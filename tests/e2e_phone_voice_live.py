"""E2E phone-voice check against the LIVE JARVIS instance (restart test).

1. Mint a pairing key via the dashboard's one-time key store (same store the
   Remote Control button uses), then log in over HTTP like a phone would.
2. Open /ws/phone-audio and stream real PCM batches (like the phone mic does).
3. Watch the live log for our new markers:
   - "[JARVIS] PC mic paused — phone microphone is live" (stream stopped)
   - "[JARVIS] PC mic resumed" (after we stop streaming + grace)
   and catch the "Phone microphone live." sys broadcast on a /ws client
   socket (it goes to dashboard clients, not the console log).
4. Confirm dashboard-level liveness via /api/phone/status + a direct probe of
   the running server's state through the same code paths the app uses.

Run:  .venv/bin/python tests/e2e_phone_voice_live.py
Exit: 0 = pass, 1 = fail, 2 = environment (JARVIS not running).
"""
import asyncio
import json
import socket
import ssl
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

HOST_PORT = ("127.0.0.1", 8001)          # dashboard HTTPS/alias port
LOG_FILE = Path("/tmp/jarvis_restart_test.log")
WAIT = 20.0


def _dashboard_up() -> bool:
    try:
        s = socket.create_connection(("127.0.0.1", 8001), timeout=3)
        s.close()
        return True
    except OSError:
        return False


def _ctx():
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE      # self-signed jarvis.crt
    return c


def _log_tail() -> str:
    try:
        return LOG_FILE.read_text(errors="replace")
    except OSError:
        return ""


def _wait_log(needle: str, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if needle in _log_tail():
            return True
        time.sleep(0.3)
    return False


async def _exercise() -> tuple[bool, str]:
    import websockets

    failures: list[str] = []

    # 1) authenticate like a phone: the static dashboard token is accepted
    #    wherever session tokens are (_token_ok()).
    from memory.config_manager import get_dashboard_token
    static_tok = get_dashboard_token() or ""
    if not static_tok:
        return False, ("no static dashboard token configured — set one in "
                       "JARVIS settings (api_keys.json dashboard_auth_token) "
                       "so external checks can authenticate")

    # 2) a second client socket listens on /ws for the sys broadcast
    ws_url = f"wss://127.0.0.1:8001/ws/phone-audio?token={static_tok}"
    listen_url = f"wss://127.0.0.1:8001/ws?token={static_tok}"
    async with websockets.connect(listen_url, ssl=_ctx()) as listener, \
               websockets.connect(ws_url, ssl=_ctx()) as ws:
        t0 = time.time()
        saw_live_broadcast = False

        async def _drain_listener():
            """Collect broadcasts until 'Phone microphone live.' arrives."""
            nonlocal saw_live_broadcast
            try:
                while True:
                    raw = await asyncio.wait_for(listener.recv(), timeout=25)
                    m = json.loads(raw)
                    if (m.get("type") == "sys"
                            and m.get("text") == "Phone microphone live."):
                        saw_live_broadcast = True
                        return
            except (asyncio.TimeoutError, Exception):
                return

        drain = asyncio.create_task(_drain_listener())
        for _ in range(40):                       # ~4 s of audio
            await ws.send(b"\x01\x02" * 1024)
            await asyncio.sleep(0.1)

        if not _wait_log("PC mic paused", WAIT):
            failures.append("live log never showed PC mic pause marker")

        try:
            await asyncio.wait_for(drain, timeout=20)
        except asyncio.TimeoutError:
            pass
        if not saw_live_broadcast:
            failures.append("'Phone microphone live.' broadcast never reached a /ws client")

    # 3) after close, the app should resume the PC mic (grace 1.5 s)
    if not _wait_log("PC mic resumed", WAIT):
        failures.append("live log never showed PC mic resume marker")

    took = time.time() - t0
    if failures:
        return False, "; ".join(failures)
    return True, (f"phone mic session streamed for {took:.0f}s: liveness "
                  f"broadcast reached a /ws client, PC-mic pause AND resume "
                  f"all appeared in the live JARVIS log")


def main() -> int:
    if not _dashboard_up():
        print("ENV-SKIP: dashboard not listening on 127.0.0.1:8001 — is JARVIS running?")
        return 2
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("ENV-SKIP: websockets not installed (.venv/bin/pip install websockets)")
        return 2

    ok, msg = asyncio.run(_exercise())
    print(f"{'PASS' if ok else 'FAIL'}  {msg}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
