"""
tests/test_vimax_phase5.py — Phase 5: ViMax adapter surface.

MOCKED / OFFLINE by design:
    * progress mapping tested with a REAL subprocess (no ViMax, no network)
    * live ViMax files (adapter-generated .jarvis/*) are read as fixtures —
      the tracked ViMax repo is never touched
    * no paid provider calls anywhere
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from core.repo_agent.jobs import RepoJobStore                # noqa: E402
from core.video_agent.runner import describe_job             # noqa: E402


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "jarvis"
    tools = tmp_path / "JarvisTools"
    home.mkdir()
    tools.mkdir()
    monkeypatch.setenv("JARVIS_HOME", str(home))
    monkeypatch.setenv("JARVIS_TOOLS_ROOT", str(tools))
    monkeypatch.setenv("JARVIS_REPO_AGENT_NOTIFY_HERMES", "0")
    return {"home": home, "tools": tools}


def _register_vimax_ready():
    from core.repo_agent import manifests as M
    M.upsert_repo({"name": "vimax", "url": "local", "commit": "t",
                   "status": "READY"})
    M.upsert_skill({"skill_id": "vimax-video", "repo": "vimax",
                    "name": "ViMax", "enabled": True, "status": "READY"})


# ── live render progress (real subprocess through run_skill) ────────────────

def test_run_skill_maps_vimax_progress_lines(isolated, tmp_path):
    from core.repo_agent import manifests as M
    from core.repo_agent import skills as S
    cmd = [sys.executable, "-c",
           "print('[VIMAX_PROGRESS] extract_characters: start', flush=True); "
           "import time; time.sleep(1.0); "
           "print('[VIMAX_PROGRESS] concat_start: joining', flush=True); "
           "from pathlib import Path; import os; "
           "Path(os.environ['OUT_DIR']) / 'artifact.txt'.write_text('made') "
           "if False else (Path(os.environ['OUT_DIR']) / 'artifact.txt')"
           ".write_text('made it')"]
    manifest = {
        "skill_id": "dummy-skill", "name": "Dummy", "repo": "dummy-repo",
        "version": "test", "description": "offline fixture skill",
        "task_types": ["test"], "requires_config": [],
        "entry_points": {"write": {"cmd": cmd, "timeout_s": 30}},
        "input_schema": {"write": {"name": {"type": "string",
                                            "max_chars": 50}}},
        "outputs": {"write": ["{out_dir}/artifact.txt"]},
        "progress_pattern": r"\[VIMAX_PROGRESS\] ([a-z_0-9]+)",
        "progress_stages": {
            "extract_characters": ["extracting characters", 0.15],
            "concat_start": ["concatenating", 0.9]},
        "limits": {"run_timeout_s": 30},
    }
    M.write_json(M.skill_manifest_path("dummy-repo"), manifest)
    M.upsert_repo({"name": "dummy-repo", "url": "local", "commit": "test",
                   "status": "READY"})
    M.upsert_skill({"skill_id": "dummy-skill", "repo": "dummy-repo",
                    "name": "Dummy", "enabled": True, "status": "READY"})
    seen: list[tuple] = []
    result = S.run_skill("dummy-skill", "write", {"name": "x"},
                         progress=lambda ph, pc, msg: seen.append((ph, pc)),
                         run_context={"out_dir": str(tmp_path / "rr")})
    assert result["status"] == "COMPLETED", result
    phases = [p for p, _ in seen]
    assert "extracting characters" in phases
    assert "concatenating" in phases
    # mapped stages arrive in order and the concat marker is the last mapped
    mapped = [(ph, pc) for ph, pc in seen
              if ph in ("extracting characters", "concatenating")]
    assert [pc for _, pc in mapped] == [0.15, 0.9]


def test_pump_run_on_line_receives_stdout(isolated, tmp_path):
    from core.repo_agent import skills as S
    seen: list[str] = []
    code, _ram = S._pump_run(
        [sys.executable, "-c",
         "print('plain line'); print('[VIMAX_PROGRESS] frame_done: 1')"],
        cwd=tmp_path, env={}, timeout=30,
        out_log=tmp_path / "o.log", err_log=tmp_path / "e.log",
        cancel=lambda: False, on_line=seen.append)
    assert code == 0
    assert any("plain line" in l for l in seen)
    assert any("VIMAX_PROGRESS" in l for l in seen)


# ── live ViMax adapter files ─────────────────────────────────────────────────

def test_vimax_skill_declares_progress_and_outputs():
    from core.repo_agent import skills as S
    skill = S.load_skill("vimax-video")
    assert skill["outputs"]["idea2video"] == ["{out_dir}/**/*.mp4"]
    assert skill["outputs"]["script2video"] == ["{out_dir}/**/*.mp4"]
    assert skill["progress_pattern"] == r"\[VIMAX_PROGRESS\] ([a-z_0-9]+)"
    assert "concat_done" in skill["progress_stages"]
    assert skill["entry_points"]["script2video"]["timeout_s"] == 7200


def test_wrapper_emits_progress_lines():
    text = Path("/home/nagendra/JarvisTools/vimax/.jarvis/jarvis_run.py") \
        .read_text(encoding="utf-8")
    assert "[VIMAX_PROGRESS]" in text
    assert "progress=_progress" in text
    assert "idea=payload" in text and "script=payload" in text


def test_script2video_schema_enforced():
    """required script + 8000-char cap — validated BEFORE any provider call."""
    from core.repo_agent import manifests as M
    from core.repo_agent import skills as S
    from core.repo_agent.security import RepoSecurityError
    skill = S.load_skill("vimax-video")
    with pytest.raises(RepoSecurityError):
        S._validate_args(skill, "script2video", {"script": ""})
    with pytest.raises(RepoSecurityError):
        S._validate_args(skill, "script2video", {"script": "x" * 8001})
    clean = S._validate_args(skill, "script2video",
                             {"script": "A short script. " * 10})
    assert clean["script"].startswith("A short script.")


def test_no_dedicated_storyboard_pipeline_is_documented():
    """Honest contract: this ViMax build has no standalone storyboard
    entrypoint — storyboard artifacts are produced INSIDE an idea2video/
    script2video run (Phase 6 added i2v for image-to-video); the tool never
    claims a storyboard-only op."""
    skill = json.loads(Path("/home/nagendra/JarvisTools/vimax/.jarvis/"
                            "skill.json").read_text())
    assert set(skill["entry_points"]) == {"idea2video", "script2video",
                                          "i2v", "artifacts"}
    assert "storyboard" not in skill["entry_points"]


# ── action layer ─────────────────────────────────────────────────────────────

def _token_of(reply: str) -> str:
    import re
    match = re.search(r"token ([0-9a-f]{16})", reply)
    assert match, f"no approval token in reply:\n{reply}"
    return match.group(1)


def _approve(params: dict, method: str = "VIMAX") -> str:
    """Walk the Phase-6 mandatory method approval: create → token →
    create with token + chosen method."""
    from actions.video_creator import video_creator
    first = video_creator(dict(params))
    return video_creator({**params, "approve_token": _token_of(first),
                          "method": method})


def test_video_creator_script_routes_to_script2video(isolated, tmp_path):
    from actions.video_creator import video_creator
    _register_vimax_ready()
    _approve({"action": "create", "mode": "ai_cinematic",
              "script": "INT. CITY — NIGHT. Rain on neon.",
              "confirm": True,
              "save_dir": str(tmp_path / "s2v")})
    rows = RepoJobStore().list(limit=5, kind="run_skill")
    assert rows
    params = json.loads(rows[0]["params"])
    assert params["op"] == "script2video"
    assert "Rain on neon" in params["args"]["script"]
    # the idea path is unchanged when no script is given
    _approve({"action": "create", "mode": "ai_cinematic",
              "topic": "futuristic city", "confirm": True,
              "save_dir": str(tmp_path / "i2v")})
    rows = RepoJobStore().list(limit=5, kind="run_skill")
    last = sorted(rows, key=lambda r: r["created_at"])[-1]
    params = json.loads(last["params"])
    assert params["op"] == "idea2video"
    assert params["args"]["topic"] == "futuristic city"


def test_video_creator_script_too_long_refused(isolated, tmp_path):
    from actions.video_creator import video_creator
    _register_vimax_ready()
    params = {"action": "create", "mode": "ai_cinematic",
              "script": "x" * 8001, "confirm": True,
              "save_dir": str(tmp_path / "long")}
    first = video_creator(dict(params))
    answer = video_creator({**params, "approve_token": _token_of(first),
                            "method": "VIMAX"})
    assert "8000" in answer and "too long" in answer.lower()
    assert not RepoJobStore().list(limit=10, kind="run_skill")


def test_artifacts_action_lists_and_is_honest(isolated, tmp_path):
    from actions.video_creator import video_creator
    empty = video_creator({"action": "artifacts",
                           "save_dir": str(tmp_path / "none")})
    assert "No finished video files" in empty
    target = tmp_path / "lib"
    (target / "render").mkdir(parents=True)
    (target / "render" / "documentary.mp4").write_bytes(b"v" * 2048)
    out = video_creator({"action": "artifacts", "save_dir": str(target)})
    assert "documentary.mp4" in out


def test_tool_declares_script_and_artifacts():
    from actions.video_creator import TOOL
    props = TOOL["parameters"]["properties"]
    assert "script" in props
    assert "artifacts" in props["action"]["description"]
