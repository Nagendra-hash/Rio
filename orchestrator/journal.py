"""
orchestrator/journal.py — orchestration logging + audit trail.

Two layers:
  * standard Python logging ("jarvis.orchestrator") for operators;
  * an append-only JSONL journal (memory/orchestration/journal.jsonl by
    default) so any routing decision can be reconstructed later. The journal
    is best-effort: a write failure degrades to logging only and never
    breaks a user turn.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any


def get_logger() -> logging.Logger:
    return logging.getLogger("jarvis.orchestrator")


class OrchestrationJournal:
    """Append-only JSONL audit log. stdlib-only, thread-safe, never throws."""

    def __init__(self, path: str | Path | None = None, enabled: bool = True) -> None:
        if path is None:
            path = self._default_path()
        self.path = Path(path)
        self.enabled = bool(enabled)
        self._lock = threading.Lock()

    @staticmethod
    def _default_path() -> Path:
        import os

        base = os.environ.get("JARVIS_HOME", "").strip()
        root = (
            Path(base).expanduser() if base else Path(__file__).resolve().parent.parent
        )
        return root / "memory" / "orchestration" / "journal.jsonl"

    def record(self, event: str, task_id: str = "", **fields: Any) -> None:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
            + f".{int(time.time() * 1000) % 1000:03d}Z",
            "event": event,
        }
        if task_id:
            entry["task_id"] = task_id
        entry.update(fields)
        line = json.dumps(entry, ensure_ascii=False, default=str)
        if not self.enabled:
            get_logger().info("journal(off) %s", line)
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception as exc:  # degrade, never raise
            get_logger().warning("journal write failed: %s", exc)

    # ── convenience event helpers ─────────────────────────────────────────
    def task_received(self, task_id: str, text: str, channel: str) -> None:
        self.record("task.received", task_id, channel=channel, text=text[:200])

    def classified(
        self, task_id: str, task_type: str, confidence: float, reason: str
    ) -> None:
        self.record(
            "task.classified",
            task_id,
            task_type=task_type,
            confidence=confidence,
            reason=reason,
        )

    def routed(self, task_id: str, decision: "Any") -> None:
        self.record(
            "task.routed",
            task_id,
            agent=decision.chosen_agent,
            fallback=decision.to_fallback,
            candidates=decision.candidates,
            reason=decision.reason,
        )

    def agent_result(
        self, task_id: str, agent: str, status: str, detail: str = ""
    ) -> None:
        self.record(
            "task.agent_result",
            task_id,
            agent=agent,
            status=status,
            detail=detail[:200],
        )

    def fallback(self, task_id: str, reason: str) -> None:
        self.record(
            "task.fallback", task_id, reason=reason[:200], agent="gemini_assistant"
        )

    def error(self, task_id: str, where: str, error: str) -> None:
        self.record("task.error", task_id, where=where, error=error[:300])

    def approval_requested(self, task_id: str, agent: str, summary: str) -> None:
        self.record("approval.requested", task_id, agent=agent, summary=summary[:200])

    def approval_resolved(self, task_id: str, approved: bool, by: str) -> None:
        self.record("approval.resolved", task_id, approved=approved, by=by)
