"""
scripts/diag_resumption_hops.py — isolate the 2026-09-28 resumption reaping.

Production evidence (journald, jarvis-gui):
  - Rotation 1 after process start: handle accepted, conversation restored.
  - Every rotation 2+: connect with an ARMED handle (<60s old) rejected with
    APIError: 1008 None. BidiGenerateContent session not found.
  - SRU firehose: a NEW resumable handle every ~1.5s (while mic audio flows),
    stored correctly client-side.

Suspect (forum, open bug, Aug 2026): resumption broken for
gemini-3.1-flash-live-preview with long system instructions.
  https://discuss.ai.google.dev/t/session-resumption-for-gemini-3-1-flash-live-preview-does-not-seem-to-work-with-long-systeminstructions/136654

Method: hop sessions WITHOUT waiting for GoAway — the rejection happens at
connect time, so each hop is: connect → stream mic-like silence → take a
short text turn → capture latest resumable handle → close → reconnect with it.

  A  minimal config (tiny sysinstr, nothing else)
  B  EXACT production config via JarvisLive._build_config()
  C  B minus context_window_compression   ┐ bisect the production-only
  D  B minus tools                        │ ingredients when B fails
  E  B on v1beta                          ┘

Run:  .venv/bin/python scripts/diag_resumption_hops.py
"""

import asyncio
import json
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


SILENCE_60MS = b"\x00" * 1920  # 60 ms of int16 mono silence @ 16 kHz


def _client(api_version: str) -> genai.Client:
    return genai.Client(
        api_key=jarvis_main._get_api_key(),
        http_options={"api_version": api_version, "timeout": 120_000},
    )


def _strip_cfg(cfg: types.LiveConnectConfig, drop: str) -> types.LiveConnectConfig:
    d = cfg.model_copy(deep=True).model_dump(exclude_none=True)
    d.pop(drop, None)
    return types.LiveConnectConfig.model_validate(d)


async def _one_hop(client: genai.Client, model: str, config) -> tuple[str | None, str]:
    """Connect, stream silence like the mic, take a short turn, return the
    latest resumable handle. status: ok / NO-HANDLE / TIMEOUT / REJECTED."""

    async def _session_work() -> str:
        async with client.aio.live.connect(model=model, config=config) as session:
            got: str | None = None
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
                # Stream for a few seconds first (mirrors production, where
                # handles arrive while the mic runs), then force a turn.
                await asyncio.sleep(4)
                await session.send_client_content(
                    turns={
                        "role": "user",
                        "parts": [{"text": "Reply with exactly: ok"}],
                    },
                    turn_complete=True,
                )
                deadline = asyncio.get_event_loop().time() + 40
                async for resp in session.receive():
                    sru = getattr(resp, "session_resumption_update", None)
                    if (
                        sru is not None
                        and getattr(sru, "resumable", False)
                        and getattr(sru, "new_handle", None)
                    ):
                        got = sru.new_handle
                    sc = getattr(resp, "server_content", None)
                    if sc is not None and getattr(sc, "turn_complete", False) and got:
                        break
                    if asyncio.get_event_loop().time() > deadline:
                        break
                # Linger with audio flowing so the server materializes state.
                await asyncio.sleep(5)
            finally:
                stop.set()
                sender.cancel()
            return got or ""

    try:
        got = await asyncio.wait_for(_session_work(), timeout=70)
        if not got:
            return None, "NO-HANDLE: no resumable handle in the whole session"
        return got, "ok"
    except asyncio.TimeoutError:
        return None, "TIMEOUT after 70s"
    except Exception as e:  # noqa: BLE001 — the rejection IS the datum
        return None, f"REJECTED: {str(e)[:120]}"


async def run_series(
    tag: str, label: str, api_version: str, config, hops: int
) -> list[str]:
    print(f"\n═══ series {tag}: {label} ═══")
    client = _client(api_version)
    model = jarvis_main.LIVE_MODEL
    handle: str | None = None
    results = []
    for hop in range(1, hops + 1):
        h, status = await _one_hop(client, model, config)
        print(
            f"  hop {hop}: sent={'None(fresh)' if handle is None else handle[:14] + '…'} "
            f"→ {status}" + (f", got {h[:14]}…" if h else "")
        )
        results.append(f"{tag}{hop} [{label}]: sent={str(handle)[:14]} {status}")
        if h is None:
            break
        handle = h
        await asyncio.sleep(2)
    return results


async def main_async() -> int:
    j = jarvis_main.JarvisLive(_StubUI())
    tiny_cfg = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        session_resumption=types.SessionResumptionConfig(),
        system_instruction="You are a helpful assistant.",
    )
    j._resume_handle = None
    prod_cfg = j._build_config()
    print(
        f"minimal sysinstr: {len('You are a helpful assistant.'):,} chars | "
        f"production config built (sysinstr "
        f"{len(prod_cfg.system_instruction):,} chars, "
        f"tools={prod_cfg.tools is not None}, "
        f"compression={prod_cfg.context_window_compression is not None})"
    )

    out: list[str] = []
    out += await run_series("A", "minimal", "v1alpha", tiny_cfg, 3)

    out += await run_series(
        "B",
        "production config",
        j._api_version_str() if hasattr(j, "_api_version_str") else "v1alpha",
        prod_cfg,
        3,
    )
    b_failed = any("REJECTED" in r for r in out[-3:])

    if b_failed:
        out += await run_series(
            "C",
            "production minus compression",
            "v1alpha",
            _strip_cfg(prod_cfg, "context_window_compression"),
            3,
        )
        out += await run_series(
            "D", "production minus tools", "v1alpha", _strip_cfg(prod_cfg, "tools"), 3
        )
        out += await run_series("E", "production on v1beta", "v1beta", prod_cfg, 2)

    print("\n═══ SUMMARY ═══")
    for r in out:
        print(" ", r)
    fails = [r for r in out if "REJECTED" in r]
    if fails:
        print("\nVERDICT: REPRODUCED — failing rows above identify the trigger")
    else:
        print("\nVERDICT: NOT reproduced in isolation — re-read production logs")
    Path("/tmp/resumption_diag.json").write_text(json.dumps(out, indent=2))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
