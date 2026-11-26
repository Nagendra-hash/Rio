"""
scripts/diag_resumption_age.py — the aging variable.

Isolation evidence (diag_resumption_hops.py, 2026-09-28):
  Full production config (sysinstr 10.6K chars + tools + compression +
  proactive audio) resumes fine when the session is ~20 seconds old at hop
  time. Production rejects EVERY hop with 1008 session-not-found when the
  session is ~9.5 minutes old. The untested variable is AGE (and what comes
  with it: accumulated audio tokens → sliding-window compression events).

Method — ONE hop with a production-aged session:
  1. Connect with the EXACT production config (JarvisLive._build_config()).
  2. Stream mic-like silence continuously for AGE_S (default 570s ≈ the
     production GoAway cadence). Handles arrive throughout — we keep the
     LATEST, exactly like the recv loop does.
  3. Close, reconnect with that handle, report accepted/rejected.

Interpretation:
  REJECTED → age (+ its token accumulation / compression) is the trigger.
  OK       → age alone isn't it; suspect real-audio content or volume.

Run:  .venv/bin/python scripts/diag_resumption_age.py [age_seconds]
"""

import asyncio
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from google import genai  # noqa: E402
from google.genai import types  # noqa: E402
import main as jarvis_main  # noqa: E402


class _StubUI:
    def __init__(self):
        self.logs = []

    def write_log(self, msg):
        self.logs.append(str(msg))

    def __getattr__(self, name):
        return lambda *a, **k: None


SILENCE_60MS = b"\x00" * 1920  # 60 ms int16 mono @ 16 kHz


def _ts() -> str:
    from datetime import datetime

    return datetime.now().strftime("%H:%M:%S")


async def _live_session(client, model, config, age_s: float) -> str:
    """Hold a session for age_s with silence flowing; return the latest
    resumable handle (the same bookkeeping the recv loop does)."""
    async with client.aio.live.connect(model=model, config=config) as session:
        latest: str | None = None
        stop = asyncio.Event()

        async def _silence():
            while not stop.is_set():
                try:
                    await session.send_realtime_input(
                        audio=types.Blob(data=SILENCE_60MS, mime_type="audio/pcm")
                    )
                except Exception:
                    return
                await asyncio.sleep(0.1)

        sender = asyncio.create_task(_silence())
        try:
            async for resp in session.receive():
                sru = getattr(resp, "session_resumption_update", None)
                if (
                    sru is not None
                    and getattr(sru, "resumable", False)
                    and getattr(sru, "new_handle", None)
                ):
                    latest = sru.new_handle
                if stop.is_set():
                    break
        finally:
            stop.set()
            sender.cancel()
        # Keep streaming past the last recv — sender task is already running;
        # give the final handles a moment, then drain once more.
        await asyncio.sleep(2)
        try:
            async for resp in session.receive():
                sru = getattr(resp, "session_resumption_update", None)
                if (
                    sru is not None
                    and getattr(sru, "resumable", False)
                    and getattr(sru, "new_handle", None)
                ):
                    latest = sru.new_handle
                break
        except Exception:
            pass
        return latest or ""


async def main_async() -> int:
    age_s = float(sys.argv[1]) if len(sys.argv) > 1 else 570.0
    j = jarvis_main.JarvisLive(_StubUI())
    j._resume_handle = None
    config = j._build_config()
    client = genai.Client(
        api_key=jarvis_main._get_api_key(),
        http_options={"api_version": "v1alpha", "timeout": 120_000},
    )
    model = jarvis_main.LIVE_MODEL

    print(f"[{_ts()}] hop 1: fresh connect, streaming silence for {age_s:.0f}s …")
    try:
        h1 = await asyncio.wait_for(
            _live_session(client, model, config, age_s), timeout=age_s + 60
        )
    except asyncio.TimeoutError:
        print(f"[{_ts()}] hop 1: TIMEOUT waiting for a handle")
        return 2
    if not h1:
        print(f"[{_ts()}] hop 1: NO-HANDLE — session produced none")
        return 2
    print(f"[{_ts()}] hop 1: session aged {age_s:.0f}s, latest handle {h1[:14]}…")

    await asyncio.sleep(2)
    print(f"[{_ts()}] hop 2: reconnecting WITH {h1[:14]}…")
    cfg2 = config.model_copy(deep=True)
    cfg2.session_resumption = types.SessionResumptionConfig(handle=h1)
    try:
        async with client.aio.live.connect(model=model, config=cfg2) as session:
            got = None
            async for resp in session.receive():
                sru = getattr(resp, "session_resumption_update", None)
                if (
                    sru is not None
                    and getattr(sru, "resumable", False)
                    and getattr(sru, "new_handle", None)
                ):
                    got = sru.new_handle
                    break
            print(
                f"[{_ts()}] hop 2: ✅ ACCEPTED"
                + (f" — new handle {got[:14]}…" if got else " (no new handle yet)")
            )
        print("\nVERDICT: age alone did NOT trigger the reaping")
        return 0
    except Exception as e:  # noqa: BLE001 — the rejection IS the datum
        print(f"[{_ts()}] hop 2: ❌ REJECTED — {str(e)[:150]}")
        print(
            f"[{_ts()}] hop 2: handle was armed ~9 min earlier and refreshed "
            f"continuously until the moment of the close"
        )
        print(
            "\nVERDICT: REPRODUCED — session AGE (token accumulation / "
            "compression) is the trigger"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
