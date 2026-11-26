"""
tests/test_cinematic_phase6.py — Phase 6: cinematic provider interfaces.

MOCKED / OFFLINE by design:
    * capability matrix composes measured provider truth (no network, no
      paid calls)
    * i2v tested at the adapter boundary (schema, prepare, wrapper wiring) —
      the provider call itself stays billing-blocked and is NEVER faked
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from core.repo_agent.jobs import RepoJobStore                # noqa: E402
from core.video_agent import cinematic                       # noqa: E402


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


# ── capability matrix ────────────────────────────────────────────────────────

def test_capability_matrix_covers_all_three_ops():
    rows = {r["op"]: r for r in cinematic.capability_matrix()}
    for op in ("TEXT_TO_VIDEO", "IMAGE_TO_VIDEO", "SCENE_TO_VIDEO"):
        assert op in rows
        assert rows[op]["interface"] == "implemented"
    assert rows["TEXT_TO_VIDEO"]["executor"] == "ViMax idea2video"
    assert rows["IMAGE_TO_VIDEO"]["executor"] == "ViMax i2v"
    assert rows["SCENE_TO_VIDEO"]["executor"] == "ViMax script2video"


def test_capability_matrix_is_honestly_blocked():
    """No free AI video backend exists here (measured) — the matrix says so
    and names the zero-cost alternative instead of pretending."""
    rows = {r["op"]: r for r in cinematic.capability_matrix()}
    for op in ("TEXT_TO_VIDEO", "IMAGE_TO_VIDEO", "SCENE_TO_VIDEO"):
        assert "BLOCKED" in rows[op]["provider_status"]
    assert any(r["op"] == "LOCAL_WAN_LTX" for r in cinematic.capability_matrix())
    wan = next(r for r in cinematic.capability_matrix()
               if r["op"] == "LOCAL_WAN_LTX")
    assert wan["interface"] == "NOT IMPLEMENTED"
    assert "hardware" in wan["provider_status"].lower()


def test_matrix_text_offers_documentary_alternative():
    text = cinematic.matrix_text()
    assert "documentary" in text.lower()
    assert "never" in text.lower() and "pixel-perfect" in text.lower()


# ── scenes → script (SCENE_TO_VIDEO input, no LLM) ───────────────────────────

def test_scenes_to_script_from_list():
    script = cinematic.scenes_to_script([
        "A paper airplane lifts off a windowsill.",
        "It glides between neon towers."])
    assert script.startswith("SCENE 1:")
    assert "SCENE 2:" in script and "neon towers" in script


def test_scenes_to_script_passthrough_and_cap():
    assert cinematic.scenes_to_script("  One scene only.  ") == \
        "One scene only."
    assert len(cinematic.scenes_to_script(["x" * 9000] * 3)) <= 8000


# ── i2v adapter boundary (schema + prepare + wrapper wiring) ─────────────────

def test_i2v_in_live_skill_manifest():
    from core.repo_agent import skills as S
    skill = S.load_skill("vimax-video")
    assert "i2v" in skill["entry_points"]
    assert skill["input_schema"]["i2v"]["image"]["required"] is True
    assert skill["input_schema"]["i2v"]["prompt"]["required"] is True
    assert skill["outputs"]["i2v"] == ["{out_dir}/**/*.mp4"]


def test_i2v_wrapper_wiring():
    wrapper = Path("/home/nagendra/JarvisTools/vimax/.jarvis/jarvis_run.py") \
        .read_text(encoding="utf-8")
    assert '"i2v"' in wrapper and "i2v_saved" in wrapper
    assert "generate_single_video" in wrapper
    assert "reference_image_paths=list(payload" in wrapper


def test_i2v_schema_validation():
    from core.repo_agent import skills as S
    from core.repo_agent.security import RepoSecurityError
    skill = S.load_skill("vimax-video")
    with pytest.raises(RepoSecurityError):
        S._validate_args(skill, "i2v", {"prompt": "no image"})
    with pytest.raises(RepoSecurityError):
        S._validate_args(skill, "i2v", {"image": "/tmp/a.png"})
    clean = S._validate_args(skill, "i2v",
                             {"image": "/tmp/a.png", "prompt": "fly"})
    assert clean["image"] == "/tmp/a.png"
    # optional enum absent → omitted from the clean args (default None)
    assert "aspect_ratio" not in clean or clean["aspect_ratio"] in \
        ("16:9", "9:16", "1:1")


def test_prepare_i2v_copies_image_and_never_moves_it(isolated, tmp_path,
                                                     monkeypatch):
    """The user's original image is COPIED into the run scratch dir — the
    original file must remain untouched (mission cleanup rule)."""
    import memory.config_manager as cm
    cfg_dir = isolated["home"] / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "api_keys.json").write_text(json.dumps(
        {"openrouter_keys": ["sk-or-test"]}))
    # CONFIG_FILE is bound at import time — pin it hermetically (the same
    # cross-file pollution guard as test_repo_agent.py)
    monkeypatch.setattr(cm, "CONFIG_FILE", cfg_dir / "api_keys.json")
    from core.repo_agent import vimax
    src = tmp_path / "photo.png"
    src.write_bytes(b"\x89PNG fake image bytes")
    before = src.read_bytes()
    run_dir = tmp_path / "run"
    ctx = vimax.prepare(skill_id="vimax-video", op="i2v",
                        params={"image": str(src), "prompt": "fly",
                                "aspect_ratio": "16:9"},
                        run_dir=run_dir)
    payload = json.loads(Path(ctx["params_file"]).read_text())
    assert payload["prompt"] == "fly"
    assert Path(payload["reference_images"][0]).is_file()
    assert Path(payload["reference_images"][0]) != src      # scratch copy
    assert src.read_bytes() == before                        # original intact
    assert payload["output_path"].endswith("i2v.mp4")


def test_prepare_i2v_missing_image_fails_honestly(isolated, tmp_path,
                                                  monkeypatch):
    import memory.config_manager as cm
    cfg_dir = isolated["home"] / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "api_keys.json").write_text(json.dumps(
        {"openrouter_keys": ["sk-or-test"]}))
    monkeypatch.setattr(cm, "CONFIG_FILE", cfg_dir / "api_keys.json")
    from core.repo_agent import vimax
    with pytest.raises(FileNotFoundError):
        vimax.prepare(skill_id="vimax-video", op="i2v",
                      params={"image": str(tmp_path / "nope.png"),
                              "prompt": "fly"},
                      run_dir=tmp_path / "run")


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


def test_video_creator_image_routes_to_i2v(isolated, tmp_path):
    from actions.video_creator import video_creator
    _register_vimax_ready()
    img = tmp_path / "city.png"
    img.write_bytes(b"\x89PNG fake")
    params = {"action": "create", "mode": "ai_cinematic",
              "image": str(img), "prompt": "the city wakes up",
              "confirm": True, "save_dir": str(tmp_path / "i2v-out")}
    _approve(params)                              # Phase 6 method approval
    rows = RepoJobStore().list(limit=5, kind="run_skill")
    assert rows
    params_row = json.loads(rows[0]["params"])
    assert params_row["op"] == "i2v"
    assert params_row["args"]["image"] == str(img)
    assert params_row["args"]["prompt"] == "the city wakes up"


def test_video_creator_image_without_prompt_asks_one_question(isolated, tmp_path):
    from actions.video_creator import video_creator
    _register_vimax_ready()
    img = tmp_path / "city.png"
    img.write_bytes(b"\x89PNG fake")
    params = {"action": "create", "mode": "ai_cinematic",
              "image": str(img), "confirm": True,
              "save_dir": str(tmp_path / "i2v-out")}
    first = video_creator(dict(params))
    assert "Which method" in first                # the method question first
    token = _token_of(first)
    # choosing VIMAX pre-flights the missing motion prompt WITHOUT burning
    # the token (nothing consumed, nothing queued)
    answer = video_creator({**params, "approve_token": token,
                            "method": "VIMAX"})
    assert answer.count("?") == 1
    assert not RepoJobStore().list(limit=10, kind="run_skill")
    from core.video_agent import approval as vappr
    assert vappr.get(token)["state"] == "PENDING"


def test_video_creator_image_missing_refused(isolated, tmp_path):
    from actions.video_creator import video_creator
    _register_vimax_ready()
    params = {"action": "create", "mode": "ai_cinematic",
              "image": str(tmp_path / "ghost.png"),
              "prompt": "fly", "confirm": True,
              "save_dir": str(tmp_path / "x")}
    first = video_creator(dict(params))
    answer = video_creator({**params, "approve_token": _token_of(first),
                            "method": "VIMAX"})
    assert "does not exist" in answer.lower()
    assert not RepoJobStore().list(limit=10, kind="run_skill")


def test_video_creator_scenes_route_to_script2video(isolated, tmp_path):
    from actions.video_creator import video_creator
    _register_vimax_ready()
    params = {"action": "create", "mode": "ai_cinematic",
              "scenes": ["Rain over neon towers.",
                         "The airplane rises."],
              "confirm": True, "save_dir": str(tmp_path / "sc-out")}
    _approve(params)
    rows = RepoJobStore().list(limit=5, kind="run_skill")
    params_row = json.loads(rows[0]["params"])
    assert params_row["op"] == "script2video"
    assert "SCENE 1: Rain over neon towers." in params_row["args"]["script"]
    assert "SCENE 2:" in params_row["args"]["script"]


def test_failed_cinematic_status_offers_documentary(isolated, tmp_path):
    """Honest alternative (mission rule): when a cinematic job fails at the
    provider gate, the status offers the zero-cost documentary path — and
    never a silent substitution."""
    from actions.video_creator import video_creator
    store = RepoJobStore()
    created = store.create("run_skill", "vimax-video",
                           params={"op": "i2v", "args": {}})
    store.update(created["job_id"], status="FAILED", finished_at=1,
                 error="402 — would exceed your available credits")
    out = video_creator({"action": "status",
                         "job_id": created["job_id"]})
    assert "documentary" in out.lower()
    assert "never passed off" in out.lower()


def test_cinematic_action_renders_matrix(isolated):
    from actions.video_creator import video_creator
    out = video_creator({"action": "cinematic"})
    assert "TEXT_TO_VIDEO" in out and "IMAGE_TO_VIDEO" in out
    assert "SCENE_TO_VIDEO" in out and "BLOCKED" in out


def test_tool_declares_cinematic_surface():
    from actions.video_creator import TOOL
    props = TOOL["parameters"]["properties"]
    for key in ("image", "prompt", "scenes"):
        assert key in props
    assert "cinematic" in props["action"]["description"]
