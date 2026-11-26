"""The HUD's one rule for "sound is on, so close the microphone".

Two halves, and the user meets both as the same bug: JARVIS answering a video or
a song. The first half is the HUD's own player — a video or in-HUD music. The
second is everything else: Spotify's desktop app, a browser tab, VLC, a video
that fell back to `webbrowser.open`. core/echo.py cannot help with either,
because it subtracts only the assistant's own output.

These drive the real widget offscreen. Qt is skipped, not failed, when the
machine cannot create a window or lacks Qt multimedia — the rule is what is
under test, not the box it runs on.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def hud():
    from gui.window import HAVE_VIDEO, MainWindow

    if not HAVE_VIDEO:
        pytest.skip("Qt multimedia is not installed, so there is no video to mute for")
    app = QApplication.instance() or QApplication([])
    try:
        win = MainWindow("")
    except Exception as e:                     # no display, no GL, no fonts
        pytest.skip(f"the HUD cannot open on this machine: {e}")
    win._timer.stop() if hasattr(win, "_timer") else None
    yield win
    win.close()
    app.processEvents()


@pytest.fixture(autouse=True)
def _clean(hud):
    """Each test starts from "nothing is playing and nobody has pressed F4"."""
    hud._muted = False
    hud._video_auto_muted = False
    hud._sound_override = False
    hud._external_media = False
    hud._external_label = ""
    hud._video_on = False
    hud._video_split = False
    if hud._video_audio:
        hud._video_audio.setMuted(True)
    if hud._video_sound_out:
        hud._video_sound_out.setMuted(True)
    yield


def test_another_app_playing_closes_the_microphone(hud):
    hud._on_external_media(True, "Spotify — Kesariya")
    assert hud._muted is True
    assert hud._video_auto_muted is True      # JARVIS closed it, not the user
    assert hud._external_label == "Spotify — Kesariya"


def test_the_microphone_comes_back_when_the_sound_stops(hud):
    hud._on_external_media(False, "")
    assert hud._muted is False
    assert hud._video_auto_muted is False


def test_a_mute_the_user_chose_is_never_undone(hud):
    hud._toggle_mute()                        # the user mutes it themselves
    assert hud._muted is True
    hud._on_external_media(True, "VLC")
    hud._on_external_media(False, "")
    assert hud._muted is True                 # their choice, not ours to reverse
    hud._toggle_mute()                        # put it back for the next test
    assert hud._muted is False


def test_f4_during_media_survives_a_track_change(hud):
    """Pressing F4 is the user saying "I want to talk". The next song starting
    must not silently take the microphone away again."""
    hud._on_external_media(True, "Spotify — Kesariya")
    assert hud._muted is True
    hud._toggle_mute()
    assert hud._muted is False

    hud._on_external_media(True, "Spotify — Naatu Naatu")   # track changed
    assert hud._muted is False

    hud._on_external_media(False, "")
    assert hud._muted is False


def test_the_video_that_is_actually_playing_decides(hud):
    """Combined audio lives on the player's own output; a split YouTube stream
    keeps it in the separate one. Asking the wrong player answers about silence
    that is not there."""
    hud._video_on = True

    hud._video_split = False                  # a local file: sound is combined
    hud._video_audio.setMuted(False)
    hud._sync_mic_for_video()
    assert hud._muted is True

    hud._video_audio.setMuted(True)
    hud._sync_mic_for_video()
    assert hud._muted is False

    hud._video_split = True                   # YouTube: sound is its own stream
    hud._video_audio.setMuted(True)
    hud._video_sound_out.setMuted(False)
    hud._sync_mic_for_video()
    assert hud._muted is True

    hud._video_sound_out.setMuted(True)
    hud._video_on = False
    hud._sync_mic_for_video()
    assert hud._muted is False
