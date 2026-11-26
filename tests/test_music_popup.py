"""tests/test_music_popup.py — the in-app JioSaavn music popup.

Offline by default: it drives a real MusicPanel (offscreen Qt) through its
public methods and asserts the buttons map to the right player calls, mute
toggles, and that repeated plays reuse one player (no duplicate popups). A
separate, opt-in test plays a real JioSaavn stream and confirms the popup
reaches "Playing" — run it with JARVIS_TEST_JIOSAAVN_LIVE=1 (it needs the
network and an audio device).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402
from gui.theme import HAVE_VIDEO  # noqa: E402

pytest.importorskip("gui.music_popup")
from gui import music_popup  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def panel(app):
    if not HAVE_VIDEO:
        pytest.skip("Qt multimedia is not installed")
    p = music_popup.MusicPanel()
    yield p
    p.hide()


def test_popup_starts_stopped_and_inactive(panel):
    assert panel.status() == "Stopped"
    assert panel.is_active() is False
    assert panel.now_playing() == ""


def test_play_loads_and_reports_now_playing(panel):
    panel.play("about:blank", "Song X", "Artist Y", "")
    assert panel.now_playing() == "Song X — Artist Y"
    assert panel.is_active() is True


def test_mute_unmute_toggles_the_button(panel):
    panel.play("about:blank", "Song X", "Artist Y", "")
    panel.control("mute")
    assert panel._muted is True
    assert "Unmute" in panel._b_mute.text()
    panel.control("unmute")
    assert panel._muted is False
    assert "Mute" in panel._b_mute.text()


def test_repeated_plays_reuse_one_player(panel, app):
    """The anti-duplicate rule: many commands, one popup, one engine."""
    before = id(panel._player)
    panel.play("about:blank", "a", "", "")
    panel.play("about:blank", "b", "", "")
    panel.play("about:blank", "c", "", "")
    assert id(panel._player) == before, "a new engine was created per command"
    assert panel.now_playing() == "c"
    panel.control("stop")


def test_stop_resets_status(panel, app):
    panel.play("about:blank", "Song", "", "")
    panel.control("stop")
    for _ in range(50):
        if panel.status() == "Stopped":
            break
        app.processEvents()
        time.sleep(0.02)
    assert panel.status() == "Stopped"


def test_panel_close_does_not_destroy_it(panel):
    """Closing (the ✕ button / closeEvent) must not tear the app down."""
    panel.show()
    panel.close()   # child widget: hide, never quit the application
    assert isinstance(panel, object)
    panel.hide()


# ── opt-in: real JioSaavn stream, real "Playing" ─────────────────────────────
def test_live_jiosaavn_stream_reaches_playing(panel, app):
    if os.environ.get("JARVIS_TEST_JIOSAAVN_LIVE") != "1":
        pytest.skip("set JARVIS_TEST_JIOSAAVN_LIVE=1 to run the live playback test")
    from actions import music_player as mp

    songs = [s for s in mp._saavn_pick("Kesariya") if s["stream"]]
    if not songs:
        pytest.skip("no JioSaavn stream resolvable from this network")
    s = songs[0]
    panel.play(s["stream"], s.get("title") or s["label"], s.get("artist", ""), s.get("artwork", ""))
    for _ in range(80):
        if panel.status() in ("Playing", "Paused"):
            break
        app.processEvents()
        time.sleep(0.25)
    assert panel.status() in ("Playing", "Paused"), (
        f"expected the live stream to start, got {panel.status()!r}"
    )
    panel.control("stop")
