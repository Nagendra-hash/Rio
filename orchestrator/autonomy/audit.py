"""
orchestrator/autonomy/audit.py — the append-only decision log for Phase 20.

An autonomy engine that acts without a witness is unaccountable. Every gate
the engine evaluates — grant, downgrade, deny, stop, approval, recovery —
writes one JSONL line here BEFORE the action it describes is taken. The log
is append-only: nothing in this codebase rewrites or prunes it, and a
failed write never breaks the operation it audits.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from memory.config_manager import BASE_DIR

AUDIT_FILE = BASE_DIR / "memory" / "autonomy" / "audit.jsonl"

_lock = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class AutonomyAudit:
    """Append-only JSONL audit trail with an in-memory tail for inspection."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else AUDIT_FILE

    def record(self, event: str, *, run_id: str = "", workflow: str = "",
               actor: str = "system", detail: dict | None = None) -> None:
        """One decision, one line. Never raises: auditing must not break
        the operation it witnesses."""
        row = {
            "ts": _now(),
            "event": str(event),
            "run_id": str(run_id),
            "workflow": str(workflow),
            "actor": str(actor),
        }
        if detail:
            row["detail"] = detail
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with _lock:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def tail(self, limit: int = 50, event: str | None = None) -> list[dict]:
        """The most recent rows, oldest first — for the dashboard and tests."""
        rows: list[dict] = []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if event and r.get("event") != event:
                        continue
                    rows.append(r)
        except FileNotFoundError:
            return []
        return rows[-max(1, int(limit)):]
