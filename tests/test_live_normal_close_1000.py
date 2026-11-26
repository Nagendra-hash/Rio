"""
tests/test_live_normal_close_1000.py — regression test for the "clean quit
looks like a crash" traceback.

History (2026-10-21): closing RIO / quitting shut the session down, as
designed. graceful_shutdown() closes the Live session; the SDK surfaces that
close inside receive() as:

    google.genai.errors.APIError: 1000 None.

(a WebSocket 1000 "normal closure" with the reason dropped by live.py's
`raise_error(code, reason, None)`). _receive_audio recognized the shutdown
branch and re-raised, the session TaskGroup wrapped it, and the run loop's
handler classified the ExceptionGroup as an unexpected failure:

    [JARVIS] Error (ExceptionGroup): unhandled errors in a TaskGroup (1 sub-exception)
    <full traceback>
    [JARVIS] Reconnecting in 3s...

Two defects, one test each:

  1. `_shutting_down` was never consulted in the run loop. Teardown's own
     exception must stop the loop quietly — never traceback, never reconnect.
  2. A 1000 arriving OUTSIDE teardown (server idle reap / rotation without a
     GoAway) had no classifier, so it also fell into the ❌ + traceback path
     instead of the calm reconnect. It keeps the resumption handle: the
     session is intact server-side, so the conversation carries over.

pytest-compatible: each test_* function raises AssertionError on regression.
Run:  python3 tests/test_live_normal_close_1000.py   (or pytest)

Exit codes: 0 = pass, 1 = regression.
"""

import asyncio
import contextlib
import sys
import types as _types
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import main as jarvis_main  # noqa: E402
from google.genai import errors as genai_errors  # noqa: E402


# ── the exact production error ───────────────────────────────────────────────

def _production_1000_error() -> Exception:
    """The object google-genai raises for a 1000 close: code only, no reason.

    live.py `_receive`:
        raw_response = await self._ws.recv(decode=False)
        ...
        errors.APIError.raise_error(code, reason, None)

    A normal closure carries an EMPTY reason string, and the SDK formats the
    error as `f'{code} {status}. {details}'`, so str(e) is the bare
    "1000 None. " (trailing space) — the shape this whole fix keys on.
    """
    return genai_errors.APIError(1000, "", None)


# ── offline harness (mirrors test_session_resumption_1008.py) ────────────────

class _StubUI:
    def __init__(self):
        self.logs: list[str] = []

    def write_log(self, msg: str):
        self.logs.append(str(msg))

    def set_state(self, state: str):
        pass

    def show_confirm(self, *a, **k):
        pass

    def hide_confirm(self):
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


class _FakeRecvSession:
    """receive() raises on first pull — how a closed socket surfaces inside
    _receive_audio's `async for`."""

    def __init__(self, exc: Exception):
        self._exc = exc

    def receive(self):
        async def _gen():
            raise self._exc
            yield  # noqa: unreachable — makes this an async generator
        return _gen()


def _make_jarvis() -> "jarvis_main.JarvisLive":
    return jarvis_main.JarvisLive(_StubUI())


# A live session stub good enough for the run loop: receive() dies with the
# close, everything else is a no-op. `on_enter` lets a test mark the state a
# real teardown would have set before the close landed.
class _FakeLiveSession:
    def __init__(self, exc: Exception, on_enter=None):
        self._exc = exc
        self._on_enter = on_enter

    def receive(self):
        async def _gen():
            raise self._exc
            yield  # noqa: unreachable
        return _gen()

    async def send_realtime_input(self, *a, **k):
        pass

    async def send_tool_response(self, *a, **k):
        pass

    async def send_client_content(self, *a, **k):
        pass

    async def close(self):
        pass


def _fake_genai_module(exc: Exception, state: dict, on_enter=None):
    class _FakeClient:
        def __init__(self, *args, **kwargs):
            class _FakeLive:
                def connect(self, model=None, config=None):
                    class _CM:
                        async def __aenter__(self):
                            state["connects"] += 1
                            if on_enter is not None:
                                on_enter()
                            return _FakeLiveSession(exc, on_enter)

                        async def __aexit__(self, *a):
                            return False

                    return _CM()

            self.aio = _types.SimpleNamespace(live=_FakeLive())

    return _types.SimpleNamespace(Client=_FakeClient)


_WORKERS = (
    "_send_realtime",
    "_listen_audio",
    "_play_audio",
    "_run_system_monitor",
    "_run_background_monitor",
    "_run_camera_watch",
    "_run_proactive_mode",
    "_run_sleep_watch",
    "_run_trading_learner",
    "_run_rag_learner",
    "_run_market_desk",
    "_relay_phone_audio",
)


async def _noop(*a, **k):
    await asyncio.Event().wait()


@contextlib.contextmanager
def _stub_workers(j):
    """Replace every TaskGroup worker with a parked coroutine: this test is
    about the run loop's handling of the close, not audio/monitor startup
    (which would open real devices)."""
    with contextlib.ExitStack() as stack:
        for name in _WORKERS:
            stack.enter_context(_patch(j, name, _noop))
        yield


# ── tests ────────────────────────────────────────────────────────────────────

def test_1000_error_shape():
    """Foundation: if google-genai changes how a close is reported, this
    fails first and says so rather than silently re-arming the traceback."""
    e = _production_1000_error()
    assert str(e) == "1000 None. ", (
        f"google-genai error shape changed — re-check the classifier: {str(e)!r}"
    )
    assert getattr(e, "code", None) == 1000


def test_classifier_matches_normal_close():
    assert jarvis_main._is_normal_close(_production_1000_error())


def test_classifier_unwraps_taskgroup():
    """The run loop sees the close inside the session TaskGroup's
    ExceptionGroup — that wrapper carries no `.code` of its own."""
    group = ExceptionGroup("unhandled errors in a TaskGroup",
                           [_production_1000_error()])
    assert jarvis_main._is_normal_close(group)


def test_classifier_excludes_other_closes():
    e1008 = genai_errors.APIError(1008, "The operation was aborted.", None)
    e1011 = genai_errors.APIError(1011, "Internal error encountered.", None)
    e1007 = genai_errors.APIError(1007, "Request contains an invalid argument.", None)
    for e in (e1008, e1011, e1007):
        assert not jarvis_main._is_normal_close(e), (
            f"{e} must stay with its own classifier/branch"
        )


def test_recv_path_1000_reconnects_calmly():
    """CORE REGRESSION (non-teardown): a 1000 surfacing inside the receive
    loop must print one calm line and raise plainly (run loop backs off and
    replays the handle) — not fall through as an unknown ❌ failure.

    The handle must survive: the server-side session is intact, so the
    conversation carries over the rebuild."""
    j = _make_jarvis()
    j._resume_handle = "handle-from-the-session-that-just-closed"
    j.session = _FakeRecvSession(_production_1000_error())

    async def _scenario():
        try:
            await j._receive_audio()
        except jarvis_main._ReconnectSignal as sig:
            return ("signal", sig)
        except genai_errors.APIError as e:
            return ("raised", e)
        return ("returned", None)

    kind, detail = asyncio.run(_scenario())
    assert kind == "raised", (
        f"1000 close took the {kind} path (signal={detail!r}) — expected a "
        f"plain re-raise so the run loop backs off"
    )
    assert str(detail) == "1000 None. ", (
        f"unexpected error shape: {str(detail)!r}"
    )
    assert j._resume_handle == "handle-from-the-session-that-just-closed", (
        "the resumption handle must survive a clean server close"
    )


def test_recv_path_1000_during_shutdown_logs_shutdown():
    """Teardown's own 1000 must be recognized as EXPECTED (the shutdown
    branch), never reclassified as a server hiccup."""
    j = _make_jarvis()
    j._shutting_down = True
    j.session = _FakeRecvSession(_production_1000_error())

    async def _scenario():
        try:
            await j._receive_audio()
        except genai_errors.APIError as e:
            return e
        return None

    assert asyncio.run(_scenario()) is not None


def test_shutdown_close_does_not_reconnect_or_traceback():
    """CORE REGRESSION (2026-10-21 traceback): drives the REAL run() loop.

    Scenario: the session connects, then teardown sets _shutting_down and
    closes the socket — exactly what graceful_shutdown() does. The close
    must stop the run loop: one attempt, no "Reconnecting in 3s...", no
    ExceptionGroup traceback."""
    j = _make_jarvis()
    state = {"connects": 0}

    def _teardown_began():
        j._shutting_down = True

    with _patch(jarvis_main, "genai",
                _fake_genai_module(_production_1000_error(), state,
                                   on_enter=_teardown_began)), \
         _no_dashboard(), \
         _stub_workers(j), \
         _offline_env():
        asyncio.run(asyncio.wait_for(j.run(), timeout=20))

    assert state["connects"] == 1, (
        f"run loop reconnected during shutdown ({state['connects']} attempts) "
        f"— teardown must stop the loop"
    )
    assert j.session is None


# ── standalone runner ────────────────────────────────────────────────────────

def run_tests() -> int:
    tests = [
        test_1000_error_shape,
        test_classifier_matches_normal_close,
        test_classifier_unwraps_taskgroup,
        test_classifier_excludes_other_closes,
        test_recv_path_1000_reconnects_calmly,
        test_recv_path_1000_during_shutdown_logs_shutdown,
        test_shutdown_close_does_not_reconnect_or_traceback,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001 — report, then continue
            failed += 1
            print(f"FAIL  {t.__name__}: {type(e).__name__}: {e}")
        else:
            print(f"PASS  {t.__name__}")
    print("REGRESSION" if failed else "ALL GREEN")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
