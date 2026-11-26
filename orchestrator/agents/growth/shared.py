"""
orchestrator/agents/growth/shared.py — the shared Phase-18 toolbox.

Three business capabilities ship as three agents on the standard BaseAgent
contract, sharing ONE toolbox + ONE vault + ONE checkpoint class so the
existing infrastructure (productivity security, ApprovalGate, RAG ACL,
Reflector) is wrapped, never duplicated:

    growth/shared.py            ← this file
    growth/self_eval_agent.py   ← TaskType.SELF_EVAL  (self-evaluation)
    growth/lead_agent.py        ← TaskType.LEAD_SCORE (lead scoring)
    growth/marketing_agent.py   ← TaskType.MARKETING  (marketing)

THE VAULT (memory/growth/ — relocated by JARVIS_HOME)
    One protected domain, like finance/ and legal/ before it:
      0600 files in a 0700 dir, redact_obj() before every write,
      ACTIONS-ONLY audit (counts + ids — never lead PII bodies, never
      pasted goals), atomic-ish writes, path containment, never raises.

THE CHECKPOINT (memory/growth/checkpoints/)
    The productivity contract: save_for(task_id, …) approval slots + a
    rolling snapshot ring, 0600/0700, crash-safe.

GUARDRAILS (safety.py rules, in code — never prompt hope)
    * consequential lead ACTIONS (contacting a lead, discarding one,
      editing contact data) are APPROVAL_REQUIRED; SCORING itself is
      read-only and never needs approval.
    * the DECISION guardrail: a lead score is an opinion to help a human
      PRIORITIZE outreach. It never auto-sends, snoozes, or discards a
      lead — every consequential action needs the parked payload + a
      human approval, and every reply carries the disclaimer that a
      human review is required for consequential decisions.
    * marketing BLOCKED class: sending campaigns, publishing posts,
      purchasing ads, importing purchased lead lists. These never run,
      even approved — the marketing agent produces PLANS and evidence,
      it executes nothing.
    * self-eval BLOCKED class: user-data deletion. Reflection reviews;
      it never deletes anything.
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
from orchestrator.agents.productivity.checkpoint import ProductivityCheckpoint
from orchestrator.agents.productivity.store import PERMS_DIR, PERMS_FILE

MAX_GOALS = 100
MAX_ACTIVITIES = 2_000
MAX_LEADS = 2_000
MAX_SCORES = 2_000
MAX_RULES = 60
MAX_STRATEGIES = 40
MAX_NUMERIC_CELLS = 400          # numeric cells a CSV ingest may carry
MAX_JSON_BYTES = 4_000_000
MAX_AUDIT_LINES = 2_000

# the two record kinds every store write funnels into
KIND_GOAL = "goal"

CONSEQUENTIAL_MARKETING = ("send_campaign", "publish_post", "buy_ads",
                           "import_purchased_list")


def growth_dir(explicit: str | os.PathLike | None = None) -> Path:
    """The growth vault root: <JARVIS_HOME or repo>/memory/growth."""
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
    return base / "memory" / "growth"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_ref(prefix: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:6]}"


class GrowthCheckpoint(ProductivityCheckpoint):
    """Approval slots + rolling snapshots under memory/growth/checkpoints."""

    def __init__(self, directory: str | os.PathLike | None = None) -> None:
        super().__init__(directory or (growth_dir() / "checkpoints"))


class GrowthVault:
    """The only writer of growth data. Thread-safe, stdlib-only."""

    def __init__(self, directory: str | os.PathLike | None = None) -> None:
        self.root = Path(directory) if directory else growth_dir()
        self._lock = threading.RLock()
        self._ensure_private_dir()

    # ── permissions / containment ────────────────────────────────────────
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
    def _safe_name(name: str, cap: int = 60) -> str:
        safe = "".join(c for c in str(name or "") if c.isalnum() or c in "-_")
        return safe[:cap] or "unnamed"

    def _inside(self, path: Path) -> bool:
        try:
            resolved = path.resolve().relative_to(self.root.resolve())
            # the safe-name pass runs BEFORE this check, so an escape
            # attempt like "../escape" is already mangled — but a raw
            # path with separators must never resolve outside the root
            return not str(resolved).startswith("..")
        except Exception:
            return False

    # ── low-level io ─────────────────────────────────────────────────
    @staticmethod
    def _sanitize(obj: Any) -> Any:
        """redact_obj + mask_secrets on every persisted blob — token-shaped
        AND credential-shaped material never reaches disk (the reflection
        module's choke point, reused)."""
        from orchestrator.agents.coding_tools import mask_secrets
        masked, _kinds = mask_secrets(
            json.dumps(security.redact_obj(obj), ensure_ascii=False,
                       default=str))
        return masked

    def _write_json(self, name: str, payload: Any) -> bool:
        path = self.root / f"{self._safe_name(name)}.json"
        if not self._inside(path):
            return False
        blob = self._sanitize(payload)[:MAX_JSON_BYTES]
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

    def _append_jsonl(self, name: str, obj: dict, cap: int) -> bool:
        path = self.root / f"{self._safe_name(name)}.jsonl"
        if not self._inside(path):
            return False
        line = self._sanitize(obj)
        try:
            with self._lock:
                lines = []
                if path.exists():
                    lines = path.read_text(encoding="utf-8") \
                        .splitlines()[-(cap - 1):]
                lines.append(line)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                             PERMS_FILE)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + "\n")
                self._enforce(path)
            return True
        except Exception:
            return False

    def _read_jsonl(self, name: str, cap: int, limit: int | None = None) -> list[dict]:
        path = self.root / f"{self._safe_name(name)}.jsonl"
        rows: list[dict] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-cap:]
            rows = [json.loads(ln) for ln in lines if ln.strip()]
        except Exception:
            return []
        return rows[-limit:] if limit else rows

    def _rewrite_jsonl(self, name: str, rows: list[dict]) -> bool:
        """Replace a jsonl table in place (the forget/edit path)."""
        path = self.root / f"{self._safe_name(name)}.jsonl"
        if not self._inside(path):
            return False
        lines = [self._sanitize(r) for r in (rows or [])]
        try:
            with self._lock:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                             PERMS_FILE)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + ("\n" if lines else ""))
                self._enforce(path)
            return True
        except Exception:
            return False

    _AUDIT_TEXT_CLIP = 12          # actions-only row: an ID fragment, never
                                   # pasted goals/posts/leads — 12 chars is an
                                   # id fragment; 60 was a whole free-text field.

    def audit(self, event: str, **fields: Any) -> None:
        """ACTIONS-ONLY audit row: ids, counts, timestamps — never lead
        contact details, never pasted goals/posts."""
        safe: dict[str, Any] = {"event": str(event or "")[:40],
                                "ts": _now_iso()}
        for k, v in (fields or {}).items():
            if isinstance(v, (int, float, bool)) or v is None:
                safe[str(k)[:30]] = v
            else:
                safe[str(k)[:30]] = str(v or "")[:self._AUDIT_TEXT_CLIP]
        self._append_jsonl("audit", safe, MAX_AUDIT_LINES)

    # ── self-eval records ────────────────────────────────────────────────
    def goals(self) -> list[dict]:
        rows = self._read_json("goals", [])
        return rows if isinstance(rows, list) else []

    def save_goals(self, rows: list[dict]) -> bool:
        return self._write_json("goals", rows[:MAX_GOALS])

    def activities(self, limit: int = 200) -> list[dict]:
        return self._read_jsonl("activities", MAX_ACTIVITIES, limit)

    def log_activity(self, row: dict) -> bool:
        return self._append_jsonl("activities", row, MAX_ACTIVITIES)

    def reviews(self) -> list[dict]:
        rows = self._read_json("reviews", [])
        return rows if isinstance(rows, list) else []

    def save_reviews(self, rows: list[dict]) -> bool:
        return self._write_json("reviews", rows[-MAX_GOALS * 2:])

    # ── lead scoring ─────────────────────────────────────────────────────
    def leads(self, limit: int = MAX_LEADS) -> list[dict]:
        return self._read_jsonl("leads", MAX_LEADS, limit)

    def save_leads(self, rows: list[dict]) -> bool:
        """Replace the whole lead table (the approved-ingest path)."""
        return self._rewrite_jsonl("leads", rows[:MAX_LEADS])

    def add_leads(self, rows: list[dict]) -> dict:
        """Append with dedupe on (name, company, email) — honest counts."""
        existing = {self.dedupe_key(r) for r in self.leads()}
        added, dupes, invalid = 0, 0, 0
        clean_rows: list[dict] = []
        for raw in (rows or []):
            clean = clean_lead(raw)
            if clean is None:
                invalid += 1
                continue
            key = self.dedupe_key(clean)
            if key in existing:
                dupes += 1
                continue
            existing.add(key)
            clean_rows.append(clean)
        for row in clean_rows:
            if self._append_jsonl("leads", row, MAX_LEADS):
                added += 1
        self.audit("leads_added", added=added, dupes=dupes, invalid=invalid)
        return {"added": added, "duplicates": dupes, "invalid": invalid}

    @staticmethod
    def dedupe_key(row: dict) -> str:
        email = str(row.get("email") or "").strip().lower()
        if email:
            return f"e:{email}"
        return ("n:" + " ".join(
            (str(row.get(k) or "").strip().lower() for k in
             ("name", "company"))).strip())

    def score_writes(self, limit: int = MAX_SCORES) -> list[dict]:
        return self._read_jsonl("scoring", MAX_SCORES, limit)

    def log_score_write(self, row: dict) -> bool:
        return self._append_jsonl("scoring", row, MAX_SCORES)

    # ── scoring rules (user-defined, the whole scoring contract) ─────────
    def rules(self) -> list[dict]:
        rows = self._read_json("scoring_rules", [])
        return rows if isinstance(rows, list) else []

    def save_rules(self, rows: list[dict]) -> bool:
        return self._write_json("scoring_rules", rows[:MAX_RULES])

    # ── marketing ────────────────────────────────────────────────────────
    def strategies(self) -> list[dict]:
        rows = self._read_json("strategies", [])
        return rows if isinstance(rows, list) else []

    def save_strategies(self, rows: list[dict]) -> bool:
        return self._write_json("strategies", rows[:MAX_STRATEGIES])

    # ── wipe ─────────────────────────────────────────────────────────────
    def summary(self) -> dict:
        return {"goals": len(self.goals()),
                "leads": len(self.leads()),
                "rules": len(self.rules()),
                "activities": len(self.activities(limit=10 ** 6))}

    def wipe(self) -> bool:
        ok = True
        with self._lock:
            for name in ("goals", "reviews", "scoring_rules",
                         "strategies", "leads"):
                try:
                    (self.root / f"{self._safe_name(name)}.json").unlink(
                        missing_ok=True)
                except Exception:
                    ok = False
            for name in ("activities", "scoring", "audit", "leads"):
                try:
                    (self.root / f"{self._safe_name(name)}.jsonl").unlink(
                        missing_ok=True)
                except Exception:
                    ok = False
            self.audit("wipe")
        return ok


# ── lead + rule cleaning (never invent fields; report invalid rows) ──────

def clean_lead(raw: Any) -> Optional[dict]:
    """Normalize one raw lead dict. Returns None for an unusable row.

    A lead without a name AND without a company identifies nothing —
    scoring it would be scoring fog."""
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()[:120]
    company = str(raw.get("company") or "").strip()[:120]
    if not name and not company:
        return None
    email = str(raw.get("email") or "").strip()[:200]
    return {
        "lead_id": str(raw.get("lead_id") or "").strip()[:60]
        or _new_ref("lead"),
        "name": name,
        "company": company,
        "email": email,
        "phone": str(raw.get("phone") or "").strip()[:40],
        "source": str(raw.get("source") or "ingest")[:60],
        "notes": str(raw.get("notes") or "")[:600],
        # permitted numeric facts a scorer may read — nothing else is data
        "fields": {str(k)[:40]: v for k, v in
                   (raw.get("fields") or {}).items()
                   if isinstance(k, str)}
        if isinstance(raw.get("fields"), dict) else {},
        "added_at": _now_iso(),
    }


def clean_rule(raw: Any) -> tuple[Optional[dict], list[str]]:
    """Validate one user-defined scoring rule. Returns (rule, errors).

    Rule shape (JSON-safe):
        {"name": "senior-depth", "points": 30,
         "where": {"industry": "saas"},                # exact matches
         "min": {"team_size": 50},                     # numeric floors
         "max": {"poke_hours": 24},
         "has": ["decision_maker"],
         "source": "user"
        }
    Every field is optional but a rule must DO something, or it is noise.
    """
    errors: list[str] = []
    if not isinstance(raw, dict):
        return None, ["rule must be an object"]
    name = str(raw.get("name") or "").strip()[:60]
    if not name:
        errors.append("rule name missing")
    try:
        points = int(raw.get("points"))
    except (TypeError, ValueError):
        points, handled = 0, False
    where = raw.get("where") if isinstance(raw.get("where"), dict) else {}
    mins = raw.get("min") if isinstance(raw.get("min"), dict) else {}
    maxs = raw.get("max") if isinstance(raw.get("max"), dict) else {}
    has_items = [str(v).strip()[:40] for v in (raw.get("has") or [])
                 if str(v).strip()] \
        if isinstance(raw.get("has"), list) else []
    if not (where or mins or maxs or has_items):
        errors.append("rule matches nothing: give where/min/max/has")
    clean = {"name": name, "points": points, "where": where,
             "min": mins, "max": maxs, "has": has_items,
             "source": str(raw.get("source") or "user")[:30]}
    return (clean if not errors else None), errors


# ── marker text shared by all three disclaimers ───────────────────────────
HUMAN_REVIEW_NOTE = (
    "Scores are advisory: a person reviews and decides anything "
    "consequential (outreach, discarding, contact changes).")
