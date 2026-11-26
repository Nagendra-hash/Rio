"""
scripts/diag_connect_handshake.py — raw wire capture of the Live connect handshake.

Why: production GoAway rotations get `1008 BidiGenerateContent session not found`
on EVERY connect that carries a handle (except the first rotation of a process).
The SDK surfaces only the close-frame reason. This script taps the websocket at
the transport level (monkeypatches google.genai.live.ws_connect) and also speaks
raw JSON to the endpoint, so we can see:

  - the exact setup TX the SDK produces (incl. sessionResumption.handle)
  - the server's first RX (setupComplete vs in-band error object)
  - close frames (code+reason) on every scenario
  - whether the server honours an undocumented `forceRegeneration` field
    (the Python SDK 1.65 has no such field; the old JS SDK did)

Scenarios:
  1 fresh-baseline   full sysinstr, handle=None, plant codeword, harvest handle
  2 resume-young     resume with scenario-1's handle (~seconds old), recall
  3 bogus-handle     resume with a fabricated handle (expect rejection shape)
  4 force-regen-fresh  fresh connect + setup.sessionResumption.forceRegeneration=true
                       (tests whether the server knows the field at all)
  5 tiny-sysinstr    SHORT sysinstr plant+resume chain (A/B vs 1+2 for the
                     ">200 tokens breaks resumption" forum/GitHub bug)

Also writes harvested handles to /tmp/diag_handles.json so other scripts can
resume them later (e.g. a handle from a GoAway-aged session).

Run:  .venv/bin/python scripts/diag_connect_handshake.py
"""

import asyncio
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import websockets  # noqa: E402
from google import genai  # noqa: E402
from google.genai import types  # noqa: E402
from google.genai import _common  # noqa: E402
from google.genai import _transformers as t  # noqa: E402
from google.genai import _live_converters as live_converters  # noqa: E402
from google.genai._common import set_value_by_path as setv  # noqa: E402
import google.genai.live as glive  # noqa: E402

import main as jarvis_main  # noqa: E402

CODEWORD = "pineapple-42"
HANDLE_FILE = Path("/tmp/diag_handles.json")


def _ts() -> str:
    return time.strftime("%H:%M:%S")


# ─── wire tap ────────────────────────────────────────────────────────────────


class _Tap:
    """Wraps a ClientConnection; logs TX/RX frames."""

    def __init__(self, ws):
        self._ws = ws

    def __getattr__(self, name):
        return getattr(self._ws, name)

    async def send(self, data):
        summary = data
        try:
            j = json.loads(data)
            setup = j.get("setup", {})
            sr = setup.get("sessionResumption", {})
            summary = (
                f"setup keys={sorted(setup.keys())} "
                f"sessionResumption={json.dumps(sr)[:140]} "
                f"sysinstr_len={len(json.dumps(setup.get('systemInstruction', '')))}"
            )
        except Exception:  # noqa: BLE001
            summary = str(data)[:140]
        print(f"[{_ts()}]   TX >> {summary}")
        return await self._ws.send(data)

    async def recv(self, *a, **k):
        r = await self._ws.recv(*a, **k)
        print(f"[{_ts()}]   RX << {str(r)[:180]}")
        return r

    async def __aenter__(self):
        await self._ws.__aenter__()
        return self

    async def __aexit__(self, *exc):
        return await self._ws.__aexit__(*exc)


class _TapCtx:
    def __init__(self, cm):
        self._cm = cm

    async def __aenter__(self):
        return _Tap(await self._cm.__aenter__())

    async def __aexit__(self, *exc):
        return await self._cm.__aexit__(*exc)


_orig_ws_connect = glive.ws_connect


def _tapped_ws_connect(uri, **kw):
    print(f"[{_ts()}] [ws] {uri}")
    return _TapCtx(_orig_ws_connect(uri, **kw))


glive.ws_connect = _tapped_ws_connect


# ─── setup JSON builder (mirrors the SDK's mldev connect path) ───────────────


async def build_setup_json_async(api_client, model, config, force_reg=False) -> str:
    parameter_model = await glive._t_live_connect_config(api_client, config)
    transformed_model = t.t_model(api_client, model)
    request_dict = _common.convert_to_dict(
        live_converters._LiveConnectParameters_to_mldev(
            api_client=api_client,
            from_object=types.LiveConnectParameters(
                model=transformed_model,
                config=parameter_model,
            ).model_dump(exclude_none=True),
        )
    )
    del request_dict["config"]
    request_dict = _common.encode_unserializable_types(request_dict)
    setv(request_dict, ["setup", "model"], transformed_model)
    if force_reg:
        setv(request_dict, ["setup", "sessionResumption", "forceRegeneration"], True)
    return json.dumps(request_dict)


# ─── raw scenario runner ─────────────────────────────────────────────────────


async def run_case(
    client,
    model,
    name,
    config,
    *,
    plant=False,
    recall=False,
    force_reg=False,
    idle_s=0.0,
    dial=False,
) -> dict:
    """One raw-socket session: connect → observe → (plant|recall) → close."""
    res = {
        "name": name,
        "status": "ok",
        "handle": None,
        "transcript": "",
        "first_rx": None,
        "close_code": None,
        "close_reason": None,
        "error_json": None,
        "setup_session_id": None,
    }
    api_client = client.aio.live._api_client
    ver = api_client._http_options.api_version
    uri = (
        f"{api_client._websocket_base_url()}/ws/"
        f"google.ai.generativelanguage.{ver}.GenerativeService.BidiGenerateContent"
    )
    headers = dict(api_client._http_options.headers or {})
    headers["x-goog-api-key"] = api_client.api_key
    request = await build_setup_json_async(
        api_client, model, config, force_reg=force_reg
    )

    print(f"[{_ts()}] ── {name} ──")
    try:
        async with websockets.connect(
            uri, additional_headers=headers, **api_client._websocket_ssl_ctx
        ) as ws:
            await ws.send(request)
            try:
                first = await asyncio.wait_for(ws.recv(), timeout=30)
                res["first_rx"] = str(first)[:200]
                j = json.loads(first)
                if "error" in j or "code" in j:
                    res["status"] = f"ERROR-JSON: {str(first)[:160]}"
                    print(f"[{_ts()}]   → {res['status']}")
                    return res
                sc = j.get("setupComplete", {})
                res["setup_session_id"] = sc.get("sessionId")
            except asyncio.TimeoutError:
                res["status"] = "TIMEOUT waiting for setupComplete"
                return res

            async def _drain(until_turn, budget):
                text, handle = "", None
                try:
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), timeout=budget)
                        m = json.loads(raw)
                        if "goAway" in m:
                            print(
                                f"[{_ts()}]   RX goAway (timeLeft={m['goAway'].get('timeLeft')})"
                            )
                        sru = m.get("sessionResumptionUpdate")
                        if sru and sru.get("resumable") and sru.get("newHandle"):
                            handle = sru["newHandle"]
                            print(f"[{_ts()}]   RX sru handle={handle[:14]}…")
                        sc = m.get("serverContent")
                        if sc:
                            ot = (sc.get("outputTranscription") or {}).get("text")
                            if ot:
                                text += ot
                            if sc.get("turnComplete"):
                                print(f"[{_ts()}]   RX turnComplete ({text.strip()!r})")
                                if until_turn:
                                    return text.strip(), handle
                        if "goAway" in m and not until_turn:
                            return text.strip(), handle
                except asyncio.TimeoutError:
                    return text.strip(), handle

            if plant or recall:
                prompt = (
                    f"Remember this codeword and repeat it only when I ask: "
                    f"{CODEWORD}. Now just say: ready."
                    if plant
                    else "Say only the codeword I told you earlier."
                )
                await ws.send(
                    json.dumps(
                        {
                            "clientContent": {
                                "turns": [
                                    {"role": "user", "parts": [{"text": prompt}]}
                                ],
                                "turnComplete": True,
                            }
                        }
                    )
                )
                text, handle = await _drain(until_turn=True, budget=20)
                res["transcript"] = text
                if recall:
                    ok = CODEWORD.split("-")[0] in text.lower()
                    res["status"] = "RECALLED" if ok else "FORGOT"
            if handle is None:
                text, handle = await _drain(until_turn=False, budget=idle_s or 10)
            res["handle"] = handle
    except websockets.exceptions.ConnectionClosed as e:
        code = e.rcvd.code if e.rcvd else None
        reason = e.rcvd.reason if e.rcvd else ""
        res["close_code"], res["close_reason"] = code, reason
        res["status"] = f"CLOSED {code} {reason}"[:160]
        print(f"[{_ts()}]   → close {code} {reason!r}")
    except Exception as e:  # noqa: BLE001
        res["status"] = f"EXC {type(e).__name__}: {str(e)[:120]}"
        print(f"[{_ts()}]   → {res['status']}")
    print(
        f"[{_ts()}]   ⇒ {name}: status={res['status']} "
        f"handle={(res['handle'] or 'None')[:14]}… transcript={res['transcript']!r}"
    )
    return res


def _save_handle(name, handle):
    data = {}
    if HANDLE_FILE.exists():
        try:
            data = json.loads(HANDLE_FILE.read_text())
        except Exception:  # noqa: BLE001
            pass
    data[name] = {"handle": handle, "ts": time.time()}
    HANDLE_FILE.write_text(json.dumps(data, indent=2))


async def main_async() -> int:
    j = jarvis_main.JarvisLive.__new__(jarvis_main.JarvisLive)
    # Minimal init: only what _build_config needs.
    j._resume_handle = None
    j._enhanced_live = False
    j._action_registry = type("R", (), {"get_tool_declarations": staticmethod(list)})()
    j._plugin_registry = type("R", (), {"get_tool_declarations": staticmethod(list)})()
    full_config = j._build_config()

    tiny_config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        output_audio_transcription={},
        system_instruction="You are JARVIS, a helpful assistant. Be brief.",
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Kore")
            )
        ),
    )

    client = genai.Client(
        api_key=jarvis_main._get_api_key(),
        http_options={"api_version": "v1alpha", "timeout": 120_000},
    )
    model = jarvis_main.LIVE_MODEL
    print(f"[{_ts()}] model={model} api_version=v1alpha codeword={CODEWORD}")

    results = []

    # 1 — fresh baseline, full sysinstr, plant codeword
    c1 = full_config.model_copy(deep=True)
    c1.session_resumption = types.SessionResumptionConfig(handle=None)
    r1 = await run_case(
        client, model, "1-fresh-baseline(full-sysinstr)", c1, plant=True, idle_s=12
    )
    results.append(r1)
    if r1["handle"]:
        _save_handle("full_sysinstr", r1["handle"])

    # 2 — resume with the young handle
    h1 = r1["handle"]
    if h1:
        await asyncio.sleep(2)
        c2 = full_config.model_copy(deep=True)
        c2.session_resumption = types.SessionResumptionConfig(handle=h1)
        r2 = await run_case(
            client, model, "2-resume-young(full-sysinstr)", c2, recall=True, idle_s=12
        )
        results.append(r2)
    else:
        print(f"[{_ts()}] !! no handle from case 1 — skipping resume case")

    # 3 — bogus handle (rejection shape reference)
    c3 = full_config.model_copy(deep=True)
    c3.session_resumption = types.SessionResumptionConfig(
        handle="session_resumption/00000000-0000-0000-0000-000000000000"
    )
    r3 = await run_case(
        client, model, "3-bogus-handle(full-sysinstr)", c3, recall=False, idle_s=8
    )
    results.append(r3)

    # 4 — forceRegeneration injected into setup (undocumented field probe)
    c4 = full_config.model_copy(deep=True)
    c4.session_resumption = types.SessionResumptionConfig(handle=None)
    r4 = await run_case(
        client,
        model,
        "4-force-regen(fresh,full-sysinstr)",
        c4,
        force_reg=True,
        idle_s=10,
    )
    results.append(r4)

    # 5 — tiny sysinstr plant+resume chain (length A/B)
    c5a = tiny_config.model_copy(deep=True)
    c5a.session_resumption = types.SessionResumptionConfig(handle=None)
    r5a = await run_case(
        client, model, "5a-tiny-sysinstr-plant", c5a, plant=True, idle_s=12
    )
    results.append(r5a)
    if r5a["handle"]:
        _save_handle("tiny_sysinstr", r5a["handle"])
        await asyncio.sleep(2)
        c5b = tiny_config.model_copy(deep=True)
        c5b.session_resumption = types.SessionResumptionConfig(handle=r5a["handle"])
        r5b = await run_case(
            client, model, "5b-tiny-sysinstr-resume", c5b, recall=True, idle_s=12
        )
        results.append(r5b)

    print("\n═══ HANDSHAKE CAPTURE SUMMARY ═══")
    for r in results:
        print(f"  {r['name']:38s} {r['status'][:70]}")
        if r["close_code"]:
            print(f"      close={r['close_code']} {r['close_reason']!r}")
        if r["setup_session_id"]:
            print(f"      setupComplete.sessionId={r['setup_session_id']}")
    print(f"\nhandles saved → {HANDLE_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
