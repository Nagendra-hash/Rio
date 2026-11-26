"""
scripts/diag_goaway_handle_probe.py — the decisive reaping experiment.

Production pattern: first rotation of a process resumes fine; every later
rotation (all GoAway-triggered, ~9.5 min sessions) gets
`1008 BidiGenerateContent session not found` when it presents its handle.

This probe answers the question directly, on the wire:

  Stage A  Age a FULL production-config session (silence audio, same recipe
           that reached GoAway in diag_resumption_age.py) until GoAway,
           recording every SRU handle with timestamps — specifically the LAST
           handle before GoAway and ANY handle sent after GoAway.
  Stage B  Immediately after closing (~1.5 s past GoAway, production-faithful),
           attempt fresh connects presenting, in order:
             B1  the last pre-GoAway handle     (production's exact case)
             B2  the post-GoAway handle, if any (did the server re-issue?)
             B3  control: a young handle from a brand-new short session
                 (proves handles in general still work TODAY)
  Verdict per connect: setupComplete (accepted) vs close frame 1008.

Usage:
  python scripts/diag_goaway_handle_probe.py age            # Stage A: grow a session to GoAway, harvest handles
  python scripts/diag_goaway_handle_probe.py check          # Stage B: reconnect trials (reads /tmp/goaway_probe_handles.json)
  python scripts/diag_goaway_handle_probe.py check --young  # Stage B + fresh young-handle control connect
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from google import genai  # noqa: E402
from google.genai import types  # noqa: E402

import main as jarvis_main  # noqa: E402

SILENCE_60MS = b"\x00" * 1920  # 60 ms int16 mono @ 16 kHz
GOAWAY_GRACE_S = 1.5  # production closes ~1 s after GoAway
STATE_FILE = Path("/tmp/goaway_probe_handles.json")


def _ts() -> str:
    return time.strftime("%H:%M:%S")


class _StubUI:
    def write_log(self, msg):
        print(f"[ui] {msg}")

    def __getattr__(self, name):
        return lambda *a, **k: None


async def stage_a(client, model, config) -> dict:
    out = {
        "pre_handle": None,
        "post_handle": None,
        "goaway": False,
        "age": 0.0,
        "pre_handle_age_at_goaway": None,
        "server_closed_us": None,
        "t_goaway": None,
    }

    def _save():
        STATE_FILE.write_text(
            json.dumps({"stage_a": out, "saved_at": time.time()}, indent=2)
        )

    t0 = time.monotonic()
    last_sru_time = None
    phase = "pre"

    async with client.aio.live.connect(model=model, config=config) as session:
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
                now = time.monotonic() - t0
                sru = getattr(resp, "session_resumption_update", None)
                if (
                    sru is not None
                    and getattr(sru, "resumable", False)
                    and getattr(sru, "new_handle", None)
                ):
                    last_sru_time = now
                    if phase == "pre":
                        out["pre_handle"] = sru.new_handle
                        _save()
                        print(
                            f"[{_ts()}]   sru(pre) {sru.new_handle[:14]}… @ {now:.0f}s"
                        )
                    else:
                        out["post_handle"] = sru.new_handle
                        _save()
                        print(
                            f"[{_ts()}]   sru(POST-GOAWAY) {sru.new_handle[:14]}… @ {now:.0f}s"
                        )
                ga = getattr(resp, "go_away", None)
                if ga is not None and not out["goaway"]:
                    out["goaway"] = True
                    out["t_goaway"] = now
                    out["pre_handle_age_at_goaway"] = (
                        now - last_sru_time if last_sru_time is not None else None
                    )
                    phase = "post"
                    _save()
                    print(
                        f"[{_ts()}]   GoAway @ {now:.0f}s "
                        f"(timeLeft={getattr(ga, 'time_left', '?')}) "
                        f"pre-handle last seen {out['pre_handle_age_at_goaway']}s before"
                    )
                    await asyncio.sleep(GOAWAY_GRACE_S)
                if out["goaway"] and (time.monotonic() - t0) > (
                    out["t_goaway"] + GOAWAY_GRACE_S + 3
                ):
                    break  # collected post-GoAway SRUs for ~3 s; done
        except Exception as e:  # noqa: BLE001
            out["server_closed_us"] = time.monotonic() - t0
            _save()
            print(
                f"[{_ts()}]   server closed the session: {type(e).__name__}: {str(e)[:110]}"
            )
        finally:
            stop.set()
            sender.cancel()
    out["age"] = time.monotonic() - t0
    return out


async def try_resume(
    client, model, base_config, label: str, handle: str, probe_recall: bool = True
) -> dict:
    res = {
        "label": label,
        "handle": handle,
        "accepted": False,
        "err": None,
        "transcript": "",
    }
    cfg = base_config.model_copy(deep=True)
    cfg.session_resumption = types.SessionResumptionConfig(handle=handle)
    try:
        async with client.aio.live.connect(model=model, config=cfg) as session:
            res["accepted"] = True
            print(f"[{_ts()}]   [{label}] setupComplete — handle ACCEPTED")
            # Best-effort recall probe (full-config model often stays silent
            # on text turns — silence is NOT proof of context loss; the
            # accept/reject verdict is the datum here).
            if probe_recall:

                async def _recall():
                    await session.send_client_content(
                        turns={
                            "role": "user",
                            "parts": [
                                {"text": "Say only the codeword " "I told you earlier."}
                            ],
                        },
                        turn_complete=True,
                    )
                    async for resp in session.receive():
                        sc = getattr(resp, "server_content", None)
                        if sc is not None:
                            ot = getattr(sc, "output_transcription", None)
                            if ot is not None and getattr(ot, "text", None):
                                res["transcript"] += ot.text
                            if getattr(sc, "turn_complete", False):
                                return
                    # Cancelling this generator on timeout is fine: the
                    # session is closed immediately after the probe.

                try:
                    await asyncio.wait_for(_recall(), timeout=12)
                except Exception:  # noqa: BLE001
                    pass
    except Exception as e:  # noqa: BLE001 — rejection IS the datum
        res["err"] = f"{type(e).__name__}: {str(e)[:120]}"
        print(f"[{_ts()}]   [{label}] REJECTED → {res['err']}")
    return res


async def young_handle(client, model, base_config) -> str | None:
    """Control: harvest a handle from a fresh ~15 s session (voluntary close)."""
    _t0 = time.monotonic()
    cfg = base_config.model_copy(deep=True)
    cfg.session_resumption = types.SessionResumptionConfig(handle=None)
    latest = None
    async with client.aio.live.connect(model=model, config=cfg) as session:
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
                if time.monotonic() > _t0 + 15:
                    break
        except Exception:  # noqa: BLE001
            pass
        finally:
            stop.set()
            sender.cancel()
    return latest


def _mk():
    j = jarvis_main.JarvisLive(_StubUI())
    j._resume_handle = None
    j._enhanced_live = False  # proactivity off: isolates reaping from
    # the proactive-audio variable entirely
    base_config = j._build_config()
    base_config.session_resumption = types.SessionResumptionConfig(handle=None)
    client = genai.Client(
        api_key=jarvis_main._get_api_key(),
        http_options={"api_version": "v1alpha", "timeout": 120_000},
    )
    return client, base_config


async def race_cmd(gap_s: float) -> int:
    """A/B/C: same young-handle lineage, three close styles and gaps.
      A  clean close (normal context exit) + gap_s   → resume
      B  ABORTIVE close (transport fail, like an exception unwind) + gap_s → resume
      C  clean close + 2.5s gap                      → resume (control)
    Production = B with gap≈0.3-1s (TaskGroup unwind on _ReconnectSignal).
    """
    client, base_config = _mk()
    model = jarvis_main.LIVE_MODEL
    results = []

    async def _harvest(abortive: bool):
        """Fresh short session; if abortive, kill the transport mid-run
        (mimics prod's exception unwind), else exit cleanly."""
        cfg = base_config.model_copy(deep=True)
        cfg.session_resumption = types.SessionResumptionConfig(handle=None)
        latest = None
        try:
            async with client.aio.live.connect(model=model, config=cfg) as session:
                stop = asyncio.Event()

                async def _silence():
                    while not stop.is_set():
                        try:
                            await session.send_realtime_input(
                                audio=types.Blob(
                                    data=SILENCE_60MS, mime_type="audio/pcm"
                                )
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
                        if time.monotonic() > _t0 + 13:
                            break
                finally:
                    if abortive:
                        # Production's unwind: the TaskGroup cancels children
                        # and the websocket dies without a graceful close.
                        ws = getattr(session, "_ws", None) or getattr(
                            session, "websocket", None
                        )
                        try:
                            await ws.fail_connection(code=1006)
                        except Exception:  # noqa: BLE001
                            try:
                                ws.transport.abort()
                            except Exception:  # noqa: BLE001
                                pass
                    stop.set()
                    sender.cancel()
        except Exception:  # noqa: BLE001 — abortive path raises; handle kept
            pass
        return latest

    _t0 = time.monotonic()
    for label, abortive, gap in (
        ("A-clean+%.1fs" % gap_s, False, gap_s),
        ("B-abortive+%.1fs" % gap_s, True, gap_s),
        ("C-clean+2.5s", False, 2.5),
    ):
        h = await _harvest(abortive)
        if not h:
            print(f"[{_ts()}]   [{label}] no handle harvested — skip")
            results.append({"label": label, "accepted": None, "err": "no handle"})
            continue
        await asyncio.sleep(gap)
        r = await try_resume(client, model, base_config, label, h, probe_recall=False)
        results.append(r)
        await asyncio.sleep(1)

    print("\n═══ RACE VERDICT (gap=%.1fs) ═══" % gap_s)
    for r in results:
        v = {True: "ACCEPTED", False: "REJECTED", None: "SKIPPED"}[r["accepted"]]
        print(f"  {r['label']:22s} {v:9s} {r['err'] or ''}")
    return 0


async def live_cmd() -> int:
    """Mechanism test: present a handle while its OWNER session is still open.
    Production signature: reconnect fires ~1s after GoAway; if the old
    socket's teardown hasn't completed server-side, the handle belongs to a
    LIVE session. Steps:
      1  open A (silence), harvest handle H, keep A OPEN
      2  open B presenting H  →  if 1008 'session not found' → mechanism proven
      3  close A and B; open C presenting H  →  expect ACCEPTED
    """
    client, base_config = _mk()
    model = jarvis_main.LIVE_MODEL

    async def _open(handle, hold_s=12.0, label=""):
        cfg = base_config.model_copy(deep=True)
        cfg.session_resumption = types.SessionResumptionConfig(handle=handle)
        outcome = {"accepted": False, "handle": None, "err": None}
        try:
            async with client.aio.live.connect(model=model, config=cfg) as session:
                outcome["accepted"] = True
                stop = asyncio.Event()

                async def _silence():
                    while not stop.is_set():
                        try:
                            await session.send_realtime_input(
                                audio=types.Blob(
                                    data=SILENCE_60MS, mime_type="audio/pcm"
                                )
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
                            outcome["handle"] = sru.new_handle
                        if time.monotonic() > _tl + hold_s:
                            break
                finally:
                    stop.set()
                    sender.cancel()
        except Exception as e:  # noqa: BLE001
            outcome["err"] = f"{type(e).__name__}: {str(e)[:100]}"
        print(
            f"[{_ts()}]   [{label}] "
            f"{'setupComplete' if outcome['accepted'] else 'REJECTED ' + (outcome['err'] or '')}"
        )
        return outcome

    print(f"[{_ts()}] step 1: open session A (fresh), harvest H, keep A OPEN")
    _tl = time.monotonic()
    a = await _open(None, label="A(fresh)")
    if not a["accepted"] or not a["handle"]:
        print(f"[{_ts()}] A failed: {a['err']} — aborting")
        return 1
    h = a["handle"]

    print(f"[{_ts()}] step 2: open B presenting H while A is STILL OPEN")
    _tl = time.monotonic()
    b = await _open(h, label="B(resume-H, A-live)")

    print(f"[{_ts()}] step 3: A and B now closed — open C presenting H")
    await asyncio.sleep(2)
    _tl = time.monotonic()
    c = await _open(h, label="C(resume-H, A-closed)")

    print("\n═══ LIVE-OVERLAP VERDICT ═══")
    print(
        f"  B resume while owner live : {'ACCEPTED' if b['accepted'] else 'REJECTED — ' + (b['err'] or '')}"
    )
    print(
        f"  C resume after owner closed: {'ACCEPTED' if c['accepted'] else 'REJECTED — ' + (c['err'] or '')}"
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["age", "check", "race", "live"])
    ap.add_argument(
        "--young",
        action="store_true",
        help="check: include the young-handle control connect",
    )
    ap.add_argument(
        "--gap", type=float, default=0.4, help="race: close→connect gap in seconds"
    )
    args = ap.parse_args()
    if args.cmd == "age":
        return asyncio.run(cmd_age())
    if args.cmd == "race":
        return asyncio.run(race_cmd(args.gap))
    if args.cmd == "live":
        return asyncio.run(live_cmd())
    return asyncio.run(cmd_check(args.young))


async def cmd_age() -> int:
    client, base_config = _mk()
    model = jarvis_main.LIVE_MODEL
    print(
        f"[{_ts()}] STAGE A: age a full-config session to GoAway "
        f"(model={model}, api=v1alpha, silence stream, proactivity off)"
    )
    a = await stage_a(client, model, base_config)
    print(
        f"[{_ts()}] STAGE A result: age={a['age']:.0f}s goaway={a['goaway']} "
        f"pre_handle={(a['pre_handle'] or 'None')[:14]}… "
        f"post_handle={(a['post_handle'] or 'None')[:14]}… "
        f"server_closed_us={a['server_closed_us']}"
    )
    print(f"[{_ts()}] state in {STATE_FILE} — now run: check --young")
    return 0


async def cmd_check(with_young: bool) -> int:
    if not STATE_FILE.exists():
        print(f"no {STATE_FILE} — run `age` first")
        return 1
    a = json.loads(STATE_FILE.read_text())["stage_a"]
    client, base_config = _mk()
    model = jarvis_main.LIVE_MODEL
    print(f"[{_ts()}] STAGE B: reconnect trials (each with a FRESH connect)")
    trials = []
    if a.get("pre_handle"):
        trials.append(
            await try_resume(
                client, model, base_config, "B1 pre-goaway-handle", a["pre_handle"]
            )
        )
        await asyncio.sleep(2)
    else:
        print(f"[{_ts()}]   B1 skipped — no pre-GoAway handle captured")
    if a.get("post_handle"):
        trials.append(
            await try_resume(
                client, model, base_config, "B2 post-goaway-handle", a["post_handle"]
            )
        )
        await asyncio.sleep(2)
    else:
        print(f"[{_ts()}]   B2 skipped — server sent no post-GoAway handle")
    if with_young:
        print(f"[{_ts()}]   B3 control: young handle from a fresh 15 s session…")
        yh = await young_handle(client, model, base_config)
        if yh:
            await asyncio.sleep(2)
            trials.append(
                await try_resume(
                    client, model, base_config, "B3 young-handle-control", yh
                )
            )
        else:
            print(f"[{_ts()}]   B3 skipped — fresh session yielded no handle")

    print("\n═══ GOAWAY HANDLE PROBE — VERDICT ═══")
    for t in trials:
        v = "ACCEPTED" if t["accepted"] else "REJECTED"
        extra = f" recall={t['transcript'].strip()!r}" if t["accepted"] else ""
        print(f"  {t['label']:26s} {v:9s} {t['err'] or ''}{extra}")
    if not a.get("goaway"):
        print(
            "  (WARNING: Stage A never saw GoAway — inconclusive for the "
            "GoAway-reaping hypothesis)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
