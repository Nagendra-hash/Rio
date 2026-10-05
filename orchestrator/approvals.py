"""
orchestrator/approvals.py — human approval state.

Some capabilities must not run without a human "yes" (Phase 2: trading
execution, anything destructive). An agent signals NEEDS_APPROVAL; the
orchestrator parks a pending approval here and only lets the task proceed
after an explicit resolve(approved=True). Unresolved approvals expire — a
stale "yes" can never fire a forgotten task. No approval is ever
auto-granted anywhere in this package.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from orchestrator.contracts import ApprovalState

DEFAULT_TTL_S = 30 * 60          # a pending approval dies after 30 minutes


class ApprovalGate:
    """Thread-safe store of pending human-approval decisions."""

    def __init__(self, ttl_s: float = DEFAULT_TTL_S,
                 clock=time.time) -> None:
        self._pending: dict[str, ApprovalState] = {}
        self._ttl = float(ttl_s)
        self._clock = clock
        self._lock = threading.RLock()

    # ── lifecycle ─────────────────────────────────────────────────────────
    def request(self, task_id: str, agent: str, summary: str) -> ApprovalState:
        state = ApprovalState(task_id=task_id, agent=agent, summary=summary)
        state.created_monotonic = self._clock()   # expiry uses OUR clock
        with self._lock:
            self._prune()
            self._pending[task_id] = state
        return state

    def get(self, task_id: str) -> Optional[ApprovalState]:
        with self._lock:
            self._prune()
            return self._pending.get(task_id)

    def resolve(self, task_id: str, approved: bool,
                by: str = "user") -> bool:
        """Resolve a pending approval. Returns True when a live one existed."""
        with self._lock:
            self._prune()
            state = self._pending.pop(task_id, None)
        if state is None:
            return False
        state.resolved = True
        state.approved = approved
        state.resolved_by = by
        state.resolved_at = time.time()
        return True

    def is_pending(self, task_id: str) -> bool:
        return self.get(task_id) is not None

    # ── expiry ────────────────────────────────────────────────────────────
    def _prune(self) -> None:
        now = self._clock()
        expired = [tid for tid, s in self._pending.items()
                   if now - s.created_monotonic > self._ttl]
        for tid in expired:
            del self._pending[tid]

    def __len__(self) -> int:
        with self._lock:
            self._prune()
            return len(self._pending)
