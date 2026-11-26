"""Character-animation pipeline unit tests (fast, offline). The REAL render
e2e runs via scripts/dance_video_test.py and is reported separately."""
from __future__ import annotations

from pathlib import Path

import pytest

from core.video_agent import spec as vspec
from core.video_agent.engines import registry as engine_registry
from core.video_agent.pipeline_character_animation import synthesize_music


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    yield


def test_synthesized_music_is_original_and_beat_exact(tmp_path):
    result = synthesize_music(6.0, tmp_path / "music.wav", bpm=100)
    import wave
    with wave.open(result["file"]) as wf:
        frames = wf.getnframes()
        rate = wf.getframerate()
    assert abs(frames / rate - 6.0) < 0.05
    assert result["beat_s"] == pytest.approx(0.6)
    assert "original" in result["license"]
    # 6 s at 100 BPM = exactly 10 beats — the choreography grid is exact
    assert abs(6.0 / result["beat_s"] - 10.0) < 0.01


def test_character_animation_mode_detection():
    mode, _ = vspec.detect_mode("make a dance video with the characters")
    assert mode == "CHARACTER_ANIMATION"
    mode2, _ = vspec.detect_mode("3d character animation for kids")
    assert mode2 == "CHARACTER_ANIMATION"


def test_character_animation_mode_defaults():
    spec = vspec.build_spec({"topic": "playground dance",
                             "mode": "CHARACTER_ANIMATION",
                             "narration": "the boy dances",
                             "duration_s": 18})
    assert spec["pipeline"] == "character_animation"
    assert spec["aspect_ratio"] == "9:16"
    assert spec["resolution"] == "720p"
    assert spec["narration"] == "the boy dances"
    assert 15 <= spec["duration_s"] <= 20


def test_engine_registered_and_ready():
    engine = engine_registry.get("CHARACTER_ANIMATION")
    assert engine is not None
    status = engine.probe()
    assert status.status == "READY_LOCAL"       # blender verified on this box
    params = engine.start_params({"pipeline": "character_animation",
                                  "out_dir": "/tmp/x"})
    assert params and params["ref"] == "character_animation"


def test_engine_stages_resolve_through_registry():
    from core.video_agent import pipelines
    stages = pipelines.stages_for("character_animation")
    assert stages and "generating" in stages and "validating" in stages
    note = stages["planning"]({"spec": {}, "run_dir": None,
                               "log": lambda m: None})
    assert "joint-pivot" in note["cast"]
    assert "armature" in note["honest_limits"]


def test_scene_builder_script_has_required_actions():
    script = (Path(__file__).resolve().parent.parent
              / "core" / "video_agent" / "blender"
              / "build_character_dance_scene.py")
    src = script.read_text()
    for action in ("def walk", "def dance_sidestep", "def clap", "def wave",
                   "def bounce", "def celebrate", "def point", "def look",
                   "def laugh", "def talk", "def idle"):
        assert action in src, f"animation action missing: {action}"
    for marker in ("build_character", "build_playground", "choreograph",
                   "Expert", "Boy"):
        assert marker in src


def test_fps_and_loudness_guards_read_real_files(tmp_path):
    """The validator refuses non-24-fps output and a silent mix; prove the
    two measurements it relies on actually read a real container."""
    import shutil
    import subprocess

    from core.video_agent.pipeline_character_animation import (
        _mean_volume_db, _video_fps)

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    for rate in (24, 30):
        clip = tmp_path / f"clip{rate}.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-f", "lavfi", "-i", f"testsrc=size=64x64:rate={rate}",
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
             "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-c:a", "aac", str(clip)], check=True, timeout=180)
        assert _video_fps(clip) == pytest.approx(float(rate), abs=0.01)
        assert _mean_volume_db(clip) > -24.0
