"""tests/test_voice_desktop_repair.py — regression tests for the
RIO voice/desktop repair.

These pin the four defects that made the assistant look broken, so none of them
can come back quietly:

1. A microphone level printed under the wrong name, so "RMS" exceeded "peak".
2. Desktop actions dropped from the tool list because `import pyautogui`
   connected to the display at import time.
3. python-xlib rejecting Mutter's XWayland cookie (empty display number).
4. "Switch to cloud" persisting a flag that the engine never read.

Everything here is offline: no model, no audio device, no X server required.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent


# ── 1. PCM level reporting ───────────────────────────────────────────────────
def test_pcm_stats_rms_cannot_exceed_peak():
    """The old log line read RMS=0.1276, peak=0.0318 for the same block."""
    from main import _pcm_stats

    rng = np.random.default_rng(7)
    block = (rng.normal(0, 900, 1024)).astype(np.int16)
    rms, peak = _pcm_stats(block)
    assert 0.0 < rms <= peak <= 1.0


def test_pcm_stats_full_scale():
    from main import _pcm_stats

    block = np.array([-32768, 32767, 0, 0], dtype=np.int16)
    rms, peak = _pcm_stats(block)
    assert peak == pytest.approx(1.0, abs=1e-6)
    assert rms == pytest.approx(0.7071, abs=1e-3)   # two samples at full scale


def test_pcm_stats_float_input_and_empty():
    from main import _pcm_stats

    rms, peak = _pcm_stats(np.array([0.5, -0.5], dtype=np.float32))
    assert peak == pytest.approx(0.5)
    assert rms == pytest.approx(0.5)
    assert _pcm_stats(np.array([], dtype=np.int16)) == (0.0, 0.0)


def test_dbfs_scale():
    from main import _dbfs

    assert _dbfs(1.0) == pytest.approx(0.0, abs=1e-6)
    assert _dbfs(0.5) == pytest.approx(-6.02, abs=0.02)
    assert _dbfs(0.0) == -120.0                     # floored, not -inf


def test_pcm_level_stays_a_hud_scale():
    """`_pcm_level` must keep returning 0..1 for the HUD, separate from dBFS."""
    from main import _pcm_level

    quiet = np.zeros(1024, dtype=np.int16)
    loud = np.full(1024, 20000, dtype=np.int16)
    assert _pcm_level(quiet) == 0.0
    assert 0.0 < _pcm_level(loud) <= 1.0


# ── 2. X11 auth shim ─────────────────────────────────────────────────────────
class _FakeAuthority:
    """Mutter's XWayland file: the cookie with an EMPTY display number."""

    def __init__(self, entries):
        self.entries = entries


def test_relaxed_lookup_accepts_empty_display_number():
    from core.x11_compat import _relaxed_lookup

    cookie = b"\x01" * 16
    auth = _FakeAuthority([
        (256, b"host", b"", b"MIT-MAGIC-COOKIE-1", cookie),
    ])
    assert _relaxed_lookup(auth, 256, "host", 0) == (b"MIT-MAGIC-COOKIE-1", cookie)


def test_relaxed_lookup_keeps_conventional_entries():
    from core.x11_compat import _relaxed_lookup

    auth = _FakeAuthority([
        (256, b"host", b"0", b"MIT-MAGIC-COOKIE-1", b"a" * 16),
        (256, b"host", b"9", b"MIT-MAGIC-COOKIE-1", b"b" * 16),
    ])
    assert _relaxed_lookup(auth, 256, "host", 0)[1] == b"a" * 16
    assert _relaxed_lookup(auth, 256, "host", 9)[1] == b"b" * 16
    assert _relaxed_lookup(auth, 256, b"host", 5) is None


def test_x11_compat_install_is_idempotent():
    from core import x11_compat

    first = x11_compat.install()
    second = x11_compat.install()
    assert first == second
    assert isinstance(x11_compat.describe(), str)


# ── 3. Lazy desktop input ────────────────────────────────────────────────────
def test_lazy_pyautogui_does_not_import_at_construction():
    from core.desktop_input import LazyPyAutoGUI

    proxy = LazyPyAutoGUI()
    assert proxy._mod is None            # nothing imported yet
    assert isinstance(proxy.available(), bool)


def test_desktop_actions_import_without_a_display():
    """The import must never connect to X — that is what dropped four actions."""
    code = (
        "import importlib\n"
        "for m in ('computer_control','computer_settings','desktop',"
        "'send_message','music_player'):\n"
        "    importlib.import_module('actions.' + m)\n"
        "print('ok')\n"
    )
    env = dict(os.environ)
    env["DISPLAY"] = ":99"                       # no such server
    env["XAUTHORITY"] = "/nonexistent/.Xauthority"
    env.pop("WAYLAND_DISPLAY", None)
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert "ok" in out.stdout


# ── 4. The engine switch is actually read back ───────────────────────────────
def test_switch_writes_both_flags(tmp_path, monkeypatch):
    import memory.config_manager as cm

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.delenv("JARVIS_LLM_GEMINI_LIVE_ENABLED", raising=False)
    monkeypatch.setattr(cm, "CONFIG_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(cm, "ensure_config_dir", lambda: tmp_path.mkdir(
        parents=True, exist_ok=True))

    cm.save_provider_mode("gemini")
    data = json.loads((tmp_path / "api_keys.json").read_text())
    assert data["provider_mode"] == "gemini"
    assert data["gemini_live_enabled"] is True
    assert cm.get_gemini_live_enabled() is True

    cm.save_provider_mode("pipeline")
    data = json.loads((tmp_path / "api_keys.json").read_text())
    assert data["provider_mode"] == "pipeline"
    assert data["gemini_live_enabled"] is False
    assert cm.get_gemini_live_enabled() is False


def test_env_override_wins(tmp_path, monkeypatch):
    import memory.config_manager as cm

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setattr(cm, "CONFIG_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(cm, "ensure_config_dir", lambda: tmp_path.mkdir(
        parents=True, exist_ok=True))
    cm.save_provider_mode("pipeline")

    monkeypatch.setenv("JARVIS_LLM_GEMINI_LIVE_ENABLED", "true")
    assert cm.get_gemini_live_enabled() is True
    monkeypatch.setenv("JARVIS_LLM_GEMINI_LIVE_ENABLED", "false")
    assert cm.get_gemini_live_enabled() is False
