"""core/media_prep.py — decide, convert once, cache, and never guess.

The bug these cover: the FFmpeg bundled with PyQt6 has no software AV1 decoder,
so an AV1 file handed to QMediaPlayer produces zero frames and three error lines
per frame. media_prep probes the file first, converts AV1 to H.264 once, and
hands Qt the converted copy.

The clips are generated with a real ffmpeg; when this machine has none, the
tests that need one skip rather than lie. Nothing here touches the network.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from core import media_prep


def _ffmpeg() -> str:
    return media_prep.ffmpeg_bin()


def _make(path, codec_args) -> str:
    ff = _ffmpeg()
    cmd = [
        ff, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=64x48:rate=10:duration=0.3",
        *codec_args, str(path),
    ]
    subprocess.run(cmd, check=True, timeout=120)
    assert path.exists() and path.stat().st_size > 0
    return str(path)


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    if not _ffmpeg() or not media_prep.ffprobe_bin():
        pytest.skip("no ffmpeg/ffprobe on this machine")
    d = tmp_path_factory.mktemp("clips")
    return {
        "h264": _make(d / "h264.mp4", ["-c:v", "libx264", "-preset", "ultrafast",
                                        "-pix_fmt", "yuv420p"]),
        "av1": _make(d / "av1.mp4", ["-c:v", "libaom-av1", "-cpu-used", "8",
                                      "-crf", "50", "-b:v", "0",
                                      "-pix_fmt", "yuv420p"]),
    }


def test_probe_reads_the_real_codec(clips):
    info = media_prep.probe(clips["h264"])
    assert info is not None
    assert info.video == "h264"
    assert "mp4" in info.container


def test_h264_is_handed_straight_to_qt(clips):
    """No conversion, no added latency, hardware decoding untouched."""
    assert media_prep.plan(clips["h264"]).action == "play"


def test_av1_is_converted_when_the_bundle_has_no_software_decoder(clips):
    if media_prep.qt_has_software_av1():
        pytest.skip("this Qt bundle has a software AV1 decoder, so no conversion is needed")
    decision = media_prep.plan(clips["av1"])
    assert decision.action == "convert"
    assert "AV1" in decision.reason


def test_prepare_converts_av1_to_h264_and_reuses_the_copy(clips):
    if media_prep.qt_has_software_av1():
        pytest.skip("no conversion needed on this Qt bundle")

    first, note = media_prep.prepare(clips["av1"])
    assert first != clips["av1"]
    assert note
    info = media_prep.probe(first)
    assert info is not None and info.video == "h264"

    mtime = os.path.getmtime(first)
    second, _ = media_prep.prepare(clips["av1"])
    assert second == first, "a second play must not convert the same file again"
    assert os.path.getmtime(second) == mtime


def test_a_missing_file_is_an_error_not_a_conversion(tmp_path):
    decision = media_prep.plan(str(tmp_path / "nope.mp4"))
    assert decision.action == "error"
    assert "does not exist" in decision.reason


def test_a_corrupt_file_is_reported_as_unreadable(tmp_path):
    bad = tmp_path / "broken.mp4"
    bad.write_bytes(b"this is not a video" * 100)
    decision = media_prep.plan(str(bad))
    assert decision.action == "error"
    assert "corrupt" in decision.reason or "could not be read" in decision.reason


def test_no_ffmpeg_means_behave_as_before(monkeypatch, clips):
    """The tools are optional: without them the file goes to Qt untouched."""
    monkeypatch.setattr(media_prep, "ffmpeg_bin", lambda: "")
    monkeypatch.setattr(media_prep, "ffprobe_bin", lambda: "")
    assert media_prep.plan(clips["av1"]).action == "play"


def test_ffmpeg_is_found_where_it_actually_lives():
    if not media_prep.ffmpeg_bin():
        pytest.skip("no ffmpeg on this machine")
    assert os.path.isabs(media_prep.ffmpeg_bin())
    assert media_prep.ffprobe_bin()
