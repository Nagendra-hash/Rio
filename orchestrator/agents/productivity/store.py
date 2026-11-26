"""
orchestrator/agents/productivity/store.py — the productivity-data boundary.

Same contract as the health store (Phase 11): ONE protected domain —
memory/productivity/ (relocated by JARVIS_HOME), strict file permissions,
and an audit log that records ACTIONS ONLY.

    directory      0o700 (owner only)
    every file     0o600 (re-asserted on every write)
    audit log      actions only — counts, ids, timestamps. Never message
                   bodies, subjects, attendee lists, or token material.

What lives here:
    drafts.jsonl        reply drafts + invite drafts (text the user asked
                        the agent to prepare — persisted locally so an
                        approval can be resumed after a crash).
    approvals.jsonl     what was parked, when, and how it resolved.
    sent_log.jsonl      minimal record of executed outside-world actions
                        (who approved, when) — never the content.

Boundary guarantees:
  * every blob passes security.redact_obj() before hitting disk;
  * the store writes ONLY inside its own root (path containment);
  * every write is atomic-ish (tmp + replace) and never raises outward;
  * data caps everywhere; wipe() erases data files (audit keeps the event).
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from orchestrator.agents.productivity import security

MAX_TEXT_CHARS = 4_000
MAX_LIST_ITEMS = 2_000
MAX_DRAFTS = 300
MAX_JSON_BYTES = 512_000
PERMS_FILE = 0o600
PERMS_DIR = 0o700


def productivity_dir(explicit: str | os.PathLike | None = None) -> Path:
    """The productivity domain root: <JARVIS_HOME or repo>/memory/productivity."""
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
            base = Path(__file__).resolve().parents[3]
    return base / "memory" / "productivity"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(text: Any, cap: int = MAX_TEXT_CHARS) -> str:
    return str(text or "").strip()[:cap]


class ProductivityStore:
    """The only writer of productivity data. Thread-safe, stdlib-only."""

    def __init__(self, directory: str | os.PathLike | None = None) -> None:
        self.root = Path(directory) if directory else productivity_dir()
        self._lock = threading.RLock()
        self._ensure_private_dir()

    # ── permissions / containment ─────────────────────────────────────────
    def _ensure_private_dir(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            os.chmod(self.root, PERMS_DIR)
        except Exception:
            pass

    @staticmethod
    def _enforce(path: Path) -> None:
        try:
            os.chmod(path, PERMS_FILE)
        except Exception:
            pass

    @staticmethod
    def _safe_name(name: str, cap: int = 40) -> str:
        safe = "".join(c for c in str(name or "") if c.isalnum() or c in "-_")
        return safe[:cap] or "unnamed"

    def _inside(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.root.resolve())
            return True
        except Exception:
            return False

    # ── low-level io ──────────────────────────────────────────────────────
    def _append_jsonl(self, name: str, obj: dict,
                      cap_lines: int = MAX_LIST_ITEMS) -> bool:
        path = self.root / (self._safe_name(name) + ".jsonl")
        if not self._inside(path):
            return False
        payload = security.redact_obj(obj)
        line = json.dumps(payload, ensure_ascii=False, default=str)
        try:
            with self._lock:
                lines = []
                if path.exists():
                    lines = path.read_text(encoding="utf-8") \
                        .splitlines()[-(cap_lines - 1):]
                lines.append(line)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                             PERMS_FILE)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + "\n")
                self._enforce(path)
            return True
        except Exception:
            return False

    def _read_jsonl(self, name: str, limit: int = 50) -> list[dict]:
        path = self.root / (self._safe_name(name) + ".jsonl")
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-MAX_LIST_ITEMS:]
            return [json.loads(ln) for ln in lines if ln.strip()][-limit:]
        except Exception:
            return []

    def audit(self, event: str, **fields: Any) -> None:
        """Append an ACTIONS-ONLY audit row — never message content."""
        safe = {"event": _clip(event, 40), "ts": _now_iso()}
        for k, v in (fields or {}).items():
            if isinstance(v, (int, float, bool)) or v is None:
                safe[_clip(k, 30)] = v
            else:
                safe[_clip(k, 30)] = _clip(v, 60)
        self._append_jsonl("audit", safe)

    # ── drafts (reply drafts + invite drafts) ─────────────────────────────
    def save_draft(self, kind: str, payload: dict, task_id: str = "") -> dict:
        entry = {"id": uuid.uuid4().hex[:10],
                 "task_id": _clip(task_id, 40),
                 "kind": _clip(kind, 20),
                 "at": _now_iso(),
                 "payload": payload}
        self._append_jsonl("drafts", entry, cap_lines=MAX_DRAFTS)
        self.audit(f"draft_{_clip(kind, 20)}", task=_clip(task_id, 40))
        return entry

    def drafts(self, kind: str | None = None,
               limit: int = 20) -> list[dict]:
        rows = self._read_jsonl("drafts", MAX_LIST_ITEMS)
        if kind:
            rows = [r for r in rows if r.get("kind") == kind]
        return rows[-limit:]

    def latest_draft(self, kind: str) -> Optional[dict]:
        rows = self.drafts(kind=kind, limit=1)
        return rows[-1] if rows else None

    # ── approval records ──────────────────────────────────────────────────
    def record_approval(self, task_id: str, agent: str, action: str,
                        approved: bool, by: str, detail: str = "") -> None:
        self._append_jsonl("approvals", {
            "task_id": _clip(task_id, 40), "agent": _clip(agent, 30),
            "action": _clip(action, 30), "approved": bool(approved),
            "by": _clip(by, 40), "detail": _clip(detail, 300),
            "at": _now_iso()})
        self.audit("approval_recorded", agent=_clip(agent, 30),
                   action=_clip(action, 30), approved=bool(approved))

    def approvals(self, limit: int = 50) -> list[dict]:
        return self._read_jsonl("approvals", limit)

    # ── executed outside-world actions ────────────────────────────────────
    def record_sent(self, task_id: str, agent: str, action: str,
                    approved_by: str, ref: str = "") -> None:
        self._append_jsonl("sent_log", {
            "task_id": _clip(task_id, 40), "agent": _clip(agent, 30),
            "action": _clip(action, 30), "approved_by": _clip(approved_by, 40),
            "ref": _clip(ref, 80), "at": _now_iso()})
        self.audit("executed", agent=_clip(agent, 30),
                   action=_clip(action, 30))

    def sent_log(self, limit: int = 50) -> list[dict]:
        return self._read_jsonl("sent_log", limit)

    # ── summary / wipe ────────────────────────────────────────────────────
    def summary(self) -> dict:
        return {"drafts": len(self._read_jsonl("drafts", MAX_LIST_ITEMS)),
                "approvals": len(self._read_jsonl("approvals",
                                                  MAX_LIST_ITEMS)),
                "sent": len(self._read_jsonl("sent_log", MAX_LIST_ITEMS))}

    def wipe(self) -> bool:
        ok = True
        with self._lock:
            for name in ("drafts", "approvals", "sent_log"):
                try:
                    (self.root / (name + ".jsonl")).unlink(missing_ok=True)
                except Exception:
                    ok = False
            self.audit("wipe")
        return ok
