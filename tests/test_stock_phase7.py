"""Phase 7 (Universal Video Studio) — stock footage + remote + hybrid.

Honesty contract:
 * no network anywhere: the Pexels HTTP layer and the ssh/scp transport are
   MOCKED; nothing here touches a real provider or a real worker
 * stock clips ALWAYS carry license metadata and the pipeline REFUSES to
   validate without an honest manifest; output is never labelled
   AI-generated
 * remote generation requires a VERIFIED worker — configured-but-unverified
   keeps engines at NOT_CONFIGURED
 * hybrid runs only an APPROVED scene plan whose engines measure ready
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from core.video_agent import remote, stock
from core.video_agent.engines import registry as engine_registry


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    yield


@pytest.fixture()
def fake_config():
    """An in-memory config store wired into every reader/writer — tests
    never touch the real api_keys.json."""
    store: dict = {}

    def _write(data: dict) -> None:
        store.clear()
        store.update(data)

    with patch.object(remote, "_load_config", lambda: dict(store)), \
            patch.object(remote, "_write_config", _write), \
            patch("memory.config_manager.load_api_keys",
                  lambda: dict(store)):
        yield store


def _set_worker(fake_config, **overrides):
    worker = _fake_worker(**overrides)
    fake_config.update({"repo_agent":
                        {"video_remote_workers": [worker]}})
    return worker


# ── stock provider layer ─────────────────────────────────────────────────────

def test_no_provider_configured_is_honest():
    provider, reason = stock.get_provider()
    assert provider is None and "no stock provider" in reason


def test_pexels_provider_search_parses_api(monkeypatch):
    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"videos": [{
                "id": 123, "url": "https://pexels.com/video/123",
                "duration": 12,
                "user": {"name": "Jane Doe"},
                "video_files": [
                    {"link": "https://cdn/small.mp4", "width": 640,
                     "height": 360, "file_type": "video/mp4"},
                    {"link": "https://cdn/hd.mp4", "width": 1280,
                     "height": 720, "file_type": "video/mp4"},
                ]}]}).encode()

    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["auth"] = req.headers.get("Authorization")
        return FakeResp()

    monkeypatch.setattr(stock.urllib.request, "urlopen", fake_urlopen)
    provider = stock.PexelsProvider("test-key")
    clips = provider.search("ocean waves", per_page=2)
    assert len(clips) == 1
    assert clips[0]["clip_id"] == "123"
    assert clips[0]["height"] == 720          # closest to the 720p target
    assert "Pexels License" in clips[0]["license"]
    assert captured["auth"] == "test-key"
    assert "ocean%20waves" in captured["url"]


def test_download_clip_writes_license_metadata(tmp_path, monkeypatch):
    class FakeResp:
        headers = {}

        def __init__(self):
            self._done = False

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n=0):
            if self._done:
                return b""
            self._done = True
            return b"v" * 4096

    monkeypatch.setattr(stock.urllib.request, "urlopen",
                        lambda req, timeout: FakeResp())
    dest = tmp_path / "clips" / "clip_01.mp4"
    meta = stock.download_clip({
        "provider": "pexels", "clip_id": "123",
        "url": "https://cdn/hd.mp4", "page_url": "https://pexels.com/v/123",
        "author": "Jane Doe", "license": "Pexels License",
        "query": "ocean waves", "width": 1280, "height": 720},
        dest)
    assert dest.read_bytes() == b"v" * 4096
    assert meta["license"] == "Pexels License"
    assert meta["author"] == "Jane Doe"
    assert meta["ai_generated"] is False
    assert meta["file"] == str(dest)


def test_download_clip_leaves_no_part_on_failure(tmp_path, monkeypatch):
    def boom(req, timeout):
        raise OSError("network down (simulated)")

    monkeypatch.setattr(stock.urllib.request, "urlopen", boom)
    with pytest.raises(OSError):
        stock.download_clip({"url": "https://cdn/x.mp4"},
                            tmp_path / "c.mp4")
    assert not (tmp_path / "c.mp4").exists()
    assert not (tmp_path / "c.mp4.part").exists()


def test_stock_pipeline_refuses_without_provider():
    from core.video_agent import pipeline_stock
    ctx = {"run_dir": Path("/tmp/opencode/stock-x"), "spec": {}, "log":
           lambda m: None}
    with pytest.raises(Exception, match="stock footage unavailable"):
        pipeline_stock._plan_stock(ctx)


# ── remote workers ───────────────────────────────────────────────────────────

def test_no_workers_configured():
    assert remote.workers() == []
    assert "none configured" in remote.state_text()


def test_check_unknown_worker_refused():
    result = remote.check("no-such-worker")
    assert result["ok"] is False
    assert "no remote worker named" in result["error"]


def _fake_worker(**overrides):
    worker = {"name": "lab-gpu", "host": "10.0.0.9", "user": "nagendra",
              "port": 22, "remote_dir": "~/wan-jobs",
              "cmd": "python generate.py"}
    worker.update(overrides)
    return worker


def test_check_runs_real_ssh_probe_and_records(fake_config, monkeypatch):
    _set_worker(fake_config)
    calls = []

    def fake_run(argv, timeout_s, cancel=None):
        calls.append(list(argv))
        class P:
            returncode = 0
            stdout = "remote-ok\n"
            stderr = ""
        return P()

    monkeypatch.setattr(remote, "_run", fake_run)
    result = remote.check("lab-gpu")
    assert result["ok"] is True and result["verified"] is True
    assert any("echo" in " ".join(c) for c in calls)
    saved = remote._load_config()["repo_agent"]["video_remote_workers"][0]
    assert saved.get("verified_at")            # persisted verification


def test_verified_worker_makes_engine_ready_remote(fake_config):
    _set_worker(fake_config, verified_at="2026-10-27T00:00:00Z")
    worker = remote.workers()[0]
    assert remote.is_verified(worker) is True
    engine = engine_registry.get("WAN_2_1")
    status = engine.probe()
    assert status.status == "READY_REMOTE"
    assert "lab-gpu" in status.detail
    # and a start triple now exists for an explicit pipeline request
    params = engine.start_params({"pipeline": "wan_2_1", "mode":
                                  "AI_CINEMATIC", "out_dir": "/tmp/x"})
    assert params and params["ref"] == "wan_2_1"


def test_unverified_worker_stays_not_configured(fake_config):
    _set_worker(fake_config)
    engine = engine_registry.get("WAN_2_1")
    status = engine.probe()
    assert status.status == "INCOMPATIBLE"     # local still impossible
    assert status.paths["remote"] == "NOT_CONFIGURED"
    assert engine.start_params({"pipeline": "wan_2_1"}) is None


def test_submit_and_fetch_full_flow_mocked(tmp_path, monkeypatch,
                                           fake_config):
    worker = _set_worker(fake_config, verified_at="2026-10-27T00:00:00Z")
    dest = tmp_path / "remote" / "result.mp4"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"\x00\x00\x00\x18ftypmp42")

    def fake_run(argv, timeout_s, cancel=None):
        class P:
            returncode = 0
            stdout = "remote-ok"
            stderr = ""
        return P()

    def fake_subprocess_run(argv, **kwargs):
        input_data = kwargs.get("input")
        if input_data:                        # the request upload (ssh stdin)
            class Up:
                returncode = 0
                stdout = ""
                stderr = ""
            return Up()
        # the generation command → write the result remotely (simulated)
        dest.write_bytes(b"\x00\x00\x00\x18ftypmp42")
        class Run:
            returncode = 0
            stdout = "done"
            stderr = ""
        return Run()

    def fake_fetch(argv, capture_output, text, timeout):
        # the scp fetch: the "remote" file already lands at dest
        class F:
            returncode = 0
            stdout = ""
            stderr = ""
        return F()

    monkeypatch.setattr(remote, "_run", fake_run)
    monkeypatch.setattr(remote.subprocess, "run", fake_subprocess_run)
    # scp fetch target resolution happens inside submit_and_fetch; patch the
    # LAST subprocess call (scp) to a no-op
    real_run = remote.subprocess.run

    def selective(argv, **kwargs):
        if argv and argv[0] == "scp":
            return fake_fetch(argv, capture_output=True, text=True,
                              timeout=600)
        return fake_subprocess_run(argv, **kwargs)

    monkeypatch.setattr(remote.subprocess, "run", selective)
    result = remote.submit_and_fetch(worker, {"prompt": "ocean"},
                                     dest)
    assert result["status"] == "COMPLETED", result
    assert dest.is_file()


def test_submit_refused_when_worker_unreachable(tmp_path, monkeypatch,
                                                fake_config):
    _set_worker(fake_config)
    monkeypatch.setattr(remote, "_run",
                        lambda argv, timeout_s, cancel=None:
                        _fail_run())
    result = remote.submit_and_fetch(_fake_worker(), {"prompt": "x"},
                                     tmp_path / "r.mp4")
    assert result["status"] == "FAILED"
    assert "not reachable" in result["error"]


def _fail_run():
    class P:
        returncode = 255
        stdout = ""
        stderr = "connection refused (simulated)"
    return P()


# ── engine-owned pipelines resolve through the registry seam ────────────────

def test_engine_pipelines_resolve():
    from core.video_agent import pipelines
    wan = pipelines.stages_for("wan_2_1")
    ltx = pipelines.stages_for("ltx_video")
    stock_stages = pipelines.stages_for("stock_footage")
    assert wan and set(wan) >= {"planning", "generating", "editing",
                                "validating"}
    assert ltx and "generating" in ltx
    assert stock_stages and "asset_collection" in stock_stages
    assert pipelines.stages_for("no_such") is None
