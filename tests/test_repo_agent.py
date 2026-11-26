"""
tests/test_repo_agent.py — Universal Repository Agent test suite.

MOCKED / OFFLINE by design:
    * every test runs against a tmp JARVIS_HOME + JARVIS_TOOLS_ROOT
    * clones use a local fixture git repository via file:// (allow_local,
      the TEST-ONLY escape hatch that is never exposed to the LLM)
    * skill runs execute a harmless python one-liner, never ViMax itself
    * NO paid API is touched; the real ViMax render path is covered by the
      provider-gating tests instead (a run without keys must be refused)
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from core.repo_agent import manifests as M                     # noqa: E402
from core.repo_agent import skills                             # noqa: E402
from core.repo_agent.jobs import (RepoJobStore, STATES,        # noqa: E402
                                  spawn_worker)
from core.repo_agent.manager import RepositoryManager          # noqa: E402
from core.repo_agent.security import (RepoSecurityError,       # noqa: E402
                                      coerce_param, redact,
                                      risk_summary, safe_dir_name,
                                      scan_text_risk, scrub_env,
                                      validate_repo_url)


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """Point JARVIS_HOME + JARVIS_TOOLS_ROOT at a tmp dir; kill notifications."""
    home = tmp_path / "jarvis"
    tools = tmp_path / "JarvisTools"
    home.mkdir()
    tools.mkdir()
    monkeypatch.setenv("JARVIS_HOME", str(home))
    monkeypatch.setenv("JARVIS_TOOLS_ROOT", str(tools))
    monkeypatch.setenv("JARVIS_REPO_AGENT_NOTIFY_HERMES", "0")
    return {"home": home, "tools": tools}


@pytest.fixture()
def fixture_repo(tmp_path):
    """A tiny local git repo: requirements.txt (no deps) + pyproject."""
    repo = tmp_path / "fixture-repo"
    repo.mkdir()
    (repo / "requirements.txt").write_text("# no dependencies\n")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "fixture-app"\nversion = "0.1.0"\n'
        'requires-python = ">=3.10"\n')
    (repo / "README.md").write_text("# Fixture\nSafe test repository.\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def make_skill(tmp_path: Path, *, op: str = "write", cmd: list[str] | None = None,
               timeout_s: int = 20, outputs: list[str] | None = None) -> str:
    """Register a dummy skill whose 'render' writes a real file (offline)."""
    repo = tmp_path / "dummy-repo"
    repo.mkdir(exist_ok=True)
    (repo / ".jarvis").mkdir(exist_ok=True)
    cmd = cmd or [sys.executable, "-c",
                  "from pathlib import Path; import os, sys; "
                  "out = Path(os.environ['OUT_DIR']) / 'artifact.txt'; "
                  "out.write_text('made it'); print('hello stdout'); "
                  "print('boom stderr', file=sys.stderr)"]
    manifest = {
        "skill_id": "dummy-skill", "name": "Dummy", "repo": "dummy-repo",
        "version": "test", "description": "offline fixture skill",
        "task_types": ["test"], "requires_config": [],
        "entry_points": {op: {"cmd": cmd, "timeout_s": timeout_s}},
        "input_schema": {op: {"name": {"type": "string", "max_chars": 50}}},
        "outputs": {op: outputs if outputs is not None
                    else ["{out_dir}/artifact.txt"]},
        "limits": {"run_timeout_s": timeout_s},
    }
    M.write_json(M.skill_manifest_path("dummy-repo"), manifest)
    M.upsert_repo({"name": "dummy-repo", "url": "local", "commit": "test",
                   "status": "READY"})
    M.upsert_skill({"skill_id": "dummy-skill", "repo": "dummy-repo",
                    "name": "Dummy", "enabled": True, "status": "READY"})
    (repo / ".jarvis" / "home").mkdir(parents=True, exist_ok=True)
    return "dummy-skill"


# ── 1. URL + name validation ─────────────────────────────────────────────────

def test_validate_repo_url_accepts_canonical_forms():
    assert validate_repo_url("https://github.com/HKUDS/ViMax.git") == \
        ("HKUDS", "ViMax")
    assert validate_repo_url("github.com/owner/repo") == ("owner", "repo")
    assert validate_repo_url("http://www.github.com/Owner/repo-name/") == \
        ("Owner", "repo-name")


@pytest.mark.parametrize("bad", [
    "", "https://gitlab.com/owner/repo", "https://github.com/owner/repo?x=1",
    "https://user:pass@github.com/owner/repo",
    "https://github.com/../etc", "https://github.com/owner/repo/extra",
    "https://github.com/owner/repoevil extra",
])
def test_validate_repo_url_rejects_unsafe(bad):
    with pytest.raises(RepoSecurityError):
        validate_repo_url(bad)


def test_safe_dir_name_never_escapes_tools_root():
    assert safe_dir_name("ViMax") == "vimax"
    for bad in ("../../etc", "..", ".", "", "../../Mark-III"):
        with pytest.raises(RepoSecurityError):
            safe_dir_name(bad)
    # odd characters normalise safely — they can never traverse
    assert safe_dir_name("a/b") == "a-b"
    assert safe_dir_name("/abs/path") == "abs-path"
    assert safe_dir_name(".hidden") == "hidden"
    # the join is always one level under the tools root
    assert M.repo_dir("vimax").parent == M.tools_root()


# ── 2. static risk scan + redaction ──────────────────────────────────────────

def test_risk_scan_flags_dangerous_installs():
    findings = scan_text_risk(
        "Quick start:\n"
        "    sudo ./install.sh\n"
        "    curl https://get.example.com | bash\n"
        "    cat ~/.ssh/id_rsa\n")
    level = risk_summary(findings)
    assert level == "high"
    assert len(findings) >= 3
    joined = " ".join(f["finding"].lower() for f in findings)
    assert "root" in joined and "shell" in joined and "ssh" in joined


def test_risk_scan_clean_readme_is_low():
    assert risk_summary(scan_text_risk(
        "# ViMax\nuv sync\npython main_idea2video.py\n")) == "low"


def test_redact_scrubs_secrets():
    dirty = ("key=sk-abcdef1234567890abcdef1234 token=ghp_"
             "abcdefghijklmnopqrst api_key='supersecret99' "
             "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig")
    clean = redact(dirty)
    assert "sk-abcdef" not in clean and "ghp_" not in clean
    assert "supersecret99" not in clean and "eyJhbGciOiJIUzI1NiJ9" not in clean
    assert "[REDACTED]" in clean


def test_scrub_env_hides_jarvis_secrets(monkeypatch):
    monkeypatch.setenv("JARVIS_DASHBOARD_TOKEN", "topsecret")
    monkeypatch.setenv("VIMAX_LLM_API_KEY", "leak-me")
    env = scrub_env({"ADDED": "1"})
    assert "JARVIS_DASHBOARD_TOKEN" not in env
    assert "VIMAX_LLM_API_KEY" not in env
    assert env["ADDED"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_coerce_param_types_bounds_and_control_chars():
    schema = {"n": {"type": "integer", "minimum": 10, "maximum": 600},
              "s": {"type": "string", "max_chars": 5, "required": True},
              "e": {"type": "string", "enum": ["a", "b"]}}
    assert coerce_param(schema, "n", "60") == 60
    with pytest.raises(RepoSecurityError):
        coerce_param(schema, "n", 5)
    with pytest.raises(RepoSecurityError):
        coerce_param(schema, "n", 1000)
    with pytest.raises(RepoSecurityError):
        coerce_param(schema, "s", "way too long")
    with pytest.raises(RepoSecurityError):
        coerce_param(schema, "s", "bad\nnewline")
    with pytest.raises(RepoSecurityError):
        coerce_param(schema, "e", "c")
    with pytest.raises(RepoSecurityError):
        coerce_param(schema, "unknown", "x")


# ── 3. registry / manifests ──────────────────────────────────────────────────

def test_registry_roundtrip_and_skill_state(isolated):
    M.upsert_repo({"name": "Test Repo", "url": "https://github.com/o/test-repo",
                   "commit": "abc", "status": "READY"})
    reg = M.load_registry()
    assert "test-repo" in reg["repos"]
    M.upsert_skill({"skill_id": "test-skill", "repo": "test-repo",
                    "name": "T", "enabled": True, "status": "READY"})
    M.set_skill_state("test-skill", enabled=False, status="NEEDS_CONFIGURATION",
                      detail="missing keys")
    entry = M.load_registry()["skills"]["test-skill"]
    assert entry["enabled"] is False and entry["status"] == "NEEDS_CONFIGURATION"
    M.remove_repo_entry("test-repo")
    assert "test-repo" not in M.load_registry()["repos"]
    # removing the repo also detaches its skill
    assert "test-skill" not in M.load_registry()["skills"]


def test_repo_dir_never_outside_tools_root(isolated):
    assert str(M.repo_dir("ok-name")).startswith(str(isolated["tools"]))
    with pytest.raises(RepoSecurityError):
        M.repo_dir("../../Mark-III")


# ── 4. job store: dedup, states, cancel, recovery ────────────────────────────

def test_jobstore_dedup_repeated_voice_events(isolated):
    store = RepoJobStore()
    first = store.create("install", "vimax",
                         params={"url": "https://github.com/HKUDS/ViMax.git"})
    second = store.create("install", "vimax",
                          params={"url": "https://github.com/HKUDS/ViMax.git"})
    assert first["job_id"] == second["job_id"]
    assert second["deduplicated"] is True
    other = store.create("install", "vimax",
                         params={"url": "https://github.com/other/repo.git"})
    assert other["job_id"] != first["job_id"]


def test_jobstore_finish_maps_partial_to_completed(isolated):
    store = RepoJobStore()
    created = store.create("install", "x", params={})
    store.mark_running(created["job_id"], pid=os.getpid())
    state = store.finish(created["job_id"],
                         {"status": "PARTIAL", "message": "smoke partial"})
    assert state == "COMPLETED"
    assert store.get(created["job_id"])["status"] == "COMPLETED"


def test_jobstore_cancel_and_recovery(isolated):
    store = RepoJobStore()
    created = store.create("run_skill", "dummy-skill",
                           params={"op": "write", "args": {}})
    job_id = created["job_id"]
    # simulate a dead worker from a previous boot
    store.update(job_id, status="RUNNING", pid=999999999)
    recovered = store.recover_stale()
    assert any(r["job_id"] == job_id for r in recovered)
    assert store.get(job_id)["status"] == "FAILED"
    # cancel on a fresh active job
    store.update(job_id, status="QUEUED", pid=None)
    result = store.cancel(job_id, reason="test")
    assert result["ok"] is True
    assert store.get(job_id)["status"] == "CANCELLED"
    again = store.cancel(job_id)
    assert again["ok"] is False          # already terminal


def test_jobstore_notification_dedup(isolated):
    store = RepoJobStore()
    assert store.claim_notification("k1", event="e") is True
    assert store.claim_notification("k1", event="e") is False


def test_spawn_worker_refuses_non_queued(isolated):
    store = RepoJobStore()
    created = store.create("run_skill", "dummy-skill", params={})
    store.update(created["job_id"], status="RUNNING")
    result = spawn_worker(created["job_id"])
    assert result.get("already_running") is True


# ── 5. install flow (offline, file:// fixture) ───────────────────────────────

def test_install_happy_path_with_rollback_manifest(isolated, fixture_repo):
    manager = RepositoryManager()
    result = manager.install(str(fixture_repo), allow_local=True)
    assert result["ok"] is True
    assert result["name"] == "fixture-repo"
    assert result["commit"] not in ("", "unknown")
    dest = M.repo_dir("fixture-repo")
    assert (dest / ".jarvis" / "home").is_dir()
    manifest = M.load_repo_manifest("fixture-repo")
    assert manifest["license"] is None           # honest: fixture has no LICENSE
    assert manifest["status"] in ("READY", "PARTIAL")
    assert manifest["smoke"]["ok"] is True
    reg = M.load_registry()["repos"]["fixture-repo"]
    assert reg["commit"] == manifest["commit"]
    # per-repo fake HOME must be used by the child env
    env = scrub_env({"HOME": str(dest / ".jarvis" / "home")})
    assert env["HOME"].endswith(".jarvis/home")


def test_install_rolls_back_on_dependency_failure(isolated, fixture_repo):
    (fixture_repo / "requirements.txt").write_text(
        "definitely-not-a-real-package-xyz-123==9.9.9\n")
    subprocess.run(["git", "add", "-A"], cwd=fixture_repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "break deps"], cwd=fixture_repo,
                   check=True)
    manager = RepositoryManager()
    result = manager.install(str(fixture_repo), allow_local=True)
    assert result["ok"] is False
    assert result.get("rolled_back") is True
    assert not M.repo_dir("fixture-repo").exists()   # tree removed
    assert "fixture-repo" not in M.load_registry()["repos"]


def test_install_refuses_unknown_manifest(isolated, tmp_path):
    repo = tmp_path / "mystery-repo"
    repo.mkdir()
    (repo / "README.md").write_text("run ./setup.sh to install\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=repo, check=True)
    result = RepositoryManager().install(str(repo), allow_local=True)
    assert result["ok"] is False
    assert "no supported dependency manifest" in result["error"]


def test_install_rejects_github_urls_without_network_in_strict_mode(isolated):
    # production path validates the URL shape first — a bad URL never clones
    with pytest.raises(RepoSecurityError):
        RepositoryManager().install("https://github.com/../evil")


# ── 6. skill runner: allowlist, timeout, cancel, output verification ─────────

def test_run_skill_writes_and_verifies_output(isolated, tmp_path):
    skill_id = make_skill(tmp_path)
    out_dir = tmp_path / "run-out"
    result = skills.run_skill(skill_id, "write", {"name": "job"},
                              run_context={"out_dir": str(out_dir)})
    assert result["status"] == "COMPLETED"
    assert (out_dir / "artifact.txt").read_text() == "made it"
    assert result["outputs"] == [str(out_dir / "artifact.txt")]
    # separated + redacted logs exist
    assert Path(result["out_log"]).exists() and \
        Path(result["err_log"]).exists()
    assert "hello stdout" in Path(result["out_log"]).read_text()
    assert "boom stderr" in Path(result["err_log"]).read_text()


def test_run_skill_failure_when_no_output(isolated, tmp_path):
    skill_id = make_skill(tmp_path, outputs=["{out_dir}/never-produced.bin"])
    result = skills.run_skill(skill_id, "write", {"name": "x"},
                              run_context={"out_dir": tmp_path / "o2"})
    assert result["status"] == "FAILED"
    assert "no declared output" in result.get("error", "")


def test_run_skill_timeout_kills_process(isolated, tmp_path):
    sleeper = [sys.executable, "-c", "import time; print('start', flush=True);"
               " time.sleep(60)"]
    skill_id = make_skill(tmp_path, cmd=sleeper, timeout_s=2,
                          outputs=["{out_dir}/artifact.txt"])
    started = time.time()
    result = skills.run_skill(skill_id, "write", {"name": "x"},
                              run_context={"out_dir": tmp_path / "o3"})
    elapsed = time.time() - started
    assert result["status"] == "FAILED"
    assert elapsed < 25                       # killed, not waited out
    assert "timed out" in result.get("message", "") or \
        "timed out" in result.get("error", "")


def test_run_skill_cancel_mid_run(isolated, tmp_path):
    sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
    skill_id = make_skill(tmp_path, cmd=sleeper, timeout_s=60,
                          outputs=[])
    stop = {"flag": False}

    def cancel():
        return stop["flag"]

    import threading
    timer = threading.Timer(2.0, lambda: stop.update(flag=True))
    timer.start()
    try:
        result = skills.run_skill(skill_id, "write", {"name": "x"},
                              cancel=cancel,
                              run_context={"out_dir": tmp_path / "o4"})
    finally:
        timer.cancel()
    assert result["status"] == "CANCELLED"


def test_run_skill_disabled_or_unready_refused(isolated, tmp_path):
    skill_id = make_skill(tmp_path)
    M.set_skill_state(skill_id, enabled=False)
    with pytest.raises(RepoSecurityError):
        skills.run_skill(skill_id, "write", {"name": "x"})
    M.set_skill_state(skill_id, enabled=True, status="NEEDS_CONFIGURATION")
    with pytest.raises(RepoSecurityError, match="NEEDS_CONFIGURATION"):
        skills.run_skill(skill_id, "write", {"name": "x"})


# ── 7. full detached job roundtrip (worker process) ──────────────────────────

def test_worker_executes_skill_job_end_to_end(isolated, tmp_path):
    skill_id = make_skill(tmp_path)
    store = RepoJobStore()
    created = store.create("run_skill", skill_id,
                           params={"op": "write", "args": {"name": "worker"},
                                   "out_dir": str(tmp_path / "w-out")})
    job_id = created["job_id"]
    spawned = spawn_worker(job_id)
    assert spawned.get("ok") is True
    deadline = time.time() + 60
    while time.time() < deadline:
        job = store.get(job_id)
        if job["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            break
        time.sleep(0.5)
    job = store.get(job_id)
    assert job["status"] == "COMPLETED", job
    assert (tmp_path / "w-out" / "artifact.txt").exists()
    result = json.loads((RepoJobStore().path.parent / "results" /
                         f"{job_id}.json").read_text())
    assert result["state"] == "COMPLETED"
    # restart recovery: no lingering active jobs
    assert store.recover_stale() == []


def test_worker_install_job_via_file_protocol(isolated, fixture_repo):
    store = RepoJobStore()
    created = store.create("install", "fixture-repo",
                           params={"url": str(fixture_repo),
                                   "allow_local": True})
    job_id = created["job_id"]
    assert spawn_worker(job_id).get("ok") is True
    deadline = time.time() + 120
    while time.time() < deadline:
        if store.get(job_id)["status"] in ("COMPLETED", "FAILED"):
            break
        time.sleep(0.5)
    assert store.get(job_id)["status"] == "COMPLETED"
    assert M.load_repo_manifest("fixture-repo")


# ── 8. ViMax adapter honesty gates ───────────────────────────────────────────

def test_vimax_video_verification(isolated):
    from core.repo_agent.vimax import verify_video
    assert verify_video("/nonexistent/nope.mp4")["ok"] is False
    empty = isolated["home"] / "empty.mp4"
    empty.write_bytes(b"")
    assert verify_video(empty)["ok"] is False
    fake = isolated["home"] / "fake.mp4"
    fake.write_bytes(b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2"
                     + b"\x00" * 64)
    ok = verify_video(fake)
    assert ok["ok"] is True and ok["size_bytes"] > 0
    notvideo = isolated["home"] / "text.mp4"
    notvideo.write_bytes(b"just text, no mp4 signature" * 4)
    assert verify_video(notvideo)["ok"] is False


def test_vimax_run_refused_without_providers(isolated, monkeypatch):
    """Without configured keys the adapter must refuse — never fake-ready."""
    monkeypatch.setenv("JARVIS_HOME", str(isolated["home"]))
    # empty api_keys.json in the isolated home → no openrouter keys
    cfg_dir = isolated["home"] / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "api_keys.json").write_text("{}")
    # CONFIG_FILE is bound at import time (module snapshot of JARVIS_HOME) —
    # pin it to the isolated file so this test is hermetic no matter which
    # suite imported config_manager first (cross-file pollution, 2026-10-25)
    import memory.config_manager as cm
    monkeypatch.setattr(cm, "CONFIG_FILE", cfg_dir / "api_keys.json")
    import importlib
    from core.repo_agent import vimax as vimax_mod
    importlib.reload(vimax_mod)
    status = vimax_mod.provider_status()
    assert status["ready"] is False
    assert set(status["missing"]) == {"llm", "image", "video"}
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        with pytest.raises(RuntimeError, match="provider configuration"):
            vimax_mod._write_run_config("idea2video", Path(td))


def test_vimax_generated_config_class_names_and_token_cap(isolated):
    """2026-10-24 live findings, pinned: (1) the generated YAML must use
    ViMax's exact class names (capital R — ImageGeneratorOpenRouterAPI; the
    lowercase variant 404'd at config load), and (2) chat_model must carry an
    explicit max_tokens cap, or the model's 65535 maximum 402s on OpenRouter
    even with affordable balance."""
    cfg_dir = isolated["home"] / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "api_keys.json").write_text(
        json.dumps({"openrouter_keys": ["sk-or-test"]}))
    import importlib
    import memory.config_manager as cm
    from core.repo_agent import vimax as vimax_mod
    importlib.reload(vimax_mod)
    # patch the loader, not the env: config_manager caches across tests
    import contextlib
    orig_load = cm.load_api_keys
    cm.load_api_keys = lambda: {"openrouter_keys": ["sk-or-test"]}
    try:
        assert vimax_mod.provider_status()["ready"] is True
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            path = vimax_mod._write_run_config("idea2video", Path(td))
            yaml_text = path.read_text(encoding="utf-8")
    finally:
        cm.load_api_keys = orig_load
    assert "ImageGeneratorOpenRouterAPI" in yaml_text
    assert "VideoGeneratorOpenRouterAPI" in yaml_text
    assert "OpenrouterAPI" not in yaml_text          # the exact regression
    assert "max_tokens: 4096" in yaml_text           # the 402 regression
    # default (openrouter) rungs carry an explicit model
    assert "    model: " in yaml_text

    # the google rung must NOT receive a model kwarg: the class hardcodes it
    # and passing one is a TypeError at pipeline init (2026-10-24)
    cm.load_api_keys = lambda: {
        "openrouter_keys": ["sk-or-test"],
        "gemini_api_key": "g-test",
        "vimax": {
            "image": {"provider": "google",
                      "api_key_ref": "gemini_api_key"},
            "video": {"provider": "google",
                      "api_key_ref": "gemini_api_key"},
        },
    }
    try:
        with tempfile.TemporaryDirectory() as td:
            path = vimax_mod._write_run_config("idea2video", Path(td))
            gtext = path.read_text(encoding="utf-8")
    finally:
        cm.load_api_keys = orig_load
    assert "ImageGeneratorNanobananaGoogleAPI" in gtext
    assert "VideoGeneratorVeoGoogleAPI" in gtext
    # api_key is resolved through api_key_ref (image + video; the chat model
    # intentionally stays on OpenRouter), and no model kwarg is emitted
    assert gtext.count("api_key: g-test") == 2
    assert "api_key: sk-or-test" in gtext      # chat_model stays OpenRouter
    image_block = gtext.split("image_generator:")[1].split("video_generator:")[0]
    assert "model:" not in image_block


def test_vimax_skill_registered_after_install(isolated, tmp_path):
    """The manager registers the vimax adapter when the repo name matches."""
    repo = tmp_path / "vimax"
    repo.mkdir()
    (repo / "requirements.txt").write_text("# none\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=repo, check=True)
    result = RepositoryManager().install(str(repo), allow_local=True)
    assert result["ok"] is True
    assert result["skill"] and result["skill"]["skill_id"] == "vimax-video"
    entry = M.load_registry()["skills"]["vimax-video"]
    # isolated home has no provider keys → honest NEEDS_CONFIGURATION
    assert entry["status"] == "NEEDS_CONFIGURATION"


# ── 9. action-layer guards ───────────────────────────────────────────────────

def test_video_creator_refuses_protected_output_dirs(isolated):
    from actions.video_creator import _out_dir
    with pytest.raises(ValueError):
        _out_dir(str(BASE / "memory"))
    with pytest.raises(ValueError):
        _out_dir(str(Path.home() / "JarvisTools" / "vimax"))
    ok = _out_dir(str(isolated["home"] / "Videos" / "mine"))
    assert ok.is_dir()


def test_repo_agent_action_validates_urls(isolated):
    from actions.repo_agent import repo_agent
    answer = repo_agent({"action": "install", "url":
                         "https://evil.example.com/owner/repo"})
    assert "Refused" in answer or "only canonical GitHub" in answer
    assert "queued" not in answer.lower()


def test_video_creator_gates_paid_runs(isolated, tmp_path, monkeypatch):
    """Not-ready skill and unconfirmed runs must both refuse cleanly.
    Phase 2: cinematic requests are explicit (mode=ai_cinematic) — an
    unqualified create is a DOCUMENTARY request now and is refused honestly
    (see tests/test_video_agent.py)."""
    import importlib
    import actions.video_creator as vc
    importlib.reload(vc)
    # no skill in the isolated registry → install guidance, no job
    answer = vc.video_creator({"action": "create", "mode": "ai_cinematic",
                               "topic": "a futuristic city at night"})
    assert "not installed" in answer.lower()
    # dummy skill named vimax-video in READY state but confirm missing
    repo = tmp_path / "vimax"
    repo.mkdir(exist_ok=True)
    (repo / ".jarvis" / "home").mkdir(parents=True, exist_ok=True)
    M.upsert_repo({"name": "vimax", "url": "local", "commit": "t",
                   "status": "READY"})
    M.upsert_skill({"skill_id": "vimax-video", "repo": "vimax",
                    "name": "ViMax", "enabled": True, "status": "READY"})
    answer = vc.video_creator({"action": "create", "mode": "ai_cinematic",
                               "topic": "a futuristic city at night",
                               "confirm": False})
    # Phase 6: the mandatory method question lists VIMAX with its PAID cost
    # and nothing is queued without an approved, single-use token — a
    # stronger gate than the legacy confirm=true parameter
    low = answer.lower()
    assert "which method" in low and "paid" in low and "token" in low
    jobs_before = len(RepoJobStore().list(limit=100))
    assert jobs_before == 0


# ── 10. states contract ──────────────────────────────────────────────────────

def test_job_states_match_specification():
    for state in ("QUEUED", "INSTALLING", "NEEDS_CONFIGURATION", "READY",
                  "RUNNING", "WAITING_FOR_APPROVAL", "COMPLETED", "FAILED",
                  "CANCELLED"):
        assert state in STATES


# ── 11. RIO shell + dashboard API wiring ─────────────────────────────────────

def test_rio_serves_tools_page_and_repo_api_requires_auth(isolated):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import dashboard.repo_api as repo_api
    import dashboard.rio_api as rio_api

    def _auth(request: object) -> bool:
        return request.headers.get("Authorization") == "Bearer t"  # type: ignore[attr-defined]

    app = FastAPI()
    rio_api.register(app, _auth)
    repo_api.register(app, _auth)
    client = TestClient(app)

    # the Tools page is a real shell route and tools.js is whitelisted
    assert "/tools" in rio_api.ROUTES
    assert "tools.js" in rio_api._ASSETS
    page = client.get("/tools")
    assert page.status_code == 200
    assert "tools.js" in page.text
    asset = client.get("/rio/assets/tools.js")
    assert asset.status_code == 200
    assert "RIOTools" in asset.text

    # the repo API is auth-gated like every other dashboard surface
    assert client.get("/api/repo/overview").status_code == 401
    assert client.post("/api/repo/install", json={"url": "x"}).status_code \
        == 401
    ok = client.get("/api/repo/overview",
                    headers={"Authorization": "Bearer t"})
    assert ok.status_code == 200 and ok.json()["ok"] is True

def test_repo_api_rejects_bad_urls(isolated):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import dashboard.repo_api as repo_api

    app = FastAPI()
    repo_api.register(app, lambda request: True)
    client = TestClient(app)
    auth = {"Authorization": "Bearer t"}
    r = client.post("/api/repo/install", json={"url": "not-a-github-url"},
                    headers=auth)
    assert r.status_code == 400
    r = client.post("/api/repo/run", json={"skill_id": "vimax-video",
                                           "op": "idea2video", "args": {}},
                    headers=auth)
    assert r.status_code == 400 and "confirmation" in r.json()["error"]


# ── 12. routing: applications never dead-end in skill_manager ────────────────

def test_skill_manager_classifies_application_repo_as_unknown(tmp_path):
    """A repo with no SKILL.md and no TOOL= modules is not a skill package."""
    from actions.skill_manager import _classify
    repo = tmp_path / "agency-agents"
    repo.mkdir()
    (repo / "README.md").write_text("# Agents\n")
    (repo / "cli.py").write_text("print('hello')\n")   # no module-level TOOL
    assert _classify(repo) == ("unknown", "")


def test_skill_manager_hands_application_to_repo_agent(monkeypatch):
    """The 2026-10-24 bug: 'install agency-agents' dead-ended with
    'nothing JARVIS can install'. An application repo must instead be queued
    as a repo_agent install and the user told which job carries it."""
    import core.repo_agent.jobs as jobs
    from actions.skill_manager import _handoff_to_repo_agent
    seen = {}

    def fake_start_job(kind, ref, *, source="cli", params=None):
        seen.update(kind=kind, ref=ref, source=source, params=params)
        return {"ok": True, "deduplicated": False, "job_id": "rt-TEST123"}

    monkeypatch.setattr(jobs, "start_job", fake_start_job)
    answer = _handoff_to_repo_agent("msitarzewski", "agency-agents")
    assert seen["kind"] == "install"
    assert seen["ref"] == "agency-agents"
    assert seen["source"] == "skill_manager"
    assert seen["params"]["url"] == \
        "https://github.com/msitarzewski/agency-agents"
    assert "rt-TEST123" in answer
    assert "application" in answer.lower()
    assert "nothing was added" not in answer


def test_skill_manager_handoff_degrades_when_jobs_unavailable(monkeypatch):
    """A broken job layer must never raise out of the skill path."""
    import core.repo_agent.jobs as jobs
    from actions.skill_manager import _handoff_to_repo_agent

    def boom(*a, **k):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(jobs, "start_job", boom)
    answer = _handoff_to_repo_agent("owner", "repo")
    assert "repository agent" in answer
    assert "https://github.com/owner/repo" in answer


# ── 13. transient 1011 / deadline classification ─────────────────────────────

class _FakeClose(Exception):
    """Stand-in for google-genai APIError with a websocket close code."""
    def __init__(self, code, text=""):
        self.code = code
        super().__init__(text or f"{code}")


def test_transient_server_error_unwraps_taskgroup():
    """A TaskGroup wraps the 1011 close, so the run loop only sees a group
    whose own str() says nothing. The classifier must look inside."""
    from main import _is_transient_server_error
    group = BaseExceptionGroup(
        "unhandled errors in a TaskGroup", [_FakeClose(1011)])
    assert "1011" not in str(group)          # the regression condition
    assert _is_transient_server_error(group) is True


def test_transient_server_error_matches_deadline_text():
    """The 2026-10-24 live shape surfaced 'Deadline expired' rather than a
    usable code attribute."""
    from main import _is_transient_server_error
    err = Exception("Deadline expired before operation could complete")
    assert _is_transient_server_error(err) is True
    nested = BaseExceptionGroup("unhandled errors in a TaskGroup", [err])
    assert _is_transient_server_error(nested) is True


def test_transient_server_error_rejects_other_errors():
    from main import _is_transient_server_error
    assert _is_transient_server_error(Exception("some ordinary bug")) is False
    assert _is_transient_server_error(_FakeClose(1000)) is False
    assert _is_transient_server_error(
        BaseExceptionGroup("g", [Exception("unrelated")])) is False


def test_cancellation_detected_inside_taskgroup():
    """The run loop catches BaseException; it must still recognise a cancelled
    task so a cancelled session stops instead of hot-looping reconnects."""
    import asyncio
    from main import _is_cancellation
    assert _is_cancellation(asyncio.CancelledError()) is True
    assert _is_cancellation(
        BaseExceptionGroup("g", [asyncio.CancelledError()])) is True
    assert _is_cancellation(Exception("ordinary")) is False
    assert _is_cancellation(_FakeClose(1011)) is False
