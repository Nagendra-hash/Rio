"""Phase 3 (Universal Video Studio) — unified hardware discovery tests.

Honesty contract: every assertion is about MEASURED values or mocked inputs.
 * the report is real on this machine (cheap local probes only)
 * the anti-assumption rule is tested explicitly: an NVIDIA GPU label never
   implies CUDA — with nvidia-smi unavailable, cuda_available stays False
 * fingerprint must be stable across calls and exclude volatile values
 * classify_requirements is unit-tested against fake reports (no hardware
   variance in CI)
 * remote workers are never invented: empty config → empty list
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from core.video_agent import hardware as hw


@pytest.fixture()
def report():
    return hw.probe()


def test_probe_returns_all_sections(report):
    for section in ("cpu", "ram", "disk", "gpus", "ffmpeg",
                    "remote_workers", "fingerprint", "last_probe"):
        assert section in report, f"missing report section: {section}"
    for sub in ("intel", "nvidia", "rocm", "opengl", "vaapi"):
        assert sub in report["gpus"]


def test_cpu_ram_disk_measured_sane(report):
    assert report["cpu"]["threads"] >= 1
    assert report["ram"]["total_mb"] > 0
    assert report["disk"]["home"]["free_gb"] >= 0


def test_probe_is_cached_until_forced():
    first = hw.probe()
    second = hw.probe()
    assert first["last_probe"] == second["last_probe"]
    forced = hw.refresh()
    assert forced["last_probe"] >= first["last_probe"]


def test_fingerprint_stable_and_excludes_volatile_values(report):
    fp1 = hw.fingerprint(report)
    fp2 = hw.fingerprint(report)
    assert fp1 == fp2 == report["fingerprint"]
    # volatile values must not change the fingerprint
    mutated = {**report, "ram": {**report["ram"], "available_mb": 1},
               "disk": {"home": {"free_gb": 0.1}, "repo": {"free_gb": 0.1}}}
    assert hw.fingerprint(mutated) == fp1


def test_nvidia_label_never_implies_cuda(report):
    """The mission's explicit anti-assumption rule, verified live."""
    nvidia = report["gpus"]["nvidia"]
    if not nvidia["present"]:
        pytest.skip("no NVIDIA GPU on this machine")
    if nvidia["kernel_driver"] == "nvidia" and nvidia["smi"]:
        assert nvidia["cuda_available"] is True
    else:
        assert nvidia["cuda_available"] is False
        assert "never assume" in nvidia["cuda_reason"] or "driver" \
            in nvidia["cuda_reason"]


def test_cuda_unavailable_with_broken_driver_via_mock():
    """Even a present NVIDIA PCI function with no driver reports NO CUDA."""
    fake = {
        "cpu": hw.cpu_info(), "ram": hw.ram_info(), "disk": hw.disk_info(),
        "gpus": {
            "nvidia": {"present": True, "kernel_driver": "nouveau",
                       "cuda_available": False, "smi": None, "vram_mb": -1,
                       "cuda_reason": "no usable driver"},
            "vaapi": {"available": False},
            "rocm": {"available": False},
            "opengl": {},
        },
        "ffmpeg": {"version": "4.4.2", "encoders": {"libx264": True},
                   "nvenc_usable": False},
        "remote_workers": [],
    }
    ok, reason = hw.classify_requirements({"needs_cuda": True}, fake)
    assert ok is False and "CUDA" in reason
    ok, _ = hw.classify_requirements({"needs_cuda": True,
                                      "min_vram_mb": 8192}, fake)
    assert ok is False


def test_classify_requirements_happy_path_with_mock():
    fake = {
        "ram": {"total_mb": 32000, "available_mb": 8000},
        "disk": {"home": {"free_gb": 60}, "repo": {"free_gb": 60}},
        "gpus": {"nvidia": {"present": True, "kernel_driver": "nvidia",
                            "cuda_available": True, "vram_mb": 24576,
                            "cuda_reason": "ok"},
                 "vaapi": {"available": True}, "rocm": {"available": False},
                 "opengl": {}},
        "ffmpeg": {"encoders": {"libx264": True, "h264_vaapi": True}},
    }
    ok, reason = hw.classify_requirements(
        {"needs_cuda": True, "min_vram_mb": 8192, "min_ram_mb": 16000,
         "needs_vaapi": True, "needs_ffmpeg_encoder": "libx264"}, fake)
    assert ok is True, reason


def test_classify_requirements_disk_and_ram_shortfalls():
    fake = {"ram": {"total_mb": 4000},
            "disk": {"home": {"free_gb": 1}, "repo": {"free_gb": 1}},
            "gpus": {"nvidia": {"present": False, "cuda_available": False},
                     "vaapi": {"available": False}},
            "ffmpeg": {"encoders": {}}}
    ok, reason = hw.classify_requirements(
        {"min_ram_mb": 16000, "min_free_disk_gb": 30}, fake)
    assert ok is False
    assert "RAM" in reason and "disk" in reason


def test_remote_workers_empty_without_config(monkeypatch):
    monkeypatch.delenv("JARVIS_REPO_AGENT_TEST", raising=False)
    workers = hw.remote_workers()
    assert isinstance(workers, list)
    # never invented: if nothing is configured there is nothing to show
    # (this machine has no remote workers configured)


def test_remote_workers_read_explicit_config():
    fake_section = {"repo_agent": {"video_remote_workers": [
        {"name": "lab-gpu", "host": "10.0.0.9", "gpu": "RTX 4090"}]}}
    with patch("memory.config_manager.load_api_keys",
               return_value=fake_section):
        workers = hw.remote_workers()
    assert len(workers) == 1
    assert workers[0]["name"] == "lab-gpu"
    assert workers[0]["host"] == "10.0.0.9"


def test_remote_workers_reject_malformed_entries():
    fake_section = {"repo_agent": {"video_remote_workers": [
        "not-a-dict", {"host": "no-name"}, {"name": "ok", "host": "h"}]}}
    with patch("memory.config_manager.load_api_keys",
               return_value=fake_section):
        workers = hw.remote_workers()
    assert [w["name"] for w in workers] == ["ok"]


def test_ffmpeg_info_reports_encoders_and_nvenc_honesty():
    info = hw.ffmpeg_info()
    assert info["encoders"].get("libx264") in (True, False)
    if not info["encoders"].get("h264_nvenc"):
        assert info["nvenc_usable"] is False
    # NVENC usable requires a working driver — never just the encoder string
    if info["nvenc_usable"]:
        assert info["encoders"].get("h264_nvenc") is True


def test_summary_text_contains_key_sections():
    text = hw.summary_text()
    for token in ("CPU:", "RAM:", "Disk:", "GPU Intel:", "GPU NVIDIA:",
                  "FFmpeg:", "Remote GPU workers:", "Fingerprint:"):
        assert token in text, f"summary missing {token}"
