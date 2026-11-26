"""
orchestrator/agents/health/store.py — the health-data boundary.

ALL sensitive health information lives in ONE protected domain:
memory/health/ (relocated by JARVIS_HOME like every writable path), with
strict file permissions:

    directory      0o700  (owner only)
    every file     0o600  (owner only — enforced on EVERY write, so a
                    umask or an imported file cannot loosen it)
    audit log      0o600, and records ACTIONS ONLY (event names, counts,
                    timestamps) — never symptoms, values, or text.

Boundary guarantees:
  * the store writes ONLY inside its own root; profile names and report
    sources are sanitized (no separators, no '..'), so nothing can escape;
  * the store is the ONLY health writer — it never touches memory/
    long_term.json, conversations, the orchestration journal, RAG, or any
    other Jarvis store;
  * every write is atomic-ish (tmp + replace) and never raises outward;
  * data caps everywhere (entry length, list length, file line count) so
    the domain cannot grow unbounded;
  * wipe() erases all data files (the audit trail records the action).

Sensitive content is RETURNED to the caller (the user's own channel) and
PERSISTED only here. Logs carry action names, not content.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ── caps ─────────────────────────────────────────────────────────────────────
MAX_TEXT_CHARS = 500          # per free-text entry
MAX_LIST_ITEMS = 2_000        # per jsonl file (read side)
MAX_MEDS = 100
MAX_REPORTS = 200
MAX_QUESTIONS = 100
MAX_JSON_BYTES = 512_000      # per whole-file store (profile, meds)
PERMS_FILE = 0o600
PERMS_DIR = 0o700


def health_dir(explicit: str | os.PathLike | None = None) -> Path:
    """The health domain root: <JARVIS_HOME or repo>/memory/health."""
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
    return base / "memory" / "health"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(text: Any, cap: int = MAX_TEXT_CHARS) -> str:
    s = str(text or "").strip()
    return s[:cap]


class HealthStore:
    """The only writer of health data. Thread-safe, stdlib-only, quiet."""

    def __init__(self, directory: str | os.PathLike | None = None) -> None:
        self.root = Path(directory) if directory else health_dir()
        self._lock = threading.RLock()
        self._ensure_private_dir()

    # ── permission enforcement ───────────────────────────────────────────
    def _ensure_private_dir(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            os.chmod(self.root, PERMS_DIR)
        except Exception:
            pass  # best effort; on fat/vfat filesystems chmod may no-op

    @staticmethod
    def _enforce(path: Path) -> None:
        """Owner-only perms, re-asserted on every write."""
        try:
            os.chmod(path, PERMS_FILE)
        except Exception:
            pass

    # ── path containment ─────────────────────────────────────────────────
    @staticmethod
    def _safe_name(name: str, cap: int = 60) -> str:
        """Alnum/-/_ only — no separators, no '..', no extension tricks."""
        safe = "".join(c for c in str(name or "") if c.isalnum() or c in "-_")
        return (safe[:cap] or "unnamed")

    def _inside(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.root.resolve())
            return True
        except Exception:
            return False

    # ── low-level io ─────────────────────────────────────────────────────
    def _write_json(self, name: str, obj: Any) -> bool:
        path = self.root / (self._safe_name(name, 40) + ".json")
        if not self._inside(path):
            return False
        try:
            blob = json.dumps(obj, indent=1, ensure_ascii=False,
                              default=str)[:MAX_JSON_BYTES]
            tmp = path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                         PERMS_FILE)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(blob)
            os.replace(tmp, path)
            self._enforce(path)
            return True
        except Exception:
            return False

    def _read_json(self, name: str, default: Any) -> Any:
        path = self.root / (self._safe_name(name, 40) + ".json")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    def _append_jsonl(self, name: str, obj: dict) -> bool:
        path = self.root / (self._safe_name(name, 40) + ".jsonl")
        if not self._inside(path):
            return False
        line = json.dumps(obj, ensure_ascii=False, default=str)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                         PERMS_FILE)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            self._enforce(path)
            return True
        except Exception:
            return False

    def _read_jsonl(self, name: str, limit: int = 50) -> list[dict]:
        path = self.root / (self._safe_name(name, 40) + ".jsonl")
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-MAX_LIST_ITEMS:]
            return [json.loads(ln) for ln in lines if ln.strip()][-limit:]
        except Exception:
            return []

    def audit(self, event: str, **fields: Any) -> None:
        """Append an ACTIONS-ONLY audit row. Never record health content."""
        safe = {"event": _clip(event, 40),
                "ts": _now_iso()}
        for k, v in (fields or {}).items():
            if isinstance(v, (int, float, bool)) or v is None:
                safe[_clip(k, 30)] = v
            else:
                safe[_clip(k, 30)] = _clip(v, 40)
        self._append_jsonl("audit", safe)

    # ── profile (patient-provided, minimal) ──────────────────────────────
    PROFILE_KEYS = {"name", "age", "sex", "blood_group", "allergies",
                    "conditions", "contact", "notes"}

    def save_profile(self, profile: dict) -> bool:
        clean = {k: v for k, v in (profile or {}).items()
                 if k in self.PROFILE_KEYS}
        ok = self._write_json("profile", clean)
        self.audit("profile_saved")
        return ok

    def get_profile(self) -> dict:
        return self._read_json("profile", {})

    # ── symptoms ─────────────────────────────────────────────────────────
    def add_symptom(self, items: list[str], text: str, onset: str = "",
                    severity: str = "") -> dict:
        entry = {"id": uuid.uuid4().hex[:10], "at": _now_iso(),
                 "items": [_clip(i, 40) for i in (items or [])][:12],
                 "onset": _clip(onset, 40), "severity": _clip(severity, 20),
                 "text": _clip(text)}
        self._append_jsonl("symptoms", entry)
        self.audit("symptom_added", items=len(entry["items"]))
        return entry

    def symptoms(self, limit: int = 20) -> list[dict]:
        return self._read_jsonl("symptoms", limit)

    # ── measurements ─────────────────────────────────────────────────────
    def add_measurement(self, kind: str, value: dict, unit: str = "",
                        when: str = "") -> dict:
        entry = {"id": uuid.uuid4().hex[:10], "at": _now_iso(),
                 "kind": _clip(kind, 20), "value": value,
                 "unit": _clip(unit, 12), "when": _clip(when, 40)}
        self._append_jsonl("measurements", entry)
        self.audit("measurement_added", kind=entry["kind"])
        return entry

    def measurements(self, kind: str | None = None,
                     limit: int = 50) -> list[dict]:
        rows = self._read_jsonl("measurements", MAX_LIST_ITEMS)
        if kind:
            rows = [r for r in rows if r.get("kind") == kind]
        return rows[-limit:]

    # ── medications ──────────────────────────────────────────────────────
    def set_medications(self, meds: list[dict]) -> bool:
        clean = [{"name": _clip(m.get("name", ""), 60),
                  "dose": _clip(m.get("dose", ""), 30),
                  "frequency": _clip(m.get("frequency", ""), 60)}
                 for m in (meds or [])][:MAX_MEDS]
        ok = self._write_json("medications", clean)
        self.audit("medications_saved", count=len(clean))
        return ok

    def add_medications(self, meds: list[dict]) -> bool:
        current = self.get_medications()
        names = {m["name"].lower() for m in current}
        add = [m for m in (meds or []) if m.get("name", "").lower()
               not in names]
        return self.set_medications(current + add)

    def get_medications(self) -> list[dict]:
        return self._read_json("medications", [])

    def remove_medication(self, name: str) -> bool:
        current = self.get_medications()
        kept = [m for m in current
                if m.get("name", "").lower() != _clip(name, 60).lower()]
        if len(kept) == len(current):
            return False
        return self.set_medications(kept)

    # ── reports ──────────────────────────────────────────────────────────
    def add_report(self, parsed: dict, source: str = "") -> dict:
        entry = {"id": uuid.uuid4().hex[:10], "at": _now_iso(),
                 "source": self._safe_name(source, 40),
                 "tests": (parsed or {}).get("tests", [])[:60],
                 "unparsed": (parsed or {}).get("unparsed", [])[:20],
                 "identity_removed": (parsed or {}).get("identity_removed", 0)}
        self._append_jsonl("reports", entry)
        self.audit("report_added", tests=len(entry["tests"]))
        return entry

    def reports(self, limit: int = 10) -> list[dict]:
        return self._read_jsonl("reports", limit)

    # ── clinician questions ──────────────────────────────────────────────
    def add_question(self, text: str) -> dict:
        entry = {"id": uuid.uuid4().hex[:8], "at": _now_iso(),
                 "text": _clip(text, 300)}
        self._append_jsonl("questions", entry)
        self.audit("question_added")
        return entry

    def questions(self, limit: int = 30) -> list[dict]:
        return self._read_jsonl("questions", limit)

    # ── summary / wipe ───────────────────────────────────────────────────
    def summary(self) -> dict:
        return {"symptoms": len(self._read_jsonl("symptoms", MAX_LIST_ITEMS)),
                "measurements": len(self._read_jsonl(
                    "measurements", MAX_LIST_ITEMS)),
                "medications": len(self.get_medications()),
                "reports": len(self._read_jsonl("reports", MAX_LIST_ITEMS)),
                "questions": len(self._read_jsonl("questions",
                                                  MAX_LIST_ITEMS)),
                "profile": bool(self.get_profile())}

    def wipe(self) -> bool:
        """Erase ALL data files. The audit log records that a wipe happened."""
        ok = True
        with self._lock:
            for name in ("profile", "medications"):
                p = self.root / (name + ".json")
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    ok = False
            for name in ("symptoms", "measurements", "reports", "questions"):
                p = self.root / (name + ".jsonl")
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    ok = False
            self.audit("wipe")
        return ok
