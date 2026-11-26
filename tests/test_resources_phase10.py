"""
tests/test_resources_phase10.py — Phase 10: resource management & safety.

All offline: real lock files, real disk metrics, real ffmpeg atomic writes.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from core.repo_agent.jobs import RepoJobStore                # noqa: E402
from core.video_agent import resources as R                 # noqa: E402


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "jarvis"
    home.mkdir()
    monkeypatch.setenv("JARVIS_HOME", str(home))
    monkeypatch.setenv("JARVIS_REPO_AGENT_NOTIFY_HERMES", "0")
    return {"home": home}


# ── threads ──────────────────────────────────────────────────────────────────

def test_ffmpeg_threads_resource_aware(isolated, monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: 4)
    assert R.ffmpeg_threads() == 2            # never all cores on 2C/4T
    monkeypatch.setattr(os, "cpu_count", lambda: 2)
    assert R.ffmpeg_threads() == 1
    monkeypatch.setenv("JARVIS_REPO_AGENT_VIDEO_FFMPEG_THREADS", "3")
    assert R.ffmpeg_threads() == 3            # explicit override wins


# ── disk gate ────────────────────────────────────────────────────────────────

def test_ensure_disk_passes_with_headroom(isolated, tmp_path):
    free = R.ensure_disk(tmp_path, needed_gb=0.0001)
    assert free >= 0


def test_ensure_disk_refuses_impossible_request(isolated, tmp_path):
    from core.video_agent.pipelines import StageError
    with pytest.raises(StageError) as exc:
        R.ensure_disk(tmp_path, needed_gb=10 ** 9)
    assert "free disk space" in str(exc.value).lower()
    assert "nothing was started" in str(exc.value).lower()


def test_ensure_disk_uses_configured_minimum(isolated, tmp_path, monkeypatch):
    import memory.config_manager as cm
    cfg = isolated["home"] / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "api_keys.json").write_text(json.dumps(
        {"repo_agent": {"video_min_free_gb": 10 ** 9}}))
    monkeypatch.setattr(cm, "CONFIG_FILE", cfg / "api_keys.json")
    from core.video_agent.pipelines import StageError
    with pytest.raises(StageError):
        R.ensure_disk(tmp_path)               # configured minimum applies


# ── render lock ──────────────────────────────────────────────────────────────

def test_render_lock_serializes_two_holders(isolated, tmp_path):
    lock1 = R.RenderLock(tmp_path / "run")
    lock1.acquire()
    lock2 = R.RenderLock(tmp_path / "run", timeout_s=1)
    with pytest.raises(Exception) as exc:
        lock2.acquire()
    assert "one at a time" in str(exc.value).lower()
    lock1.release()
    lock2.acquire()                            # now free
    lock2.release()


def test_render_lock_breaks_stale_lock(isolated, tmp_path):
    """A lock left by a DEAD worker (crash) must never block forever."""
    run = tmp_path / "run"
    run.mkdir()
    (run / "render.lock").write_text("999999999")   # dead pid
    with R.RenderLock(run, timeout_s=2):
        pass                                    # acquired instantly


def test_render_lock_concurrent_threads_serialize(isolated, tmp_path):
    """Two threads contending for the same run dir: exactly one inside at a
    time (checked via the lock file's existence)."""
    run = tmp_path / "run"
    run.mkdir()
    inside = []

    def worker(n):
        with R.RenderLock(run, timeout_s=30):
            assert (run / "render.lock").is_file()
            inside.append(n)
            time.sleep(0.3)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(worker, range(2)))
    assert sorted(inside) == [0, 1]
    assert not (run / "render.lock").exists()   # released


# ── atomic finals ────────────────────────────────────────────────────────────

def test_atomic_final_replaces_only_on_success(isolated, tmp_path):
    final = tmp_path / "render" / "video.mp4"
    part = tmp_path / "render" / "video.mp4.part"
    part.parent.mkdir(parents=True, exist_ok=True)
    assert not final.exists()                   # nothing at the final path
    part.write_bytes(b"encoded data")
    R.atomic_final(part, final)
    assert final.read_bytes() == b"encoded data"
    assert not part.exists()                    # temp consumed


def test_atomic_final_no_partial_on_missing_tmp(isolated, tmp_path):
    final = tmp_path / "render" / "video.mp4"
    R.atomic_final(tmp_path / "never.mp4.part", final)
    assert not final.exists()                   # crash sim: no partial file


def test_smoke_pipeline_writes_atomic_final(tmp_path):
    """The real smoke render's final path must not exist until the encode
    finished — encoded .part, then replace."""
    from core.video_agent.runner import execute_video_job
    out = tmp_path / "atomic-run"
    result = execute_video_job(
        {"job_id": "rt-a"},
        {"spec": {"pipeline": "smoke", "topic": "atomicity"},
         "out_dir": str(out)},
        progress=lambda *_: None, cancelled=lambda: False, log=lambda m: None)
    assert result["status"] == "COMPLETED"
    assert (out / "render" / "smoke.mp4").is_file()
    assert not list(out.rglob("*.part"))        # temp consumed
    # work/ intermediates cleaned (bounded cache)
    assert not (out / "work").exists()


# ── cleanup ──────────────────────────────────────────────────────────────────

def test_cleanup_deletes_intermediates_preserves_outputs(isolated, tmp_path):
    run = tmp_path / "run"
    (run / "work").mkdir(parents=True)
    (run / "work" / "scene_01.mp4").write_bytes(b"x" * 1024)
    (run / "render").mkdir()
    final = run / "render" / "documentary.mp4"
    final.write_bytes(b"v" * 2048)
    (run / "research").mkdir()
    (run / "research" / "sources.json").write_text("{}")
    (run / "narration").mkdir()
    (run / "narration" / "segment_01.mp3").write_bytes(b"a")
    (run / "leftover.part").write_bytes(b"p")
    freed = R.cleanup_run_dir(run)
    assert freed["freed_mb"] >= 0
    assert not (run / "work").exists()
    assert not (run / "leftover.part").exists()
    assert final.is_file()                      # successful output preserved
    assert (run / "research" / "sources.json").is_file()
    assert (run / "narration" / "segment_01.mp3").is_file()  # source asset


def test_cleanup_never_touches_user_originals(isolated, tmp_path):
    """User originals live OUTSIDE the run dir (assets are copies) — cleanup
    only ever walks the run dir."""
    user_file = tmp_path / "user-photo.png"
    user_file.write_bytes(b"original")
    run = tmp_path / "run"
    run.mkdir()
    R.cleanup_run_dir(run)
    assert user_file.read_bytes() == b"original"


# ── retry cap ────────────────────────────────────────────────────────────────

def test_retry_respects_attempt_cap(isolated, tmp_path, monkeypatch):
    from actions.video_creator import video_creator
    store = RepoJobStore()
    created = store.create("video", "documentary", params={"spec": {}})
    store.update(created["job_id"], status="FAILED", finished_at=1,
                 attempts=3, error="x")
    answer = video_creator({"action": "retry", "job_id": created["job_id"]})
    assert "cap" in answer.lower()
    rows = [r for r in store.list(limit=10, kind="video")
            if r["job_id"] != created["job_id"]]
    assert not rows                             # nothing re-queued


# ── static safety audits ─────────────────────────────────────────────────────

def test_no_shell_true_in_video_paths():
    """Unsafe third-party commands: argument arrays only — grep the whole
    video surface for shell=True."""
    for path in (BASE / "core" / "video_agent", BASE / "core" / "repo_agent",
                 BASE / "dashboard" / "video_api.py",
                 BASE / "actions" / "video_creator.py",
                 BASE / "scripts" / "repo_agent_run.py"):
        if path.is_file():
            assert "shell=True" not in path.read_text(), path
        elif path.is_dir():
            for py in path.rglob("*.py"):
                assert "shell=True" not in py.read_text(), py


def test_no_video_files_in_git_tree(isolated):
    """Mission rule: large video files never live inside the repository."""
    strays = [p for p in BASE.rglob("*.mp4")
              if ".venv" not in p.parts and "node_modules" not in p.parts
              and "skills/vibe-trading" not in str(p)]
    assert not strays, strays


def test_config_defaults_expose_phase10_keys():
    from core.repo_agent.jobs import config
    cfg = config()
    assert cfg["video_min_free_gb"] == 2
    assert cfg["video_render_lock_wait_s"] == 300
    assert cfg["video_max_attempts"] == 3
