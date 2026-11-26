"""
tests/test_gemini_live_keepalive.py — regression tests for the Live-socket
keepalive failure that surfaced as

    GEMINI_AUDIO_SEND_FAILED — ConnectionClosedError:
    sent 1011 (internal error) keepalive ping timeout;
    no close frame received

ROOT CAUSE (established, not guessed)
    `session.receive()` is the only reader of the Live WebSocket. `_receive_audio`
    awaited tool calls inline — `dev_agent`, `screen_agent`, a screen capture, any
    plugin — so while a tool ran, nothing pulled on the receive iterator.
    websockets buffers arriving frames and pauses reading once its queue passes
    the high-water mark (16 messages by default); a paused socket never processes
    PONG frames, so the client's own keepalive ping went unanswered and the
    library failed the connection itself with a 1011 "keepalive ping timeout".
    The audio sender was just the first code path to touch the dead socket.

WHAT THIS FILE PROVES
    1. The reader pump in `_live_messages` keeps pulling while the consumer is
       busy (a tool call, in miniature) — and still ends cleanly at
       `turn_complete`, and still passes the original exception object through.
    2. With the real websockets library: a frozen consumer loses the connection
       with exactly the production error string; a pumping consumer does not.
    3. One audio sender per session: a sender left over from a replaced session
       retires instead of draining (and double-sending) the new queue.
    4. A full mic queue drops the newest frame without raising on the audio
       thread, and the drop log is throttled.
    5. The classifiers name a keepalive/transport loss and exclude 1000/1007/1008
       and a generic 1011.
    6. The run loop treats a keepalive close as a network event: calm reconnect,
       resumption handle KEPT, attempt counter advanced.
    7. The keepalive values actually reach a real google-genai client
       (`_websocket_ssl_ctx`) and are used by `_open_session`.
    8. The audio format is untouched.

No live Gemini connection is used or claimed.

Run:  .venv/bin/python -m pytest tests/test_gemini_live_keepalive.py -q
      .venv/bin/python tests/test_gemini_live_keepalive.py      (standalone)
"""

import asyncio
import contextlib
import sys
import types as _types
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import main as jarvis_main  # noqa: E402
from google.genai import errors as genai_errors  # noqa: E402


# ── shared offline harness ───────────────────────────────────────────────────

class _StubUI:
    def __init__(self):
        self.logs: list[str] = []

    def write_log(self, msg: str):
        self.logs.append(str(msg))

    def set_state(self, state: str):
        pass

    def __getattr__(self, name):
        return lambda *a, **k: None


@contextlib.contextmanager
def _patch(obj, attr, value):
    had = hasattr(obj, attr)
    old = getattr(obj, attr, None)
    setattr(obj, attr, value)
    try:
        yield
    finally:
        if had:
            setattr(obj, attr, old)
        else:
            delattr(obj, attr)


@contextlib.contextmanager
def _no_dashboard():
    class _Disabled:
        def __init__(self, *a, **k):
            raise RuntimeError("dashboard disabled in test")

    saved = sys.modules.get("dashboard.server")
    sys.modules["dashboard.server"] = _types.SimpleNamespace(DashboardServer=_Disabled)
    try:
        yield
    finally:
        if saved is not None:
            sys.modules["dashboard.server"] = saved
        else:
            sys.modules.pop("dashboard.server", None)


@contextlib.contextmanager
def _offline_env():
    with _patch(jarvis_main, "get_wake_word_enabled", lambda: False), \
         _patch(jarvis_main, "get_push_to_talk_enabled", lambda: False), \
         _patch(jarvis_main, "get_brief_enabled", lambda: False), \
         _patch(jarvis_main, "get_input_device", lambda: None), \
         _patch(jarvis_main, "get_output_device", lambda: None), \
         _patch(jarvis_main, "_get_api_key", lambda: "test-key"), \
         _patch(jarvis_main, "audio_devices",
                _types.SimpleNamespace(configure=lambda *a, **k: None,
                                       prefetch=lambda *a, **k: None)):
        yield


def _make_jarvis() -> "jarvis_main.JarvisLive":
    return jarvis_main.JarvisLive(_StubUI())


# ── a session whose receive() we can script ──────────────────────────────────

class _ScriptedSession:
    """`receive()` yields `count` messages, `delay` apart, then ends — or raises
    `error` after the last one. `pulled` counts what the socket actually read,
    independently of what the consumer has handled."""

    def __init__(self, count: int, delay: float = 0.005, error: Exception | None = None):
        self.count = count
        self.delay = delay
        self.error = error
        self.pulled = 0
        self.receive_calls = 0

    async def _gen(self):
        self.receive_calls += 1
        for i in range(self.count):
            await asyncio.sleep(self.delay)
            self.pulled += 1
            yield {"i": i}
        if self.error is not None:
            raise self.error

    def receive(self):
        return self._gen()


class _CountingSession:
    """Records what the audio sender hands to the session."""

    def __init__(self):
        self.sent = []

    async def send_realtime_input(self, audio=None):
        self.sent.append(audio)


# ── 1. the reader pump ───────────────────────────────────────────────────────

def test_pump_reads_while_the_consumer_is_busy():
    """CORE REGRESSION: the whole point of the pump.

    40 messages arrive 5 ms apart (~200 ms). The consumer handles the first one
    and then sits in a 300 ms "tool call". Before the pump, nothing pulled on
    `receive()` during those 300 ms and websockets started buffer-and-pause;
    now every message must already be read by the time the consumer looks again.
    """
    async def _scenario():
        j = _make_jarvis()
        session = _ScriptedSession(40, delay=0.005)
        seen, state = [], {"pulled_while_busy": None}

        async for msg in j._live_messages(session):
            seen.append(msg["i"])
            if state["pulled_while_busy"] is None:
                await asyncio.sleep(0.30)          # a tool call, in miniature
                state["pulled_while_busy"] = session.pulled
        return seen, state["pulled_while_busy"], session.receive_calls

    seen, pulled_while_busy, calls = asyncio.run(_scenario())
    assert pulled_while_busy == 40, (
        f"only {pulled_while_busy}/40 messages were read while the consumer was "
        f"busy — a paused reader is what fails the keepalive ping")
    assert seen == list(range(40)), "the pump changed message order or lost one"
    assert calls == 1, "receive() was re-entered instead of read once"


def test_pump_ends_at_turn_complete_and_clears_itself():
    """`receive()` ends at every turn_complete; the wrapper must return, not
    spin or leave the pump task behind."""
    async def _scenario():
        j = _make_jarvis()
        got = [m["i"] async for m in j._live_messages(_ScriptedSession(3))]
        return got, getattr(j, "_recv_pump", "unset")

    got, pump = asyncio.run(_scenario())
    assert got == [0, 1, 2]
    assert pump is None, "the pump task was left attached after the stream ended"


def test_pump_re_raises_the_original_exception_object():
    """The run loop's classifiers inspect the exception; a copied or wrapped
    exception would change the branch it takes."""
    err = genai_errors.APIError(1011, "keepalive ping timeout", None)

    async def _scenario():
        j = _make_jarvis()
        try:
            async for _ in j._live_messages(_ScriptedSession(2, error=err)):
                pass
        except BaseException as e:      # noqa: BLE001 — the assertion is identity
            return e
        return None

    assert asyncio.run(_scenario()) is err


def test_a_new_stream_retires_the_previous_pump():
    """If an exception ended the consumer's body, its generator may still be
    suspended with a pump inside `session.receive()`. A second reader on one
    socket is a ConcurrencyError, so the next stream must cancel it."""
    async def _scenario():
        j = _make_jarvis()
        first = _ScriptedSession(10_000, delay=0.001)
        gen1 = j._live_messages(first)
        await gen1.__anext__()
        await asyncio.sleep(0.05)
        pulled_before = first.pulled

        got2 = [m["i"] async for m in j._live_messages(_ScriptedSession(3))]
        await asyncio.sleep(0.05)
        pulled_after = first.pulled
        await gen1.aclose()
        return pulled_before, pulled_after, got2

    before, after, got2 = asyncio.run(_scenario())
    assert got2 == [0, 1, 2]
    assert after <= before + 1, (
        f"the abandoned stream kept reading ({before} -> {after}): two readers "
        f"on one socket")


# ── 2. the same failure, against the real websockets library ────────────────

async def _websockets_keepalive_probe(consume: bool) -> str:
    websockets_client = pytest.importorskip("websockets.asyncio.client")
    websockets_server = pytest.importorskip("websockets.asyncio.server")
    connect = websockets_client.connect
    serve = websockets_server.serve

    async def handler(ws):
        # More frames than the client's default buffer (16), then silence.
        for i in range(40):
            await ws.send("payload-%02d" % i)
        await asyncio.sleep(5)

    async with serve(handler, "127.0.0.1", 0, ping_interval=None) as server:
        port = server.sockets[0].getsockname()[1]

        async def _drain(ws):
            async for _ in ws:
                pass

        try:
            # The real library's keepalive, scaled down to test speed.
            async with connect(f"ws://127.0.0.1:{port}",
                               ping_interval=0.2, ping_timeout=0.6) as ws:
                if consume:
                    task = asyncio.create_task(_drain(ws))
                await asyncio.sleep(1.6)
                # Exactly what `_send_realtime` does when the socket is dead.
                await ws.send("still-here")
                if consume:
                    task.cancel()
                    with contextlib.suppress(BaseException):
                        await task
                return "sent"
        except Exception as e:                       # noqa: BLE001 — reported
            return f"{type(e).__name__}: {e}"


def test_frozen_consumer_loses_the_keepalive():
    """The production failure, reproduced: a consumer that stops reading long
    enough for the buffer to fill makes websockets kill its own connection."""
    out = asyncio.run(_websockets_keepalive_probe(consume=False))
    assert out.startswith("ConnectionClosedError"), (
        f"expected the keepalive failure, got {out!r}")
    assert "keepalive ping timeout" in out.lower(), out


def test_pumping_consumer_survives_the_same_socket():
    """The fix, at the library level: the only difference is that something is
    reading the socket, and the connection stays usable."""
    assert asyncio.run(_websockets_keepalive_probe(consume=True)) == "sent"


# ── 3. one audio sender per session ─────────────────────────────────────────

def test_bound_sender_forwards_its_own_frames():
    async def _scenario():
        j = _make_jarvis()
        j.session = _CountingSession()
        q = asyncio.Queue()
        j.out_queue = q
        task = asyncio.create_task(j._send_realtime(q))

        for i in range(3):
            q.put_nowait({"data": b"%d" % i, "mime_type": "audio/pcm"})
        for _ in range(100):
            await asyncio.sleep(0.01)
            if len(j.session.sent) >= 3:
                break

        j.out_queue = asyncio.Queue()      # a newer session takes over
        q.put_nowait({"data": b"late", "mime_type": "audio/pcm"})
        await asyncio.wait_for(task, timeout=2)
        return [b.data for b in j.session.sent], task.done()

    sent, done = asyncio.run(_scenario())
    assert sent == [b"0", b"1", b"2"], f"frames lost or reordered: {sent}"
    assert done, "the superseded sender did not stop"


def test_stale_sender_never_touches_the_new_session():
    """`out_queue` is replaced on reconnect. A sender from the closed session
    must retire, not wake up holding the NEW queue and send every frame twice."""
    async def _scenario():
        j = _make_jarvis()
        j.session = _CountingSession()
        old_q = asyncio.Queue()
        j.out_queue = old_q
        task = asyncio.create_task(j._send_realtime(old_q))

        new_q = asyncio.Queue()
        j.out_queue = new_q                          # reconnect swapped the queue
        new_q.put_nowait({"data": b"new", "mime_type": "audio/pcm"})
        old_q.put_nowait({"data": b"old", "mime_type": "audio/pcm"})
        await asyncio.wait_for(task, timeout=2)
        return task.done(), list(j.session.sent)

    done, sent = asyncio.run(_scenario())
    assert done, "the stale sender is still running"
    assert sent == [], f"the stale sender wrote to the current session: {sent}"


# ── 4. microphone back-pressure ─────────────────────────────────────────────

def test_full_mic_queue_drops_the_newest_frame_without_raising():
    async def _scenario():
        j = _make_jarvis()
        j.out_queue = asyncio.Queue(maxsize=1)
        j.out_queue.put_nowait({"data": b"keep", "mime_type": "audio/pcm"})
        loop = asyncio.get_running_loop()
        for _ in range(5):
            j._queue_mic_frame(b"drop", loop)        # never raises
        await asyncio.sleep(0.05)
        kept = j.out_queue.get_nowait()
        return getattr(j, "_mic_drop_count", 0), kept["data"], j.out_queue.empty()

    drops, kept, empty = asyncio.run(_scenario())
    assert drops == 1, f"the drop log was not throttled (count={drops})"
    assert kept == b"keep", "audio already queued was disturbed"
    assert empty, "a dropped frame was queued anyway"


def test_mic_frame_keeps_the_wire_format():
    async def _scenario():
        j = _make_jarvis()
        j.out_queue = asyncio.Queue()
        j._queue_mic_frame(b"\x00\x01", asyncio.get_running_loop())
        await asyncio.sleep(0.02)
        return j.out_queue.get_nowait()

    frame = asyncio.run(_scenario())
    assert frame == {"data": b"\x00\x01", "mime_type": "audio/pcm"}


def test_mic_frame_is_safe_before_a_session_exists():
    """The callback can fire between sessions; it must not raise."""
    j = _make_jarvis()
    j.out_queue = None
    j._queue_mic_frame(b"x")                         # no queue, no loop, no raise


# ── 5. classification ───────────────────────────────────────────────────────

def test_keepalive_close_is_named_and_reconnects_as_a_network_event():
    keepalive = genai_errors.APIError(1011, "keepalive ping timeout", None)
    abnormal = genai_errors.APIError(1006, "Abnormal closure.", None)

    assert jarvis_main._is_keepalive_close(keepalive)
    assert jarvis_main._is_connection_closed(keepalive)
    assert not jarvis_main._is_keepalive_close(abnormal)
    assert jarvis_main._is_connection_closed(abnormal)

    group = ExceptionGroup("unhandled errors in a TaskGroup", [keepalive])
    assert jarvis_main._is_keepalive_close(group)
    assert jarvis_main._is_connection_closed(group)


def test_other_closes_keep_their_own_branches():
    """1000 (graceful), 1007 (bad config) and 1008 (session limit) are not
    network faults and must not be swallowed by the socket-lost branch."""
    for code, reason in ((1000, ""),
                         (1007, "Request contains an invalid argument."),
                         (1008, "The operation was aborted.")):
        e = genai_errors.APIError(code, reason, None)
        assert not jarvis_main._is_keepalive_close(e), code
        assert not jarvis_main._is_connection_closed(e), code

    # A generic 1011 is not a keepalive failure either.
    internal = genai_errors.APIError(1011, "Internal error encountered.", None)
    assert not jarvis_main._is_keepalive_close(internal)
    assert not jarvis_main._is_connection_closed(internal)


def test_websockets_close_exception_is_recognized():
    from websockets.exceptions import ConnectionClosedError
    from websockets.frames import Close

    e = ConnectionClosedError(Close(1011, "keepalive ping timeout"), None)
    assert "keepalive ping timeout" in str(e).lower(), str(e)
    assert jarvis_main._is_keepalive_close(e)
    assert jarvis_main._is_connection_closed(e)


# ── 6. the run loop ─────────────────────────────────────────────────────────

def _fake_genai_module(exc: Exception, state: dict):
    class _FakeClient:
        def __init__(self, *args, **kwargs):
            state.setdefault("client_keys", []).append(kwargs.get("api_key"))
            state.setdefault("http_options", []).append(kwargs.get("http_options"))

            class _FakeLive:
                def connect(self, model=None, config=None):
                    class _CM:
                        async def __aenter__(self):
                            state["connects"] += 1
                            raise exc

                        async def __aexit__(self, *a):
                            return False

                    return _CM()

            self.aio = _types.SimpleNamespace(live=_FakeLive())

    return _types.SimpleNamespace(Client=_FakeClient)


def test_keepalive_close_reconnects_and_keeps_the_handle():
    """CORE REGRESSION: a 1011 keepalive close must take the network path —
    calm reconnect, attempt counter advanced, resumption handle replayed — not
    the unknown-error path that printed a traceback and forgot the session."""
    e = genai_errors.APIError(1011, "keepalive ping timeout", None)
    j = _make_jarvis()
    j._resume_handle = "handle-still-good"
    state = {"connects": 0}

    async def _scenario():
        task = asyncio.ensure_future(j.run())
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 20
            while loop.time() < deadline:
                await asyncio.sleep(0.02)
                if state["connects"] >= 2 or task.done():
                    break
            await asyncio.sleep(0.05)
        finally:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task

    with _patch(jarvis_main, "genai", _fake_genai_module(e, state)), \
         _no_dashboard(), _offline_env():
        asyncio.run(asyncio.wait_for(_scenario(), timeout=30))

    assert state["connects"] >= 2, f"the run loop did not reconnect: {state!r}"
    assert j._resume_handle == "handle-still-good", (
        "a keepalive close must not sacrifice the resumption handle")
    assert getattr(j, "_socket_lost_streak", 0) >= 1, (
        "the keepalive close was not classified as a socket loss")
    assert getattr(j, "_reconnect_attempt", 0) >= 1, (
        "the reconnect attempt counter did not advance")
    assert any("Connection lost" in line for line in j.ui.logs), (
        f"no calm user-facing line; logs={j.ui.logs!r}")
    assert not any("starting fresh" in line.lower() for line in j.ui.logs), (
        f"the handle was dropped despite not being rejected: {j.ui.logs!r}")


# ── 7. the keepalive values actually reach the SDK ─────────────────────────

def test_keepalive_values_are_the_documented_ones():
    assert jarvis_main._LIVE_PING_INTERVAL == 20
    assert jarvis_main._LIVE_PING_TIMEOUT == 60
    assert jarvis_main._LIVE_PING_TIMEOUT > 20, (
        "a pong timeout at or below the library default is the bug, not the fix")


def test_http_options_carry_the_keepalive():
    j = _make_jarvis()
    j._enhanced_live = False
    opts = j._live_http_options()
    assert opts["api_version"] == "v1beta"
    assert opts["async_client_args"] == {
        "ping_interval": jarvis_main._LIVE_PING_INTERVAL,
        "ping_timeout": jarvis_main._LIVE_PING_TIMEOUT,
    }

    j._enhanced_live = True
    assert j._live_http_options()["api_version"] == "v1alpha"


def test_real_sdk_client_receives_the_keepalive_arguments():
    """No connection is opened: google-genai builds the websockets argument
    dict at Client construction, which is exactly what we assert here."""
    real_genai = pytest.importorskip("google.genai")
    j = _make_jarvis()
    client = real_genai.Client(api_key="AIzaFAKE-test",
                               http_options=j._live_http_options())
    ws_args = client._api_client._websocket_ssl_ctx
    assert ws_args["ping_interval"] == jarvis_main._LIVE_PING_INTERVAL
    assert ws_args["ping_timeout"] == jarvis_main._LIVE_PING_TIMEOUT


def test_open_session_builds_the_client_with_keepalive():
    captured = {}

    class _FakeClient:
        def __init__(self, *a, **kw):
            captured.update(kw.get("http_options") or {})

            class _Live:
                def connect(self, model=None, config=None):
                    class _CM:
                        async def __aenter__(self):
                            return object()

                        async def __aexit__(self, *a):
                            return False

                    return _CM()

            self.aio = _types.SimpleNamespace(live=_Live())

    async def _scenario():
        j = _make_jarvis()
        j._engine_caps = None
        with _patch(jarvis_main, "genai", _types.SimpleNamespace(Client=_FakeClient)):
            async with j._open_session("models/gemini-test", None) as session:
                return session is not None

    assert asyncio.run(_scenario())
    assert captured["async_client_args"]["ping_interval"] == \
        jarvis_main._LIVE_PING_INTERVAL
    assert captured["async_client_args"]["ping_timeout"] == \
        jarvis_main._LIVE_PING_TIMEOUT


def test_open_session_falls_back_if_the_sdk_rejects_the_arguments():
    """A google-genai too old for the arguments must still produce a session —
    the reader pump alone removes the stall."""
    calls = []

    class _PickyClient:
        def __init__(self, *a, **kw):
            calls.append(kw.get("http_options") or {})
            if "async_client_args" in (kw.get("http_options") or {}):
                raise TypeError("unexpected keyword argument 'async_client_args'")

            class _Live:
                def connect(self, model=None, config=None):
                    class _CM:
                        async def __aenter__(self):
                            return object()

                        async def __aexit__(self, *a):
                            return False

                    return _CM()

            self.aio = _types.SimpleNamespace(live=_Live())

    async def _scenario():
        j = _make_jarvis()
        j._engine_caps = None
        with _patch(jarvis_main, "genai", _types.SimpleNamespace(Client=_PickyClient)):
            async with j._open_session("models/gemini-test", None) as session:
                return session is not None

    assert asyncio.run(_scenario())
    assert len(calls) == 2, f"expected one fallback attempt, got {calls!r}"
    assert "async_client_args" not in calls[1]


# ── 8. nothing else moved ──────────────────────────────────────────────────

def test_audio_format_is_unchanged():
    assert jarvis_main.SEND_SAMPLE_RATE == 16000
    assert jarvis_main.RECEIVE_SAMPLE_RATE == 24000
    assert jarvis_main.CHANNELS == 1
    assert jarvis_main.CHUNK_SIZE == 1024


# ── standalone runner ───────────────────────────────────────────────────────

def run_tests() -> int:
    tests = [
        test_pump_reads_while_the_consumer_is_busy,
        test_pump_ends_at_turn_complete_and_clears_itself,
        test_pump_re_raises_the_original_exception_object,
        test_a_new_stream_retires_the_previous_pump,
        test_frozen_consumer_loses_the_keepalive,
        test_pumping_consumer_survives_the_same_socket,
        test_bound_sender_forwards_its_own_frames,
        test_stale_sender_never_touches_the_new_session,
        test_full_mic_queue_drops_the_newest_frame_without_raising,
        test_mic_frame_keeps_the_wire_format,
        test_mic_frame_is_safe_before_a_session_exists,
        test_keepalive_close_is_named_and_reconnects_as_a_network_event,
        test_other_closes_keep_their_own_branches,
        test_websockets_close_exception_is_recognized,
        test_keepalive_close_reconnects_and_keeps_the_handle,
        test_keepalive_values_are_the_documented_ones,
        test_http_options_carry_the_keepalive,
        test_real_sdk_client_receives_the_keepalive_arguments,
        test_open_session_builds_the_client_with_keepalive,
        test_open_session_falls_back_if_the_sdk_rejects_the_arguments,
        test_audio_format_is_unchanged,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as e:                       # noqa: BLE001 — reported
            failed += 1
            print(f"  FAIL  {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run_tests())
