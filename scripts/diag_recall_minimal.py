"""
scripts/diag_recall_minimal.py — validate the recall instrument itself.

diag_resumption_chain hop 1 (ONE continuous session, no resume involved)
received zero model content — so before blaming resumption, prove the
instrument: minimal config, no audio streaming, one text turn, print every
server message. If the model answers here, the silence stream / config is
what suppresses turns; if not, text-turn + AUDIO modality needs different
handling (e.g. response modality setup) in the diag.
"""

import asyncio
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from google import genai  # noqa: E402
from google.genai import types  # noqa: E402
import main as jarvis_main  # noqa: E402


def _ts() -> str:
    import time

    return time.strftime("%H:%M:%S")


async def main_async() -> int:
    client = genai.Client(
        api_key=jarvis_main._get_api_key(),
        http_options={"api_version": "v1alpha", "timeout": 120_000},
    )
    model = jarvis_main.LIVE_MODEL
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        output_audio_transcription={},
        session_resumption=types.SessionResumptionConfig(),
        system_instruction="You are a helpful assistant. Always answer in one short sentence.",
    )

    async def _work():
        async with client.aio.live.connect(model=model, config=config) as session:
            print(f"[{_ts()}] connected; sending text turn (no audio at all)…")
            await asyncio.wait_for(
                session.send_client_content(
                    turns={
                        "role": "user",
                        "parts": [
                            {
                                "text": "Remember the codeword pineapple-42. Now say: ready."
                            }
                        ],
                    },
                    turn_complete=True,
                ),
                timeout=15,
            )
            ait = session.receive().__aiter__()
            while True:
                try:
                    resp = await asyncio.wait_for(anext(ait), timeout=20)
                except asyncio.TimeoutError:
                    print(f"[{_ts()}] SILENT >20s — no more messages")
                    break
                except StopAsyncIteration:
                    break
                if getattr(resp, "session_resumption_update", None) is not None:
                    sru = resp.session_resumption_update
                    print(
                        f"[{_ts()}] sru resumable={getattr(sru, 'resumable', None)} "
                        f"handle={str(getattr(sru, 'new_handle', None))[:14]}"
                    )
                elif getattr(resp, "server_content", None) is not None:
                    sc = resp.server_content
                    ot = getattr(sc, "output_transcription", None)
                    txt = getattr(ot, "text", None) if ot else None
                    print(
                        f"[{_ts()}] content: transcript={txt!r} "
                        f"data={len(resp.data) if resp.data else 0}B "
                        f"turn_complete={getattr(sc, 'turn_complete', None)}"
                    )
                    if getattr(sc, "turn_complete", False):
                        break
                elif getattr(resp, "go_away", None) is not None:
                    print(f"[{_ts()}] go_away: {resp.go_away}")
                else:
                    print(f"[{_ts()}] other message: {str(resp)[:120]}")

    try:
        await asyncio.wait_for(_work(), timeout=90)
        print("\nVERDICT: instrument validated (model responded to a text turn)")
        return 0
    except asyncio.TimeoutError:
        print("\nVERDICT: session hung even with minimal config + no audio")
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
