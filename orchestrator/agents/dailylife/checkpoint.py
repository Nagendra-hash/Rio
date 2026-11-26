"""
orchestrator/agents/dailylife/checkpoint.py — crash-safe checkpoints.

The SAME contract as the productivity/health/trading checkpoints: stdlib
only, atomic-ish (tmp + os.replace), thread-safe, never raises, keeps the
last MAX_CHECKPOINTS snapshots + latest.json, with 0o600 files in a 0o700
dir and redaction before serialization.

This is a subclass of the productivity checkpoint over the daily-life
directory — one implementation of the crash-safety rules, two domains.
"""
from __future__ import annotations

import os
from pathlib import Path

from orchestrator.agents.dailylife.store import dailylife_dir
from orchestrator.agents.productivity.checkpoint import (
    MAX_CHECKPOINTS as MAX_SNAPSHOTS,
    ProductivityCheckpoint)


class DailyLifeCheckpoint(ProductivityCheckpoint):
    """Crash-safe snapshots for the daily-life specialists.

    save(state)          — timestamped snapshot + latest.json (audit style).
    save_for(task_id, …) — ONE file per pending item keyed by task_id.
    """

    def __init__(self, directory: str | os.PathLike | None = None) -> None:
        # ProductivityCheckpoint.__init__ handles mkdir/chmod/lock; we only
        # redirect the default directory into the daily-life domain.
        super().__init__(directory or (dailylife_dir() / "checkpoints"))

    # Snapshot filenames get a domain-distinct prefix via save(); the
    # inherited _prune only knows the "prod-" prefix, so prune our own.
    def save(self, state: dict):
        try:
            import datetime as _dt
            import uuid as _uuid
            stamp = _dt.datetime.now(_dt.timezone.utc) \
                .strftime("%Y%m%dT%H%M%S")
            path = self.dir / f"dailylife-{stamp}-{_uuid.uuid4().hex[:4]}.json"
            if self._write(path, state):
                self._write(self.dir / "latest.json", state)
                self._prune()
                return path
            return None
        except Exception:
            return None

    def _prune(self) -> None:
        try:
            snaps = sorted(p for p in self.dir.glob("dailylife-*.json"))
            for old in snaps[:-MAX_SNAPSHOTS]:
                old.unlink(missing_ok=True)
        except Exception:
            pass

    def list_ids(self) -> list[str]:
        try:
            return sorted(p.stem for p in self.dir.glob("dailylife-*.json"))
        except Exception:
            return []
