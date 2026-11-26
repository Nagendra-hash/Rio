"""Real playback through the real HUD widget, offscreen.

These are not mocks: they open a generated H.264 file in the same
QMediaPlayer/QGraphicsVideoItem the user watches, count decoded frames, and
drive pause / seek / resume / close. The AV1 test is the regression for the bug
this work started with — the bundled Qt FFmpeg cannot software-decode AV1, so
core/media_prep.py converts it first and the *converted* file must play.

Qt is skipped, not failed, when the machine cannot create a window or has no
multimedia plugins.
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from core import media_prep  # noqa: E402


def _make_clip(path, args) -> str:
    ff = media_prep.ffmpeg_bin()
    subprocess.run(
        [
            ff, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=15:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            *args, "-shortest", str(path),
        ],
        check=True, timeout=180,
    )
    return str(path)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    if not media_prep.ffmpeg_bin():
        pytest.skip("no ffmpeg on this machine")
    d = tmp_path_factory.mktemp("video")
    return {
        "h264": _make_clip(
            d / "h264.mp4",
            ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-c:a", "aac"],
        ),
        "av1": _make_clip(
            d / "av1.mp4",
            ["-c:v", "libaom-av1", "-cpu-used", "8", "-crf", "50", "-b:v", "0",
             "-pix_fmt", "yuv420p", "-c:a", "aac"],
        ),
        "vp9": _make_clip(
            d / "vp9.webm",
            ["-c:v", "libvpx-vp9", "-b:v", "300k", "-pix_fmt", "yuv420p",
             "-c:a", "libopus"],
        ),
        "mkv": _make_clip(
            d / "h264.mkv",
            ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-c:a", "aac"],
        ),
    }


@pytest.fixture(scope="module")
def hud(app):
    from gui.window import HAVE_VIDEO, MainWindow

    if not HAVE_VIDEO:
        pytest.skip("Qt multimedia is not installed")
    try:
        win = MainWindow("")
    except Exception as e:  # no display, no GL, no fonts
        pytest.skip(f"the HUD cannot open on this machine: {e}")
    if hasattr(win, "_timer"):
        win._timer.stop()
    yield win
    win.close()
    app.processEvents()


class _Probe:
    """Count frames and remember the last error, from the HUD's own sink."""

    def __init__(self, win):
        self.frames = 0
        self.size = ""
        self.error = ""
        self._sink = win._video_item.videoSink()
        self._sink.videoFrameChanged.connect(self._on_frame)
        win._video_player.errorOccurred.connect(self._on_error)

    def _on_frame(self, frame):
        if frame.isValid():
            self.frames += 1
            self.size = f"{frame.width()}x{frame.height()}"

    def _on_error(self, _e, message):
        self.error = message

    def wait(self, app, ms=4000, until=None):
        t0 = time.time()
        while (time.time() - t0) * 1000 < ms:
            app.processEvents()
            time.sleep(0.01)
            if until and until():
                return


def _play(app, win, path, muted=True):
    probe = _Probe(win)
    win._video_open_sig.emit(str(path), os.path.basename(str(path)), muted, "")
    probe.wait(app, 5000, lambda: probe.frames > 0 and win._video_player.position() > 400)
    return probe


def test_h264_plays_with_frames_and_audio(app, hud, clips):
    probe = _play(app, hud, clips["h264"], muted=False)
    try:
        assert probe.frames > 0, f"no frames decoded ({probe.error})"
        assert probe.size == "160x120"
        assert hud._video_player.playbackState().name == "PlayingState"
        assert hud._video_player.hasAudio()
        assert not hud._video_audio.isMuted()
    finally:
        hud._video_close_sig.emit()
        app.processEvents()


@pytest.mark.parametrize("name", ["vp9", "mkv"])
def test_common_containers_and_codecs_play(app, hud, clips, name):
    """WebM/VP9 and Matroska/H.264 go straight to Qt — no conversion."""
    assert media_prep.plan(clips[name]).action == "play"
    probe = _play(app, hud, clips[name])
    try:
        assert probe.frames > 0, f"{name} produced no frames ({probe.error})"
        assert probe.size == "160x120"
    finally:
        hud._video_close_sig.emit()
        app.processEvents()


def test_pause_seek_and_resume(app, hud, clips):
    probe = _play(app, hud, clips["h264"])
    try:
        hud._video_player.pause()
        app.processEvents()
        assert hud._video_player.playbackState().name == "PausedState"

        hud._on_video_seek(1.2, relative=False)
        probe.wait(app, 1500, lambda: hud._video_player.position() >= 1100)
        assert hud._video_player.position() >= 1100

        hud._video_player.play()
        app.processEvents()
        assert hud._video_player.playbackState().name == "PlayingState"
    finally:
        hud._video_close_sig.emit()
        app.processEvents()


def test_av1_is_converted_and_then_plays(app, hud, clips):
    """The original bug: AV1 produced zero frames. The converted copy must not."""
    if media_prep.qt_has_software_av1():
        pytest.skip("this Qt bundle decodes AV1 in software; no conversion is used")
    path, _note = media_prep.prepare(clips["av1"])
    probe = _play(app, hud, path)
    try:
        assert probe.frames > 0, f"converted AV1 produced no frames ({probe.error})"
        assert hud._video_player.playbackState().name == "PlayingState"
    finally:
        hud._video_close_sig.emit()
        app.processEvents()


def test_a_corrupt_file_is_reported_not_retried(hud, tmp_path):
    bad = tmp_path / "broken.mp4"
    bad.write_bytes(b"\x00" * 4096)
    decision = media_prep.plan(str(bad))
    assert decision.action == "error"


def test_a_local_mp3_plays_through_the_same_player(app, hud, tmp_path):
    """Local music uses this player too (music_player._play_in_hud).

    An audio-only file has no frames to count, so the evidence is the transport:
    Qt must recognise audio, start, and advance the position. This is the same
    surface the user watches, not a second player bolted on beside it.
    """
    ff = media_prep.ffmpeg_bin()
    if not ff:
        pytest.skip("no ffmpeg on this machine")
    song = tmp_path / "song.mp3"
    subprocess.run(
        [ff, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
         "-c:a", "libmp3lame", "-b:a", "128k", str(song)],
        check=True, timeout=120,
    )
    probe = _Probe(hud)
    hud._video_open_sig.emit(str(song), "song.mp3", False, "")
    probe.wait(app, 6000, lambda: hud._video_player.position() > 300)
    try:
        assert hud._video_player.hasAudio(), (
            f"the mp3 was not recognised as audio ({probe.error})"
        )
        assert hud._video_player.position() > 300, "the mp3 never advanced"
        assert not hud._video_audio.isMuted()
    finally:
        hud._video_close_sig.emit()
        app.processEvents()
