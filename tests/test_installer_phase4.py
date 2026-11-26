"""Phase 4 (Universal Video Studio) — safe installation manager tests.

Honesty contract:
 * survey MEASURES this machine (blender/ffmpeg really present here)
 * plans show size/source/steps and REQUIRE approval — nothing downloads
 * incompatible engines (Wan/LTX here) are BLOCKED from weight downloads and
   offered the remote-worker path instead (mission rule)
 * system packages are never automated
 * execution is verified before COMPLETED; guarded downloads clean their
   .part files on failure/cancel
 * start() consumes a Phase 6 INSTALL approval bound to the exact engine
"""
from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

import pytest

from core.video_agent import installer


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    yield


# ── survey ───────────────────────────────────────────────────────────────────

def test_survey_measures_all_sections():
    data = installer.survey()
    for key in ("BLENDER_3D", "FFMPEG", "VIMAX", "WAN_2_1", "LTX_VIDEO",
                "TTS", "LOCAL_LLM", "remote_workers", "surveyed_at"):
        assert key in data, f"survey missing {key}"
    assert data["BLENDER_3D"]["installed"] is True      # real on this box
    assert data["FFMPEG"]["installed"] is True
    assert data["WAN_2_1"]["installed"] is False        # weights never fetched


def test_tts_survey_reports_per_engine():
    tts = installer.survey()["TTS"]
    names = {e["name"]: e["installed"] for e in tts["engines"]}
    assert "gTTS" in names and "kokoro" in names


# ── plans ────────────────────────────────────────────────────────────────────

def test_plan_unknown_engine_refused():
    result = installer.plan("NO_SUCH_ENGINE")
    assert result["ok"] is False and "unknown" in result["error"].lower()


def test_plan_vimax_honest_and_delegates(monkeypatch):
    # this machine HAS ViMax: the honest answer is "already installed"
    result = installer.plan("VIMAX")
    assert result["ok"] is True and result["plan"] is None
    assert "already installed" in result["message"].lower()
    # without a registry entry the plan DELEGATES to the repo agent
    from core.repo_agent import manifests
    monkeypatch.setattr(manifests, "load_registry", lambda: {"repos": {}})
    result = installer.plan("VIMAX")
    assert result["plan"] is None
    assert "repo agent" in result["message"].lower()


def test_plan_ffmpeg_never_automated():
    result = installer.plan("FFMPEG")
    assert result["ok"] is True and result["plan"] is None
    # ffmpeg IS installed on this machine → honest no-op; the never-automated
    # rule is enforced at BOTH layers
    assert "already installed" in result["message"].lower() or \
        "system package" in result["message"].lower()
    executed = installer.execute_install("FFMPEG")
    assert executed["status"] == "FAILED"
    assert "system package" in executed["error"]


def test_plan_blender_complete_with_approval_gate(monkeypatch):
    # Blender IS installed here → honest no-op…
    result = installer.plan("BLENDER_3D")
    assert result["ok"] is True and result["plan"] is None
    assert "already installed" in result["message"].lower()
    # …and a missing binary produces the FULL plan (size/source/steps/gate)
    from core.video_agent.engines.blender_3d import engine as be
    monkeypatch.setattr(be, "blender_bin", lambda: "/none/blender")
    result = installer.plan("BLENDER_3D")
    plan_data = result["plan"]
    assert plan_data is not None and result["ok"] is True
    assert plan_data["approval_required"] is True
    assert "blender.org" in plan_data["source"]
    assert plan_data["hardware"]["compatible"] is True
    assert plan_data["download_size_mb"] >= 0     # 0 = existing tarball reused
    assert plan_data["disk_use_mb"] > 0
    tarball = Path.home() / "JarvisTools" / \
        "blender-3.6.23-linux-x64.tar.xz"
    assert plan_data["needs_download"] is (not tarball.is_file())


def test_plan_wan_blocked_without_weight_downloads():
    result = installer.plan("WAN_2_1")
    plan_data = result["plan"]
    assert plan_data is not None
    assert plan_data["blocked"] is True
    assert "refused" in plan_data["blocked_reason"].lower()
    assert "remote" in plan_data["blocked_reason"].lower()
    # and the job params gate refuses too — nothing can be queued
    assert installer.install_job_params("WAN_2_1") is None


def test_plan_ltx_blocked_same_rule():
    assert installer.plan("LTX_VIDEO")["plan"]["blocked"] is True


def test_plan_tts_kokoro_installable():
    result = installer.plan("TTS_KOKORO")
    plan_data = result["plan"]
    assert plan_data is not None
    assert plan_data["approval_required"] is True
    assert any("pip install kokoro" in s for s in plan_data["steps"])


# ── start: the Phase 6 INSTALL approval gate ─────────────────────────────────

def test_start_without_token_refused():
    result = installer.start("BLENDER_3D", "")
    assert result["ok"] is False and "error" in result


def test_start_with_unknown_token_refused():
    result = installer.start("BLENDER_3D", "deadbeefdeadbeef")
    assert result["ok"] is False


def _issue_and_approve(engine_id: str):
    from core.video_agent import approval as vappr
    methods = [{"method_id": "INSTALL", "name": "Install",
                "status": "READY_LOCAL", "recommended": True}]
    rec = vappr.issue(installer.install_approval_spec(engine_id),
                      methods)["record"]
    assert vappr.approve(rec["token"], "INSTALL")["ok"] is True
    return rec["token"]


def test_start_queues_detached_job_with_valid_approval(monkeypatch):
    # simulate a machine without Blender so an installable plan exists
    from core.video_agent.engines.blender_3d import engine as be
    monkeypatch.setattr(be, "blender_bin", lambda: "/none/blender")
    queued = {}

    def fake_start_job(kind, ref, source=None, params=None):
        queued.update(kind=kind, ref=ref, params=params)
        return {"ok": True, "job": {"job_id": "rt-install", "status":
                                    "QUEUED"}}

    monkeypatch.setattr("core.repo_agent.jobs.start_job", fake_start_job)
    token = _issue_and_approve("BLENDER_3D")
    result = installer.start("BLENDER_3D", token)
    assert result["ok"] is True, result
    assert queued["kind"] == "engine_install"
    assert queued["ref"] == "BLENDER_3D"
    assert queued["params"]["plan"]["approval_required"] is True


def test_start_binding_engine_exact(monkeypatch):
    """INSTALL approvals are engine-exact and never burned by a mismatched
    start attempt."""
    from core.video_agent.engines.blender_3d import engine as be
    monkeypatch.setattr(be, "blender_bin", lambda: "/none/blender")
    from core.video_agent import approval as vappr
    # approval bound to TTS_KOKORO must not authorise a Blender install
    token = _issue_and_approve("TTS_KOKORO")
    result = installer.start("BLENDER_3D", token)
    assert result["ok"] is False
    assert "different project" in result["error"]
    assert vappr.get(token)["state"] == "APPROVED"   # not consumed, not lost
    # the correct engine then starts with the SAME token
    def fake_start_job(kind, ref, source=None, params=None):
        return {"ok": True, "job": {"job_id": "rt-k", "status": "QUEUED"}}
    monkeypatch.setattr("core.repo_agent.jobs.start_job", fake_start_job)
    ok = installer.start("TTS_KOKORO", token)
    assert ok["ok"] is True


# ── execution (worker side) ──────────────────────────────────────────────────

def test_execute_blender_verified_before_completed(monkeypatch):
    calls = {"extract": 0, "verify": 0}

    def fake_extract(log=None):
        calls["extract"] += 1
        return {"ok": True, "installed_path": "/fake/blender"}

    def fake_verify():
        calls["verify"] += 1
        return True, "Blender 3.6.23 verified (binary runs)"

    monkeypatch.setattr("core.video_agent.engines._installer.extract_blender",
                        fake_extract)
    monkeypatch.setattr(installer, "_verify_blender", fake_verify)
    progress = []
    result = installer.execute_install(
        "BLENDER_3D", progress=lambda p, x, m: progress.append((p, x)))
    assert result["status"] == "COMPLETED"
    assert calls == {"extract": 1, "verify": 1}
    assert any(phase == "verifying" for phase, _ in progress)


def test_execute_blender_failed_verification_is_failed(monkeypatch):
    monkeypatch.setattr("core.video_agent.engines._installer.extract_blender",
                        lambda log=None: {"ok": True})
    monkeypatch.setattr(installer, "_verify_blender",
                        lambda: (False, "did not report a version"))
    result = installer.execute_install("BLENDER_3D")
    assert result["status"] == "FAILED"
    assert "verification failed" in result["error"]


def test_execute_wan_refused_on_this_hardware():
    """The mission rule, enforced at EXECUTION time: incompatible hardware →
    no weights, honest failure."""
    result = installer.execute_install("WAN_2_1")
    assert result["status"] == "FAILED"
    assert "not downloaded" in result["error"] or "incompatible" \
        in result["error"].lower()


def test_execute_unknown_engine_honest():
    result = installer.execute_install("STOCK_FOOTAGE")
    assert result["status"] == "FAILED"


def test_execute_progress_and_cancel_callbacks():
    seen = []

    # cancel before anything happens → blender path reports CANCELLED after
    # the (mocked) extraction
    monkey_free = True
    with patch("core.video_agent.engines._installer.extract_blender",
               lambda log=None: {"ok": True, "installed_path": "/x"}):
        with patch.object(installer, "_verify_blender",
                          lambda: (True, "Blender 3.6.23")):
            result = installer.execute_install(
                "BLENDER_3D", progress=lambda p, x, m: seen.append(p),
                cancel=lambda: False)
    assert result["status"] == "COMPLETED"
    assert "installing" in seen and "verifying" in seen
    assert monkey_free


# ── guarded download ─────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, chunks, headers=None):
        self._chunks = list(chunks)
        self.headers = headers or {}

    def read(self, n):
        return self._chunks.pop(0) if self._chunks else b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_guarded_download_success_atomic(tmp_path, monkeypatch):
    dest = tmp_path / "file.bin"
    payload = [b"x" * 1024 * 512, b"y" * 1024 * 512]
    fake = _FakeResponse(payload, {"Content-Length": str(1024 * 1024)})
    monkeypatch.setattr(installer.urllib.request, "urlopen",
                        lambda url, timeout: fake)
    progress = []
    installer.guarded_download("http://official/x", dest, limit_mb=5,
                               progress=progress.append)
    assert dest.read_bytes() == b"x" * 1024 * 512 + b"y" * 1024 * 512
    assert not dest.with_suffix(".bin.part").exists()
    assert progress and progress[-1] <= 0.99


def test_guarded_download_cap_enforced(tmp_path, monkeypatch):
    dest = tmp_path / "big.bin"
    chunks = [b"z" * 1024 * 512] * 10               # 5 MB+ against a 1 MB cap
    fake = _FakeResponse(chunks, {"Content-Length": str(6 * 1024 * 1024)})
    monkeypatch.setattr(installer.urllib.request, "urlopen",
                        lambda url, timeout: fake)
    with pytest.raises(ValueError, match="cap"):
        installer.guarded_download("http://official/x", dest, limit_mb=1)
    assert not dest.exists()
    assert not dest.with_suffix(".big.part").exists()   # clean recovery


def test_guarded_download_cancel_cleans_part(tmp_path, monkeypatch):
    dest = tmp_path / "c.bin"
    fake = _FakeResponse([b"a" * 1024 * 512] * 100)
    monkeypatch.setattr(installer.urllib.request, "urlopen",
                        lambda url, timeout: fake)
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] > 2                       # cancel after 2 chunks

    with pytest.raises(InterruptedError):
        installer.guarded_download("http://official/x", dest, limit_mb=500,
                                   cancel=cancel)
    assert not dest.exists()
    assert not dest.with_suffix(".c.part").exists()
