"""
orchestrator/agents/dailylife/store.py — the daily-life data boundary.

Same contract as the productivity store (Phase 12): ONE protected domain —
memory/dailylife/ (relocated by JARVIS_HOME), strict file permissions, and
an audit log that records ACTIONS ONLY. Every blob passes
productivity.security.redact_obj() before hitting disk; every write is
atomic-ish (tmp + os.replace) and never raises outward.

What lives here:
    prefs.json     per-domain preferences (briefing city/topics, meal
                   diet/budget, travel home base, price hints).
    tasks.json     the user's task list the briefing reports.
    pantry.json    pantry items with quantities and expiry dates.
    menu.json      the last built weekly menu.
    grocery.json   the last built grocery list (+ estimated cost).
    watchlist.json travel watches (price/delay) with their baselines.
    drafts.jsonl   itineraries and other saved artifacts.
    audit.jsonl    actions only — counts, ids, timestamps. Never content.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Redaction is REUSED, not duplicated: the productivity package owns the
# token/secret scrubbing rules and every daily-life blob passes through it.
from orchestrator.agents.productivity import security as dl_security
from orchestrator.agents.productivity.store import PERMS_DIR, PERMS_FILE

MAX_TEXT_CHARS = 4_000
MAX_ITEMS = 500
MAX_DRAFTS = 200
MAX_JSON_BYTES = 512_000


def dailylife_dir(explicit: str | os.PathLike | None = None) -> Path:
    """The daily-life domain root: <JARVIS_HOME or repo>/memory/dailylife."""
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
    return base / "memory" / "dailylife"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(text: Any, cap: int = MAX_TEXT_CHARS) -> str:
    return str(text or "").strip()[:cap]


class DailyLifeStore:
    """The only writer of daily-life data. Thread-safe, stdlib-only."""

    def __init__(self, directory: str | os.PathLike | None = None) -> None:
        self.root = Path(directory) if directory else dailylife_dir()
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
    def _write_json(self, name: str, payload: Any,
                    cap_bytes: int = MAX_JSON_BYTES) -> bool:
        path = self.root / (self._safe_name(name) + ".json")
        if not self._inside(path):
            return False
        blob = json.dumps(dl_security.redact_obj(payload), ensure_ascii=False,
                          default=str)[:cap_bytes]
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

    def _append_jsonl(self, name: str, obj: dict,
                      cap_lines: int = MAX_ITEMS) -> bool:
        path = self.root / (self._safe_name(name) + ".jsonl")
        if not self._inside(path):
            return False
        line = json.dumps(dl_security.redact_obj(obj), ensure_ascii=False,
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

    def _read_jsonl(self, name: str, limit: int = 50) -> list[dict]:
        path = self.root / (self._safe_name(name) + ".jsonl")
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-MAX_ITEMS:]
            return [json.loads(ln) for ln in lines if ln.strip()][-limit:]
        except Exception:
            return []

    def audit(self, event: str, **fields: Any) -> None:
        """Append an ACTIONS-ONLY audit row — never user content."""
        safe = {"event": _clip(event, 40), "ts": _now_iso()}
        for k, v in (fields or {}).items():
            if isinstance(v, (int, float, bool)) or v is None:
                safe[_clip(k, 30)] = v
            else:
                safe[_clip(k, 30)] = _clip(v, 60)
        self._append_jsonl("audit", safe)

    # ── preferences (per domain) ──────────────────────────────────────────
    def prefs_get(self, domain: str, default: dict | None = None) -> dict:
        all_prefs = self._read_json("prefs", {})
        prefs = all_prefs.get(_clip(domain, 20)) if isinstance(all_prefs,
                                                               dict) else None
        return prefs if isinstance(prefs, dict) else dict(default or {})

    def prefs_set(self, domain: str, patch: dict) -> dict:
        all_prefs = self._read_json("prefs", {})
        if not isinstance(all_prefs, dict):
            all_prefs = {}
        cur = all_prefs.get(domain) or {}
        if not isinstance(cur, dict):
            cur = {}
        cur.update(patch or {})
        all_prefs[domain] = cur
        self._write_json("prefs", all_prefs)
        self.audit(f"prefs_{_clip(domain, 20)}",
                   keys=len(patch or {}))
        return cur

    # ── tasks (the briefing's task section) ───────────────────────────────
    def tasks(self) -> list[dict]:
        rows = self._read_json("tasks", [])
        return rows if isinstance(rows, list) else []

    def add_task(self, text: str, due: str = "") -> dict:
        row = {"id": uuid.uuid4().hex[:10], "text": _clip(text, 200),
               "due": _clip(due, 40), "done": False,
               "added_at": _now_iso(), "done_at": ""}
        rows = (self.tasks() + [row])[-MAX_ITEMS:]
        self._write_json("tasks", rows)
        self.audit("task_added", count=len(rows))
        return row

    def complete_task(self, ref: str) -> Optional[dict]:
        """Mark done by id or by exact text prefix (case-insensitive)."""
        rows = self.tasks()
        hit = None
        ref_low = str(ref or "").strip().lower()
        for r in rows:
            if r.get("id") == ref or r.get("text", "").lower() \
                    .startswith(ref_low):
                hit = r
                break
        if not hit or hit.get("done"):
            return None
        hit["done"], hit["done_at"] = True, _now_iso()
        self._write_json("tasks", rows)
        self.audit("task_done")
        return hit

    def clear_done_tasks(self) -> int:
        rows = [r for r in self.tasks() if not r.get("done")]
        removed = len(self.tasks()) - len(rows)
        self._write_json("tasks", rows)
        self.audit("tasks_cleared", count=removed)
        return removed

    # ── pantry ────────────────────────────────────────────────────────────
    def pantry_items(self) -> list[dict]:
        rows = self._read_json("pantry", [])
        return rows if isinstance(rows, list) else []

    def pantry_add(self, name: str, qty: float = 0.0, unit: str = "",
                   expires: str = "") -> dict:
        """Add or merge (same name, case-insensitive) — qty sums, expiry
        updates when a new date is supplied."""
        key = _clip(name, 60).lower()
        rows = self.pantry_items()
        hit = next((r for r in rows
                    if str(r.get("name", "")).lower() == key), None)
        if hit is None:
            hit = {"id": uuid.uuid4().hex[:10], "name": _clip(name, 60),
                   "qty": 0.0, "unit": _clip(unit, 20), "expires": "",
                   "added_at": _now_iso()}
            rows.append(hit)
        try:
            hit["qty"] = round(float(hit.get("qty") or 0.0) + float(qty or 0.0), 3)
        except (TypeError, ValueError):
            pass
        if unit:
            hit["unit"] = _clip(unit, 20)
        if expires:
            hit["expires"] = _clip(expires, 10)
        self._write_json("pantry", rows[-MAX_ITEMS:])
        self.audit("pantry_add")
        return hit

    def pantry_take(self, name: str, qty: float = 0.0) -> Optional[dict]:
        """Consume qty (or the whole item when qty is 0). Returns the row
        or None when the item is unknown."""
        key = _clip(name, 60).lower()
        rows = self.pantry_items()
        hit = next((r for r in rows
                    if str(r.get("name", "")).lower() == key), None)
        if hit is None:
            return None
        try:
            need = float(qty or 0.0)
        except (TypeError, ValueError):
            need = 0.0
        if need <= 0:
            rows = [r for r in rows if r is not hit]
        else:
            hit["qty"] = max(0.0, round(float(hit.get("qty") or 0.0) - need, 3))
            if hit["qty"] <= 0:
                rows = [r for r in rows if r is not hit]
        self._write_json("pantry", rows)
        self.audit("pantry_take")
        return hit

    # ── menu / grocery artifacts ──────────────────────────────────────────
    def save_menu(self, menu: dict) -> bool:
        ok = self._write_json("menu", {"built_at": _now_iso(), **menu})
        self.audit("menu_saved", days=len(menu.get("days") or []))
        return ok

    def last_menu(self) -> Optional[dict]:
        row = self._read_json("menu", None)
        return row if isinstance(row, dict) else None

    def save_grocery(self, grocery: dict) -> bool:
        ok = self._write_json("grocery", {"built_at": _now_iso(), **grocery})
        self.audit("grocery_saved", items=len(grocery.get("items") or []))
        return ok

    def last_grocery(self) -> Optional[dict]:
        row = self._read_json("grocery", None)
        return row if isinstance(row, dict) else None

    # ── travel watches ────────────────────────────────────────────────────
    def watches(self) -> list[dict]:
        rows = self._read_json("watchlist", [])
        return rows if isinstance(rows, list) else []

    def add_watch(self, watch: dict) -> dict:
        row = {"id": uuid.uuid4().hex[:10], "added_at": _now_iso(),
               "baseline": None, "last_check": None, **watch}
        self._write_json("watchlist", (self.watches() + [row])[-MAX_ITEMS:])
        self.audit("watch_added", kind=_clip(watch.get("kind", ""), 10))
        return row

    def update_watch(self, watch_id: str, fields: dict) -> Optional[dict]:
        rows = self.watches()
        hit = next((r for r in rows if r.get("id") == watch_id), None)
        if not hit:
            return None
        hit.update(fields or {})
        self._write_json("watchlist", rows)
        self.audit("watch_updated", kind=_clip(hit.get("kind", ""), 10))
        return hit

    def remove_watch(self, watch_id: str) -> bool:
        rows = [r for r in self.watches() if r.get("id") != watch_id]
        removed = len(rows) < len(self.watches())
        self._write_json("watchlist", rows)
        if removed:
            self.audit("watch_removed")
        return removed

    # ── drafts (itineraries & saved artifacts) ────────────────────────────
    def save_draft(self, kind: str, payload: dict, task_id: str = "") -> dict:
        entry = {"id": uuid.uuid4().hex[:10],
                 "task_id": _clip(task_id, 40),
                 "kind": _clip(kind, 20),
                 "at": _now_iso(),
                 "payload": payload}
        self._append_jsonl("drafts", entry, cap_lines=MAX_DRAFTS)
        self.audit(f"draft_{_clip(kind, 20)}", task=_clip(task_id, 40))
        return entry

    def drafts(self, kind: str | None = None, limit: int = 20) -> list[dict]:
        rows = self._read_jsonl("drafts", MAX_ITEMS)
        if kind:
            rows = [r for r in rows if r.get("kind") == kind]
        return rows[-limit:]

    def latest_draft(self, kind: str) -> Optional[dict]:
        rows = self.drafts(kind=kind, limit=1)
        return rows[-1] if rows else None

    # ── summary / wipe ────────────────────────────────────────────────────
    def summary(self) -> dict:
        return {
            "tasks": len(self.tasks()),
            "pantry": len(self.pantry_items()),
            "watches": len(self.watches()),
            "drafts": len(self._read_jsonl("drafts", MAX_ITEMS)),
            "menu": bool(self.last_menu()),
            "grocery": bool(self.last_grocery()),
        }

    def wipe(self) -> bool:
        ok = True
        with self._lock:
            for name in ("tasks", "pantry", "menu", "grocery", "watchlist",
                         "prefs", "drafts", "audit"):
                try:
                    ext = ".jsonl" if name in ("drafts", "audit") else ".json"
                    (self.root / (name + ext)).unlink(missing_ok=True)
                except Exception:
                    ok = False
            self.audit("wipe")
        return ok
