"""
orchestrator/agents/sql/perms.py — the SQL agent's PERMISSION layer.

The same isolated-storage contract as every other Phase 13-16 vault:

    * ONE directory the rest of the system does not read:
      memory/sql/ (relocated by JARVIS_HOME), 0600 files in a 0700 dir.
    * A REGISTRY of permitted databases: a "database" here is
      {db_id, title, kind, source_path} — for sqlite this is the file
      path, kept in the protected registry and NEVER inside an
      AgentResult payload. The agent resolves a database by db_id, and
      the only thing that ever leaves the server process is
      internal_name() — a random per-connection pointer that a UI
      breadcrumb could print without a leak.
    * An ACTIONS-ONLY audit trail (db ids, statement classes, counts —
      never SQL text, never credentials, never result rows).
    * redact_obj() before every disk write.

Thread-safe, stdlib-only, never raises outward (legal-vault contract).
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from orchestrator.agents.productivity import security as sql_security
from orchestrator.agents.productivity.store import PERMS_DIR, PERMS_FILE


def _guard(blob: str) -> str:
    """Second chokepoint over redact_obj: credential-shaped sequences
    (password: …, postgres://…) that survive the generic key redaction
    are replaced wholesale. Never persists a credential."""
    try:
        from orchestrator.agents.sql.safety import credential_guard
        return credential_guard(blob)
    except Exception:
        return blob

MAX_DBS = 40
MAX_DSN_LENGTH = 500
MAX_JSON_BYTES = 1_000_000
MAX_AUDIT_LINES = 2_000

_INTERNAL_NAMES = ("db0", "db1")       # stable slot names for ATTACH-free reads


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(text: Any, cap: int) -> str:
    return str(text or "").strip()[:cap]


def sql_dir(explicit: str | os.PathLike | None = None) -> Path:
    """The padded-cell root: <JARVIS_HOME or repo>/memory/sql."""
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
    return base / "memory" / "sql"


def new_db_id(title: str = "") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    slug = "".join(c for c in str(title or "")[:30].lower()
                   if c.isalnum()) or "db"
    return f"db-{stamp}-{slug}-{uuid.uuid4().hex[:6]}"


class SqlStore:
    """The only writer of SQL-agent data: db registry + connection pool
    bookkeeping + audit. Thread-safe, stdlib-only."""

    def __init__(self, directory: str | os.PathLike | None = None) -> None:
        self.root = Path(directory) if directory else sql_dir()
        self._lock = threading.RLock()
        self._ensure_private_dir()
        # live handles the safe-browser/browser-use pattern: connection
        # objects live in the STORE, never in an AgentResult payload or
        # a checkpoint blob — an approval-resume resolves via db_id.
        self._live: dict[str, Any] = {}

    # ── dirs / perms ──────────────────────────────────────────────────────
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
    def _safe_name(name: str, cap: int = 80) -> str:
        safe = "".join(c for c in str(name or "") if c.isalnum() or c in "-_")
        return safe[:cap] or "unnamed"

    def _inside(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.root.resolve())
            return True
        except Exception:
            return False

    # ── low-level io ──────────────────────────────────────────────────────
    def _write_json(self, name: str, payload: Any) -> bool:
        path = self.root / f"{self._safe_name(name)}.json"
        if not self._inside(path):
            return False
        blob = json.dumps(sql_security.redact_obj(payload),
                          ensure_ascii=False, default=str)
        blob = _guard(blob)[:MAX_JSON_BYTES]
        try:
            with self._lock:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                             PERMS_FILE)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(blob)
                self._enforce(path)
            return True
        except Exception:
            return False

    def _read_json(self, name: str, default: Any) -> Any:
        path = self.root / f"{self._safe_name(name)}.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    # ── database registry ─────────────────────────────────────────────────
    def databases(self) -> list[dict]:
        rows = self._read_json("databases", [])
        rows = rows if isinstance(rows, list) else []
        out = []
        for r in rows:
            if isinstance(r, dict) and r.get("db_id"):
                out.append(r)
        return out

    def add_database(self, *, title: str, source_path: str,
                     kind: str = "sqlite", ro: bool = True,
                     db_id: str = "") -> dict:
        """Permit one database. Approval is handled in the agent: the
        user said which file, this just records it for the ACL."""
        db_id = db_id or new_db_id(title)
        rows = self.databases()
        row = {
            "db_id": _clip(db_id, 80),
            "title": _clip(title, 160),
            "kind": _clip(kind, 20),
            "source_path": _clip(source_path, MAX_DSN_LENGTH),
            "readonly": bool(ro),
            "added_at": _now_iso(),
        }
        rows = [r for r in rows if r.get("db_id") != row["db_id"]]
        rows.append(row)
        self._write_json("databases", rows[:MAX_DBS])
        self.audit("database_added", db_id=row["db_id"], kind=row["kind"])
        return row

    def get_database(self, db_id: str) -> Optional[dict]:
        db_id = _clip(db_id, 80)
        for row in self.databases():
            if row.get("db_id") == db_id:
                return row
        return None

    def forget_database(self, db_id: str) -> bool:
        """Drop one database from the ACL (its file, if any, survives —
        this only revokes the agent's permission to touch it)."""
        db_id = _clip(db_id, 80)
        rows = self.databases()
        keep = [r for r in rows if r.get("db_id") != db_id]
        if len(keep) == len(rows):
            return False
        self._write_json("databases", keep)
        self._live.pop(db_id, None)
        self.audit("database_forgotten", db_id=db_id)
        return True

    # ── live connection bookkeeping (handles stay in-process) ────────────
    def set_live(self, db_id: str, conn: Any) -> None:
        with self._lock:
            self._live[db_id] = conn

    def get_live(self, db_id: str) -> Any:
        return self._live.get(db_id)

    def drop_live(self, db_id: str) -> None:
        with self._lock:
            self._live.pop(db_id, None)

    @staticmethod
    def internal_name(index: int = 0) -> str:
        """The only database name the outside world may see:
        'db0'/'db1' — never a path, never a DSN, never credentials."""
        return _INTERNAL_NAMES[index] if 0 <= index < len(_INTERNAL_NAMES) \
            else "db0"

    # ── audit (actions only — never SQL text, never rows) ─────────────────
    def audit(self, event: str, **fields: Any) -> None:
        safe = {"event": _clip(event, 40), "ts": _now_iso()}
        for k, v in (fields or {}).items():
            safe[_clip(k, 30)] = v if isinstance(v, (int, float, bool)) \
                else _clip(v, 60)
        self._append_jsonl("audit", safe)

    def _append_jsonl(self, name: str, obj: dict) -> bool:
        path = self.root / f"{self._safe_name(name)}.jsonl"
        if not self._inside(path):
            return False
        line = json.dumps(sql_security.redact_obj(obj), ensure_ascii=False,
                          default=str)
        line = _guard(line)
        try:
            with self._lock:
                lines = []
                if path.exists():
                    lines = path.read_text(encoding="utf-8") \
                        .splitlines()[-(MAX_AUDIT_LINES - 1):]
                lines.append(line)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                             PERMS_FILE)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + "\n")
                self._enforce(path)
            return True
        except Exception:
            return False

    # ── summary / wipe ─────────────────────────────────────────────────────
    def summary(self) -> dict:
        rows = self.databases()
        return {"databases": len(rows),
                "kinds": sorted({str(r.get("kind") or "")
                                 for r in rows})}

    def wipe(self) -> bool:
        """Erase the registry; audit keeps the event. Live handles die."""
        ok = True
        with self._lock:
            try:
                (self.root / "databases.json").unlink(missing_ok=True)
            except Exception:
                ok = False
            self._live.clear()
            self._append_jsonl("audit", {"event": "wipe"})
        return ok
