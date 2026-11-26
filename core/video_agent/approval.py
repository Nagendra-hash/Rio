"""
core/video_agent/approval.py — Phase 6: the mandatory method-approval core.

Mission rules encoded here (there are no exceptions):

  * NO video generation starts without an approved method — Jarvis asks
    "Which method would you like to use for this video?" and waits
  * an approval is bound to ONE project (a plan hash), ONE method and the
    configuration shown/confirmed at approval time
  * tokens are SINGLE-USE and EXPIRE (default 30 min)
  * approval from an earlier project never authorizes a new one — the token
    is consumed by exactly one job start
  * every decision is logged WITHOUT secrets: script/image bodies are
    replaced by their sha1, provider keys never enter a plan record
  * repeated identical requests (voice retries, Telegram duplicate delivery)
    return the SAME pending token instead of stacking approvals

States: PENDING → APPROVED → USED;  PENDING/APPROVED → CANCELLED or EXPIRED.

Storage: memory/repo_agent/approvals.json (JARVIS_HOME-aware, atomic writes,
bounded to the most recent records).
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from typing import Any

__all__ = ["issue", "get", "approve", "consume", "cancel", "status_text",
           "recent", "project_key", "redact_plan", "pending_for_project"]

DEFAULT_TTL_S = 30 * 60
_MAX_RECORDS = 200
_CONFIG_KEYS = ("duration_s", "resolution", "language", "subtitles",
                "transitions", "encoder", "cloud_allowed", "budget",
                "max_budget")

_lock = threading.Lock()


def state_path():
    from core.repo_agent.jobs import db_path
    return db_path().parent / "approvals.json"


# ── plan redaction + project identity ────────────────────────────────────────

_REDACT_BODY_KEYS = ("script", "image", "scenes")
_MAX_TEXT = 300


def _sha1(text: str) -> str:
    return hashlib.sha1(str(text).encode("utf-8", "replace")).hexdigest()[:16]


def redact_plan(spec: dict[str, Any]) -> dict[str, Any]:
    """A secrets-free, size-bounded view of the plan (mission: log approval
    decisions without exposing secrets; scripts/images never stored raw)."""
    safe: dict[str, Any] = {}
    for key, value in dict(spec or {}).items():
        if key in _REDACT_BODY_KEYS:
            text = str(value or "")
            if not text:
                continue
            safe[f"{key}_sha1"] = _sha1(text)
            safe[f"{key}_len"] = len(text)
        elif isinstance(value, str):
            safe[key] = value[:_MAX_TEXT]
        else:
            safe[key] = value
    return safe


def project_key(spec: dict[str, Any]) -> str:
    """Stable identity of the PROJECT the approval is bound to: what the
    video IS (topic/mode/style/inputs) — not the production knobs the user
    is explicitly allowed to choose at approval time (duration, resolution,
    language, subtitles, encoder, format). Changing the bound fields is a
    material change → new approval required; changing the knobs is the
    approval's own content (recorded in the approved config)."""
    spec = spec or {}
    core = {k: spec.get(k) for k in
            ("mode", "topic", "style", "aspect_ratio", "pipeline",
             "user_assets_dir", "music_file")
            if spec.get(k) not in (None, "")}
    return _sha1(json.dumps(core, sort_keys=True, default=str))


# ── persistence ──────────────────────────────────────────────────────────────

def _load() -> dict[str, Any]:
    try:
        payload = json.loads(state_path().read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(
                payload.get("approvals"), dict):
            return payload
    except (OSError, ValueError):
        pass
    return {"updated_at": 0.0, "approvals": {}}


def _save(payload: dict[str, Any]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_at"] = time.time()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    tmp.replace(path)


def _prune(payload: dict[str, Any], now: float) -> None:
    """Bound storage: drop records dead for >24 h, cap at _MAX_RECORDS."""
    approvals = payload.get("approvals") or {}
    for token in list(approvals):
        rec = approvals[token]
        dead_since = rec.get("used_at") or rec.get("cancelled_at") or \
            (rec.get("expires_at") or 0
             if str(rec.get("state")) in ("EXPIRED",) else None)
        if dead_since and now - float(dead_since) > 24 * 3600:
            approvals.pop(token, None)
    if len(approvals) > _MAX_RECORDS:
        for token, _ in sorted(approvals.items(),
                               key=lambda kv: kv[1].get("created_at", 0)
                               )[:len(approvals) - _MAX_RECORDS]:
            approvals.pop(token, None)


def _expired(rec: dict[str, Any], now: float | None = None) -> bool:
    now = now if now is not None else time.time()
    return bool(rec.get("expires_at")) and now > float(rec["expires_at"])


def _normalize(rec: dict[str, Any], now: float) -> dict[str, Any]:
    """Lazy expiry: PENDING/APPROVED past their deadline become EXPIRED."""
    if rec.get("state") in ("PENDING", "APPROVED") and _expired(rec, now):
        rec["state"] = "EXPIRED"
        rec["expired_at"] = now
    return rec


# ── public API ───────────────────────────────────────────────────────────────

def issue(spec: dict[str, Any],
          methods: list[dict[str, Any]],
          *,
          ttl_s: int = DEFAULT_TTL_S,
          question: str = "Which method would you like to use for this "
                          "video?") -> dict[str, Any]:
    """Create (or reuse) a PENDING approval for this project. Returns the
    full record including the token. NEVER starts anything."""
    now = time.time()
    key = project_key(spec)
    with _lock:
        payload = _load()
        # dedup: identical pending project → same token (voice retries,
        # Telegram duplicate delivery must not stack approvals)
        for rec in payload["approvals"].values():
            rec = _normalize(rec, now)
            if rec.get("state") == "PENDING" \
                    and rec.get("project_key") == key \
                    and not _expired(rec, now):
                rec["deduplicated"] = True
                _save(payload)
                return {"ok": True, "deduplicated": True, "record": rec}
        record = {
            "token": secrets.token_hex(8),
            "project_key": key,
            "state": "PENDING",
            "created_at": now,
            "expires_at": now + float(ttl_s),
            "question": question,
            "plan": redact_plan(spec),
            "methods": methods,
            "approved": None,
            "used_at": None,
            "cancelled_at": None,
        }
        payload["approvals"][record["token"]] = record
        _prune(payload, now)
        _save(payload)
    return {"ok": True, "deduplicated": False, "record": record}


def get(token: str) -> dict[str, Any] | None:
    token = str(token or "").strip()
    if not token:
        return None
    now = time.time()
    with _lock:
        payload = _load()
        rec = payload["approvals"].get(token)
        if rec is None:
            return None
        changed = rec.get("state") in ("PENDING", "APPROVED") \
            and _expired(rec, now)
        rec = _normalize(rec, now)
        if changed:
            _save(payload)
        return rec


def approve(token: str, method: str,
            config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Record the user's method choice. PENDING → APPROVED. Does NOT start
    the job — `consume` does, exactly once."""
    now = time.time()
    method = str(method or "").strip().upper()
    with _lock:
        payload = _load()
        rec = payload["approvals"].get(str(token or "").strip())
        if rec is None:
            return {"ok": False, "error": "unknown approval token"}
        rec = _normalize(rec, now)
        if rec.get("state") == "EXPIRED":
            return {"ok": False, "error": "approval expired — ask again and "
                                          "I will issue a fresh one"}
        if rec.get("state") == "USED":
            return {"ok": False, "error": "approval already used — every "
                                          "video needs its own approval"}
        if rec.get("state") == "CANCELLED":
            return {"ok": False, "error": "approval was cancelled"}
        offered = {str(m.get("method_id") or "").upper()
                   for m in (rec.get("methods") or [])}
        if method not in offered:
            offered_text = ", ".join(sorted(offered)) or "none"
            return {"ok": False,
                    "error": f"method '{method or '?'}' is not one of the "
                             f"offered methods ({offered_text})"}
        clean_cfg: dict[str, Any] = {}
        for key, value in dict(config or {}).items():
            if key in _CONFIG_KEYS and value not in (None, ""):
                clean_cfg[key] = value
        rec["state"] = "APPROVED"
        rec["approved"] = {"method": method, "config": clean_cfg,
                           "approved_at": now}
        _save(payload)
    return {"ok": True, "record": rec}


def consume(token: str, project_key_expected: str | None = None
            ) -> dict[str, Any]:
    """Single-use gate BEFORE the job starts. Verifies: exists, APPROVED,
    not expired, and (when given) bound to THIS project. PENDING tokens are
    NOT consumable — an explicit approve() must have happened (voice flow
    does approve+consume in one call; the dashboard may approve first)."""
    now = time.time()
    with _lock:
        payload = _load()
        rec = payload["approvals"].get(str(token or "").strip())
        if rec is None:
            return {"ok": False, "error": "unknown or missing approval "
                                          "token — nothing was started"}
        rec = _normalize(rec, now)
        state = rec.get("state")
        if state == "PENDING":
            return {"ok": False,
                    "error": "approval token exists but no method was "
                             "approved yet — answer the method question "
                             "first"}
        if state == "EXPIRED":
            return {"ok": False, "error": "approval expired — request a "
                                          "fresh one"}
        if state == "CANCELLED":
            return {"ok": False, "error": "approval was cancelled"}
        if state == "USED":
            return {"ok": False, "error": "approval already used — every "
                                          "video needs its own approval"}
        if project_key_expected and \
                rec.get("project_key") != project_key_expected:
            return {"ok": False,
                    "error": "approval is bound to a different project — "
                             "any material change requires a new approval"}
        rec["state"] = "USED"
        rec["used_at"] = now
        rec["consumed_for_project"] = rec.get("project_key")
        _save(payload)
    return {"ok": True, "record": rec}


def cancel(token: str) -> dict[str, Any]:
    now = time.time()
    with _lock:
        payload = _load()
        rec = payload["approvals"].get(str(token or "").strip())
        if rec is None:
            return {"ok": False, "error": "unknown approval token"}
        if rec.get("state") in ("PENDING", "APPROVED") and \
                not _expired(rec, now):
            rec["state"] = "CANCELLED"
            rec["cancelled_at"] = now
            _save(payload)
            return {"ok": True, "record": rec}
        return {"ok": False,
                "error": f"cannot cancel a {rec.get('state')} approval"}


def pending_for_project(spec: dict[str, Any]) -> dict[str, Any] | None:
    """The live PENDING approval for this project, if any."""
    key = project_key(spec)
    now = time.time()
    with _lock:
        payload = _load()
        for rec in payload["approvals"].values():
            rec = _normalize(rec, now)
            if rec.get("state") == "PENDING" \
                    and rec.get("project_key") == key:
                return rec
    return None


def recent(limit: int = 10) -> list[dict[str, Any]]:
    now = time.time()
    with _lock:
        payload = _load()
        rows = [_normalize(r, now) for r in payload["approvals"].values()]
    rows.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
    return rows[:max(1, int(limit))]


def status_text(token: str) -> str:
    rec = get(token)
    if rec is None:
        return "Unknown approval token."
    approved = rec.get("approved") or {}
    lines = [
        f"Approval {rec['token']}: {rec.get('state')}",
        f"Project: {rec.get('project_key')}",
    ]
    if approved:
        lines.append(f"Method: {approved.get('method')} — config: "
                     f"{json.dumps(approved.get('config') or {})}")
    if rec.get("used_at"):
        used = time.strftime("%Y-%m-%d %H:%M:%S",
                             time.localtime(rec["used_at"]))
        lines.append(f"Used at {used} (single-use)")
    return "\n".join(lines)
