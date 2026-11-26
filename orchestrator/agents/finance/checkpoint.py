"""
orchestrator/agents/finance/checkpoint.py — crash-safe checkpoints.

Same contract as productivity/dailylife/trading: stdlib-only, atomic-ish,
thread-safe, never raises, prune to MAX_CHECKPOINTS, 0600 files in a
0700 dir, redaction before serialization. Subclasses the productivity
implementation over the FINANCE vault — one crash-safety implementation,
one more domain.

The checkpoint matters more here than anywhere else: every parked
approval holds the EXACT payload the human approved (the import rows,
the edit, the delete) — the resume path executes that payload, never a
fresh parse of the user's words.
"""
from __future__ import annotations

import os
from pathlib import Path

from orchestrator.agents.finance.store import finance_dir
from orchestrator.agents.productivity.checkpoint import (
    MAX_CHECKPOINTS as MAX_SNAPSHOTS, ProductivityCheckpoint)


class FinanceCheckpoint(ProductivityCheckpoint):

    def __init__(self, directory: str | os.PathLike | None = None) -> None:
        super().__init__(directory or (finance_dir() / "checkpoints"))

    def save(self, state: dict):
        try:
            import datetime as _dt
            import uuid as _uuid
            stamp = _dt.datetime.now(_dt.timezone.utc) \
                .strftime("%Y%m%dT%H%M%S")
            path = self.dir / f"finance-{stamp}-{_uuid.uuid4().hex[:4]}.json"
            if self._write(path, state):
                self._write(self.dir / "latest.json", state)
                self._prune()
                return path
            return None
        except Exception:
            return None

    def _prune(self) -> None:
        try:
            snaps = sorted(p for p in self.dir.glob("finance-*.json"))
            for old in snaps[:-MAX_SNAPSHOTS]:
                old.unlink(missing_ok=True)
        except Exception:
            pass

    def list_ids(self) -> list[str]:
        try:
            return sorted(p.stem for p in self.dir.glob("finance-*.json"))
        except Exception:
            return []
