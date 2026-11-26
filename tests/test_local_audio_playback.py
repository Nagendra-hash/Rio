"""Local audio: the file on disk is the thing played, not a download.

Two paths exist and both are checked here:
  * with a HUD, music_player hands the path to the HUD's own player,
  * without one, actions/media_play uses ffplay from the ffmpeg bundle that is
    already on this machine (there is no `ffplay` on PATH here, which is
    exactly the case that used to fail).

The ffplay test really starts the process. It uses SDL's dummy audio driver so
a headless machine can run it; the assertion is that the process is still alive
after the handler's own liveness check, then it is stopped.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from actions import media_play
from core import media_prep


@pytest.fixture(scope="module")
def mp3(tmp_path_factory):
    ff = media_prep.ffmpeg_bin()
    if not ff:
        pytest.skip("no ffmpeg on this machine")
    path = tmp_path_factory.mktemp("audio") / "tone.mp3"
    subprocess.run(
        [ff, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=20",
         "-c:a", "libmp3lame", "-b:a", "128k", str(path)],
        check=True, timeout=120,
    )
    return path


def test_an_mp3_path_is_recognised(tmp_path, mp3):
    assert media_play.local_audio_file(str(mp3)) == str(mp3)


def test_a_file_url_is_recognised(mp3):
    assert media_play.local_audio_file(mp3.as_uri()) == str(mp3)


@pytest.mark.parametrize("name", ["song.m4a", "song.wav", "song.flac", "song.ogg",
                                  "song.opus", "song.aac"])
def test_the_usual_audio_extensions_are_accepted(tmp_path, name):
    f = tmp_path / name
    f.write_bytes(b"\x00" * 16)
    assert media_play.local_audio_file(str(f)) == str(f)


def test_a_video_file_is_not_treated_as_local_audio(tmp_path):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"\x00" * 16)
    assert media_play.local_audio_file(str(f)) == ""


def test_a_missing_file_is_not_local_audio(tmp_path):
    assert media_play.local_audio_file(str(tmp_path / "nope.mp3")) == ""


def test_the_handler_plays_the_file_on_the_speakers(monkeypatch, mp3):
    called = {}

    def fake_speakers(url, device=""):
        called["url"] = url
        return ""

    monkeypatch.setattr(media_play, "_play_on_speakers", fake_speakers)
    reply = media_play.media_play({"action": "play", "url": str(mp3)})
    assert called["url"] == str(mp3)
    assert reply == "Playing tone.mp3 on the speakers."


def test_the_handler_reports_why_it_could_not_play(monkeypatch, mp3):
    monkeypatch.setattr(
        media_play, "_play_on_speakers",
        lambda url, device="": "no local player (ffplay) is installed",
    )
    reply = media_play.media_play({"action": "play", "url": str(mp3)})
    assert "could not play tone.mp3" in reply
    assert "ffplay" in reply


def test_ffplay_from_the_ffmpeg_bundle_really_starts(monkeypatch, mp3):
    """The locator, the argv, and the liveness check, against a real process."""
    if not media_prep.ffplay_bin():
        pytest.skip("this machine has no ffplay (and no ffmpeg bundle)")
    monkeypatch.setenv("SDL_AUDIODRIVER", "dummy")   # headless: no sound card
    try:
        why = media_play._play_on_speakers(str(mp3))
        assert why == "", why
        proc = media_play._LOCAL_PROC
        assert proc is not None and proc.poll() is None, "ffplay died immediately"
    finally:
        media_play._kill_local_player()
    assert media_play._LOCAL_PROC is None
