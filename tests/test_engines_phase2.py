"""Phase 2 (Universal Video Studio) — engine registry tests.

Honesty contract: engines are READY only when their probe MEASURES a working
backend. Tests verify:
 * discovery finds the six mission engines + CANVAS without touching core
 * every engine exposes the full mission Phase 2 descriptor surface
 * classification logic for each engine (mocked hardware/config — no network,
   no downloads, no paid calls)
 * registry persistence is JARVIS_HOME-aware and best-effort
 * a broken engine module registers as UNAVAILABLE without breaking others
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from core.video_agent.engines import base as engine_base
from core.video_agent.engines import registry
from core.video_agent.engines.base import (INCOMPATIBLE, INSTALLABLE,
                                           NOT_CONFIGURED, READY_LOCAL,
                                           READY_REMOTE, UNAVAILABLE)

MISSION_ENGINES = {"BLENDER_3D", "WAN_2_1", "LTX_VIDEO", "VIMAX",
                   "STOCK_FOOTAGE", "CUSTOM_VIDEO_MODEL"}


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    yield


@pytest.fixture()
def engines():
    registry.discover(force=True)
    return {e.descriptor.id: e for e in registry.all_engines()}


# ── discovery + descriptor surface ───────────────────────────────────────────

def test_discovery_finds_mission_engines(engines):
    missing = MISSION_ENGINES - set(engines)
    assert not missing, f"engines not discovered: {missing}"
    assert "CANVAS" in engines               # the free method


def test_every_engine_exposes_full_mission_surface(engines):
    required = {"id", "name", "version", "summary", "execution",
                "video_types", "resolutions", "duration_range_s",
                "hardware_requirements", "hw_requirements_text",
                "estimated_resources", "estimated_cost", "privacy",
                "install"}
    for engine in engines.values():
        d = engine.descriptor.as_dict()
        gaps = required - set(d)
        assert not gaps, f"{engine.descriptor.id} missing {gaps}"
        assert d["id"] and d["name"] and d["version"]


def test_probe_produces_valid_status(engines):
    for engine in engines.values():
        status = engine.probe()
        assert status.status in engine_base.STATUSES, \
            f"{engine.descriptor.id} illegal status {status.status}"
        assert isinstance(status.detail, str) and status.detail


def test_status_table_persists_snapshot(engines):
    table = registry.status_table()
    path = registry.state_path()
    assert path.is_file()
    payload = json.loads(path.read_text())
    assert set(payload["engines"]) == {row["descriptor"]["id"]
                                       for row in table}


def test_broken_engine_module_is_isolated(monkeypatch):
    """A module that raises on import must not break the other engines."""
    import importlib
    real_import = importlib.import_module

    def broken(name, *args, **kwargs):
        if name.endswith("wan_2_1.engine"):
            raise RuntimeError("boom (simulated)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", broken)
    monkeypatch.setattr(registry, "_discovered", False)
    monkeypatch.setattr(registry, "_engines", {})
    registry.discover(force=True)
    ids = {e.descriptor.id for e in registry.all_engines()}
    assert "WAN_2_1" not in ids
    assert "LTX_VIDEO" in ids


# ── BLENDER_3D ───────────────────────────────────────────────────────────────

def test_blender_ready_requires_real_version_probe(monkeypatch):
    from core.video_agent.engines.blender_3d import engine as be
    be._probe_cache.clear()                  # isolate from the status_table run
    engine = registry.get("BLENDER_3D")
    with patch("core.video_agent.engines.blender_3d.engine._version_probe",
               return_value=(True, "Blender 3.6.23 verified (binary runs)")):
        status = engine.probe()
    assert status.status == READY_LOCAL
    assert "verified" in status.detail


def test_blender_installable_when_binary_missing(monkeypatch):
    from core.video_agent.engines.blender_3d import engine as be
    be._probe_cache.clear()
    monkeypatch.setattr(be, "_version_probe",
                        lambda: (False, "Blender binary not found at /none"))
    monkeypatch.setattr(be, "blender_bin", lambda: "/none/blender")
    status = be.ENGINE.probe()
    assert status.status == INSTALLABLE
    plan = be.ENGINE.install_plan()
    assert plan and plan["approval_required"] is True


def test_blender_start_params_only_for_blender_mode():
    engine = registry.get("BLENDER_3D")
    assert engine.start_params({"mode": "DOCUMENTARY"}) is None
    params = engine.start_params({"mode": "BLENDER_3D", "out_dir": "/tmp/x"})
    assert params["kind"] == "video" and params["ref"] == "blender"
    assert params["params"]["spec"]["mode"] == "BLENDER_3D"


# ── VIMAX ────────────────────────────────────────────────────────────────────

def _fake_registry(repos):
    return {"repos": repos, "skills": {}}


def test_vimax_installable_when_absent(monkeypatch):
    from core.repo_agent import manifests, skills
    monkeypatch.setattr(manifests, "load_registry",
                        lambda: _fake_registry({}))
    engine = registry.get("VIMAX")
    with patch.object(skills, "resolve_skill", return_value={"ok": False}):
        status = engine.probe()
    assert status.status == INSTALLABLE


def test_vimax_not_configured_when_skill_needs_keys(monkeypatch):
    from core.repo_agent import manifests, skills
    monkeypatch.setattr(manifests, "load_registry", lambda: _fake_registry(
        {"vimax": {"status": "READY"}}))
    monkeypatch.setattr(skills, "resolve_skill", lambda _id: {
        "ok": True,
        "entry": {"enabled": True, "status": "NEEDS_CONFIGURATION",
                  "detail": "provider API keys missing"}})
    status = registry.get("VIMAX").probe()
    assert status.status == NOT_CONFIGURED


def test_vimax_ready_only_with_skill_ready_and_providers(monkeypatch):
    from core.repo_agent import manifests, skills, vimax as vimax_mod
    monkeypatch.setattr(manifests, "load_registry", lambda: _fake_registry(
        {"vimax": {"status": "READY"}}))
    monkeypatch.setattr(skills, "resolve_skill", lambda _id: {
        "ok": True, "entry": {"enabled": True, "status": "READY"}})
    monkeypatch.setattr(vimax_mod, "provider_status", lambda: {
        "llm": {"ok": True}, "image": {"ok": True}, "video": {"ok": True},
        "ready": True, "missing": []})
    status = registry.get("VIMAX").probe()
    assert status.status == READY_LOCAL
    assert "PAID" in status.detail or "credits" in status.detail


def test_vimax_install_delegates_and_never_duplicates(monkeypatch):
    engine = registry.get("VIMAX")
    refused = engine.install(approve=False)
    assert refused.get("approval_required") is True
    delegated = engine.install(approve=True)
    assert delegated.get("delegated") == "repo agent"


# ── WAN_2_1 / LTX_VIDEO ──────────────────────────────────────────────────────

@pytest.mark.parametrize("engine_id", ["WAN_2_1", "LTX_VIDEO"])
def test_diffusion_engines_incompatible_without_cuda(engine_id, monkeypatch):
    from core.video_agent import hardware as hw_mod
    monkeypatch.setattr(hw_mod, "classify_requirements",
                        lambda reqs, hw=None: (False, "needs a working "
                                                   "NVIDIA CUDA driver"))
    status = registry.get(engine_id).probe()
    assert status.status == INCOMPATIBLE
    assert status.paths["local"] == INCOMPATIBLE
    assert status.paths["remote"] == NOT_CONFIGURED


@pytest.mark.parametrize("engine_id,vram", [("WAN_2_1", 8192),
                                            ("LTX_VIDEO", 12288)])
def test_diffusion_engines_installable_on_capable_hw_without_weights(
        engine_id, vram, monkeypatch):
    from core.video_agent import hardware as hw_mod
    monkeypatch.setattr(hw_mod, "classify_requirements",
                        lambda reqs, hw=None: (True, "all measured "
                                                   "requirements satisfied"))
    monkeypatch.setattr(hw_mod, "remote_workers", lambda: [])
    engine = registry.get(engine_id)
    assert engine.probe().status == INSTALLABLE
    plan = engine.install_plan()
    assert plan and plan["approval_required"] is True
    assert plan["needs_download"] is True
    # install refuses without approval — no weights are ever auto-downloaded
    result = engine.install(approve=False)
    assert result["ok"] is False and result.get("approval_required") is True


@pytest.mark.parametrize("engine_id", ["WAN_2_1", "LTX_VIDEO"])
def test_diffusion_engines_remote_ready_with_worker(engine_id, monkeypatch):
    import datetime
    from core.video_agent import hardware as hw_mod
    stamp = datetime.datetime.now(datetime.timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(hw_mod, "classify_requirements",
                        lambda reqs, hw=None: (False, "no local CUDA"))
    monkeypatch.setattr(hw_mod, "remote_workers",
                        lambda: [{"name": "lab-gpu", "host": "10.0.0.9",
                                  "verified_at": stamp}])
    status = registry.get(engine_id).probe()
    assert status.status == READY_REMOTE
    assert "lab-gpu" in status.detail


def test_diffusion_engines_refuse_start_until_backend_verified():
    for engine_id in ("WAN_2_1", "LTX_VIDEO"):
        assert registry.get(engine_id).start_params({}) is None


# ── STOCK_FOOTAGE / CUSTOM / CANVAS ─────────────────────────────────────────

def test_stock_footage_not_configured_without_provider():
    status = registry.get("STOCK_FOOTAGE").probe()
    assert status.status == NOT_CONFIGURED
    assert "never labelled AI" in registry.get(
        "STOCK_FOOTAGE").descriptor.summary or "never" in status.detail


def test_stock_footage_ready_with_provider(monkeypatch):
    from core.video_agent.engines.stock_footage import engine as se
    monkeypatch.setattr(se, "_provider_config",
                        lambda: {"provider": "pexels",
                                 "api_key_ref": "stock_api_key"})
    status = se.ENGINE.probe()
    assert status.status == READY_LOCAL


def test_custom_engine_unavailable_with_documented_contract():
    engine = registry.get("CUSTOM_VIDEO_MODEL")
    status = engine.probe()
    assert status.status == UNAVAILABLE
    assert "adapter" in status.detail.lower()
    assert engine.start_params({}) is None


def test_canvas_ready_and_maps_documentary_shorts():
    engine = registry.get("CANVAS")
    status = engine.probe()
    assert status.status == READY_LOCAL
    doc = engine.start_params({"mode": "DOCUMENTARY", "out_dir": "/tmp"})
    short = engine.start_params({"mode": "SHORTS", "out_dir": "/tmp"})
    smoke = engine.start_params({"mode": "DOCUMENTARY", "pipeline": "smoke",
                                 "out_dir": "/tmp"})
    assert (doc["ref"], short["ref"], smoke["ref"]) == \
        ("documentary", "shorts", "smoke")
    assert engine.start_params({"mode": "AI_CINEMATIC"}) is None


# ── approval-facing selection ────────────────────────────────────────────────

def test_methods_for_spec_filters_by_mode():
    doc_methods = registry.methods_for_spec({"mode": "DOCUMENTARY"})
    ids = {m["method_id"] for m in doc_methods}
    assert "CANVAS" in ids and "STOCK_FOOTAGE" in ids
    assert "BLENDER_3D" not in ids
    cine = {m["method_id"] for m in
            registry.methods_for_spec({"mode": "AI_CINEMATIC"})}
    assert "VIMAX" in cine and "CANVAS" not in cine


def test_methods_for_spec_marks_recommendation():
    methods = registry.methods_for_spec({"mode": "SHORTS"})
    recommended = [m for m in methods if m["recommended"]]
    assert len(recommended) == 1
    assert recommended[0]["method_id"] == "CANVAS"


def test_status_text_lists_engines_with_status():
    text = registry.status_text()
    for engine_id in MISSION_ENGINES:
        assert engine_id in text
    assert "approval" in text.lower()
