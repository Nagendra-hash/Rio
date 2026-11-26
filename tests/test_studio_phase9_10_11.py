"""Phases 9/10/11 (Universal Video Studio) — Studio UI, voice commands,
hardware refresh.

Honesty contract:
 * the Studio sections are asserted against the real served module + the
   real APIs (auth-gated)
 * voice commands are exercised through the real TOOL surface
 * the Phase 11 refresh re-measures with REAL probes; compatibility tests
   are REAL mini-renders (0.3 s ffmpeg / 1-frame Blender) or an honest
   "nothing to test locally"
 * nothing here enables an engine beyond what a real probe enables; no
   downloads, no driver/CUDA/kernel operations
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from actions import engine_installer as ei
from actions import video_creator as vc
from core.video_agent import refresh


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    yield


# ── Phase 9: the Studio page ─────────────────────────────────────────────────

def _studio_js() -> str:
    return (Path(__file__).resolve().parent.parent
            / "dashboard" / "static" / "rio" / "video.js").read_text()


def test_studio_module_serves_all_required_sections():
    src = _studio_js()
    for marker in ("New Video", "Engine Selection", "Hardware Status",
                   "Tool Installer", "Active Jobs", "Video Library",
                   "Provider Settings"):
        assert marker in src, f"Studio missing section: {marker}"


def test_studio_approval_flow_present():
    src = _studio_js()
    assert "v-approval" in src and "v-method" in src
    assert "Approve & render" in src
    assert "approve_token" in src and "needs_method" in src
    assert "scene_plan" in src                      # hybrid support


def test_studio_registered_in_build_order():
    src = _studio_js()
    assert "newVideoSection(), enginesSection()," in src
    assert "hardwareSection(), encoderSection(), installerSection()," in src


def test_studio_apis_auth_gated():
    import os
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from dashboard.video_api import register
    app = FastAPI()
    register(app, lambda request: False)            # deny all
    tc = TestClient(app)
    for path in ("/api/video/engines", "/api/video/hardware",
                 "/api/video/installer"):
        assert tc.get(path).status_code == 401, path


def test_installer_api_plan_and_gate():
    import os
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from dashboard.video_api import register
    app = FastAPI()
    register(app, lambda request: True)
    tc = TestClient(app)
    r = tc.post("/api/video/installer", json={"action": "plan",
                                              "engine_id": "TTS_KOKORO"})
    body = r.json()
    assert body["ok"] is True and body["plan"] is not None
    assert body["plan"]["approval_required"] is True
    assert body.get("approval_token")
    # install without a token is refused
    r2 = tc.post("/api/video/installer",
                 json={"action": "install", "engine_id": "TTS_KOKORO"})
    assert r2.json()["ok"] is False


# ── Phase 10: the voice command set ─────────────────────────────────────────

def test_command_show_available_engines():
    out = vc.video_creator({"action": "engines"})
    for engine in ("BLENDER_3D", "WAN_2_1", "LTX_VIDEO", "VIMAX",
                   "CANVAS"):
        assert engine in out
    assert "approval" in out.lower()


def test_command_gpu_supports_ltx():
    """'check whether my GPU supports LTX-Video' — answered from measured
    statuses (INCOMPATIBLE here), never guessed."""
    out = vc.video_creator({"action": "engines"})
    line = [l for l in out.splitlines() if "LTX_VIDEO" in l][0]
    assert "INCOMPATIBLE" in line or "NOT_CONFIGURED" in line


def test_command_hardware_report():
    out = vc.video_creator({"action": "hardware"})
    assert "VAAPI" in out and "CUDA" in out and "Fingerprint" in out


def test_tool_description_teaches_new_commands():
    runtime = " ".join(vc.TOOL["description"].split())
    for phrase in ("show available video engines",
                   "check whether my GPU supports LTX-Video",
                   "generate a hybrid documentary"):
        assert phrase in runtime, f"TOOL description missing: {phrase}"


# ── Phase 11: the hardware-refresh flow ─────────────────────────────────────

def test_refresh_measures_and_persists_snapshot():
    first = refresh.refresh()
    assert first["fingerprint"]
    assert any(e["descriptor"]["id"] == "BLENDER_3D"
               for e in first["engines"])
    second = refresh.refresh()
    assert second["changed"] is False               # same machine, same kit
    snapshot = refresh.state_path()
    assert snapshot.is_file()


def test_refresh_detects_fingerprint_change():
    first = refresh.refresh()
    fake_hw = {"fingerprint": "different-0000", "last_probe":
               "2026-10-27T00:00:00Z"}
    with patch("core.video_agent.hardware.refresh", return_value=fake_hw), \
            patch("core.video_agent.hardware.probe",
                  return_value=fake_hw):
        result = refresh.refresh()
    # the forced engine re-probe still runs; the fingerprint diff is honest
    assert result["fingerprint"] == "different-0000"


def test_compat_test_canvas_real_mini_render():
    result = refresh.compat_test("CANVAS")
    assert result["ok"] is True, result
    assert "test render OK" in result["label"]


def test_compat_test_blender_real_one_frame_render():
    result = refresh.compat_test("BLENDER_3D")
    assert result["ok"] is True, result
    assert "1-frame" in result["label"]


def test_compat_test_wan_honest_without_worker():
    result = refresh.compat_test("WAN_2_1")
    assert result["ok"] is False
    assert "nothing to test locally" in result["label"] or \
        "no remote worker" in result["label"]


def test_compat_test_via_installer_tool():
    out = ei.engine_installer({"action": "test", "engine_id": "CANVAS"})
    assert "PASS" in out and "CANVAS" in out


def test_compat_test_never_enables_beyond_probe():
    """The test must not flip a status the real probe would not."""
    before = engine_status("BLENDER_3D")
    refresh.compat_test("BLENDER_3D")
    after = engine_status("BLENDER_3D")
    assert before["status"] == after["status"]


def engine_status(engine_id: str) -> dict:
    from core.video_agent.engines import registry as engine_registry
    entry = next(e for e in engine_registry.status_table()
                 if e["descriptor"]["id"] == engine_id)
    return entry["status"]
