"""
orchestrator/checkpoint.py — crash-safe plan checkpoints for the
Plan-and-Execute engine.

Every step transition is persisted to memory/plan_engine/<plan_id>.json so
that:
  * a parked approval can be approved/denied later without losing the plan;
  * a crash mid-plan can resume from the last verified step;
  * "what exactly is this plan about to do?" always has an inspectable
    answer (the checkpoint IS the plan, with runtime status).

Same pattern as the trading agent's CheckpointStore: stdlib-only, atomic-ish
(temp file + replace), thread-safe, never raises on save/load — a checkpoint
failure must not fail a task.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Optional


def checkpoint_dir(explicit: str | os.PathLike | None = None) -> Path:
    """Writable dir, relocated by JARVIS_HOME like every writable path."""
    if explicit:
        return Path(explicit)
    home = os.environ.get("JARVIS_HOME", "").strip()
    if home:
        base = Path(home).expanduser().resolve()
    else:
        try:
            from memory.config_manager import BASE_DIR
            base = Path(BASE_DIR)
        except Exception:
            base = Path(__file__).resolve().parents[1]
    return base / "memory" / "plan_engine"


class PlanCheckpointStore:
    """JSON-file checkpoint per plan id. Thread-safe, atomic-ish writes."""

    def __init__(self, directory: str | os.PathLike | None = None) -> None:
        self.dir = Path(directory) if directory else checkpoint_dir()
        self._lock = threading.RLock()

    # ── path safety ──────────────────────────────────────────────────────
    @staticmethod
    def _safe_name(plan_id: str) -> str:
        # alnum + hyphen + underscore only; dots excluded so a crafted id
        # can never smuggle '..' or an extension past the sanitizer.
        safe = "".join(c for c in str(plan_id) if c.isalnum() or c in "-_")
        return safe[:120] or "unnamed"

    def _path(self, plan_id: str) -> Path:
        return self.dir / f"{self._safe_name(plan_id)}.json"

    # ── crud ─────────────────────────────────────────────────────────────
    def save(self, plan_dict: dict) -> Path:
        """Persist a plan dict (Plan.to_dict()). Returns the path written."""
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            path = self._path(str(plan_dict.get("plan_id") or "unnamed"))
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(plan_dict, indent=1, default=str,
                                      ensure_ascii=False)[:1_000_000],
                           encoding="utf-8")
            tmp.replace(path)
            return path

    def load(self, plan_id: str) -> Optional[dict]:
        with self._lock:
            try:
                return json.loads(self._path(plan_id).read_text(encoding="utf-8"))
            except Exception:
                return None

    def delete(self, plan_id: str) -> bool:
        with self._lock:
            try:
                self._path(plan_id).unlink()
                return True
            except Exception:
                return False

    def list_ids(self) -> list[str]:
        with self._lock:
            if not self.dir.exists():
                return []
            return sorted(p.stem for p in self.dir.glob("*.json"))
