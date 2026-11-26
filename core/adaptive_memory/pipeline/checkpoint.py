"""
core/adaptive_memory/pipeline/checkpoint.py — CheckpointManager for resumable processing.

DESIGN
    Long-running pipeline stages (ingest, embed, index, etc.) can fail mid-way.
    This module provides a generic, thread-safe checkpoint system so any stage
    can resume from the last successful step instead of restarting from zero.

STATE FILE (one per pipeline run, under memory/checkpoints/)
    {
        "run_id": "uuid",
        "pipeline": "rag_ingest",
        "status": "running | completed | failed",
        "created_at": "ISO timestamp",
        "updated_at": "ISO timestamp",
        "steps": {
            "extract": {"status": "completed", "started_at": "...", "completed_at": "...", "output": {...}},
            "chunk":   {"status": "completed", "started_at": "...", "completed_at": "...", "output": {...}},
            "embed":   {"status": "in_progress", "started_at": "...", "completed_at": null, "output": null},
            "index":   {"status": "pending", "started_at": null, "completed_at": null, "output": null}
        },
        "meta": {...}  # free-form context (doc_id, collection, file_path, etc.)
    }

USAGE
    cm = CheckpointManager("rag_ingest", run_id="abc-123", meta={"doc_id": "xyz"})
    cm.start_step("extract")
    # ... do extraction ...
    cm.complete_step("extract", output={"pages": 42})
    cm.start_step("chunk")
    # ... if crash here, next run resumes at "chunk" ...
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from memory.config_manager import BASE_DIR

CHECKPOINT_DIR = BASE_DIR / "memory" / "checkpoints"
_checkpoint_lock = threading.RLock()


@dataclass
class Checkpoint:
    """Lightweight checkpoint record for a single pipeline step."""
    step: str
    status: str  # pending | in_progress | completed | failed | skipped
    started_at: str | None = None
    completed_at: str | None = None
    output: dict | None = None
    error: str | None = None


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ensure_checkpoint_dir() -> None:
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)


def _checkpoint_path(run_id: str) -> Path:
    return CHECKPOINT_DIR / f"{run_id}.json"


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


class CheckpointManager:
    """Thread-safe checkpoint manager for resumable pipeline execution."""

    VALID_STATUSES = ("pending", "in_progress", "completed", "failed", "skipped")

    def __init__(
        self,
        pipeline: str,
        run_id: str | None = None,
        meta: dict | None = None,
        steps: list[str] | None = None,
    ) -> None:
        """
        Initialize or resume a checkpoint.

        Args:
            pipeline: Pipeline name (e.g., "rag_ingest", "video_render", "rag_learn_cycle").
            run_id: Unique run identifier. Auto-generated if not provided.
            meta: Free-form metadata carried across the run (doc_id, collection, etc.).
            steps: Ordered list of step names. If provided, validates step order.
        """
        self.pipeline = pipeline
        self.run_id = run_id or str(uuid.uuid4())[:8]
        self._steps_order = steps or []
        self._state: dict = {}
        self._load_or_create(meta or {})

    def _load_or_create(self, meta: dict) -> None:
        with _checkpoint_lock:
            path = _checkpoint_path(self.run_id)
            existing = _read_json(path)
            if existing and isinstance(existing, dict) and existing.get("pipeline") == self.pipeline:
                self._state = existing
                # Merge any new meta keys
                if meta:
                    self._state.setdefault("meta", {}).update(meta)
                self._state["updated_at"] = _now_iso()
                _write_json_atomic(path, self._state)
            else:
                self._state = {
                    "run_id": self.run_id,
                    "pipeline": self.pipeline,
                    "status": "running",
                    "created_at": _now_iso(),
                    "updated_at": _now_iso(),
                    "steps": {},
                    "meta": meta,
                }
                _write_json_atomic(path, self._state)

    def _save(self) -> None:
        with _checkpoint_lock:
            self._state["updated_at"] = _now_iso()
            _write_json_atomic(_checkpoint_path(self.run_id), self._state)

    # ── step control ────────────────────────────────────────────────────────

    def start_step(self, step: str, output: dict | None = None) -> None:
        """Mark a step as in_progress. Fails if step order is violated."""
        self._validate_step_order(step)
        with _checkpoint_lock:
            steps = self._state.setdefault("steps", {})
            if step not in steps:
                steps[step] = {"status": "pending", "started_at": None, "completed_at": None, "output": None}
            step_data = steps[step]
            if step_data["status"] == "completed":
                return  # idempotent
            step_data["status"] = "in_progress"
            step_data["started_at"] = _now_iso()
            if output is not None:
                step_data["output"] = output
            self._state["status"] = "running"
            self._save()

    def complete_step(self, step: str, output: dict | None = None) -> None:
        """Mark a step as completed with optional output data."""
        with _checkpoint_lock:
            steps = self._state.setdefault("steps", {})
            if step not in steps:
                steps[step] = {"status": "pending", "started_at": None, "completed_at": None, "output": None}
            step_data = steps[step]
            step_data["status"] = "completed"
            step_data["completed_at"] = _now_iso()
            if output is not None:
                step_data["output"] = output
            self._save()

    def fail_step(self, step: str, error: str | None = None) -> None:
        """Mark a step as failed. Pipeline status becomes failed."""
        with _checkpoint_lock:
            steps = self._state.setdefault("steps", {})
            if step not in steps:
                steps[step] = {"status": "pending", "started_at": None, "completed_at": None, "output": None}
            step_data = steps[step]
            step_data["status"] = "failed"
            step_data["completed_at"] = _now_iso()
            if error:
                step_data["error"] = str(error)[:500]
            self._state["status"] = "failed"
            self._save()

    def skip_step(self, step: str, reason: str = "") -> None:
        """Mark a step as skipped (e.g., conditional step not needed)."""
        with _checkpoint_lock:
            steps = self._state.setdefault("steps", {})
            if step not in steps:
                steps[step] = {"status": "pending", "started_at": None, "completed_at": None, "output": None}
            step_data = steps[step]
            step_data["status"] = "skipped"
            step_data["completed_at"] = _now_iso()
            if reason:
                step_data["skip_reason"] = reason
            self._save()

    def get_step_status(self, step: str) -> str:
        """Return step status: pending | in_progress | completed | failed | skipped."""
        return self._state.get("steps", {}).get(step, {}).get("status", "pending")

    def get_step_output(self, step: str) -> dict | None:
        """Return the output dict saved for a completed step."""
        return self._state.get("steps", {}).get(step, {}).get("output")

    def get_completed_steps(self) -> list[str]:
        """Return list of step names that are completed, in order."""
        return [
            s for s, data in self._state.get("steps", {}).items()
            if data.get("status") == "completed"
        ]

    def get_failed_step(self) -> str | None:
        """Return the first failed step name, or None."""
        for s, data in self._state.get("steps", {}).items():
            if data.get("status") == "failed":
                return s
        return None

    def get_next_pending_step(self) -> str | None:
        """Return the first step that is not completed/skipped, respecting step order."""
        if self._steps_order:
            for s in self._steps_order:
                status = self.get_step_status(s)
                if status not in ("completed", "skipped"):
                    return s
        # Fallback: any pending/in_progress step
        for s, data in self._state.get("steps", {}).items():
            if data.get("status") in ("pending", "in_progress"):
                return s
        return None

    # ── pipeline status ─────────────────────────────────────────────────────

    def mark_completed(self) -> None:
        """Mark the entire pipeline as completed."""
        with _checkpoint_lock:
            self._state["status"] = "completed"
            self._state["completed_at"] = _now_iso()
            self._save()

    def mark_failed(self, error: str | None = None) -> None:
        """Mark the entire pipeline as failed."""
        with _checkpoint_lock:
            self._state["status"] = "failed"
            self._state["failed_at"] = _now_iso()
            if error:
                self._state["error"] = str(error)[:500]
            self._save()

    def get_status(self) -> str:
        return self._state.get("status", "running")

    def is_completed(self) -> bool:
        return self._state.get("status") == "completed"

    def is_failed(self) -> bool:
        return self._state.get("status") == "failed"

    # ── metadata ────────────────────────────────────────────────────────────

    def update_meta(self, meta: dict) -> None:
        """Merge additional metadata into the checkpoint."""
        with _checkpoint_lock:
            self._state.setdefault("meta", {}).update(meta)
            self._save()

    def get_meta(self) -> dict:
        return dict(self._state.get("meta", {}))

    def get_run_id(self) -> str:
        return self.run_id

    def get_pipeline(self) -> str:
        return self.pipeline

    # ── validation ──────────────────────────────────────────────────────────

    def _validate_step_order(self, step: str) -> None:
        if not self._steps_order:
            return
        if step not in self._steps_order:
            raise ValueError(f"Step '{step}' not in declared step order: {self._steps_order}")
        idx = self._steps_order.index(step)
        for prior in self._steps_order[:idx]:
            if self.get_step_status(prior) not in ("completed", "skipped"):
                raise RuntimeError(f"Step '{step}' cannot start: prior step '{prior}' is {self.get_step_status(prior)}")

    # ── serialization ───────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return dict(self._state)

    @classmethod
    def load(cls, run_id: str) -> CheckpointManager | None:
        """Load an existing checkpoint by run_id."""
        path = _checkpoint_path(run_id)
        data = _read_json(path)
        if not data:
            return None
        cm = cls.__new__(cls)
        cm.pipeline = data.get("pipeline", "unknown")
        cm.run_id = run_id
        cm._steps_order = []
        cm._state = data
        return cm

    @classmethod
    def list_checkpoints(cls, pipeline: str | None = None) -> list[dict]:
        """List all checkpoints, optionally filtered by pipeline."""
        _ensure_checkpoint_dir()
        results = []
        for path in CHECKPOINT_DIR.glob("*.json"):
            data = _read_json(path)
            if not data:
                continue
            if pipeline and data.get("pipeline") != pipeline:
                continue
            results.append({
                "run_id": data.get("run_id"),
                "pipeline": data.get("pipeline"),
                "status": data.get("status"),
                "created_at": data.get("created_at"),
                "updated_at": data.get("updated_at"),
                "completed_steps": len([
                    s for s, d in data.get("steps", {}).items()
                    if d.get("status") == "completed"
                ]),
                "total_steps": len(data.get("steps", {})),
            })
        results.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
        return results

    @classmethod
    def cleanup_old(cls, max_age_days: int = 30, keep_completed: int = 10) -> int:
        """Delete checkpoints older than max_age_days, keeping at least keep_completed completed runs."""
        _ensure_checkpoint_dir()
        now = time.time()
        max_age_sec = max_age_days * 86400
        removed = 0
        completed = []
        others = []
        for path in CHECKPOINT_DIR.glob("*.json"):
            data = _read_json(path)
            if not data:
                continue
            try:
                updated = datetime.fromisoformat(data.get("updated_at", "")).timestamp()
            except Exception:
                updated = 0
            if data.get("status") == "completed":
                completed.append((updated, path))
            else:
                others.append((updated, path))
        # Sort by updated time, newest first
        completed.sort(reverse=True)
        others.sort(reverse=True)
        # Keep the newest `keep_completed` completed runs
        for _, path in completed[keep_completed:]:
            try:
                path.unlink()
                removed += 1
            except Exception:
                pass
        # Remove old non-completed runs
        for updated, path in others:
            if now - updated > max_age_sec:
                try:
                    path.unlink()
                    removed += 1
                except Exception:
                    pass
        return removed


# ── convenience: run a callable with automatic checkpointing ──────────────────

def run_with_checkpoint(
    pipeline: str,
    steps: list[str],
    fn_map: dict[str, callable],
    run_id: str | None = None,
    meta: dict | None = None,
) -> dict:
    """
    Execute a multi-step pipeline with automatic checkpointing.

    Args:
        pipeline: Pipeline name.
        steps: Ordered list of step names.
        fn_map: Dict mapping step name -> callable(checkpoint_manager) -> output_dict.
        run_id: Optional existing run_id to resume.
        meta: Initial metadata.

    Returns:
        Final checkpoint state dict.
    """
    cm = CheckpointManager(pipeline, run_id=run_id, meta=meta, steps=steps)
    try:
        for step in steps:
            status = cm.get_step_status(step)
            if status == "completed":
                continue
            if status == "in_progress":
                # Previous run crashed mid-step; treat as pending to retry
                pass
            cm.start_step(step)
            try:
                output = fn_map[step](cm)
                cm.complete_step(step, output=output or {})
            except Exception as e:
                cm.fail_step(step, error=str(e))
                raise
        cm.mark_completed()
    except Exception:
        # Pipeline already marked failed in fail_step
        raise
    return cm.to_dict()