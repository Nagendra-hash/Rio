"""
tests/test_session_resumption_1008.py — regression test for the poisoned
resumption-handle loop.

History (2026-09-27): after a GoAway grace expiry / 1011 keepalive death, the
server-side session was gone but the client kept replaying `_resume_handle`.
The handle-rejection detector only matched resum|handle|INVALID_ARGUMENT|
NOT_FOUND — Google's WebSocket close arrives as:

    google.genai.errors.APIError: 1008 None. BidiGenerateContent session not found

which contains NONE of those markers. The same dead handle was replayed on
every retry, five identical tracebacks 3s apart, until the user hit ^C.

What this test proves, against the REAL JarvisLive.run() loop (only the
network is faked — the connect call raises the exact production error object,
and every other line of the run loop, including the exception classifier,
executes for real):

  1. With a resumption handle armed, a 1008 "session not found" connect error
     DROPS the handle: the run loop attempts a SECOND connect, and that
     attempt builds its config with session_resumption handle=None (fresh).
  2. Counter-case: an ordinary transient failure (TimeoutError) KEEPS the
     handle — resumption must only be sacrificed for genuinely dead handles.
  3. Safety net: a connect error that is neither transient nor handle-related
     escalates its retry backoff when the identical error repeats (3s → 6s)
     instead of hammering forever.

2026-09-28 follow-up: the recv path re-raises the same close code with a
DIFFERENT reason text — `APIError: 1008 None. The operation was aborted.`
(live.py _receive → errors.raise_error(code, reason, None); the websockets
reason "policy violation" is dropped there, so the old detector's
"policy violation" match missed it and a routine GoAway force-close printed a
full ❌ traceback + treated the expected close as a failure). The classifier
now keys on the 1008 close itself and only excludes the dead-handle shape.

pytest-compatible: each test_* function raises AssertionError on regression.
Run:  python3 tests/test_session_resumption_1008.py   (or pytest)

Exit codes: 0 = pass, 1 = regression.
"""

import asyncio
import contextlib
import sys
import types as _types
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# Importing main pulls in the actions/plugins registries (~5s of discovery
# prints) and the 3.10 TaskGroup shim — but nothing that opens audio or the
# network at import time. All app behaviour the test needs lives behind
# JarvisLive(), which never runs unless we drive it.
#
# NOTE: this module must NOT define a function named `main` — it would shadow
# the imported `main` module in this namespace.
import main as jarvis_main  # noqa: E402
from google.genai import errors as genai_errors  # noqa: E402


# ── the exact production error ───────────────────────────────────────────────

def _production_1008_error() -> Exception:
    """Reproduce the 2026-09-27 traceback's error object.

    google-genai raises `errors.APIError(1008, {...}, None)` from live.connect
    when the server close-frames the handshake with 1008 (policy violation)
    "BidiGenerateContent session not found". The entire regression lives in
    how THAT string classifies, so the shape test below asserts it.
    """
    return genai_errors.APIError(
        1008,
        {"error": {"code": 1008, "message": "BidiGenerateContent session not found",
                   "status": None}},
        None,
    )


# ── offline harness ──────────────────────────────────────────────────────────

class _StubUI:
    """Only what JarvisLive touches before/inside the except chain.

    write_log is recorded (the tests assert on it); every other attribute the
    reconnect tail can touch — `muted`, `set_state`, `set_audio_level`, … —
    falls back to a no-op via __getattr__. The failure paths under test were
    never exercised with a real UI in CI before, and a stub that raises
    AttributeError would mask the very classification logic being tested
    (2026-09-27 session died here on `_StubUI' object has no attribute
    'muted'` — a test-harness artifact, not an app bug)."""

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
        # Only called for attributes NOT found normally, so recorded/real
        # ones above are unaffected. Any missing method becomes a no-op;
        # any missing value reads as a safe falsy default.
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
    """Make the run loop take its designed no-dashboard path.

    The real DashboardServer needs port 8000 — already taken in a test/dev
    environment — and a broadcast from a server that never bound can stall
    the reconnect tail, which is exactly the code under test. run() already
    degrades gracefully ('[Dashboard] Disabled') when the import fails, so
    fake exactly that failure."""
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
    """Keep the run loop hermetic: no wake-model load, no PTT hotkey thread,
    no morning-briefing LLM call. (The API key never reaches the network —
    the client itself is faked below.)"""
    with _patch(jarvis_main, "get_wake_word_enabled", lambda: False), \
         _patch(jarvis_main, "get_push_to_talk_enabled", lambda: False), \
         _patch(jarvis_main, "get_brief_enabled", lambda: False):
        yield


def _fake_genai_module(exc: Exception, state: dict):
    """A stand-in for the `google.genai` module as main.py sees it: the only
    member the run loop uses is Client(...).aio.live.connect(...), whose
    __aenter__ is exactly where the real handshake failure raises."""

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            class _FakeLive:
                def connect(self, model=None, config=None):
                    class _CM:
                        async def __aenter__(self):
                            state["connects"] += 1
                            # Snapshot the resumption handle this attempt would
                            # have sent — ground truth for "started fresh".
                            sru = getattr(config, "session_resumption", None)
                            state["last_handle"] = getattr(sru, "handle", "missing")
                            # Snapshot whether the setup carried the post-
                            # rejection continuation block (context carry-over).
                            _si = getattr(config, "system_instruction", "")
                            state.setdefault("setups", []).append({
                                "handle": state["last_handle"],
                                "has_continuation":
                                    "[CONTINUATION]" in (_si if isinstance(_si, str)
                                                         else str(_si)),
                            })
                            raise exc

                        async def __aexit__(self, *a):
                            return False

                    return _CM()

            self.aio = _types.SimpleNamespace(live=_FakeLive())

    return _types.SimpleNamespace(Client=_FakeClient)


def _drive_connect_failures(j, error, max_iterations: int) -> dict:
    """Run JarvisLive.run() against the failing client until either
    `max_iterations` connect attempts have happened or 20s pass. Returns the
    state snapshot. The scenario cancels the loop once the evidence is in —
    mirroring the ^C that ended the production incident."""
    state = {"connects": 0, "last_handle": "missing"}

    async def _scenario():
        task = asyncio.ensure_future(j.run())
        try:
            loop = asyncio.get_event_loop()
            deadline = loop.time() + 20
            while loop.time() < deadline:
                await asyncio.sleep(0.02)
                if state["connects"] >= max_iterations or task.done():
                    break
            # Let the except chain of the last attempt settle.
            await asyncio.sleep(0.05)
        finally:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task

    with _patch(jarvis_main, "genai", _fake_genai_module(error, state)), \
         _no_dashboard(), \
         _offline_env():
        asyncio.run(asyncio.wait_for(_scenario(), timeout=30))
    return state


def _make_jarvis() -> "jarvis_main.JarvisLive":
    return jarvis_main.JarvisLive(_StubUI())


_ARMED = "handle-from-a-session-the-server-has-since-dropped"


def _arm_resumption(j):
    """Simulate a session that ran long enough to earn a resumption handle
    (the receive loop stores one the moment the server sends it)."""
    j._resume_handle = _ARMED


# ── tests ────────────────────────────────────────────────────────────────────

def _recv_1008_abort_error() -> Exception:
    """The exact error object the 2026-09-28 traceback raised from the recv
    path. google/genai/live.py _receive does:
        errors.APIError.raise_error(code, reason, None)
    with the websockets close reason — which arrived WITHOUT 'policy
    violation' (the ws library's `reason` slot said 'The operation was
    aborted.'). Constructed identically here."""
    return genai_errors.APIError(1008, "The operation was aborted.", None)


class _FakeRecvSession:
    """A session whose receive() async-iterator raises on first pull — how a
    force-closed socket surfaces inside _receive_audio's `async for`."""

    def __init__(self, exc: Exception):
        self._exc = exc

    def receive(self):
        async def _gen():
            raise self._exc
            yield  # noqa: unreachable — makes this an async generator
        return _gen()


def test_1008_abort_classifies_as_session_limit():
    """The recv-path abort shape must classify as an expected session-limit
    close. This is the exact string that missed the old detector (which
    required 'policy violation') and fell through to ❌ + traceback."""
    e = _recv_1008_abort_error()
    assert str(e) == "1008 None. The operation was aborted.", (
        f"google-genai error shape changed — re-check the classifier: {str(e)!r}"
    )
    assert jarvis_main._is_session_limit_close(e), (
        "recv-path 1008 'operation was aborted' not classified as a "
        "session-limit close — the GoAway safety net will miss it again"
    )


def test_1008_policy_violation_still_classifies():
    """The originally-handled shape (websockets close reason text present)
    must keep classifying — no regression of the older fix."""
    e = genai_errors.APIError(
        1008, "policy violation: The operation was aborted.", None)
    assert jarvis_main._is_session_limit_close(e)


def test_1008_dead_handle_stays_excluded():
    """The poisoned-handle shape must STILL be excluded so the run loop's
    handle-rejection branch (not the calm-reconnect path) owns it."""
    e = genai_errors.APIError(
        1008,
        {"error": {"code": 1008,
                    "message": "BidiGenerateContent session not found",
                    "status": None}},
        None,
    )
    assert not jarvis_main._is_session_limit_close(e)


def test_recv_path_1008_abort_raises_calm_keep_context_reconnect():
    """CORE REGRESSION (2026-09-28): a 1008 abort surfacing inside the
    receive loop must raise _ReconnectSignal(keep_context=True) — logged
    calmly, resumption handle kept — instead of the ❌ + traceback failure
    path. Drives the REAL _receive_audio; only the socket is faked."""
    j = _make_jarvis()
    _arm_resumption(j)
    j.session = _FakeRecvSession(_recv_1008_abort_error())

    async def _scenario():
        try:
            await j._receive_audio()
        except jarvis_main._ReconnectSignal as sig:
            return sig
        return None

    sig = asyncio.run(_scenario())
    assert sig is not None, (
        "_receive_audio did not raise _ReconnectSignal on the 1008 abort — "
        "it fell through to the generic failure path"
    )
    assert sig.keep_context is True, "reconnect must keep the conversation"
    assert j._resume_handle == _ARMED, "resumption handle must survive the rebuild"
    assert any("refreshing connection" in log.lower() for log in j.ui.logs), (
        f"expected the calm 'Session limit reached' log line, got: {j.ui.logs!r}"
    )


def test_1008_error_string_classifies_as_handle_rejection():
    """Foundation of the fix: the production error string must contain the
    markers the run loop's rejection detector matches. If google-genai ever
    changes its message format, THIS fails first and says so — instead of a
    silent return of the infinite replay loop."""
    s = str(_production_1008_error())
    assert "1008" in s, f"error string lost the close code: {s!r}"
    assert "session not found" in s.lower(), f"error string lost the reason: {s!r}"


def test_1008_session_not_found_drops_resumption_handle():
    """CORE REGRESSION: with a handle armed, the first 1008 'session not
    found' connect error must drop it — attempt 2 must build a config whose
    session_resumption.handle is None, i.e. a genuinely fresh session."""
    j = _make_jarvis()
    _arm_resumption(j)
    state = _drive_connect_failures(j, _production_1008_error(), max_iterations=2)

    assert state["connects"] >= 2, (
        f"expected a second connect attempt after the handle was dropped, "
        f"got {state['connects']}"
    )
    assert state["last_handle"] is None, (
        f"second attempt replayed handle {state['last_handle']!r} — the dead "
        f"handle was not dropped; this is the infinite-replay regression"
    )
    assert j._resume_handle is None, "handle survived the run loop"
    assert any("fresh" in log.lower() for log in j.ui.logs), (
        f"expected the 'starting fresh' log line, got: {j.ui.logs!r}"
    )


def test_handle_rejection_carries_conversation_tail():
    """Graceful degradation (2026-09-28 root-cause investigation): the server
    reaps content-bearing sessions' handles (upstream Live API bug), so every
    rotation 2+ resets to a fresh session. The fresh session must NOT be a
    total context wipe: the recent conversation tail is folded into its
    system instruction via _carry_context (one-shot, consumed by the next
    _build_config)."""
    j = _make_jarvis()
    _arm_resumption(j)
    j._session_log = [f"User: question number {i:02d}" for i in range(20)]
    state = _drive_connect_failures(j, _production_1008_error(), max_iterations=2)

    assert j._resume_handle is None, "handle must still be dropped"
    # Connect 1 = rejected resume; connect 2 = the fresh replacement. The
    # continuation block must ride on the FRESH setup (one-shot consumption,
    # so j._carry_context is None afterwards by design).
    setups = state.get("setups", [])
    assert len(setups) >= 2, f"expected 2 connect attempts, got {len(setups)}"
    assert not setups[0]["has_continuation"], (
        "the rejected RESUME setup must not carry a continuation block"
    )
    assert setups[1]["has_continuation"], (
        "fresh session's setup lacks the continuation block — the reset is a "
        "full context wipe again"
    )
    assert j._carry_context is None, "carry-over must be one-shot"
    cfg_text = "\n".join(str(s) for s in j.ui.logs)
    assert "fresh" in cfg_text.lower(), "expected the 'starting fresh' log line"


def test_no_carry_over_without_conversation():
    """A rejection with no in-proc conversation (e.g. right after a process
    restart) must leave _carry_context empty — no empty continuation block."""
    j = _make_jarvis()
    _arm_resumption(j)
    assert j._session_log == []
    _drive_connect_failures(j, _production_1008_error(), max_iterations=2)
    assert j._carry_context is None, (
        "empty session log must not produce a continuation block"
    )


def test_transient_timeout_keeps_resumption_handle():
    """Counter-case: an ordinary network timeout must NOT sacrifice the
    handle. Resumption exists to survive exactly this class of failure."""
    timeout_err = asyncio.TimeoutError("opening handshake timed out")
    j = _make_jarvis()
    _arm_resumption(j)
    state = _drive_connect_failures(j, timeout_err, max_iterations=2)

    assert state["connects"] >= 2, "run loop did not retry the timeout"
    assert state["last_handle"] == _ARMED, (
        f"transient timeout dropped the resumption handle "
        f"(got {state['last_handle']!r}) — overcorrection"
    )
    assert j._resume_handle == _ARMED


def test_repeated_nontransient_error_escalates_backoff():
    """Safety net: a connect error that is neither transient nor
    handle-related must not hammer at 3s forever — a second identical error
    doubles the backoff (capped at 60s)."""

    class _BrandNewAPIError(Exception):
        pass

    err = _BrandNewAPIError("500 server exploded in a brand new way")
    j = _make_jarvis()          # NO resumption handle → falls to the else branch
    state = _drive_connect_failures(j, err, max_iterations=3)

    assert state["connects"] >= 3, f"expected 3 attempts, got {state['connects']}"
    assert getattr(j, "_conn_backoff", 0) >= 6, (
        f"backoff did not escalate on a repeating error "
        f"(conn_backoff={getattr(j, '_conn_backoff', None)})"
    )
    assert getattr(j, "_conn_backoff", 0) <= 60, "backoff escaped its 60s cap"


# ── standalone runner ────────────────────────────────────────────────────────

def run_tests() -> int:
    tests = [
        test_1008_error_string_classifies_as_handle_rejection,
        test_1008_session_not_found_drops_resumption_handle,
        test_transient_timeout_keeps_resumption_handle,
        test_repeated_nontransient_error_escalates_backoff,
        test_1008_abort_classifies_as_session_limit,
        test_1008_policy_violation_still_classifies,
        test_1008_dead_handle_stays_excluded,
        test_recv_path_1008_abort_raises_calm_keep_context_reconnect,
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
