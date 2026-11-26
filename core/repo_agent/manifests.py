"""
core/repo_agent/manifests.py — install manifests, skill manifests, registry.

Layout (deliberately OUTSIDE ~/Mark-III):

    ~/JarvisTools/                       tools root (configurable)
      registry.json                      index of repos + skills (atomic writes)
      <repo-name>/                       one managed directory per repository
        .jarvis/
          repo_manifest.json             what was installed, from where, at what commit
          skill.json                     adapter manifest (entry points, schemas, limits)
          home/                          fake $HOME for untrusted child processes
          logs/                          install + run logs

Secrets NEVER live under JarvisTools — provider keys stay in Mark-III's
gitignored config/api_keys.json and are injected into the process environment
only for the duration of a run.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from core.repo_agent.security import safe_dir_name

__all__ = ["tools_root", "registry_path", "load_registry", "save_registry",
           "repo_dir", "repo_manifest_path", "skill_manifest_path",
           "load_repo_manifest", "load_skill_manifest", "write_json",
           "logs_dir", "db_side_dir", "now_iso"]


def _base_dir() -> Path:
    """Mark-III root (JARVIS_HOME aware, same rule as memory/config_manager)."""
    home = os.environ.get("JARVIS_HOME", "").strip()
    if home:
        return Path(home).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def tools_root() -> Path:
    """Managed tools directory — default ~/JarvisTools."""
    override = os.environ.get("JARVIS_TOOLS_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / "JarvisTools"


def registry_path() -> Path:
    return tools_root() / "registry.json"


def repo_dir(name: str) -> Path:
    """The ONLY sanctioned path join for a repository directory."""
    return tools_root() / safe_dir_name(name)


def repo_manifest_path(name: str) -> Path:
    return repo_dir(name) / ".jarvis" / "repo_manifest.json"


def skill_manifest_path(name: str) -> Path:
    return repo_dir(name) / ".jarvis" / "skill.json"


def logs_dir() -> Path:
    """Per-job run logs (stdout/stderr pairs live next to the job logs)."""
    path = db_side_dir() / "job_logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_side_dir() -> Path:
    """memory/repo_agent — job DB, logs and results (JARVIS_HOME aware)."""
    home = os.environ.get("JARVIS_HOME", "").strip()
    base = Path(home).expanduser() if home else \
        Path(__file__).resolve().parents[2]
    return base / "memory" / "repo_agent"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── registry (the index) ─────────────────────────────────────────────────────

def _empty_registry() -> dict[str, Any]:
    return {"version": 1, "updated_at": now_iso(), "repos": {}, "skills": {}}


def load_registry() -> dict[str, Any]:
    path = registry_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _empty_registry()
    if not isinstance(data, dict) or not isinstance(data.get("repos"), dict):
        return _empty_registry()
    data.setdefault("skills", {})
    data.setdefault("version", 1)
    return data


def save_registry(reg: dict[str, Any]) -> None:
    """Atomic write — a crash mid-save must never corrupt the index."""
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    reg["updated_at"] = now_iso()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".registry-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(reg, fh, indent=2, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def upsert_repo(entry: dict[str, Any]) -> dict[str, Any]:
    reg = load_registry()
    name = safe_dir_name(entry["name"])
    entry = dict(entry)
    entry["name"] = name
    entry["updated_at"] = now_iso()
    previous = reg["repos"].get(name) or {}
    entry.setdefault("installed_at", previous.get("installed_at") or now_iso())
    reg["repos"][name] = entry
    save_registry(reg)
    return entry


def remove_repo_entry(name: str) -> bool:
    reg = load_registry()
    name = safe_dir_name(name)
    existed = name in reg["repos"]
    reg["repos"].pop(name, None)
    for skill_id in [s for s, v in reg["skills"].items()
                     if v.get("repo") == name]:
        reg["skills"].pop(skill_id, None)
    save_registry(reg)
    return existed


def upsert_skill(skill: dict[str, Any]) -> dict[str, Any]:
    reg = load_registry()
    skill = dict(skill)
    skill["skill_id"] = safe_dir_name(skill["skill_id"])
    skill["updated_at"] = now_iso()
    previous = reg["skills"].get(skill["skill_id"]) or {}
    for key in ("enabled", "status"):
        skill.setdefault(key, previous.get(key))
    if skill.get("enabled") is None:
        skill["enabled"] = previous.get("enabled", True)
    if not skill.get("status"):
        skill["status"] = previous.get("status", "READY")
    reg["skills"][skill["skill_id"]] = skill
    save_registry(reg)
    return skill


def set_skill_state(skill_id: str, *, enabled: bool | None = None,
                    status: str | None = None,
                    detail: str | None = None) -> dict[str, Any] | None:
    reg = load_registry()
    skill_id = safe_dir_name(skill_id)
    skill = reg["skills"].get(skill_id)
    if not skill:
        return None
    if enabled is not None:
        skill["enabled"] = bool(enabled)
    if status is not None:
        skill["status"] = str(status)
    if detail is not None:
        skill["detail"] = str(detail)[:400]
    skill["updated_at"] = now_iso()
    save_registry(reg)
    return skill


# ── manifests on disk ────────────────────────────────────────────────────────

def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def load_repo_manifest(name: str) -> dict[str, Any] | None:
    try:
        data = json.loads(repo_manifest_path(name).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def load_skill_manifest(name: str) -> dict[str, Any] | None:
    """Load the skill manifest for a repo (by repo name)."""
    try:
        data = json.loads(skill_manifest_path(name).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None
