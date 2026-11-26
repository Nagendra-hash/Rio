"""
core/video_agent/refresh.py — Phase 11 (Universal Video Studio): the
hardware-refresh flow.

When hardware changes (a new GPU appears, a driver lands, weights are
installed), this module:

  1. re-measures everything (hardware.refresh → engines.refresh, real
     probes, cached values discarded)
  2. compares the hardware fingerprint and engine statuses against the
     last recorded snapshot
  3. reports newly supported engines (e.g. Wan 2.1 flipping from
     INCOMPATIBLE to INSTALLABLE/READY_REMOTE because a CUDA driver
     appeared) — and offers the APPROVAL-GATED next step (a plan / a
     verification), never an automatic download
  4. runs a small compatibility test on demand (engine_installer
     action=test): a REAL mini-render for local engines, a real ssh probe
     for remote ones — an engine is enabled only when its backend
     verifies (which is already the registry's only path to READY)

Drivers/CUDA/kernel are NEVER installed by any code path — the flow only
DETECTS them and says what an approved next step would be.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from core.video_agent.engines import registry as engine_registry

__all__ = ["refresh", "compat_test", "state_path", "compat_test_text"]


def state_path() -> Path:
    from core.repo_agent.jobs import db_path
    return db_path().parent / "hardware_refresh.json"


def _load_snapshot() -> dict[str, Any]:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def _save_snapshot(data: dict[str, Any]) -> None:
    try:
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(path)
    except Exception:                              # noqa: BLE001
        pass


def refresh() -> dict[str, Any]:
    """Re-measure hardware + every engine; diff against the last snapshot."""
    from core.video_agent import hardware as hw_mod
    hw = hw_mod.refresh()
    engines = engine_registry.status_table(force=True, hw=hw)
    previous = _load_snapshot()
    prev_fingerprint = str(previous.get("fingerprint") or "")
    prev_statuses = previous.get("engines") or {}
    changed = bool(prev_fingerprint) and prev_fingerprint != hw["fingerprint"]
    newly_supported: list[dict[str, Any]] = []
    newly_unsupported: list[dict[str, Any]] = []
    now = {"READY_LOCAL", "READY_REMOTE", "INSTALLABLE"}
    was = {"READY_LOCAL", "READY_REMOTE", "INSTALLABLE"}
    for entry in engines:
        eid = entry["descriptor"]["id"]
        new_status = entry["status"]["status"]
        old_status = str(prev_statuses.get(eid) or "")
        if old_status and old_status != new_status:
            if new_status in now and old_status not in was:
                newly_supported.append({
                    "engine": eid, "from": old_status, "to": new_status,
                    "detail": entry["status"]["detail"][:200]})
            elif new_status not in was and old_status in was:
                newly_unsupported.append({
                    "engine": eid, "from": old_status, "to": new_status})
    snapshot = {"fingerprint": hw["fingerprint"],
                "probed_at": hw["last_probe"],
                "engines": {e["descriptor"]["id"]: e["status"]["status"]
                            for e in engines}}
    _save_snapshot(snapshot)
    return {"fingerprint": hw["fingerprint"],
            "changed": changed,
            "previous_fingerprint": prev_fingerprint,
            "newly_supported": newly_supported,
            "newly_unsupported": newly_unsupported,
            "engines": engines,
            "probed_at": hw["last_probe"]}


# ── small compatibility tests (REAL, bounded, on demand) ─────────────────────

def compat_test(engine_id: str) -> dict[str, Any]:
    """A small REAL verification for one engine. This is the 'run a small
    compatibility test' step of the mission's Phase 11 — nothing is
    enabled by it that a real probe would not already enable."""
    engine_id = str(engine_id or "").strip().upper()
    engine = engine_registry.get(engine_id)
    if engine is None:
        return {"ok": False, "error": f"unknown engine '{engine_id}'"}
    status = engine.probe()
    if engine_id == "BLENDER_3D":
        return _compat_blender(status)
    if engine_id == "CANVAS":
        return _compat_canvas(status)
    if engine_id in ("WAN_2_1", "LTX_VIDEO"):
        # remote path: a real ssh probe is the honest compatibility test
        from core.video_agent import remote as remote_mod
        worker = (remote_mod.workers() or [None])[0]
        if worker is None:
            return {"ok": False, "engine": engine_id,
                    "label": "no remote worker configured — nothing to "
                             "test locally (local is INCOMPATIBLE)"}
        check = remote_mod.check(str(worker.get("name") or ""))
        return {"ok": bool(check.get("ok")), "engine": engine_id,
                "label": f"remote worker probe: "
                         f"{check.get('detail', check.get('error', ''))[:160]}"}
    if engine_id == "CUSTOM_VIDEO_MODEL":
        return {"ok": False, "engine": engine_id,
                "label": "no custom engine registered — nothing to test"}
    # VIMAX / STOCK_FOOTAGE / TTS_KOKORO: probe-level verification
    return {"ok": status.is_runnable(), "engine": engine_id,
            "label": f"probe-level test: {status.status} — "
                     f"{status.detail[:160]}"}


def _compat_blender(status) -> dict[str, Any]:
    """Render ONE frame of the startup scene headlessly — a real mini-render
    through the real binary (Eevee/Workbench path exercised)."""
    if not status.is_runnable():
        return {"ok": False, "engine": "BLENDER_3D",
                "label": f"not runnable: {status.detail[:120]}"}
    from core.video_agent.engines.blender_3d.engine import blender_bin
    binary = blender_bin()
    with tempfile.TemporaryDirectory() as td:
        out_prefix = str(Path(td) / "compat_")
        try:
            proc = subprocess.run(
                [binary, "-b", "--factory-startup", "-o", out_prefix,
                 "-F", "PNG", "-f", "1"],
                capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            return {"ok": False, "engine": "BLENDER_3D",
                    "label": f"render failed: {type(exc).__name__}: "
                             f"{str(exc)[:160]}"}
        frames = list(Path(td).glob("compat_*.png"))
        ok = bool(proc.returncode == 0 and frames)
        return {"ok": ok, "engine": "BLENDER_3D",
                "label": (f"1-frame headless render OK "
                          f"({frames[0].stat().st_size // 1024} KB)"
                          if ok else
                          f"render failed: {(proc.stderr or '')[-160]}")}


def _compat_canvas(status) -> dict[str, Any]:
    """A tiny real ffmpeg render — the whole Canvas backend in 0.3 s."""
    if not status.is_runnable():
        return {"ok": False, "engine": "CANVAS",
                "label": f"not runnable: {status.detail[:120]}"}
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "compat.mp4"
        try:
            proc = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                 "-i", "testsrc2=size=160x160:duration=0.3",
                 "-c:v", "libx264", "-preset", "ultrafast", str(out)],
                capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            return {"ok": False, "engine": "CANVAS",
                    "label": f"render failed: {type(exc).__name__}: "
                             f"{str(exc)[:160]}"}
        ok = proc.returncode == 0 and out.is_file()
        return {"ok": ok, "engine": "CANVAS",
                "label": "0.3 s test render OK" if ok else
                         f"render failed: {(proc.stderr or '')[-160]}"}


def compat_test_text(engine_id: str) -> str:
    result = compat_test(engine_id)
    verdict = "PASS" if result.get("ok") else "FAIL"
    return (f"Compatibility test for {result.get('engine', engine_id)}: "
            f"{verdict} — {result.get('label', result.get('error', ''))}")
