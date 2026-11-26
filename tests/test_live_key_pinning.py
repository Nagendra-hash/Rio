"""
tests/test_live_key_pinning.py — regression tests for the 2026-10-01
"cannot restore conversation after session end" incident.

Symptom (journalctl, every rotation):
    🔗 Resumption handle rejected — starting a fresh session
       (1008 None. Session does not belong to this project)

Root cause: _get_api_key() round-robined across all configured keys
(itertools.count % len(keys)) on EVERY client build. A Live resumption
handle is bound to the project that issued it, so each reconnect presented
the handle to a different project's key → 1008 "Session does not belong to
this project" → fresh session → the conversation wiped on every rotation.

The fix under test:
  1. _get_api_key() is PINNED — same key on every call within a process,
     no matter how many keys are configured (multi-key setups stay stable).
  2. Rotation is explicit only: _rotate_api_key() advances the index and
     returns True only when more than one key exists.
  3. The run loop rotates on a 429 (quota) and on an invalid-key error,
     instead of prompting for reconfiguration while alternates exist.
  4. _is_quota_error() recognises the real 429 shapes (string, and wrapped
     inside a TaskGroup ExceptionGroup).

Run:  python3 tests/test_live_key_pinning.py   (or pytest)

Exit codes: 0 = pass, 1 = regression.
"""

import asyncio
import contextlib
import json
import sys
import tempfile
import types as _types
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# Importing main pulls in the actions/plugins registries (~5s of discovery
# prints) but opens no audio and no network at import time.
import main as jarvis_main  # noqa: E402
from google.genai import errors as genai_errors  # noqa: E402


# ── config harness ───────────────────────────────────────────────────────────


class _KeyConfig:
    """Point main.API_CONFIG_PATH at a scratch file with N fake keys."""

    def __init__(self, keys: list[str]):
        self.path = Path(tempfile.mkdtemp(prefix="keypin_")) / "api_keys.json"
        self.path.write_text(
            json.dumps(
                {
                    "gemini_api_key": keys[0] if keys else "",
                    "gemini_api_keys": keys[1:],
                }
            ),
            encoding="utf-8",
        )

    def __enter__(self):
        self._saved = jarvis_main.API_CONFIG_PATH
        jarvis_main.API_CONFIG_PATH = self.path
        return self

    def __exit__(self, *a):
        jarvis_main.API_CONFIG_PATH = self._saved


def _reset_index():
    """Each test starts from the first key, whatever earlier tests did."""
    jarvis_main._gemini_key_index = 0


K1, K2, K3 = ("key-aaaaaaaa-1", "key-bbbbbbbb-2", "key-cccccccc-3")


# ── 1. pinning (the core fix) ────────────────────────────────────────────────


def test_key_is_pinned_across_rebuilds():
    """THE regression: with 6 keys configured, every client build used to get
    the NEXT key. Now one process = one key, forever, until an explicit
    rotation."""
    with _KeyConfig([K1, K2, K3]):
        _reset_index()
        seen = [jarvis_main._get_api_key() for _ in range(10)]
        assert seen == [K1] * 10, f"key drifted across builds: {seen!r}"


def test_pinning_survives_config_reload():
    """Re-reading the config file (what every connect does) must not advance
    the key — the index lives in the process, not the file mtime."""
    with _KeyConfig([K1, K2, K3]):
        _reset_index()
        for _ in range(4):
            jarvis_main._load_gemini_keys()  # simulates reconnect churn
        assert jarvis_main._get_api_key() == K1


def test_dedup_and_single_key():
    """A key repeated in gemini_api_keys must not create phantom alternates;
    a single-key setup returns that key and _rotate_api_key reports False."""
    with _KeyConfig([K1, K1, K1]):
        _reset_index()
        assert jarvis_main._load_gemini_keys() == [K1]
        assert jarvis_main._get_api_key() == K1
        assert (
            jarvis_main._rotate_api_key() is False
        ), "rotation claimed success with only one distinct key"


# ── 2. explicit rotation ─────────────────────────────────────────────────────


def test_rotation_advances_and_wraps():
    with _KeyConfig([K1, K2, K3]):
        _reset_index()
        assert jarvis_main._rotate_api_key() is True
        assert jarvis_main._get_api_key() == K2
        assert jarvis_main._rotate_api_key() is True
        assert jarvis_main._get_api_key() == K3
        assert jarvis_main._rotate_api_key() is True  # wraps to the first
        assert jarvis_main._get_api_key() == K1


# ── 3. quota classifier ──────────────────────────────────────────────────────


def test_quota_error_shapes():
    plain = genai_errors.APIError(
        429,
        {
            "error": {
                "code": 429,
                "message": "You exceeded your current quota",
                "status": "RESOURCE_EXHAUSTED",
            }
        },
        None,
    )
    assert jarvis_main._is_quota_error(plain)


def test_quota_error_found_inside_taskgroup():
    """The connect error surfaces wrapped by the TaskGroup — the classifier
    must look through (Base)ExceptionGroup / __context__ like its siblings."""

    class _Group(BaseExceptionGroup):  # 3.11+ builtin; shim covers 3.10
        pass

    inner = RuntimeError("429 RESOURCE_EXHAUSTED on live connect")
    wrapped = _Group("unhandled errors in a TaskGroup", [inner])
    assert jarvis_main._is_quota_error(wrapped)


def test_non_quota_errors_not_matched():
    e = genai_errors.APIError(
        1008,
        {
            "error": {
                "code": 1008,
                "message": "BidiGenerateContent session not found",
                "status": None,
            }
        },
        None,
    )
    assert not jarvis_main._is_quota_error(e)
    assert not jarvis_main._is_quota_error(asyncio.TimeoutError("handshake timed out"))


# ── 4. run-loop behaviour: 429 rotates, keeps the handle ────────────────────


class _StubUI:
    def __init__(self):
        self.logs: list[str] = []

    def write_log(self, msg: str):
        self.logs.append(str(msg))

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


def _fake_genai_module(connect_errors: list[Exception], state: dict):
    """genai stand-in. Attempt n raises connect_errors[min(n, last)] — every
    connect fails, so the run loop's session body (audio threads, wake
    detector, briefing) never executes and the harness stays hermetic, same
    as test_session_resumption_1008.py. The evidence (which key built each
    client, which handle each setup carried) is snapshotted BEFORE raising."""

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            # Which key this client was built with — the heart of the test.
            state.setdefault("client_keys", []).append(kwargs.get("api_key"))

            class _FakeLive:
                def connect(self, model=None, config=None):
                    class _CM:
                        async def __aenter__(self):
                            state["connects"] += 1
                            idx = min(state["connects"] - 1, len(connect_errors) - 1)
                            sru = getattr(config, "session_resumption", None)
                            state.setdefault("handles", []).append(
                                getattr(sru, "handle", "missing")
                            )
                            raise connect_errors[idx]

                        async def __aexit__(self, *a):
                            return False

                    return _CM()

            self.aio = _types.SimpleNamespace(live=_FakeLive())

    return _types.SimpleNamespace(Client=_FakeClient)


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
    with _patch(jarvis_main, "get_wake_word_enabled", lambda: False), _patch(
        jarvis_main, "get_push_to_talk_enabled", lambda: False
    ), _patch(jarvis_main, "get_brief_enabled", lambda: False):
        yield


def _drive(j, errors: list[Exception], max_connects: int) -> dict:
    """Run the REAL run() loop against a failing connect; snapshot client keys
    and the resumption handle each attempt carried."""
    state: dict = {"connects": 0}

    async def _scenario():
        task = asyncio.ensure_future(j.run())
        try:
            loop = asyncio.get_event_loop()
            deadline = loop.time() + 20
            while loop.time() < deadline:
                await asyncio.sleep(0.02)
                if state["connects"] >= max_connects or task.done():
                    break
            await asyncio.sleep(0.05)
        finally:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task

    with _KeyConfig([K1, K2, K3]), _patch(
        jarvis_main, "genai", _fake_genai_module(list(errors), state)
    ), _no_dashboard(), _offline_env():
        _reset_index()
        asyncio.run(asyncio.wait_for(_scenario(), timeout=30))
    return state


_ARMED = "handle-issued-to-project-of-key-1"


def test_429_rotates_key_and_keeps_handle():
    """CORE: a 429 on attempt 1 → attempt 2 builds its client with the NEXT
    key (quota hop) and STILL replays the resumption handle — the handle was
    never the problem; it must travel with the rotated key."""
    e429 = genai_errors.APIError(
        429,
        {
            "error": {
                "code": 429,
                "message": "Quota exceeded for quota metric",
                "status": "RESOURCE_EXHAUSTED",
            }
        },
        None,
    )
    j = jarvis_main.JarvisLive(_StubUI())
    j._resume_handle = _ARMED
    state = _drive(j, [e429, e429], max_connects=2)

    assert state["connects"] >= 2, f"no retry after 429: {state!r}"
    assert state["client_keys"][0] == K1
    assert (
        state["client_keys"][1] == K2
    ), f"429 did not rotate the key: {state['client_keys']!r}"
    assert (
        state["handles"][1] == _ARMED
    ), f"429 path dropped the resumption handle: {state['handles']!r}"
    assert j._resume_handle == _ARMED


def test_handle_rejection_drops_handle_but_keeps_key():
    """The mirror case (unchanged behaviour): 1008 session-not-found must drop
    the HANDLE — and, with the pinning fix, keep the KEY so the replacement
    fresh session at least stays on one project. Attempt 2 replays no handle
    and builds with the same key as attempt 1."""
    e1008 = genai_errors.APIError(
        1008,
        {
            "error": {
                "code": 1008,
                "message": "BidiGenerateContent session not found",
                "status": None,
            }
        },
        None,
    )
    j = jarvis_main.JarvisLive(_StubUI())
    j._resume_handle = _ARMED
    state = _drive(j, [e1008, e1008], max_connects=2)

    assert state["connects"] >= 2
    assert (
        state["handles"][1] is None
    ), f"dead handle was not dropped: {state['handles']!r}"
    assert (
        state["client_keys"][1] == K1
    ), f"handle rejection must NOT rotate the key: {state['client_keys']!r}"


# ── standalone runner ────────────────────────────────────────────────────────


def run_tests() -> int:
    tests = [
        test_key_is_pinned_across_rebuilds,
        test_pinning_survives_config_reload,
        test_dedup_and_single_key,
        test_rotation_advances_and_wraps,
        test_quota_error_shapes,
        test_quota_error_found_inside_taskgroup,
        test_non_quota_errors_not_matched,
        test_429_rotates_key_and_keeps_handle,
        test_handle_rejection_drops_handle_but_keeps_key,
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
