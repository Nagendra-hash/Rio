"""
scripts/diag_live_speech_health.py — which Live model actually answers speech?

Why: 2026-09-30 gemini-3.1-flash-live-preview had a server-side incident: every
audio session died with `1011 Internal error` ~40s in and speech was never
transcribed, while the same wav transcribed perfectly via non-Live ASR and text
turns still worked on the same key. JARVIS was switched to
gemini-2.5-flash-native-audio-latest to ride it out.

Run this BEFORE switching back: it feeds a spoken question ("Hey Jarvis, what
time is it right now?") into each candidate model and reports whether the model
(a) transcribed the speech and (b) produced a spoken reply — and (c) how fast:
time to first input transcription, first model turn, and first spoken reply
(output transcription ≈ when the voice starts). The end summary ranks healthy
models fastest-first, so a degraded-but-alive model can be told apart from a
snappy one before committing "live_model" in config/api_keys.json to it.

Usage:
  .venv/bin/python scripts/diag_live_speech_health.py            # all candidates
  .venv/bin/python scripts/diag_live_speech_health.py 3.1        # name substring
  .venv/bin/python scripts/diag_live_speech_health.py --probe \
      models/gemini-3.1-flash-live-preview [--json]              # machine mode

Machine mode (--probe): checks ONE model and exits 0 when it produced a model
turn (i.e. safe to serve voice), 1 when it did not. --json adds a one-line JSON
verdict on stdout. This is what systemd/jarvis-live-model-watch.sh calls every
30 min to decide whether the historical default model has recovered.

Requires a test wav; synthesizes one with gTTS if no fixture is available.
"""

import asyncio
import json
import sys
import time
import wave
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from google import genai  # noqa: E402
from google.genai import types  # noqa: E402

WAV = Path("/tmp/test_speech.wav")
# Committed fallback so a probe never depends on gTTS + network TTS being up
# (2026-09-30 outage taught exactly this lesson: assume the incident extends
# to whatever auxiliary service the check itself would need).
FIXTURE_WAV = BASE / "tests" / "fixtures" / "speech_probe_16k.wav"
CHUNK = 2048
PATIENCE_NO_TURN_S = 40
MODELS = [
    "models/gemini-3.1-flash-live-preview",  # historical default
    "models/gemini-3.8-live",  # newest general Live model on this key
    "models/gemini-2.5-flash-native-audio-latest",  # 2026-09-30 failover (current)
    # Pinned snapshots of the failover family: the -latest alias can lag or
    # break independently of the underlying model, so probe the dated ones too.
    "models/gemini-2.5-flash-native-audio-preview-09-2025",
    "models/gemini-2.5-flash-native-audio-preview-12-2025",
]


def _ensure_speech_wav() -> None:
    if WAV.exists():
        return
    if FIXTURE_WAV.exists():
        WAV.write_bytes(FIXTURE_WAV.read_bytes())
        return
    from gtts import gTTS
    import subprocess

    t = gTTS("Hey Jarvis, what time is it right now?")
    t.save("/tmp/test_speech.mp3")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            "/tmp/test_speech.mp3",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-acodec",
            "pcm_s16le",
            str(WAV),
        ],
        check=True,
    )


def _load_audio() -> bytes:
    with wave.open(str(WAV), "rb") as w:
        return w.readframes(w.getnframes()) * 2


def _key() -> str:
    cfg = json.loads((BASE / "config" / "api_keys.json").read_text())
    return (cfg.get("gemini_api_key") or "").strip()


def _cfg() -> types.LiveConnectConfig:
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        output_audio_transcription={},
        input_audio_transcription={},
        system_instruction="You are JARVIS. Answer the user's question briefly in English.",
        session_resumption=types.SessionResumptionConfig(handle=None),
    )


async def _trial(model: str, audio: bytes) -> dict:
    """Feed the spoken question and score the model.

    Returns {"model_turn", "transcribed", "t_transcribe", "t_model_turn",
    "t_reply"} — the t_* fields are seconds from audio start to the first
    input transcription, first model turn, and first spoken reply
    (output transcription ≈ when the voice starts)."""
    client = genai.Client(
        api_key=_key(), http_options={"api_version": "v1alpha", "timeout": 120_000}
    )
    t0 = time.time()
    result = {
        "model_turn": False,
        "transcribed": False,
        "t_transcribe": None,
        "t_model_turn": None,
        "t_reply": None,
    }
    try:
        async with asyncio.timeout(75):
            async with client.aio.live.connect(model=model, config=_cfg()) as session:

                async def feed():
                    await asyncio.sleep(0.5)
                    for i in range(0, len(audio), CHUNK):
                        await session.send_realtime_input(
                            audio=types.Blob(
                                data=audio[i : i + CHUNK],
                                mime_type="audio/pcm;rate=16000",
                            )
                        )
                        await asyncio.sleep(0.032)

                feeder = asyncio.create_task(feed())
                async for msg in session.receive():
                    sc = getattr(msg, "server_content", None)
                    if sc is not None:
                        it = getattr(sc, "input_transcription", None)
                        if it is not None and getattr(it, "text", None):
                            if not result["transcribed"]:
                                result["t_transcribe"] = time.time() - t0
                                print(
                                    f"    transcribed at {result['t_transcribe']:.1f}s"
                                )
                            result["transcribed"] = True
                        if getattr(sc, "model_turn", None):
                            if result["t_model_turn"] is None:
                                result["t_model_turn"] = time.time() - t0
                                print(
                                    f"    model turn at {result['t_model_turn']:.1f}s"
                                )
                            result["model_turn"] = True
                        ot = getattr(sc, "output_transcription", None)
                        if ot is not None and getattr(ot, "text", None):
                            if result["t_reply"] is None:
                                result["t_reply"] = time.time() - t0
                            print(f"    reply: {ot.text.strip()[:70]!r}")
                        if getattr(sc, "turn_complete", None) and (
                            result["model_turn"]
                            or time.time() - t0 > PATIENCE_NO_TURN_S
                        ):
                            break
                feeder.cancel()
                extra = (
                    f" | first reply {result['t_reply']:.1f}s"
                    if result["t_reply"] is not None
                    else ""
                )
                print(
                    f"  => transcribed: {result['transcribed']} | "
                    f"MODEL TURN: {result['model_turn']} "
                    f"({time.time()-t0:.0f}s){extra}"
                )
                return result
    except Exception as e:
        print(f"  => FAILED {time.time()-t0:.0f}s: {type(e).__name__}: {str(e)[:120]}")
        result["failed"] = str(e)[:120]
        return result


async def main() -> None:
    argv = sys.argv[1:]

    # Machine mode: probe a single model, machine-readable verdict.
    if "--probe" in argv:
        i = argv.index("--probe")
        if i + 1 >= len(argv):
            print("--probe requires a model name", file=sys.stderr)
            raise SystemExit(2)
        model = argv[i + 1]
        as_json = "--json" in argv
        _ensure_speech_wav()
        r = await _trial(model, _load_audio())
        healthy = r["model_turn"]
        if as_json:
            print(
                json.dumps(
                    {
                        "model": model,
                        "model_turn": healthy,
                        "t_transcribe": r["t_transcribe"],
                        "t_model_turn": r["t_model_turn"],
                        "t_reply": r["t_reply"],
                        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    }
                )
            )
        raise SystemExit(0 if healthy else 1)

    flt = argv[0] if argv else ""
    _ensure_speech_wav()
    audio = _load_audio()
    results = []
    for model in MODELS:
        if flt and flt.lower() not in model.lower():
            continue
        print(f"=== {model} ===")
        results.append((model, await _trial(model, audio)))
        await asyncio.sleep(2)

    healthy = [(m, r) for m, r in results if r["model_turn"]]
    if not healthy:
        print("\nHEALTHY (safe to set as live_model): NONE — stay on current model")
        return

    def _speed(item):
        _, r = item
        if r["t_reply"] is not None:
            return r["t_reply"]
        return r["t_model_turn"] if r["t_model_turn"] is not None else 999.0

    healthy.sort(key=_speed)
    print("\nHEALTHY (safe to set as live_model), fastest first:")
    for rank, (m, r) in enumerate(healthy, 1):
        reply = f"{r['t_reply']:.1f}s" if r["t_reply"] is not None else "n/a"
        turn = f"{r['t_model_turn']:.1f}s" if r["t_model_turn"] is not None else "n/a"
        print(f"  {rank}. {m} — first reply {reply} | model turn {turn}")


if __name__ == "__main__":
    asyncio.run(main())
