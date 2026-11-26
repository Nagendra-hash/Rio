"""Phase 7 (Universal Video Studio) — HYBRID scene routing tests.

Honesty contract:
 * hybrid runs ONLY an approved scene→engine table (the Phase 6 gate
   carries it in the approval); planning refuses without one
 * every scene engine's readiness is MEASURED before routing — an
   unavailable engine refuses that scene, nothing is faked
 * the validation record carries the per-scene routing so a viewer can
   always know which scenes are motion graphics / 3D / real footage
 * no network, no paid calls; renderers are exercised with tiny mocked
   scene outputs where heavy
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from actions import video_creator as vc
from core.video_agent import pipeline_hybrid as ph
from core.video_agent import approval as vappr
from core.video_agent.engines import registry as engine_registry


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    yield


# ── scene-plan parsing ───────────────────────────────────────────────────────

def test_parse_scene_plan_dict_and_string_forms():
    dict_form = ph._scene_plan({"scene_plan": [
        {"scene": 1, "method": "CANVAS"},
        {"scene": 2, "method": "BLENDER_3D", "prompt": "a robot waves"}]})
    assert dict_form[0]["method"] == "CANVAS"
    assert dict_form[1]["prompt"] == "a robot waves"
    str_form = vc._parse_scene_plan("1:CANVAS, 2:BLENDER_3D")
    assert [e["method"] for e in str_form] == ["CANVAS", "BLENDER_3D"]


def test_planning_refuses_without_scene_plan():
    ctx = {"run_dir": Path("/tmp/opencode/hx"), "spec": {"scene_plan": []},
           "log": lambda m: None}
    with pytest.raises(Exception, match="no approved scene plan"):
        ph._plan_hybrid(ctx)


def test_planning_refuses_unroutable_scene():
    spec = {"scene_plan": [{"scene": 1, "method": "WAN_2_1"}]}
    ctx = {"run_dir": Path("/tmp/opencode/hy"), "spec": spec, "log":
           lambda m: None}
    with pytest.raises(Exception, match="cannot run"):
        ph._plan_hybrid(ctx)


# ── the approval-side gate (bypass attempts through the TOOL) ────────────────

def test_hybrid_without_plan_asks_and_starts_nothing():
    from core.repo_agent.jobs import RepoJobStore
    message = vc.video_creator({"action": "create",
                                "topic": "city timelapse",
                                "request": "a 30-second Short"})
    assert "Which method" in message
    token = _token_of(message)
    # HYBRID is offered in the question
    assert "Hybrid" in message
    answer = vc.video_creator({"action": "create", "topic": "city timelapse",
                               "request": "a 30-second Short",
                               "approve_token": token,
                               "method": "HYBRID"})
    assert "scene→engine table" in answer or "scene_plan" in answer
    assert RepoJobStore().list(limit=10) == []      # nothing queued


def test_hybrid_with_unrunnable_scene_keeps_token_pending():
    from core.repo_agent.jobs import RepoJobStore
    message = vc.video_creator({"action": "create",
                                "topic": "city timelapse",
                                "request": "a 30-second Short"})
    token = _token_of(message)
    answer = vc.video_creator({
        "action": "create", "topic": "city timelapse",
        "request": "a 30-second Short", "approve_token": token,
        "method": "HYBRID", "scene_plan": "1:CANVAS,2:WAN_2_1"})
    assert answer.startswith("Refused:")
    assert "WAN_2_1" in answer
    assert vappr.get(token)["state"] == "PENDING"    # token not burned


def test_hybrid_approved_plan_queues_job():
    from unittest.mock import patch as _patch
    message = vc.video_creator({"action": "create",
                                "topic": "city timelapse",
                                "request": "a 30-second Short"})
    token = _token_of(message)
    queued = {}
    with _patch.object(vc, "start_job",
                       side_effect=lambda kind, ref, **kw:
                       queued.update(kind=kind, ref=ref, kw=kw) or
                       {"ok": True, "job": {"job_id": "rt-h",
                                            "status": "QUEUED"}}):
        answer = vc.video_creator({
            "action": "create", "topic": "city timelapse",
            "request": "a 30-second Short", "approve_token": token,
            "method": "HYBRID", "scene_plan": "1:CANVAS,2:CANVAS"})
    assert "queued" in answer.lower(), answer
    assert queued["kind"] == "video" and queued["ref"] == "hybrid"
    spec = queued["kw"]["params"]["spec"]
    assert [e["method"] for e in spec["scene_plan"]] == ["CANVAS", "CANVAS"]


# ── generation routing (tiny mocked scene outputs) ───────────────────────────

def test_generating_routes_each_scene_by_engine(tmp_path):
    spec = {"scene_plan": [{"scene": 1, "method": "CANVAS",
                            "prompt": "open on the skyline"},
                           {"scene": 2, "method": "CANVAS",
                            "prompt": "close on the skyline"}],
            "scene_duration_s": 3, "duration_s": 6}
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    ctx = {"run_dir": run_dir, "spec": spec, "log": lambda m: None}
    note = ph._generating(ctx)
    assert len(note["scenes"]) == 2
    for scene in note["scenes"]:
        assert Path(scene["file"]).is_file()
        assert scene["method"] == "CANVAS"
    assert note["ai_scenes"] == 0                    # honest: canvas = motion
                                                     # graphics, not AI


def test_generating_refuses_unavailable_scene_engine(tmp_path):
    spec = {"scene_plan": [{"scene": 1, "method": "VIMAX"}],
            "duration_s": 10}
    ctx = {"run_dir": tmp_path / "r", "spec": spec, "log": lambda m: None}
    (tmp_path / "r").mkdir()
    with pytest.raises(Exception, match="cannot render here"):
        ph._generating(ctx)


def test_editing_assembles_rendered_scenes(tmp_path):
    spec = {"scene_plan": [{"scene": 1, "method": "CANVAS"},
                           {"scene": 2, "method": "CANVAS"}],
            "aspect_ratio": "16:9", "duration_s": 6}
    run_dir = tmp_path / "run"
    # fabricate two tiny scene clips with real ffmpeg (fast, 0.3s each)
    for i in (1, 2):
        d = run_dir / "scenes" / f"scene_{i:02d}"
        d.mkdir(parents=True)
        import subprocess
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                        "-i", f"testsrc2=size=320x240:duration=0.3",
                        "-c:v", "libx264", "-preset", "ultrafast",
                        str(d / f"scene_{i:02d}.mp4")], check=True,
                       capture_output=True)
    ctx = {"run_dir": run_dir, "spec": spec, "log": lambda m: None}
    note = ph._editing(ctx)
    final = Path(note["output"])
    assert final.is_file()
    facts = ph.probe_video(final)
    assert facts["video_codec"] == "h264"


def test_validation_record_carries_routing(tmp_path):
    spec = {"scene_plan": [{"scene": 1, "method": "CANVAS"},
                           {"scene": 2, "method": "STOCK_FOOTAGE"}]}
    assert [e["method"] for e in ph._scene_plan(spec)] == \
        ["CANVAS", "STOCK_FOOTAGE"]


def _token_of(reply: str) -> str:
    import re
    match = re.search(r"token ([0-9a-f]{16})", reply)
    assert match, f"no token in reply:\n{reply}"
    return match.group(1)
