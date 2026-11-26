"""
tests/test_video_api_phase9.py — Phase 9: RIO Video Studio API + wiring.

MOCKED / OFFLINE by design: FastAPI TestClient against the real register();
library/file/thumb use real files under a tmp output root; no network, no
paid calls.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.repo_agent.jobs import RepoJobStore                # noqa: E402
from core.video_agent import pipelines                       # noqa: E402


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "jarvis"
    tools = tmp_path / "JarvisTools"
    home.mkdir()
    tools.mkdir()
    monkeypatch.setenv("JARVIS_HOME", str(home))
    monkeypatch.setenv("JARVIS_TOOLS_ROOT", str(tools))
    monkeypatch.setenv("JARVIS_REPO_AGENT_NOTIFY_HERMES", "0")
    # hermetic HOME: the API's default output root is ~/Videos/JARVIS —
    # without this the library would scan the user's real videos
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home" / "Videos" / "JARVIS").mkdir(parents=True)
    return {"home": home, "tools": tools}


@pytest.fixture()
def client(isolated, tmp_path):
    from dashboard.video_api import register
    app = FastAPI()
    register(app, lambda request: True)
    return TestClient(app), tmp_path


@pytest.fixture()
def client_denied(isolated, tmp_path):
    from dashboard.video_api import register
    app = FastAPI()
    register(app, lambda request: False)
    return TestClient(app)


def _auth_headers():
    return {"Authorization": "Bearer t"}


def _queue_job(tmp_path, pipeline="smoke", topic="tea"):
    from core.repo_agent.jobs import start_job
    out = tmp_path / f"out-{abs(hash(topic)) % 9999}"
    result = start_job("video", pipeline, source="test", params={
        "spec": {"pipeline": pipeline, "mode": "DOCUMENTARY",
                 "topic": topic},
        "out_dir": str(out)})
    return result["job_id"], out


# ── auth ─────────────────────────────────────────────────────────────────────

def test_video_api_requires_auth(client_denied):
    tc = client_denied
    for path, method in (("/api/video/overview", "get"),
                         ("/api/video/jobs", "get"),
                         ("/api/video/library", "get"),
                         ("/api/video/capabilities", "get"),
                         ("/api/video/file?path=/x.mp4", "get"),
                         ("/api/video/create", "post"),
                         ("/api/video/cancel", "post"),
                         ("/api/video/retry", "post"),
                         ("/api/video/split", "post")):
        r = getattr(tc, method)(path)
        assert r.status_code == 401, (path, r.status_code)


# ── overview + capabilities ──────────────────────────────────────────────────

def test_overview_shape_and_pipelines(client):
    tc, _ = client
    r = tc.get("/api/video/overview", headers=_auth_headers())
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and "jobs" in body
    assert body["stages"] == list(pipelines.VIDEO_STAGES)
    assert {"documentary", "shorts", "smoke"} <= set(body["pipelines"])
    ops = {row["op"] for row in body["capabilities"]}
    assert {"TEXT_TO_VIDEO", "IMAGE_TO_VIDEO", "SCENE_TO_VIDEO"} <= ops


def test_capabilities_mentions_honest_status(client):
    tc, _ = client
    r = tc.get("/api/video/capabilities", headers=_auth_headers())
    text = r.json()["providers"]
    assert "BLOCKED" in text            # measured provider truth
    assert "never passed off" in text or "never" in text


# ── create / cancel / retry ──────────────────────────────────────────────────

def test_create_queues_documentary_sample(client, tmp_path):
    """Phase 6: the API create returns the method question first; the
    approved second call queues the sample-first documentary."""
    import re
    tc, tmp = client
    out = tmp / "api-out"
    body = {"action": "create", "topic": "why we dream",
            "request": "a 10-minute documentary", "save_dir": str(out)}
    r = tc.post("/api/video/create", headers=_auth_headers(), json=body)
    assert r.status_code == 200
    first = r.json()
    assert first["ok"] is False and "Which method" in first["message"]
    token = re.search(r"token ([0-9a-f]{16})", first["message"])
    assert token, first["message"]
    r2 = tc.post("/api/video/create", headers=_auth_headers(), json={
        **body, "approve_token": token.group(1), "method": "CANVAS"})
    assert r2.status_code == 200
    result = r2.json()
    assert result["ok"] is True and "sample" in result["message"].lower()
    rows = RepoJobStore().list(limit=5, kind="video")
    assert rows and rows[0]["ref"] == "documentary"


def test_create_invalid_url_style_request_is_refused(client, tmp_path):
    tc, tmp = client
    r = tc.post("/api/video/create", headers=_auth_headers(), json={
        "action": "create", "topic": "", "save_dir": str(tmp)})
    body = r.json()
    assert body["ok"] is False and "topic" in body["message"].lower()


def test_cancel_endpoint_without_id_targets_latest(client, tmp_path):
    tc, tmp = client
    jid, _out = _queue_job(tmp, topic="cancel-target")
    r = tc.post("/api/video/cancel", headers=_auth_headers(), json={})
    assert r.status_code == 200
    assert jid in r.json()["message"]
    # the detached worker may be mid-startup: it honours cancel on its next
    # poll — wait for the terminal state instead of racing it
    import time
    store = RepoJobStore()
    deadline = time.time() + 15
    while time.time() < deadline:
        if store.get(jid)["status"] == "CANCELLED":
            break
        time.sleep(0.3)
    assert store.get(jid)["status"] == "CANCELLED"


def test_retry_endpoint_requeues(client, tmp_path):
    tc, tmp = client
    store = RepoJobStore()
    created = store.create("video", "shorts", params={
        "spec": {"pipeline": "shorts"}, "out_dir": str(tmp_path / "r")})
    store.update(created["job_id"], status="FAILED", finished_at=1,
                 error="x")
    r = tc.post("/api/video/retry", headers=_auth_headers(), json={})
    assert "Retried as" in r.json()["message"]


# ── library / file / thumb (real files, whitelisted) ─────────────────────────

def _seed_video(out_root) -> Path:
    out = Path(out_root)
    (out / "render").mkdir(parents=True, exist_ok=True)
    # REAL 1s mp4 via ffmpeg — ffprobe + thumbnail need a real stream
    target = out / "render" / "clip.mp4"
    import subprocess
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi",
                    "-i", "color=c=0x223344:s=320x180:d=1",
                    "-c:v", "libx264", "-preset", "ultrafast",
                    str(target)], capture_output=True, timeout=60, check=True)
    return target


def test_library_lists_real_video_with_facts(client, tmp_path):
    tc, tmp = client
    _jid, out = _queue_job(tmp, topic="lib-job")
    target = _seed_video(out)
    r = tc.get("/api/video/library", headers=_auth_headers())
    items = r.json()["items"]
    match = [i for i in items if i["path"] == str(target)]
    assert match, items
    item = match[0]
    assert item["width"] == 320 and item["duration_s"] >= 0.8
    assert item["title"]                       # derived from filename


def test_file_endpoint_serves_whitelisted_video(client, tmp_path):
    tc, tmp = client
    _jid, out = _queue_job(tmp, topic="file-job")
    target = _seed_video(out)
    r = tc.get("/api/video/file", headers=_auth_headers(),
               params={"path": str(target)})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("video/mp4")


def test_file_endpoint_blocks_traversal(client, tmp_path):
    tc, tmp = client
    _jid, _out = _queue_job(tmp, topic="trav")
    outside = tmp / "secret.mp4"
    outside.write_bytes(b"x")
    r = tc.get("/api/video/file", headers=_auth_headers(),
               params={"path": str(outside)})
    assert r.status_code == 404                # outside known output roots
    r = tc.get("/api/video/file", headers=_auth_headers(),
               params={"path": "/etc/passwd"})
    assert r.status_code == 404                # not a media file


def test_thumb_generates_cached_jpeg(client, tmp_path):
    tc, tmp = client
    _jid, out = _queue_job(tmp, topic="thumb-job")
    target = _seed_video(out)
    r1 = tc.get("/api/video/thumb", headers=_auth_headers(),
                params={"path": str(target)})
    assert r1.status_code == 200
    assert r1.headers["content-type"] == "image/jpeg"
    thumbs = list(Path.home().joinpath(".jarvis_video_thumbs").glob("*.jpg")) \
        if Path.home().joinpath(".jarvis_video_thumbs").is_dir() else []
    assert thumbs
    r2 = tc.get("/api/video/thumb", headers=_auth_headers(),
                params={"path": str(target)})
    assert r2.status_code == 200               # cached second read


# ── frontend wiring ──────────────────────────────────────────────────────────

def test_rio_shell_wires_video_studio():
    html = (BASE / "dashboard" / "static" / "rio" / "index.html") \
        .read_text(encoding="utf-8")
    assert 'src="/rio/assets/video.js"' in html
    app_js = (BASE / "dashboard" / "static" / "rio" / "app.js") \
        .read_text(encoding="utf-8")
    assert '{ path: "/video"' in app_js
    assert 'video: "RIOVideo"' in app_js
    js = (BASE / "dashboard" / "static" / "rio" / "video.js") \
        .read_text(encoding="utf-8")
    for section in ("New Video", "Active Jobs", "Video Library",
                    "Provider Settings"):
        assert section in js
    # design system: house classes, no invented card/btn classes
    assert "tl-card" not in js and "tl-btn" not in js


def test_server_mounts_video_api():
    src = (BASE / "dashboard" / "server.py").read_text(encoding="utf-8")
    assert "from dashboard.video_api import register as _video_register" in src
    assert "_video_register(app, _auth)" in src
