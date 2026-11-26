"""
core/video_agent/engines/registry.py — Phase 2 (Universal Video Studio).

Thread-safe engine registry with AUTO-DISCOVERY: every
`core/video_agent/engines/<id>/engine.py` that exports `ENGINE` (a
VideoEngine) is registered at first use. Adding an engine never touches the
core orchestration system — drop the folder, it appears on the next discovery.

Truth discipline:
  * statuses come from each engine's probe(); probes are REAL checks
    (binary runs, skill READY, provider config), never file-existence guesses
  * every status_table() call persists the measured snapshot to
    memory/repo_agent/engines.json (JARVIS_HOME-aware) so RIO, voice and the
    Phase 11 hardware-refresh flow all read the same truth
  * a module that fails to import registers as an honest UNAVAILABLE entry
    with the import error — it never breaks the other engines
"""
from __future__ import annotations

import importlib
import json
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from core.video_agent.engines.base import (STATUSES, UNAVAILABLE,
                                           EngineStatus, VideoEngine)

__all__ = ["register", "discover", "get", "all_engines", "status_table",
           "status_text", "refresh", "methods_for_spec"]

_lock = threading.Lock()
_engines: dict[str, VideoEngine] = {}
_discovered = False
_enrichment: dict[str, dict[str, str]] = {}   # id → import error etc.

_PACKAGE_DIR = Path(__file__).resolve().parent


def state_path() -> Path:
    """JARVIS_HOME-aware persistence, same convention as the job store."""
    import os
    from core.repo_agent.jobs import db_path
    return db_path().parent / "engines.json"


def register(engine: VideoEngine) -> None:
    """Register (or replace) an engine by id. Idempotent."""
    if not isinstance(engine, VideoEngine) or not getattr(engine, "descriptor",
                                                          None):
        raise ValueError("register() needs a VideoEngine with a descriptor")
    with _lock:
        _engines[engine.descriptor.id] = engine


def discover(force: bool = False) -> None:
    """Import every engines/<id>/engine.py once. Failures are isolated."""
    global _discovered
    with _lock:
        if _discovered and not force:
            return
        _discovered = True
    for child in sorted(_PACKAGE_DIR.iterdir()):
        if not child.is_dir() or child.name.startswith(("_", ".")):
            continue
        module_path = child / "engine.py"
        if not module_path.is_file():
            continue
        module_name = f"{__package__}.{child.name}.engine"
        try:
            module = importlib.import_module(module_name)
            engine = getattr(module, "ENGINE", None)
            if isinstance(engine, VideoEngine):
                register(engine)
                with _lock:
                    _enrichment.pop(engine.descriptor.id, None)
        except Exception as exc:                   # noqa: BLE001
            with _lock:
                _enrichment[child.name.upper()] = {
                    "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                    "trace": traceback.format_exc(limit=3)[-400:],
                }


def get(engine_id: str) -> VideoEngine | None:
    discover()
    with _lock:
        return _engines.get(str(engine_id or "").strip().upper())


def all_engines() -> list[VideoEngine]:
    discover()
    with _lock:
        return list(_engines.values())


def status_table(force: bool = False,
                 hw: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Probe every engine (each engine caches its own expensive parts) and
    persist the snapshot. `force` also re-probes the hardware report."""
    discover()
    if hw is None:
        from core.video_agent import hardware
        hw = hardware.probe(force=force)
    table: list[dict[str, Any]] = []
    for engine in sorted(all_engines(), key=lambda e: e.descriptor.id):
        try:
            status = engine.probe(hw=hw)
        except Exception as exc:                   # noqa: BLE001
            status = EngineStatus(
                UNAVAILABLE,
                f"probe failed: {type(exc).__name__}: {str(exc)[:200]}")
        entry = engine.as_dict(status)
        with _lock:
            extra = _enrichment.get(engine.descriptor.id)
        if extra:
            entry["import_error"] = extra["error"]
        table.append(entry)
    _persist(table)
    return table


def _persist(table: list[dict[str, Any]]) -> None:
    try:
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "engines": {e["descriptor"]["id"]: e["status"] for e in table},
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        tmp.replace(path)
    except Exception:                              # noqa: BLE001
        pass                                       # persistence is best-effort


def status_text(force: bool = False) -> str:
    """Human table for voice/RIO — the mission's method list."""
    rows = status_table(force=force)
    lines = ["Video engines (measured status — READY only after a real probe):"]
    for row in rows:
        d, st = row["descriptor"], row["status"]
        line = (f"• {d['name']} [{d['id']}] — {st['status']}: "
                f"{st['detail'][:150]}")
        if row.get("import_error"):
            line += f" (module error: {row['import_error']})"
        lines.append(line)
    lines.append("Jarvis never starts a video without your approval — say "
                 "'create a video about …' and pick a method when asked.")
    return "\n".join(lines)


def refresh() -> dict[str, Any]:
    """Phase 11 hook: re-measure hardware + every engine. Returns the new
    table plus the hardware fingerprint so a refresh can be diffed."""
    from core.video_agent import hardware
    hw = hardware.refresh()
    return {"hardware_fingerprint": hw["fingerprint"],
            "engines": status_table(force=True, hw=hw),
            "probed_at": hw["last_probe"]}


def methods_for_spec(spec: dict[str, Any],
                     hw: dict[str, Any] | None = None
                     ) -> list[dict[str, Any]]:
    """Engines that could carry this spec, best-first, with the facts the
    approval dialog must show (mission Phase 6). Never starts anything."""
    rows = status_table(hw=hw)
    mode = str((spec or {}).get("mode") or "").upper()
    out: list[dict[str, Any]] = []
    for row in rows:
        d, st = row["descriptor"], row["status"]
        can = _engine_suits_mode(d["id"], mode, st)
        if not can:
            continue
        out.append({
            "method_id": d["id"],
            "name": d["name"],
            "status": st["status"],
            "detail": st["detail"][:200],
            "execution": d["execution"],
            "estimated_cost": d["estimated_cost"],
            "privacy": d["privacy"],
            "resolutions": list(d["resolutions"]),
            "duration_range_s": list(d["duration_range_s"]),
            "recommended": _is_recommended(d["id"], mode),
        })
    out.sort(key=lambda m: (not m["recommended"],
                            m["status"] not in ("READY_LOCAL",
                                                "READY_REMOTE")))
    return out


def _engine_suits_mode(engine_id: str, mode: str, st: EngineStatus) -> bool:
    if engine_id == "CANVAS":
        return mode in ("DOCUMENTARY", "SHORTS")
    if engine_id == "BLENDER_3D":
        return mode == "BLENDER_3D"
    if engine_id == "VIMAX":
        return mode == "AI_CINEMATIC"
    if engine_id in ("WAN_2_1", "LTX_VIDEO"):
        return mode == "AI_CINEMATIC"
    if engine_id == "STOCK_FOOTAGE":
        return mode in ("DOCUMENTARY", "SHORTS", "AI_CINEMATIC")
    if engine_id == "CUSTOM_VIDEO_MODEL":
        return mode == "AI_CINEMATIC"
    if engine_id == "CHARACTER_ANIMATION":
        return mode == "CHARACTER_ANIMATION"
    return False


def _is_recommended(engine_id: str, mode: str) -> bool:
    """The honest default recommendation per mode (free/local first)."""
    return {"DOCUMENTARY": "CANVAS", "SHORTS": "CANVAS",
            "BLENDER_3D": "BLENDER_3D", "AI_CINEMATIC": "VIMAX",
            "CHARACTER_ANIMATION": "CHARACTER_ANIMATION",
            }.get(mode) == engine_id
