"""Offline checks for the VideoRAG integration.

No GPU, no LLM calls, no indexing — the install drill (venv build, package
import, preflight refusal, action discovery, skill allowlist) was run at
install time, 2026-10-29. These tests lock the WIRING so a refactor cannot
quietly break the code path the voice/dashboard surfaces use.

Run:  .venv/bin/python -m pytest tests/test_videorag_integration.py -q
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

RUNNER = BASE / "scripts" / "videorag_run.py"
FETCH = BASE / "scripts" / "videorag_fetch_models.sh"
ACTION = BASE / "actions" / "video_rag.py"
ADAPTER = BASE / "core" / "repo_agent" / "videorag.py"
REPO_SKILL = BASE / ".agents" / "skills" / "videorag" / "SKILL.md"
HERMES_SKILL = Path.home() / ".hermes" / "skills" / "videorag" / "SKILL.md"

TOOLS = Path("/home/nagendra/JarvisTools/videorag")
VENV_PY = TOOLS / ".venv" / "bin" / "python"
UPSTREAM = TOOLS / "VideoRAG-algorithm"


# ── install shape ───────────────────────────────────────────────────────────
def test_repo_installed_outside_mark3():
    assert UPSTREAM.is_dir(), "upstream checkout missing"
    assert VENV_PY.exists(), "isolated venv missing"
    # the pinned stack must never leak into Mark-III's own venv
    assert not (BASE / ".venv-videorag").exists()
    manifest = json.loads((TOOLS / ".jarvis" / "repo_manifest.json").read_text())
    assert manifest["url"] == "https://github.com/HKUDS/VideoRAG"
    assert manifest["env_kind"] == "uv-manual"
    assert manifest["venv_python"] == str(VENV_PY)
    assert len(manifest["commit"]) == 40


def test_package_imports_in_videorag_venv():
    out = subprocess.run(
        [str(VENV_PY), "-c",
         "import videorag, torch, imagebind.data; print(torch.__version__)"],
        capture_output=True, text=True, timeout=300, cwd=str(TOOLS))
    assert out.returncode == 0, out.stderr[-800:]
    assert "2.1.2" in out.stdout


def test_pinned_dependency_versions():
    out = subprocess.run(
        [str(VENV_PY), "-c",
         "import numpy, transformers, faster_whisper, ctranslate2;"
         "print(numpy.__version__, transformers.__version__,"
         "faster_whisper.__version__, ctranslate2.__version__)"],
        capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr[-500:]
    numpy_v, tf_v, fw_v, ct2_v = out.stdout.split()
    assert numpy_v == "1.26.4"
    assert tf_v == "4.37.1"
    assert fw_v == "1.0.3"
    assert ct2_v == "4.4.0"


# ── runner ──────────────────────────────────────────────────────────────────
def test_runner_surface_and_constants():
    code = RUNNER.read_text(encoding="utf-8")
    for cmd in ('"status"', '"index"', '"query"', '"list"'):
        assert cmd in code, f"missing subcommand {cmd}"
    assert "multiprocessing.set_start_method(\"spawn\"" in code
    assert "os.chdir(UPSTREAM)" in code, "upstream relative model paths need chdir"
    assert "freellmapi_api_key" in code
    assert "_OLLAMA_URL" in code and "_FREELLM_URL" in code
    assert "sk-" not in code, "hardcoded key material in runner"
    # QueryParam upstream lacks wo_reference as a field; the runner must set it
    assert "param.wo_reference" in code


def test_status_reports_blockers_truthfully():
    out = subprocess.run([str(VENV_PY), str(RUNNER), "status", "--json"],
                         capture_output=True, text=True, timeout=300)
    payload = json.loads(out.stdout)
    assert payload["venv_installed"] is True
    assert payload["upstream"] == str(UPSTREAM)
    assert set(payload["weights"]) == {"minicpm_v", "whisper", "imagebind"}
    assert payload["brain"]["chat_model"] and payload["brain"]["embed_model"]
    assert isinstance(payload["blockers"], list)
    assert payload["ready"] is (not payload["blockers"])
    # this box has no CUDA device — status must say so, not pretend
    assert payload["gpu"]["available"] is False
    assert any("CUDA" in b for b in payload["blockers"])
    assert out.returncode == (0 if payload["ready"] else 2)


def test_index_refuses_without_gpu():
    out = subprocess.run([str(VENV_PY), str(RUNNER), "index", "/tmp/nope.mp4"],
                         capture_output=True, text=True, timeout=300)
    assert out.returncode == 2
    assert "preflight failed" in out.stderr
    assert "CUDA" in out.stderr


# ── action ──────────────────────────────────────────────────────────────────
def test_action_shape_and_discovery():
    from actions.video_rag import TOOL, video_rag
    assert TOOL["name"] == "video_rag"
    assert TOOL["handler"] is video_rag
    props = TOOL["parameters"]["properties"]
    assert {"action", "question", "path", "workdir"} <= set(props)

    from core.action_loader import discover_actions
    reg = discover_actions(actions_dir=BASE / "actions")
    assert "video_rag" in reg.names()


def test_action_speaks_the_blocker():
    from actions.video_rag import video_rag
    reply = video_rag({"action": "status"})
    assert "CUDA" in reply or "video index is ready" in reply.lower()
    reply = video_rag({"action": "ask", "question": "what happens?"})
    assert "video" in reply.lower()


# ── repo-agent skill ────────────────────────────────────────────────────────
def test_adapter_registered_and_allowlisted():
    from core.repo_agent import manifests as M
    from core.repo_agent import skills as S

    reg = M.load_registry()
    assert reg["repos"]["videorag"]["url"] == "https://github.com/HKUDS/VideoRAG"
    skill = reg["skills"]["videorag-chat"]
    assert skill["repo"] == "videorag"
    assert skill["enabled"] is True
    # no GPU here → must never claim READY
    assert skill["status"] == "NEEDS_CONFIGURATION"

    manifest = S.load_skill("videorag-chat")
    assert set(manifest["entry_points"]) == {"ask", "index", "list", "status"}
    assert manifest["input_schema"]["ask"]["question"]["required"] is True
    assert manifest["paid_api"] is False
    assert (TOOLS / ".jarvis" / "jarvis_run.py").exists()

    with pytest.raises(Exception):
        S.run_skill("videorag-chat", "status", {})       # gated while blocked
    with pytest.raises(Exception):
        S.run_skill("videorag-chat", "index", {})        # required param


def test_wrapper_forwards_to_runner():
    params = Path("/tmp") / "videorag_test_params.json"
    params.write_text(json.dumps({"op": "status", "params": {},
                                  "workdir": str(BASE / "memory" / "videorag")}))
    out = subprocess.run(
        [str(VENV_PY), str(TOOLS / ".jarvis" / "jarvis_run.py"),
         "status", "--params-file", str(params)],
        capture_output=True, text=True, timeout=300, cwd=str(TOOLS),
        env={"HOME": str(TOOLS / ".jarvis" / "home"),
             "PATH": "/usr/local/bin:/usr/bin:/bin"})
    assert "[VIDEORAG_PROGRESS] status" in out.stdout
    assert str(UPSTREAM) in out.stdout
    assert out.returncode == 2            # blocked on this box, and says why
    params.unlink(missing_ok=True)


# ── docs ────────────────────────────────────────────────────────────────────
def test_docs_and_fetch_helper_present():
    assert REPO_SKILL.exists() and HERMES_SKILL.exists()
    text = REPO_SKILL.read_text(encoding="utf-8")
    assert "NEEDS_CONFIGURATION" in text
    assert "CUDA" in text
    assert FETCH.exists()
    assert "imagebind_huge.pth" in FETCH.read_text(encoding="utf-8")
