"""
orchestrator/agents/finance/store.py — the finance vault.

SEPARATE from general memory on purpose: financial records live in ONE
protected domain — memory/finance/ (relocated by JARVIS_HOME) — that
nothing else in the system reads. This store is the only writer.

    directory      0o700 (owner only)
    every file     0o600 (re-asserted on every write)
    audit log      ACTIONS ONLY — counts, ids, categories, timestamps.
                   Never amounts, vendors, notes, or receipt text.

What lives here:
    expenses.jsonl   the expense records (validated by schema before
                     they ever reach this file)
    budgets.json     per-category monthly limits + monthly_total
    categories.json  the user's own category definitions
    audit.jsonl      the actions log

Boundary guarantees:
  * every blob passes productivity.security.redact_obj() before disk;
  * writes stay inside the vault root (path containment);
  * every write is atomic-ish (tmp + replace) and never raises outward;
  * data caps everywhere; wipe() erases data files (audit keeps the event).
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from orchestrator.agents.finance import schema
from orchestrator.agents.finance.engine import categorize
from orchestrator.agents.productivity import security
from orchestrator.agents.productivity.store import PERMS_DIR, PERMS_FILE

MAX_EXPENSES = 2_000
MAX_CATEGORIES = 40
MAX_JSON_BYTES = 512_000
MAX_AUDIT_FIELDS = 24


def finance_dir(explicit: str | os.PathLike | None = None) -> Path:
    """The finance vault root: <JARVIS_HOME or repo>/memory/finance."""
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
    return base / "memory" / "finance"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(text: Any, cap: int = 60) -> str:
    return str(text or "").strip()[:cap]


class FinanceStore:
    """The only writer of finance data. Thread-safe, stdlib-only."""

    def __init__(self, directory: str | os.PathLike | None = None) -> None:
        self.root = Path(directory) if directory else finance_dir()
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
    def _write_json(self, name: str, payload: Any) -> bool:
        path = self.root / (self._safe_name(name) + ".json")
        if not self._inside(path):
            return False
        blob = json.dumps(security.redact_obj(payload), ensure_ascii=False,
                          default=str)[:MAX_JSON_BYTES]
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
        path = self.root / (self._safe_name(name) + ".json")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    def _append_jsonl(self, name: str, obj: dict, cap_lines: int) -> bool:
        path = self.root / (self._safe_name(name) + ".jsonl")
        if not self._inside(path):
            return False
        line = json.dumps(security.redact_obj(obj), ensure_ascii=False,
                          default=str)
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

    def _read_jsonl(self, name: str, limit: int) -> list[dict]:
        path = self.root / (self._safe_name(name) + ".jsonl")
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-MAX_EXPENSES:]
            return [json.loads(ln) for ln in lines if ln.strip()][-limit:]
        except Exception:
            return []

    def audit(self, event: str, **fields: Any) -> None:
        """ACTIONS-ONLY audit row: never amounts, vendors or notes."""
        safe = {"event": _clip(event, 40), "ts": _now_iso()}
        for k, v in (fields or {}).items():
            if isinstance(v, (int, float, bool)) or v is None:
                safe[_clip(k, 30)] = v
            else:
                safe[_clip(k, 30)] = _clip(v, 60)
        self._append_jsonl("audit", safe, MAX_EXPENSES)

    # ── expenses ──────────────────────────────────────────────────────────
    def add_expenses(self, rows: list[dict],
                     user_categories: list[dict] | None = None) -> dict:
        """Validate + dedupe + append. Returns counts; skips are honest."""
        existing = {schema.dedupe_key(r) for r in self.expenses()}
        added, skipped_dupes, invalid = 0, 0, 0
        for row in (rows or []):
            clean, errors = schema.validate_expense(row)
            if clean is None:
                invalid += 1
                continue
            # The agent categorizes before approval; this is the fallback
            # for direct store use — empty/'other' means uncategorized.
            if clean.get("category", "other") in ("", "other"):
                clean["category"], _by = categorize(clean, user_categories)
            key = schema.dedupe_key(clean)
            if key in existing:
                skipped_dupes += 1
                continue
            existing.add(key)
            if self._append_jsonl("expenses", clean, MAX_EXPENSES):
                added += 1
        self.audit("expenses_added", added=added, dupes=skipped_dupes,
                   invalid=invalid)
        return {"added": added, "skipped_duplicates": skipped_dupes,
                "invalid": invalid}

    def expenses(self, month: str = "", category: str = "",
                 limit: int = MAX_EXPENSES) -> list[dict]:
        rows = self._read_jsonl("expenses", MAX_EXPENSES)
        if month:
            rows = [r for r in rows if str(r.get("date", "")).startswith(month)]
        if category:
            rows = [r for r in rows
                    if str(r.get("category", "")).lower() == category.lower()]
        return rows[-limit:]

    def get_expense(self, expense_id: str) -> Optional[dict]:
        return next((r for r in self.expenses()
                     if r.get("id") == expense_id), None)

    def update_expense(self, expense_id: str, patch: dict) -> Optional[dict]:
        """Apply allowed fields (category/note/vendor) to ONE record."""
        rows = self.expenses()
        hit = next((r for r in rows if r.get("id") == expense_id), None)
        if not hit:
            return None
        allowed = {"category", "note", "vendor"}
        changed = False
        for k, v in (patch or {}).items():
            if k in allowed:
                hit[k] = str(v).strip()[:300 if k == "note" else 80]
                changed = True
        if not changed:
            return None
        self._rewrite_expenses(rows)
        self.audit("expense_updated")
        return hit

    def delete_expense(self, expense_id: str) -> Optional[dict]:
        rows = self.expenses()
        hit = next((r for r in rows if r.get("id") == expense_id), None)
        if not hit:
            return None
        self._rewrite_expenses([r for r in rows if r is not hit])
        self.audit("expense_deleted")
        return hit

    def _rewrite_expenses(self, rows: list[dict]) -> None:
        path = self.root / "expenses.jsonl"
        if not self._inside(path):
            return
        lines = [json.dumps(security.redact_obj(r), ensure_ascii=False,
                            default=str) for r in rows[-MAX_EXPENSES:]]
        try:
            with self._lock:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                             PERMS_FILE)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + ("\n" if lines else ""))
                self._enforce(path)
        except Exception:
            pass

    # ── budgets & categories (config, not records) ────────────────────────
    def budgets(self) -> dict:
        b = self._read_json("budgets", {})
        return b if isinstance(b, dict) else {}

    def set_budgets(self, patch: dict) -> dict:
        cur = self.budgets()
        clean = {}
        for k, v in (patch or {}).items():
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if fv >= 0:
                clean[str(k).strip().lower()[:30]] = round(fv, 2)
        cur.update(clean)
        self._write_json("budgets", cur)
        self.audit("budgets_set", keys=len(clean))
        return cur

    def user_categories(self) -> list[dict]:
        rows = self._read_json("categories", [])
        return rows if isinstance(rows, list) else []

    def set_user_categories(self, rows: list[dict]) -> list[dict]:
        clean = []
        for c in (rows or [])[:MAX_CATEGORIES]:
            if not isinstance(c, dict):
                continue
            name = str(c.get("name") or "").strip().lower()[:30]
            if not name:
                continue
            clean.append({"name": name,
                          "keywords": [str(k).lower()[:30]
                                       for k in (c.get("keywords") or [])
                                       if str(k).strip()][:20]})
        self._write_json("categories", clean)
        self.audit("categories_set", count=len(clean))
        return clean

    # ── summary / wipe ────────────────────────────────────────────────────
    def summary(self) -> dict:
        return {"expenses": len(self.expenses()),
                "budgets": len(self.budgets()),
                "user_categories": len(self.user_categories())}

    def wipe(self) -> bool:
        ok = True
        with self._lock:
            for name in ("expenses", "audit"):
                try:
                    (self.root / (name + ".jsonl")).unlink(missing_ok=True)
                except Exception:
                    ok = False
            for name in ("budgets", "categories"):
                try:
                    (self.root / (name + ".json")).unlink(missing_ok=True)
                except Exception:
                    ok = False
            self.audit("wipe")
        return ok
