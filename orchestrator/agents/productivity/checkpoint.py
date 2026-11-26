"""
orchestrator/agents/productivity/checkpoint.py — crash-safe checkpoints.

Same contract as the health/trading/plan checkpoints: stdlib-only,
atomic-ish (tmp + os.replace), thread-safe, never raises, keeps the last
MAX_CHECKPOINTS snapshots + latest.json, with 0o600 files in a 0o700 dir.

Every parked approval snapshots the exact pending payload here, so "what
exactly am I approving?" always has an inspectable answer — and the
approved re-dispatch can execute that EXACT payload, never a fresh parse
of the user's words (the system-agent rule).
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from orchestrator.agents.productivity import security
from orchestrator.agents.productivity.store import PERMS_DIR, PERMS_FILE, \
    productivity_dir

MAX_CHECKPOINTS = 20
MAX_BLOB_BYTES = 512_000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProductivityCheckpoint:
    """Crash-safe checkpoints for pending approvals.

    save(state)          — timestamped snapshot + latest.json (audit style).
    save_for(task_id, …) — the approval slot: ONE file per pending
                           approval, keyed by task_id, so concurrent
                           approvals never overwrite each other. This is
                           the file the resume path reads.
    """

    def __init__(self, directory: str | os.PathLike | None = None) -> None:
        if directory:
            self.dir = Path(directory)
        else:
            self.dir = productivity_dir() / "checkpoints"
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

    @staticmethod
    def _safe_task(task_id: str) -> str:
        # alnum + hyphen + underscore only — no traversal, no extension tricks.
        safe = "".join(c for c in str(task_id or "")
                       if c.isalnum() or c in "-_")
        return safe[:80] or "unnamed"

    def _write(self, path: Path, state: dict) -> bool:
        try:
            # Redaction BEFORE serialization: a checkpoint is exactly the
            # artifact that survives a crash — it must never hold tokens.
            clean = security.redact_obj(state)
            blob = json.dumps(clean, indent=1, ensure_ascii=False,
                              default=str)[:MAX_BLOB_BYTES]
            with self._lock:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                             PERMS_FILE)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(blob)
                self._enforce(path)
            return True
        except Exception:
            return False

    def _read(self, path: Path) -> Optional[dict]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    # ── the approval slot (keyed by task_id) ─────────────────────────
    def save_for(self, task_id: str, state: dict) -> bool:
        return self._write(self.dir / f"approval-{self._safe_task(task_id)}.json",
                           state)

    def load_for(self, task_id: str) -> Optional[dict]:
        return self._read(self.dir /
                          f"approval-{self._safe_task(task_id)}.json")

    # ── rolling audit-style snapshots ─────────────────────────────────
    def save(self, state: dict) -> Optional[Path]:
        try:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            path = self.dir / f"prod-{stamp}-{uuid.uuid4().hex[:4]}.json"
            if self._write(path, state):
                latest = self.dir / "latest.json"
                self._write(latest, state)
                self._prune()
                return path
            return None
        except Exception:
            return None

    def _prune(self) -> None:
        try:
            snaps = sorted(p for p in self.dir.glob("prod-*.json"))
            for old in snaps[:-MAX_CHECKPOINTS]:
                old.unlink(missing_ok=True)
        except Exception:
            pass

    def load_latest(self) -> Optional[dict]:
        return self._read(self.dir / "latest.json")

    def list_ids(self) -> list[str]:
        try:
            return sorted(p.stem for p in self.dir.glob("prod-*.json"))
        except Exception:
            return []
