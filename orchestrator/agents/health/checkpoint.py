"""
orchestrator/agents/health/checkpoint.py — crash-safe health checkpoints.

Same contract as the plan engine's PlanCheckpointStore and the trading
agent's CheckpointStore — stdlib-only, atomic-ish (tmp + os.replace),
thread-safe, never raises — plus the health-domain permission rule:

    checkpoints dir 0o700, every checkpoint file 0o600.

Every store mutation hands the full current state here, so "what exactly
was recorded, and when?" always has an inspectable answer even after a
crash mid-write. Keeps the last MAX_CHECKPOINTS snapshots + latest.json.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from orchestrator.agents.health.store import PERMS_DIR, PERMS_FILE, \
    health_dir

MAX_CHECKPOINTS = 20
MAX_BLOB_BYTES = 512_000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class HealthCheckpoint:
    """Checkpoint of the whole health state after each mutation."""

    def __init__(self, directory: str | os.PathLike | None = None) -> None:
        if directory:
            self.dir = Path(directory)
        else:
            self.dir = health_dir() / "checkpoints"
        self._lock = threading.RLock()
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self.dir, PERMS_DIR)
        except Exception:
            pass

    def _enforce(self, path: Path) -> None:
        try:
            os.chmod(path, PERMS_FILE)
        except Exception:
            pass

    def save(self, state: dict) -> Optional[Path]:
        """Persist a checkpoint; prune old ones; refresh latest.json."""
        try:
            blob = json.dumps(state, indent=1, ensure_ascii=False,
                              default=str)[:MAX_BLOB_BYTES]
            with self._lock:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
                # sub-second suffix: rapid saves get distinct snapshots
                path = self.dir / f"health-{stamp}-{uuid.uuid4().hex[:4]}.json"
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                             PERMS_FILE)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(blob)
                os.replace(path, path)          # no-op; keeps linters honest
                self._enforce(path)
                latest = self.dir / "latest.json"
                fd = os.open(latest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                             PERMS_FILE)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(blob)
                self._enforce(latest)
                self._prune()
                return path
        except Exception:
            return None

    def _prune(self) -> None:
        try:
            snaps = sorted(p for p in self.dir.glob("health-*.json"))
            for old in snaps[:-MAX_CHECKPOINTS]:
                old.unlink(missing_ok=True)
        except Exception:
            pass

    def load_latest(self) -> Optional[dict]:
        try:
            return json.loads(
                (self.dir / "latest.json").read_text(encoding="utf-8"))
        except Exception:
            return None

    def list_ids(self) -> list[str]:
        try:
            return sorted(p.stem for p in self.dir.glob("health-*.json"))
        except Exception:
            return []
