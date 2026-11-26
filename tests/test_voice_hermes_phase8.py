"""
tests/test_voice_hermes_phase8.py — Phase 8: voice surface + Hermes delivery.

MOCKED / OFFLINE by design:
    * notify.send is monkeypatched (no real Hermes/Telegram traffic)
    * jobs are real store rows; the smoke pipeline renders really when used
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from core.repo_agent.jobs import RepoJobStore, start_job     # noqa: E402


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


def _queue_smoke(tmp_path, topic="tea"):
    result = start_job("video", "smoke", source="test", params={
        "spec": {"pipeline": "smoke", "mode": "DOCUMENTARY", "topic": topic},
        "out_dir": str(tmp_path / f"run-{abs(hash(topic)) % 9999}")})
    assert result.get("ok")
    return result["job_id"]


# ── cancel latest ────────────────────────────────────────────────────────────

def test_cancel_latest_cancels_most_recent_active(isolated, tmp_path):
    from actions.video_creator import video_creator
    jid = _queue_smoke(tmp_path, "cancel-me")
    answer = video_creator({"action": "cancel"})      # no id → latest active
    assert jid in answer and "Cancelled" in answer
    assert RepoJobStore().get(jid)["status"] == "CANCELLED"


def test_cancel_latest_honest_when_idle(isolated):
    from actions.video_creator import video_creator
    assert "nothing to cancel" in video_creator({"action": "cancel"}).lower()


# ── retry ────────────────────────────────────────────────────────────────────

def test_retry_requeues_failed_job_with_same_params(isolated, tmp_path):
    from actions.video_creator import video_creator
    store = RepoJobStore()
    created = store.create("video", "documentary", params={
        "spec": {"pipeline": "documentary", "topic": "procrastination",
                 "duration_s": 30},
        "out_dir": str(tmp_path / "d")})
    store.update(created["job_id"], status="FAILED", finished_at=1,
                 error="provider gate")
    answer = video_creator({"action": "retry"})
    assert "Retried as" in answer
    rows = store.list(limit=10, kind="video")
    new = [r for r in rows if r["job_id"] != created["job_id"]]
    assert new and json.loads(new[0]["params"])["spec"]["topic"] == \
        "procrastination"
    assert new[0]["source"] == "jarvis-retry"


def test_retry_targets_specific_job(isolated, tmp_path):
    from actions.video_creator import video_creator
    store = RepoJobStore()
    created = store.create("video", "shorts", params={
        "spec": {"pipeline": "shorts"}, "out_dir": str(tmp_path / "r")})
    store.update(created["job_id"], status="FAILED", finished_at=1,
                 error="x")
    answer = video_creator({"action": "retry", "job_id": created["job_id"]})
    assert f"was {created['job_id']}" in answer


def test_retry_refuses_job_without_output_dir(isolated, tmp_path):
    """SAFETY (Phase 10): a job row without out_dir must never be re-queued —
    the runner would otherwise render into the worker's CWD (the repo root).
    Found live: a retry test fixture queued spec={} and BASE/render appeared."""
    from actions.video_creator import video_creator
    store = RepoJobStore()
    created = store.create("video", "shorts", params={"spec": {}})
    store.update(created["job_id"], status="FAILED", finished_at=1,
                 error="x")
    answer = video_creator({"action": "retry", "job_id": created["job_id"]})
    assert "no output directory" in answer.lower()
    assert not (BASE / "render").exists()


def test_retry_honest_when_nothing_failed(isolated):
    from actions.video_creator import video_creator
    assert "No failed video jobs" in video_creator({"action": "retry"})


def test_retry_refuses_completed_job(isolated, tmp_path):
    from actions.video_creator import video_creator
    store = RepoJobStore()
    created = store.create("video", "smoke", params={"spec": {}})
    store.update(created["job_id"], status="COMPLETED", finished_at=1)
    answer = video_creator({"action": "retry", "job_id": created["job_id"]})
    assert "retry is for" in answer.lower()


# ── duplicate protection (speech events / Telegram retries) ──────────────────

def test_repeated_identical_requests_create_one_job(isolated, tmp_path):
    """The same utterance delivered twice (speech overlap, Telegram retry)
    must queue ONE job — approval dedup before the job, signature dedup at
    the store."""
    import re
    from actions.video_creator import video_creator
    params = {"action": "create", "topic": "what is bitcoin",
              "request": "a 45-second Short about Bitcoin",
              "save_dir": str(tmp_path / "dd")}
    first = video_creator(params)               # → the method question
    second = video_creator(dict(params))        # → SAME pending token
    token_a = re.search(r"token ([0-9a-f]{16})", first)
    token_b = re.search(r"token ([0-9a-f]{16})", second)
    assert token_a and token_b and token_a.group(1) == token_b.group(1)
    started = video_creator({**params, "approve_token": token_a.group(1),
                             "method": "CANVAS"})
    assert "queued" in started.lower()
    third = video_creator(dict(params))         # → active job dedup
    assert "nothing duplicated" in third.lower()
    ids = {r["job_id"] for r in RepoJobStore().list(limit=10, kind="video")}
    assert len(ids) == 1


# ── Hermes delivery ──────────────────────────────────────────────────────────

def test_notify_video_completed_attaches_small_file(isolated, tmp_path,
                                                    monkeypatch):
    import core.repo_agent.notify as notify
    sent: list[str] = []
    monkeypatch.setattr(notify, "send",
                        lambda text, target="": sent.append(text)
                        or {"ok": True, "detail": "mock"})
    store = RepoJobStore()
    created = store.create("video", "shorts", params={"spec": {}})
    video = tmp_path / "short.mp4"
    video.write_bytes(b"v" * 1024)                     # 1 KB — attachable
    job = store.get(created["job_id"])
    store.update(created["job_id"], status="COMPLETED", finished_at=1)
    job = store.get(created["job_id"])
    outcome = notify.notify_job_result(job, {"outputs": [str(video)]})
    assert outcome["ok"] is True
    assert any("MEDIA:" in s for s in sent), sent
    assert "JARVIS video" in sent[0] and "short" in sent[0]


def test_notify_video_large_file_sends_path_only(isolated, tmp_path,
                                                 monkeypatch):
    import core.repo_agent.notify as notify
    sent: list[str] = []
    monkeypatch.setattr(notify, "send",
                        lambda text, target="": sent.append(text)
                        or {"ok": True, "detail": "mock"})
    store = RepoJobStore()
    created = store.create("video", "documentary", params={"spec": {}})
    video = tmp_path / "documentary.mp4"
    video.write_bytes(b"v" * (50 * 1024 * 1024))       # 50 MB — too large
    store.update(created["job_id"], status="COMPLETED", finished_at=1)
    job = store.get(created["job_id"])
    notify.notify_job_result(job, {"outputs": [str(video)]})
    assert sent and not any("MEDIA:" in s for s in sent)
    assert "too large" in sent[0]


def test_notify_video_failed_includes_error(isolated, tmp_path, monkeypatch):
    import core.repo_agent.notify as notify
    sent: list[str] = []
    monkeypatch.setattr(notify, "send",
                        lambda text, target="": sent.append(text)
                        or {"ok": True, "detail": "mock"})
    store = RepoJobStore()
    created = store.create("video", "documentary", params={"spec": {}})
    store.update(created["job_id"], status="FAILED", finished_at=1,
                 error="402 — would exceed your available credits")
    job = store.get(created["job_id"])
    notify.notify_job_result(job, {"error": "402 — would exceed your "
                                            "available credits"})
    assert sent and "402" in sent[0]


def test_notify_dedups_repeat_calls(isolated, tmp_path, monkeypatch):
    import core.repo_agent.notify as notify
    calls: list[str] = []
    monkeypatch.setattr(notify, "send",
                        lambda text, target="": calls.append(text)
                        or {"ok": True, "detail": "mock"})
    store = RepoJobStore()
    created = store.create("video", "smoke", params={"spec": {}})
    store.update(created["job_id"], status="COMPLETED", finished_at=1)
    job = store.get(created["job_id"])
    first = notify.notify_job_result(job, {"outputs": []})
    second = notify.notify_job_result(job, {"outputs": []})
    assert first.get("ok") is True
    assert "already notified" in str(second.get("detail"))


# ── tool surface ─────────────────────────────────────────────────────────────

def test_tool_declares_phase8_phrasings():
    from actions.video_creator import TOOL
    desc = TOOL["description"].lower()
    for phrase in ("cancel my latest", "retry", "show my video progress",
                   "make shorts from my latest documentary"):
        assert phrase in desc
    assert "retry" in TOOL["parameters"]["properties"]["action"][
        "description"]
