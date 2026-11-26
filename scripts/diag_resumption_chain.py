"""
scripts/diag_resumption_chain.py — the definitive resumption experiment.

Evidence so far (2026-09-28):
  - Production: every rotation 2+ rejected with 1008 session-not-found; all
    production rotations are GoAway-triggered (~9.5 min sessions).
  - diag_resumption_hops.py: young (~20s) sessions resume fine — even resumed
    ones — but never reached GoAway, and "accepted" was never verified as
    "context restored".
  - diag_resumption_age.py: a 9.5-min session receives its own GoAway; the
    server enforcement-kills clients that ignore it.

Hypothesis under test: resumption state is lost when a session REACHES its
duration limit (GoAway) — i.e. resume-after-GoAway-close fails, while
voluntary early closes resume fine. A codeword passed down the chain is the
ground truth: accepted-connect + correct recall = real resumption.

Modes:
  --mode early   AGE_S: each hop closes VOLUNTARILY at AGE_S (before GoAway)
  --mode goaway  each hop waits for its GoAway, closes ~1s later (production)

Run examples:
  .venv/bin/python scripts/diag_resumption_chain.py --mode early  --hop-age 60  --hops 3
  .venv/bin/python scripts/diag_resumption_chain.py --mode goaway --hops 3
  .venv/bin/python scripts/diag_resumption_chain.py --mode early  --hop-age 480 --hops 3
"""

import argparse
import asyncio
import math
import random
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from google import genai  # noqa: E402
from google.genai import types  # noqa: E402
import main as jarvis_main  # noqa: E402

CODEWORD = "pineapple-42"


class _StubUI:
    def __init__(self):
        self.logs = []

    def write_log(self, msg):
        self.logs.append(str(msg))

    def __getattr__(self, name):
        return lambda *a, **k: None


SILENCE_60MS = b"\x00" * 1920


def _speech_burst_60ms(rng: random.Random, loud: bool) -> bytes:
    """60 ms of speech-energy audio (formant-ish modulated noise) — enough to
    trigger server VAD and create real user turns, unlike constant silence."""
    amp = (24000 if loud else 6000) * math.sin(rng.random() * math.pi)
    out = bytearray()
    phase = rng.random() * 6.28
    for i in range(960):  # 60 ms @ 16 kHz
        f0 = 120 + 80 * math.sin(phase + i / 960 * 3)
        # crude glottal-ish buzz + noise → broadband speech-like energy
        v = 0.6 * math.sin(2 * math.pi * f0 * i / 16000) + 0.4 * (rng.random() * 2 - 1)
        out += int(max(-1, min(1, v)) * amp).to_bytes(2, "little", signed=True)
    return bytes(out)


def _ts() -> str:
    return time.strftime("%H:%M:%S")


async def _hop(
    client,
    model,
    config,
    hop_no: int,
    mode: str,
    hop_age: float,
    proactive: bool = False,
):
    """One session: connect (handle inside config), close per mode. Returns
    dict of observations.

    Modes:
      early   — no audio; plant/recall codeword via text turns (validated
              instrument: tiny-config recall works, 2s answers).
      goaway  — NO text turns (the accept/reject verdict is the datum, and
              text turns are unreliable under the full config anyway); stream
              60ms silence every 100ms so session time accrues, SRUs keep
              flowing, and GoAway arrives. Close ~1s after GoAway
              (production-faithful)."""
    res = {
        "hop": hop_no,
        "status": "ok",
        "handle": None,
        "recall": None,
        "goaway": False,
        "age": 0.0,
    }
    t0 = time.monotonic()
    goaway_mode = mode == "goaway"

    async def _session_work():
        async with client.aio.live.connect(model=model, config=config) as session:
            latest = None
            recall_text = ""
            asked = False
            stop = asyncio.Event()

            async def _silence():
                speech = getattr(config, "_diag_speech", False)
                rng = random.Random(hop_no * 7919)
                n = 0
                while not stop.is_set():
                    try:
                        # 2.5s burst / 7.5s rest cycle when --speech: real VAD
                        # activity (user turns) like an actual conversation;
                        # constant silence otherwise.
                        loud = speech and (n % 100) < 25
                        frame = (
                            _speech_burst_60ms(rng, loud) if speech else SILENCE_60MS
                        )
                        await session.send_realtime_input(
                            audio=types.Blob(data=frame, mime_type="audio/pcm")
                        )
                    except Exception:
                        return
                    n += 1
                    await asyncio.sleep(0.1)

            sender = None
            if goaway_mode:
                sender = asyncio.create_task(_silence())
            try:
                if goaway_mode:
                    # Accrue duration + keep SRUs flowing until GoAway.
                    pass
                else:
                    await asyncio.sleep(2)
                    if hop_no == 1:
                        prompt = (
                            f"Remember this codeword and repeat it only "
                            f"when I ask: {CODEWORD}. Now just say: ready."
                        )
                    else:
                        asked = True
                        prompt = "Say only the codeword I told you earlier."
                    await asyncio.wait_for(
                        session.send_client_content(
                            turns={"role": "user", "parts": [{"text": prompt}]},
                            turn_complete=True,
                        ),
                        timeout=15,
                    )

                # Pump-task reader: wait_for(anext(ait), timeout) CANCELS the
                # receive generator on timeout — cancelling anext() finalizes
                # the generator, so the next anext() raises StopAsyncIteration
                # and the loop ends prematurely. Instead a long-lived pump
                # task drains receive() into a queue; per-message deadlines
                # still turn "server went silent" into a finding, but the
                # generator is never cancelled.
                recv_q: asyncio.Queue = asyncio.Queue()
                _PUMP_DONE = object()

                async def _pump():
                    try:
                        async for msg in session.receive():
                            await recv_q.put(msg)
                    except Exception as e:  # noqa: BLE001
                        await recv_q.put(e)
                    finally:
                        await recv_q.put(_PUMP_DONE)

                pump = asyncio.create_task(_pump())
                last_recv_kind = "nothing"
                while True:
                    try:
                        resp = await asyncio.wait_for(recv_q.get(), timeout=15)
                    except asyncio.TimeoutError:
                        print(
                            f"[{_ts()}]   recv: SILENT >15s after {last_recv_kind} "
                            f"— server stopped sending (age {time.monotonic()-t0:.0f}s)"
                        )
                        res["recv_silent"] = True
                        break
                    if resp is _PUMP_DONE:
                        break
                    if isinstance(resp, BaseException):
                        print(f"[{_ts()}]   recv: error {str(resp)[:90]}")
                        break
                    if getattr(resp, "session_resumption_update", None) is not None:
                        last_recv_kind = "sru"
                    elif getattr(resp, "server_content", None) is not None:
                        last_recv_kind = "content"
                    elif getattr(resp, "go_away", None) is not None:
                        last_recv_kind = "goaway"
                    sru = getattr(resp, "session_resumption_update", None)
                    if (
                        sru is not None
                        and getattr(sru, "resumable", False)
                        and getattr(sru, "new_handle", None)
                    ):
                        latest = sru.new_handle
                        print(f"[{_ts()}]   recv: sru {sru.new_handle[:14]}…")
                    sc = getattr(resp, "server_content", None)
                    if sc is not None:
                        ot = getattr(sc, "output_transcription", None)
                        if ot is not None and getattr(ot, "text", None):
                            recall_text += ot.text
                        if getattr(sc, "turn_complete", False):
                            print(
                                f"[{_ts()}]   recv: turn_complete "
                                f"(transcript: {recall_text.strip()!r})"
                            )
                            # Linger for an SRU before ageing further.
                            await asyncio.sleep(6)
                    if getattr(resp, "go_away", None) is not None:
                        res["goaway"] = True
                        print(f"[{_ts()}]   recv: GoAway")
                        if goaway_mode:
                            # Production-faithful: close ~1s after GoAway.
                            await asyncio.sleep(1)
                            break
                        # early mode: ignore GoAway — keep ageing to hop_age
                        # (the server's grace gives us room; this mirrors
                        # mid-sentence deferral in production).
                    if mode == "early" and (time.monotonic() - t0) >= hop_age:
                        break
                    if goaway_mode and (time.monotonic() - t0) > 900:
                        break  # safety net if GoAway never comes
                res["recall"] = recall_text.strip()
                pump.cancel()
            finally:
                stop.set()
                if sender is not None:
                    sender.cancel()
            return latest or ""

    try:
        h = await asyncio.wait_for(
            _session_work(), timeout=(hop_age if mode == "early" else 900) + 60
        )
        res["age"] = time.monotonic() - t0
        if not h:
            res["status"] = "NO-HANDLE"
        else:
            res["handle"] = h
    except asyncio.TimeoutError:
        res["status"] = "TIMEOUT"
    except Exception as e:  # noqa: BLE001 — rejection IS the datum
        res["status"] = f"REJECTED: {str(e)[:110]}"
        res["age"] = time.monotonic() - t0
    return res


async def main_async() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["early", "goaway"], default="early")
    ap.add_argument("--hop-age", type=float, default=60.0)
    ap.add_argument("--hops", type=int, default=3)
    ap.add_argument(
        "--tiny",
        action="store_true",
        help="A/B: minimal config (short sysinstr, no tools/"
        "compression) — isolates the forum >200-token bug",
    )
    ap.add_argument(
        "--proactive",
        action="store_true",
        help="A/B: enable proactive audio (v1alpha ProactivityConfig) "
        "— production's last unreproduced ingredient",
    )
    ap.add_argument(
        "--compress",
        action="store_true",
        help="A/B: aggressive context-window compression "
        "(trigger 2048/target 1024) so it fires repeatedly "
        "before every GoAway",
    )
    ap.add_argument(
        "--speech",
        action="store_true",
        help="A/B: speech-energy bursts instead of silence — "
        "triggers VAD so the session has real user turns",
    )
    ap.add_argument(
        "--fresh-config-per-hop",
        action="store_true",
        help="A/B: rebuild config via _build_config() at every hop "
        "(production behaviour — embeds a fresh timestamp in "
        "sysinstr). Tests whether the server rejects a resume "
        "whose setup differs from the original session's.",
    )
    args = ap.parse_args()

    j = jarvis_main.JarvisLive(_StubUI())
    j._resume_handle = None
    # Proactive audio OFF by default (recall instrument); the --proactive arm
    # turns it ON — production's last ingredient no goaway test has included.
    j._enhanced_live = bool(args.proactive)
    if args.tiny:
        # Forum-bug A/B arm: same lineage protocol, short sysinstr, no tools,
        # no compression. If full-config chains get rejections but tiny
        # chains don't → sysinstr size is the trigger.
        config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            system_instruction="You are JARVIS, a helpful assistant. Be brief.",
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Kore")
                )
            ),
        )
    else:
        config = j._build_config()
        if args.compress:
            config.context_window_compression = types.ContextWindowCompressionConfig(
                trigger_tokens=2048,
                sliding_window=types.SlidingWindow(target_tokens=1024),
            )
    if args.speech:
        config._diag_speech = True  # marker read by _hop's sender
    client = genai.Client(
        api_key=jarvis_main._get_api_key(),
        http_options={"api_version": "v1alpha", "timeout": 120_000},
    )
    model = jarvis_main.LIVE_MODEL
    print(
        f"[{_ts()}] mode={args.mode} hop_age={args.hop_age} hops={args.hops} "
        f"tiny={args.tiny} proactive={args.proactive} compress={args.compress} "
        f"speech={args.speech} fresh_cfg={args.fresh_config_per_hop} "
        f"codeword={CODEWORD}"
    )

    handle = None
    results = []
    for hop in range(1, args.hops + 1):
        if args.fresh_config_per_hop:
            # Production parity: full rebuild per connect — new timestamp in
            # sysinstr, memory reloaded, etc.
            cfg = j._build_config()
        else:
            cfg = config.model_copy(deep=True)
        cfg.session_resumption = types.SessionResumptionConfig(handle=handle)
        print(
            f"[{_ts()}] hop {hop}: connect (handle="
            f"{'None(fresh)' if handle is None else handle[:14] + '…'}) …"
        )
        r = await _hop(
            client, model, cfg, hop, args.mode, args.hop_age, proactive=args.proactive
        )
        ok_recall = r["recall"] and CODEWORD.split("-")[0] in r["recall"].lower()
        print(
            f"[{_ts()}] hop {hop}: age={r['age']:.0f}s goaway={r['goaway']} "
            f"status={r['status']}"
            + (f" handle={r['handle'][:14]}…" if r["handle"] else "")
            + (f" recall={r['recall']!r}" if r["recall"] is not None else "")
        )
        verdict = "OK" if r["handle"] else "FAIL"
        if args.mode == "early" and hop > 1:
            verdict += " + RECALLED" if ok_recall else " + FORGOT"
        elif args.mode == "goaway":
            verdict += " (goaway" if r["goaway"] else " (NO-goaway"
            verdict += ", accepted)" if r["handle"] else ")"
        print(f"    → hop {hop} verdict: {verdict}")
        results.append({**r, "verdict": verdict})
        if not r["handle"]:
            break
        handle = r["handle"]
        await asyncio.sleep(2)

    print("\n═══ CHAIN SUMMARY ═══")
    for r in results:
        print(
            f"  hop {r['hop']}: {r['verdict']}  (age {r['age']:.0f}s, "
            f"goaway={r['goaway']}, recall={r['recall']!r})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
