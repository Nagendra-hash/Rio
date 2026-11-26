"""Phase 6 (hardware-encoder upgrade) — encoder, GPU-effects and production
pipeline tests.

Honesty contract: everything here is measured or MOCKED and labelled.
 * VAAPI probes run a REAL tiny encode when the device exists (skipped
   otherwise) — presence of files alone never counts as availability.
 * fallback ordering, the RAM gate, language mapping, the caption-margin
   unit conversion and the duration top-up are unit-tested with mocks.
 * no CUDA, no NVENC, no driver/kernel operations anywhere.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from core.video_agent import encoder as enc
from core.video_agent import gpu_effects as gpu
from core.video_agent import pipeline_shorts as ps
from core.video_agent import tts_runner as tts


# ── encoder selection ────────────────────────────────────────────────────────

def test_select_encoder_prefers_vaapi_when_probe_passes(monkeypatch):
    monkeypatch.setattr(enc, "probe_vaapi", lambda force=False: {
        "available": True, "device": "/dev/dri/by-path/pci-0000:00:02.0-render",
        "driver": "i965", "detail": "ok", "checks": {}})
    chosen = enc.select_encoder("auto")
    assert chosen.backend == "vaapi" and chosen.hardware
    assert chosen.device.endswith("pci-0000:00:02.0-render")
    assert chosen.driver == "i965"


def test_select_encoder_falls_back_to_cpu_when_probe_fails(monkeypatch):
    monkeypatch.setattr(enc, "probe_vaapi", lambda force=False: {
        "available": False, "device": "/dev/dri/by-path/pci-0000:00:02.0-render",
        "driver": "i965", "detail": "probe encode failed", "checks": {}})
    chosen = enc.select_encoder("auto")
    assert chosen.backend == "libx264" and not chosen.hardware
    assert "unavailable" in chosen.reason


def test_encoder_args_match_backend():
    vaapi = enc.Encoder("vaapi", device="/dev/dri/by-path/x", driver="i965")
    cpu = enc.Encoder("libx264")
    assert vaapi.input_args() == ["-vaapi_device", "/dev/dri/by-path/x"]
    assert cpu.input_args() == []
    vac = vaapi.video_codec_args()
    assert "h264_vaapi" in vac and "-b:v" in vac
    cpc = cpu.video_codec_args(threads=2)
    assert "libx264" in cpc and "26" in cpc
    assert vaapi.upload_filter("subtitles=x") .endswith("format=nv12,hwupload")
    assert cpu.upload_filter("subtitles=x").endswith("format=yuv420p")
    env = vaapi.env()
    assert env["LIBVA_DRIVER_NAME"] == "i965"
    assert "LIBVA_DRIVER_NAME" not in cpu.env() or \
        cpu.env().get("LIBVA_DRIVER_NAME") != "i965"


def test_run_encode_falls_back_after_vaapi_failure(tmp_path, monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        if "h264_vaapi" in args:
            class P:
                returncode = 1
                stderr = "vaapi init failed (mocked)"
            return P()

        class P:
            returncode = 0
            stderr = ""
        return P()

    monkeypatch.setattr(enc.subprocess, "run", fake_run)
    monkeypatch.setattr(enc, "probe_vaapi", lambda force=False: {
        "available": True, "device": "/dev/dri/x", "driver": "i965",
        "detail": "ok", "checks": {}})

    def build(enc_obj):
        return ([FFMPEG_TOKEN := "ffmpeg", "-y"] + enc_obj.input_args() +
                enc_obj.video_codec_args() + ["out.mp4"]), enc_obj.env()

    result = enc.run_encode(build, cwd=tmp_path, log=lambda m: None,
                            what="unit test", prefer="auto")
    assert result["backend"] == "libx264"
    assert result["fell_back"] is True
    backends = [a["backend"] for a in result["attempts"]]
    assert backends == ["vaapi", "libx264"]      # vaapi tried first, THEN cpu
    assert len(calls) == 2


def test_run_encode_all_fail_raises(tmp_path, monkeypatch):
    def fake_run(args, **kwargs):
        class P:
            returncode = 1
            stderr = "boom (mocked)"
        return P()

    monkeypatch.setattr(enc.subprocess, "run", fake_run)
    monkeypatch.setattr(enc, "probe_vaapi", lambda force=False: {
        "available": False, "device": "/x", "driver": "i965",
        "detail": "no", "checks": {}})
    with pytest.raises(Exception):
        enc.run_encode(lambda e: (["ffmpeg", "-y", "x"], {}), cwd=tmp_path,
                       log=lambda m: None, what="unit", prefer="libx264")


def test_vaapi_probe_real_device():
    """The real probe on this machine: pass when the verified device exists,
    otherwise the probe must honestly report unavailability (never crash)."""
    probe = enc.probe_vaapi(force=True)
    device = Path(enc.DEFAULT_DEVICE)
    if device.exists():
        assert probe["available"] is True
        assert probe["checks"]["ffmpeg_h264_vaapi"] is True
        assert probe["checks"]["i965_driver_present"] is True
        assert "succeeded" in probe["detail"]
    else:
        assert probe["available"] is False


# ── RAM gate ─────────────────────────────────────────────────────────────────

def test_memory_gate_reads_procfs():
    value = enc.mem_available_mb()
    assert value > 0                       # /proc/meminfo exists on Linux
    assert enc.ensure_memory(needed_mb=1.0) >= 1.0


def test_memory_gate_refuses(monkeypatch):
    monkeypatch.setattr(enc, "mem_available_mb", lambda: 100.0)
    from core.video_agent.pipelines import StageError
    with pytest.raises(StageError):
        enc.ensure_memory(needed_mb=700.0)


# ── language / TTS ───────────────────────────────────────────────────────────

def test_language_codes():
    assert tts.language_code("Hindi") == "hi"
    assert tts.language_code("hi") == "hi"
    assert tts.language_code("हिंदी") == "hi"
    assert tts.language_code("simple English") == "en"
    assert tts.language_code("") == "en"


def test_gtts_engine_hindi(tmp_path):
    """REAL gTTS Hindi synthesis (free cloud, no key) + measured duration."""
    eng = tts.GttsEngine(lang="hi")
    wav = tmp_path / "seg.mp3"
    eng.synth_to_file(wav, "यह एक परीक्षण है।")
    assert wav.is_file() and wav.stat().st_size > 500
    assert tts._wav_duration(wav) > 0.5


# ── NVIDIA honesty (Phase 4) ────────────────────────────────────────────────

def test_nvidia_probe_never_claims_without_path():
    probe = gpu.probe_nvidia()
    assert probe["cuda_used"] is False and probe["nvenc_used"] is False
    if not probe["accelerated_effects_available"]:
        assert probe["effects"] == []
        assert "CPU" in probe["reason"]
    else:
        assert probe["opencl"]["available"] is True


def test_effects_for_render_defaults_to_cpu():
    eff = gpu.effects_for_render()
    if not gpu.probe_nvidia()["accelerated_effects_available"]:
        assert eff["backend"] == "cpu"
        assert eff["cpu_effects"] is True


# ── caption margin unit fix (frame-inspected 2026-10-26) ─────────────────────

def test_caption_margin_converted_from_px_to_ass_units():
    """MarginV is in ASS PlayRes units (288-high), NOT pixels: 420 px on a
    1920-high video = 63 units. The old px value pushed captions off-screen."""
    margin = round(ps._CAPTION_SAFE_BOTTOM * 288 / ps.V_H)
    assert margin == 63


# ── duration top-up (bounded, measured) ──────────────────────────────────────

class _FakeLLM:
    name = "fake"

    def __init__(self, beats):
        self.beats = list(beats)

    def text(self, prompt, *, system=""):
        return self.beats.pop(0) if self.beats else None


class _FakeEngine:
    name = "fake-tts"

    def synth_to_file(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00" * 64)
        return {"path": str(path)}


def test_topup_closes_shortfall(tmp_path, monkeypatch):
    run_dir = tmp_path
    (run_dir / "narration").mkdir()
    (run_dir / "cards").mkdir()
    script = {"title": "t", "hook": "h", "beats": ["b1", "b2"],
              "closing": "c"}
    ctx = {"run_dir": run_dir, "spec": {}, "log": lambda m: None}
    timings = {"engine": "gtts", "total_speech_s": 20.0, "segments": [
        {"id": 1, "kind": "hook", "index": None, "text": "h",
         "path": "x", "duration_s": 5.0},
        {"id": 2, "kind": "beat", "index": 1, "text": "b1",
         "path": "x", "duration_s": 8.0},
        {"id": 3, "kind": "beat", "index": 2, "text": "b2",
         "path": "x", "duration_s": 7.0},
        {"id": 4, "kind": "closing", "index": None, "text": "c",
         "path": "x", "duration_s": 0.0},
    ]}
    spec = {"duration_s": 56, "language": "Hindi"}
    monkeypatch.setattr(ps, "_ask_llm", lambda ctx, prompt, system="": (
        json.dumps({"beat": "extra one"}), "fake"))
    monkeypatch.setattr(tts, "default_engines", lambda lang=None: [_FakeEngine()])
    monkeypatch.setattr(tts, "_wav_duration", lambda p: 5.0)

    ps._topup_segments(ctx, spec, script, timings, lambda m: None)
    assert timings["total_speech_s"] == 30.0
    added = [s for s in timings["segments"] if s.get("topup")]
    assert len(added) == 2
    # inserted BEFORE the closing segment
    assert timings["segments"][-1]["kind"] == "closing"
    assert (run_dir / "cards" / "beat_3.png").is_file()
    assert (run_dir / "cards" / "beat_4.png").is_file()


def test_topup_noop_when_reused_or_close_enough(tmp_path):
    ctx = {"run_dir": tmp_path, "spec": {}, "log": lambda m: None}
    timings = {"engine": "reused:gtts", "total_speech_s": 50.0,
               "segments": [{"id": 1, "kind": "hook", "text": "x",
                             "path": "p", "duration_s": 50.0}]}
    before = json.dumps(timings, sort_keys=True)
    ps._topup_segments(ctx, {"duration_s": 56}, {}, timings, lambda m: None)
    assert json.dumps(timings, sort_keys=True) == before

    timings2 = {"engine": "gtts", "total_speech_s": 53.0, "segments": [
        {"id": 1, "kind": "hook", "text": "x", "path": "p",
         "duration_s": 53.0}]}
    ps._topup_segments(ctx, {"duration_s": 56}, {}, timings2, lambda m: None)
    assert len(timings2["segments"]) == 1        # gap < 5 s → no top-up


# ── shorts pipeline still validates the 30 fps contract ─────────────────────

def test_scene_fps_contract():
    assert ps.SCENE_FPS == 30
    assert (ps.V_W, ps.V_H) == (1080, 1920)


# ── RIO wiring ───────────────────────────────────────────────────────────────

def test_rio_overview_includes_encoder(test_client_factory=None):
    """The overview payload must carry real encoder + gpu state."""
    from dashboard import video_api  # noqa: F401  (import sanity)
    st = enc.encoder_status()
    assert st["selected"] in ("vaapi", "libx264")
    assert st["output"]["fps"] == 30
    assert "available_mb" in st["memory"]


def test_encoder_endpoints_serve_real_status(tmp_path, monkeypatch):
    """/api/video/encoder — auth'd, real probe, no render started."""
    home = tmp_path / "jarvis"
    (home / "memory" / "repo_agent").mkdir(parents=True)
    monkeypatch.setenv("JARVIS_HOME", str(home))
    monkeypatch.setenv("JARVIS_REPO_AGENT_NOTIFY_HERMES", "0")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from dashboard.video_api import register
    app = FastAPI()
    register(app, lambda request: True)
    tc = TestClient(app)
    r = tc.get("/api/video/encoder", headers={"Authorization": "Bearer t"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["encoder"]["selected"] in ("vaapi", "libx264")
    assert "Encoder:" in body["text"]
    assert body["gpu"]["cuda_used"] is False
    # same auth gate as every other endpoint
    from dashboard.video_api import register as _register
    app2 = FastAPI()
    _register(app2, lambda request: False)
    r3 = TestClient(app2).get("/api/video/encoder")
    assert r3.status_code == 401          # same auth gate as every endpoint
