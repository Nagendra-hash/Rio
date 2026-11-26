"""
orchestrator/agents/study/profile.py — the study vault.

SEPARATE on purpose (the finance-vault rule, softened): everything here is
the USER'S OWN learning data — goals, plans, quiz results, the spaced-
repetition ladder — so it lives in ONE protected domain, memory/study/
(relocated by JARVIS_HOME), with strict permissions and an ACTIONS-ONLY
audit log. It never feeds general memory.

What lives here:
    goals.json       the learning goals (subject, target level, deadline)
    plans.json       generated study plans (gr7up by goal id)
    quiz_results.jsonl  every graded attempt (id, score, topic, at)
    boxes.json       the spaced-repetition ladder: topic → box 1..5
    progress.json    derived progress snapshot (mastery, streak, box rates)
    prefs.json       difficulty dial + weekly minutes budget (redacted)
    audit.jsonl      actions only — ids, counts, timestamps; never content.

Writes are 0600 in a 0700 dir, redacted through productivity.security
before disk, thread-safe, never raise outward. wipe() erases the data
files and keeps the wipe event in the audit.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from orchestrator.agents.productivity import security as study_security
from orchestrator.agents.productivity.store import PERMS_DIR, PERMS_FILE

MAX_GOALS = 50
MAX_PLANS = 100
MAX_RESULTS = 2_000
MAX_TEXT_CHARS = 4_000
MAX_JSON_BYTES = 512_000
BOXES = 5                      # the spaced-repetition ladder depth


def study_dir(explicit: str | os.PathLike | None = None) -> Path:
    """The study vault root: <JARVIS_HOME or repo>/memory/study."""
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
    return base / "memory" / "study"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(text: Any, cap: int = MAX_TEXT_CHARS) -> str:
    return str(text or "").strip()[:cap]


class StudyStore:
    """The only writer of study data. Thread-safe, stdlib-only."""

    def __init__(self, directory: str | os.PathLike | None = None) -> None:
        self.root = Path(directory) if directory else study_dir()
        self._lock = threading.RLock()
        self._ensure_private_dir()

    # ── permissions / containment ───────────────────────────────────────
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

    # ── low-level io ────────────────────────────────────────────────────
    def _write_json(self, name: str, payload: Any) -> bool:
        path = self.root / (self._safe_name(name) + ".json")
        if not self._inside(path):
            return False
        blob = json.dumps(study_security.redact_obj(payload),
                          ensure_ascii=False, default=str)[:MAX_JSON_BYTES]
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
        line = json.dumps(study_security.redact_obj(obj), ensure_ascii=False,
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
            lines = path.read_text(encoding="utf-8").splitlines()[-MAX_RESULTS:]
            return [json.loads(ln) for ln in lines if ln.strip()][-limit:]
        except Exception:
            return []

    def audit(self, event: str, **fields: Any) -> None:
        """ACTIONS-ONLY audit row — never learning content."""
        safe = {"event": _clip(event, 40), "ts": _now_iso()}
        for k, v in (fields or {}).items():
            if isinstance(v, (int, float, bool)) or v is None:
                safe[_clip(k, 30)] = v
            else:
                safe[_clip(k, 30)] = _clip(v, 60)
        self._append_jsonl("audit", safe, MAX_RESULTS)

    # ── goals ───────────────────────────────────────────────────────────
    def goals(self) -> list[dict]:
        rows = self._read_json("goals", [])
        return rows if isinstance(rows, list) else []

    def get_goal(self, goal_id: str) -> Optional[dict]:
        return next((g for g in self.goals() if g.get("id") == goal_id), None)

    def add_goal(self, subject: str, target: str = "", deadline: str = "",
                 level: str = "") -> dict:
        row = {"id": f"goal-{uuid.uuid4().hex[:10]}",
               "subject": _clip(subject, 120),
               "target": _clip(target, 200),
               "deadline": _clip(deadline, 10),
               "level": _clip(level, 40) or "beginner",
               "created_at": _now_iso(), "archived": False}
        rows = (self.goals() + [row])[-MAX_GOALS:]
        self._write_json("goals", rows)
        self.audit("goal_added")
        return row

    def archive_goal(self, goal_id: str) -> Optional[dict]:
        rows = self.goals()
        hit = next((g for g in rows if g.get("id") == goal_id and
                    not g.get("archived")), None)
        if not hit:
            return None
        hit["archived"] = True
        hit["archived_at"] = _now_iso()
        self._write_json("goals", rows)
        self.audit("goal_archived")
        return hit

    # ── plans ───────────────────────────────────────────────────────────
    def save_plan(self, goal_id: str, plan: dict) -> dict:
        entry = {"id": f"plan-{uuid.uuid4().hex[:10]}", "goal_id": _clip(goal_id, 40),
                 "built_at": _now_iso(), **plan}
        rows = (self._read_json("plans", []) + [entry])[-MAX_PLANS:]
        self._write_json("plans", rows)
        self.audit("plan_saved", goal=_clip(goal_id, 40))
        return entry

    def plans(self, goal_id: str = "") -> list[dict]:
        rows = self._read_json("plans", [])
        if isinstance(rows, list) and goal_id:
            rows = [r for r in rows if r.get("goal_id") == goal_id]
        return rows if isinstance(rows, list) else []

    def latest_plan(self, goal_id: str = "") -> Optional[dict]:
        rows = self.plans(goal_id)
        return rows[-1] if rows else None

    def complete_plan_item(self, ref: str) -> Optional[dict]:
        """Mark a plan item done by id or exact text prefix."""
        plan = self.latest_plan()
        if not plan:
            return None
        items = plan.get("items") or []
        ref_low = str(ref or "").strip().lower()
        hit = next((i for i in items if not i.get("done") and
                    (i.get("id") == ref or
                     str(i.get("topic", "")).lower().startswith(ref_low))), None)
        if not hit:
            return None
        hit["done"], hit["done_at"] = True, _now_iso()
        for r in reversed(self._read_json("plans", [])):
            if r.get("id") == plan.get("id"):
                r["items"] = items
                break
        rows = self._read_json("plans", [])
        for r in rows:
            if r.get("id") == plan.get("id"):
                r["items"] = items
        self._write_json("plans", rows)
        self.audit("plan_item_done")
        return hit

    # ── quiz results ────────────────────────────────────────────────────
    def add_quiz_result(self, grade: dict) -> dict:
        row = {"id": f"quiz-{uuid.uuid4().hex[:10]}", "at": _now_iso(), **grade}
        self._append_jsonl("quiz_results", row, MAX_RESULTS)
        self.audit("quiz_graded", score=row.get("score_pct"))
        return row

    def quiz_results(self, goal_id: str = "", limit: int = 50) -> list[dict]:
        rows = self._read_jsonl("quiz_results", MAX_RESULTS)
        if isinstance(rows, list) and goal_id:
            rows = [r for r in rows if r.get("goal_id") == goal_id]
        return rows[-limit:]

    # ── the spaced-repetition ladder ─────────────────────────────────────
    def boxes(self) -> dict:
        data = self._read_json("boxes", {})
        return data if isinstance(data, dict) else {}

    def move_topic(self, topic: str, direction: str) -> dict:
        """Promote (pass) or demote (fail) one topic on the ladder."""
        key = _clip(topic, 120).lower()
        data = self.boxes()
        entry = data.get(key) or {"topic": _clip(topic, 120), "box": 1}
        cur = int(entry.get("box") or 1)
        new = min(BOXES, cur + 1) if direction == "up" else max(1, cur - 1)
        entry["box"] = new
        entry["last_at"] = _now_iso()
        data[key] = entry
        self._write_json("boxes", data)
        self.audit("box_moved", direction=1 if direction == "up" else -1)
        return entry

    def reset_box(self, topic: str) -> dict:
        key = _clip(topic, 120).lower()
        data = self.boxes()
        entry = {"topic": _clip(topic, 120), "box": 1, "last_at": _now_iso()}
        data[key] = entry
        self._write_json("boxes", data)
        self.audit("box_reset")
        return entry

    # ── progress snapshot ───────────────────────────────────────────────
    def save_progress(self, progress: dict) -> bool:
        return self._write_json("progress", {"at": _now_iso(), **progress})

    def progress(self, goal_id: str = "") -> dict:
        snap = self._read_json("progress", {})
        return snap if isinstance(snap, dict) else {}

    # ── prefs ───────────────────────────────────────────────────────────
    def prefs(self) -> dict:
        rows = self._read_json("prefs", {})
        return rows if isinstance(rows, dict) else {}

    def set_prefs(self, patch: dict) -> dict:
        cur = self.prefs()
        cur.update(patch or {})
        self._write_json("prefs", cur)
        self.audit("prefs_set", keys=len(patch or {}))
        return cur

    # ── summary / wipe ──────────────────────────────────────────────────
    def summary(self) -> dict:
        return {"goals": len([g for g in self.goals() if not g.get("archived")]),
                "plans": len(self._read_json("plans", [])),
                "quiz_results": len(self._read_jsonl("quiz_results", MAX_RESULTS)),
                "topics_in_boxes": len(self.boxes())}

    def wipe(self) -> bool:
        ok = True
        with self._lock:
            for name in ("goals", "plans", "quiz_results", "boxes",
                         "progress", "prefs", "audit"):
                try:
                    ext = ".jsonl" if name in ("quiz_results", "audit") \
                        else ".json"
                    (self.root / (name + ext)).unlink(missing_ok=True)
                except Exception:
                    ok = False
            self.audit("wipe")
        return ok
