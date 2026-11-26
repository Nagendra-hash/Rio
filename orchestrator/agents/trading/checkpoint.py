"""
orchestrator/agents/trading/checkpoint.py — crash-safe task checkpoints.

Same pattern as the coding agent's checkpoints (memory/coding_agent/<id>.json):
every pipeline run is journaled stage-by-stage to
memory/trading_agent/<task_id>.json so an approval parked at HUMAN APPROVAL
can be resumed later without re-fetching, re-analyzing, or re-deciding —
and so "what exactly was this approval for?" always has an answer.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path


def checkpoint_dir() -> Path:
    home = os.environ.get("JARVIS_HOME", "").strip()
    if home:
        base = Path(home).expanduser().resolve()
    else:
        from memory.config_manager import BASE_DIR
        base = BASE_DIR
    return base / "memory" / "trading_agent"


class CheckpointStore:
    """JSON-file checkpoint per task id. Thread-safe, atomic-ish writes."""

    def __init__(self, directory: Path | None = None) -> None:
        self.dir = Path(directory) if directory else checkpoint_dir()
        self._lock = threading.RLock()

    def _path(self, task_id: str) -> Path:
        # alnum + hyphen + underscore only; dots are excluded so a crafted
        # task_id can never smuggle '..' or an extension past the sanitizer.
        safe = "".join(c for c in task_id if c.isalnum() or c in "-_")
        return self.dir / f"{safe or 'unnamed'}.json"

    def save(self, task_id: str, data: dict) -> None:
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            p = self._path(task_id)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, default=str),
                           encoding="utf-8")
            tmp.replace(p)

    def load(self, task_id: str) -> dict | None:
        with self._lock:
            try:
                return json.loads(self._path(task_id).read_text(encoding="utf-8"))
            except Exception:
                return None

    def delete(self, task_id: str) -> bool:
        with self._lock:
            try:
                self._path(task_id).unlink()
                return True
            except Exception:
                return False

    def list_ids(self) -> list[str]:
        with self._lock:
            if not self.dir.exists():
                return []
            return sorted(p.stem for p in self.dir.glob("*.json"))


def new_stage_trace(stage: str, ok: bool, detail: str = "") -> dict:
    return {"stage": stage, "ok": ok, "detail": detail[:200],
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
