"""Avatar e2e pipeline unit tests (fast, offline) — the REAL render/TTS
e2e is run by scripts/avatar_e2e_test.py and reported separately."""
from __future__ import annotations

from pathlib import Path

from core.video_agent.avatar_pipeline import build_srt, split_sentences


def test_split_sentences_keeps_punctuation():
    sents = split_sentences(
        "Hello! I am RIO, your personal AI assistant. "
        "I can help you organize tasks, explore ideas, and create videos. "
        "This is my first animated avatar test.")
    assert len(sents) == 4
    assert sents[0] == "Hello!"
    assert sents[-1].endswith("test.")


def test_split_sentences_empty_is_safe():
    assert split_sentences("") == []
    assert split_sentences(None) == []          # type: ignore[arg-type]


def test_build_srt_uses_measured_timings(tmp_path):
    timings = [
        {"text": "Hello!", "start": 1.2, "end": 1.98},
        {"text": "I am RIO.", "start": 2.3, "end": 4.01},
    ]
    path = build_srt(timings, tmp_path / "subs.srt")
    text = path.read_text(encoding="utf-8")
    assert "00:00:01,200 --> 00:00:01,980" in text
    assert "00:00:02,300 --> 00:00:04,010" in text
    assert text.strip().splitlines()[0] == "1"


def test_scene_builder_script_exists_and_has_talk_animation():
    script = (Path(__file__).resolve().parent.parent
              / "core" / "video_agent" / "blender"
              / "build_avatar_scene.py")
    src = script.read_text()
    for marker in ("Mouth", "blink", "talk_start", "bob",
                   "BLENDER_EEVEE"):
        assert marker in src, f"scene builder missing: {marker}"
