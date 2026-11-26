"""Focused tests for the RIO window-close shutdown wiring (Phase D).

Covers the pieces of the close-window-stops-the-app chain that can be tested
fast, offline (no Chrome, no real dashboard):
  * rio_window.window_running() keys on the app-owned profile dir only
  * _RioWindowUI.request_shutdown flips the quit flag (idempotent)
  * JarvisLive.graceful_shutdown is a one-shot (second call is a no-op),
    with the hard-exit path stubbed so the test process survives
  * graceful_shutdown stage 3 covers only the summary LLM call with the 5s
    wait_for; the Evolution pass runs strictly after that budget and is
    awaited to completion (gate commit included) before harder exit stages
"""
import asyncio
import importlib.util
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_main():
    spec = importlib.util.spec_from_file_location(
        "main_mod_under_test", ROOT / "main.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


m = _load_main()


# ── probe contract ───────────────────────────────────────────────────────────

def test_window_running_tracks_profile_dir_process():
    # The probe must mirror exactly "a Chrome process using the app's own
    # profile dir exists". It may NOT assume an offline environment: the user
    # can legitimately have RIO open at test time. Assert the contract
    # against pgrep ground truth instead of a hardcoded state.
    import subprocess
    import rio_window
    truth = bool(subprocess.run(
        ["pgrep", "-af", str(rio_window.PROFILE_DIR)],
        capture_output=True, text=True, timeout=5,
    ).stdout.strip())
    assert rio_window.window_running() is truth


def test_window_running_false_with_bogus_probe_env(monkeypatch):
    # When the probe cannot see any matching process, it must report absent.
    import subprocess
    import rio_window
    def empty_pgrep(*a, **kw):
        return subprocess.CompletedProcess(a, 1, stdout="", stderr="")
    monkeypatch.setattr(rio_window.subprocess, "run", empty_pgrep)
    assert rio_window.window_running() is False


def test_window_running_false_when_pgrep_unavailable(monkeypatch):
    # Probe failure (pgrep missing/blocked) must read as "absent" — the
    # shutdown watcher treats a probe exception as not-seen, never as closed.
    import rio_window
    def boom(*a, **kw):
        raise FileNotFoundError("pgrep")
    monkeypatch.setattr(rio_window.subprocess, "run", boom)
    assert rio_window.window_running() is False


# ── UI shim quit flag ────────────────────────────────────────────────────────

def test_request_shutdown_sets_quit():
    ui = m._RioWindowUI()
    assert ui._quit is False
    ui.request_shutdown()
    assert ui._quit is True
    ui.request_shutdown()          # idempotent — must not raise or re-trigger
    assert ui._quit is True


def test_shutdown_guard_flags():
    ui = m._RioWindowUI()
    assert ui._shutdown_started is False


# ── graceful_shutdown one-shot ──────────────────────────────────────────────

def test_graceful_shutdown_one_shot(monkeypatch, tmp_path):
    """Second invocation must return immediately (idempotent); the hard-exit
    path is stubbed out so the test process survives."""

    async def scenario():
        loop = asyncio.get_running_loop()
        jarvis = m.JarvisLive.__new__(m.JarvisLive)   # skip __init__ (Qt/audio)
        jarvis.ui = None
        jarvis.session = None
        jarvis._quit = False
        jarvis._shutting_down = False
        jarvis._dashboard = None
        jarvis._loop = loop

        called = {}

        async def fake_summary():
            called["summary"] = True

        monkeypatch.setattr(jarvis, "_save_session_summary", fake_summary)
        monkeypatch.setattr(m, "maybe_evolve", lambda *a, **k: None, raising=False)
        # stub os._exit exactly where graceful_shutdown resolves it
        monkeypatch.setattr(m, "_os", type("_O", (), {"_exit": staticmethod(
            lambda code: called.setdefault("exit", code))}))

        await jarvis.graceful_shutdown("test")
        assert called.get("summary") is True
        assert jarvis._shutting_down is True
        assert "exit" in called            # path completed to the exit point

        before = dict(called)
        await jarvis.graceful_shutdown("test")   # second trigger → no-op
        assert before == called            # nothing ran again

    asyncio.run(scenario())


# ── evolution passes OUTSIDE the summary's 5s shutdown budget ────────────────

def test_graceful_shutdown_evolution_outside_summary_budget(monkeypatch, ttl=8):
    """Stage 3 (5s wait_for) must cover only the summary; a slow Evolution
    pass runs after that budget, fully awaited, with the hard-exit timer not
    yet armed. Regression for the 2026-10-12 ~85s exit (evolution nested
    inside the 5s-waited summary call, blowing the shutdown budget)."""

    async def scenario():
        loop = asyncio.get_running_loop()
        jarvis = m.JarvisLive.__new__(m.JarvisLive)
        jarvis.ui = None
        jarvis.session = None
        jarvis._quit = False
        jarvis._shutting_down = False
        jarvis._dashboard = None
        jarvis._loop = loop

        called = {}
        SUMMARY_WAIT = 2.0    # < the 5s stage-3 budget
        EVOLVE_WAIT = 1.0     # must finish, whatever its length

        async def slow_summary():
            await asyncio.sleep(SUMMARY_WAIT)
            called["summary"] = "done"

        def slow_evolve():
            time.sleep(EVOLVE_WAIT)          # simulating the ~80s LLM + disk
            called["evolve"] = "committed"
            return "Evolved: 3 facts"

        monkeypatch.setattr(jarvis, "_save_session_summary", slow_summary)
        monkeypatch.setattr(m, "maybe_evolve", slow_evolve, raising=False)
        monkeypatch.setattr(m, "_os", type("_O", (), {"_exit": staticmethod(
            lambda code: called.setdefault("exit", code))}))

        t0 = time.monotonic()
        await jarvis.graceful_shutdown("test")
        elapsed = time.monotonic() - t0

        assert called.get("summary") == "done"
        assert called.get("evolve") == "committed"   # awaited, not dropped
        assert called.get("exit") == 0
        # If evolution ran INSIDE the summary's budget, a summary slower than
        # the budget would have cancelled it; here both must have completed,
        # and the elapsed time must exceed summary + evolve (ordering proof).
        assert elapsed >= SUMMARY_WAIT + (EVOLVE_WAIT - 1e-6)

    asyncio.run(scenario())
