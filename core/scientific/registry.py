"""
core/scientific/registry.py — enable/disable + provisioning state.

State lives in `memory/scientific/registry.json`, never in the vendored tree:
a skill's upstream hash must not change because somebody toggled it, and a
re-vendor must not resurrect a skill that was deliberately turned off.

Shape:
    {"version": 1, "skills": {"<name>": {"enabled": bool, "note": str,
                                          "env_status": str, "updated_at": iso}}}
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from core.scientific.config import (
    CORE_SKILLS,
    GUARDED_SKILLS,
    REGISTRY_PATH,
)

REGISTRY_VERSION = 1
_LOCK = threading.RLock()
_CACHE: dict[str, Any] | None = None


def _default_enabled(name: str) -> bool:
    return name in CORE_SKILLS and name not in GUARDED_SKILLS


def _empty() -> dict[str, Any]:
    return {"version": REGISTRY_VERSION, "skills": {}}


def load_registry(path: Path | None = None) -> dict[str, Any]:
    global _CACHE
    with _LOCK:
        if _CACHE is not None and path is None:
            return _CACHE
        target = Path(path or REGISTRY_PATH)
        data = _empty()
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("version") == REGISTRY_VERSION:
                data["skills"] = raw.get("skills") if isinstance(raw.get("skills"), dict) else {}
        except Exception:
            data = _empty()
        if path is None:
            _CACHE = data
        return data


def save_registry(data: dict[str, Any], path: Path | None = None) -> None:
    global _CACHE
    target = Path(path or REGISTRY_PATH)
    with _LOCK:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(target)
        if path is None:
            _CACHE = data


def reset_cache() -> None:
    global _CACHE
    with _LOCK:
        _CACHE = None


def entry(name: str) -> dict[str, Any]:
    return dict(load_registry()["skills"].get(name) or {})


def is_enabled(name: str) -> bool:
    if name in GUARDED_SKILLS:
        # A guarded skill is never on unless somebody explicitly enabled it.
        record = entry(name)
        return bool(record.get("enabled")) if "enabled" in record else False
    record = entry(name)
    if "enabled" in record:
        return bool(record["enabled"])
    return _default_enabled(name)


def enabled_names() -> list[str]:
    return sorted(name for name in load_registry()["skills"] if is_enabled(name))


def set_enabled(name: str, enabled: bool, note: str = "") -> dict[str, Any]:
    """Enable/disable one skill. Guarded skills require an explicit note."""
    with _LOCK:
        data = load_registry()
        record = dict(data["skills"].get(name) or {})
        record["enabled"] = bool(enabled)
        if name in GUARDED_SKILLS and enabled:
            record["note"] = note or f"explicitly enabled despite: {GUARDED_SKILLS[name]}"
        elif note:
            record["note"] = note
        record["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        data["skills"][name] = record
        save_registry(data)
        return record


def set_env_status(name: str, status: str, detail: str = "") -> dict[str, Any]:
    """Record provisioning/compatibility for one skill (`ready|missing|conflict`)."""
    with _LOCK:
        data = load_registry()
        record = dict(data["skills"].get(name) or {})
        record["env_status"] = status
        if detail:
            record["env_detail"] = detail[:400]
        record["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        data["skills"][name] = record
        save_registry(data)
        return record


def state() -> dict[str, Any]:
    data = load_registry()
    return {
        "version": data.get("version", REGISTRY_VERSION),
        "skills": {name: dict(record) for name, record in data["skills"].items()},
    }


def materialize_defaults(names: list[str] | None = None) -> dict[str, Any]:
    """Write the default enabled/disabled state once, for `names` (or the core set)."""
    with _LOCK:
        data = load_registry()
        touched = 0
        for name in (names or list(CORE_SKILLS)):
            if name in data["skills"]:
                continue
            data["skills"][name] = {
                "enabled": _default_enabled(name),
                "note": GUARDED_SKILLS.get(name, ""),
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            touched += 1
        if touched:
            save_registry(data)
        return {"written": touched}
