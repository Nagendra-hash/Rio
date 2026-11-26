"""
orchestrator/agents/productivity/permissions.py — the policy table for
Gmail and Drive operations.

security.py already defines the four levels (READ_ONLY / SAFE_ACTION /
APPROVAL_REQUIRED / BLOCKED) and the token redactor. This module answers the
next question the two surfaces ask, in one place:

    "for this operation, on this many items, what must happen?"
    "what exactly should the user see before they confirm?"

Keeping it deterministic (a table, not a model call) is what makes the gate
auditable: the same operation always lands at the same level.

Rules encoded here (spec Phase 8)
--------------------------------
  READ      never prompts.
  DRAFT     create/modify/delete a local draft — never sends, never prompts.
  SEND      always APPROVAL_REQUIRED, and never coverable by trusted mode.
  DELETE    trash by default; permanent delete is APPROVAL_REQUIRED and
            blocked in trusted mode; bulk delete previews the count.
  BULK      more than BULK_THRESHOLD affected items always prompts and always
            shows a preview.
  SHARING   every permission grant/change/removal is APPROVAL_REQUIRED.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from orchestrator.agents.productivity import security

READ_ONLY = security.READ_ONLY
SAFE_ACTION = security.SAFE_ACTION
APPROVAL_REQUIRED = security.APPROVAL_REQUIRED
BLOCKED = security.BLOCKED

# More than this many affected items in one action is "bulk".
BULK_THRESHOLD = 10

# ── operation tables ─────────────────────────────────────────────────────────

GMAIL_LEVELS: dict[str, str] = {
    # reads
    "list": READ_ONLY, "search": READ_ONLY, "read": READ_ONLY,
    "thread": READ_ONLY, "labels": READ_ONLY, "profile": READ_ONLY,
    "list_drafts": READ_ONLY, "get_draft": READ_ONLY, "summary": READ_ONLY,
    "briefing": READ_ONLY,
    # drafts (never send)
    "create_draft": SAFE_ACTION, "update_draft": SAFE_ACTION,
    "delete_draft": SAFE_ACTION,
    # sending
    "send": APPROVAL_REQUIRED, "reply": APPROVAL_REQUIRED,
    "reply_all": APPROVAL_REQUIRED, "forward": APPROVAL_REQUIRED,
    "send_draft": APPROVAL_REQUIRED,
    # reversible label changes
    "mark_read": SAFE_ACTION, "mark_unread": SAFE_ACTION,
    "star": SAFE_ACTION, "unstar": SAFE_ACTION,
    "add_label": SAFE_ACTION, "remove_label": SAFE_ACTION,
    "archive": SAFE_ACTION, "move_to_inbox": SAFE_ACTION,
    # deletion
    "trash": APPROVAL_REQUIRED, "untrash": SAFE_ACTION,
    "delete_permanent": APPROVAL_REQUIRED, "empty_trash": APPROVAL_REQUIRED,
    # attachments
    "download_attachment": SAFE_ACTION,
    "attachment_to_drive": SAFE_ACTION,
    # labels admin
    "create_label": SAFE_ACTION, "rename_label": SAFE_ACTION,
    "delete_label": APPROVAL_REQUIRED,
}

DRIVE_LEVELS: dict[str, str] = {
    # reads
    "list": READ_ONLY, "search": READ_ONLY, "get": READ_ONLY,
    "about": READ_ONLY, "read_text": READ_ONLY, "permissions": READ_ONLY,
    "find_duplicates": READ_ONLY, "organize_preview": READ_ONLY,
    # local writes
    "download": SAFE_ACTION,
    # drive writes that are reversible
    "upload": SAFE_ACTION, "create_folder": SAFE_ACTION,
    "ensure_folder": SAFE_ACTION, "rename": SAFE_ACTION,
    "move": SAFE_ACTION, "copy": SAFE_ACTION, "describe": SAFE_ACTION,
    "organize": APPROVAL_REQUIRED,
    # deletion
    "trash": APPROVAL_REQUIRED, "untrash": SAFE_ACTION,
    "delete_permanent": APPROVAL_REQUIRED,
    "empty_trash": APPROVAL_REQUIRED,
    # sharing
    "share": APPROVAL_REQUIRED, "update_permission": APPROVAL_REQUIRED,
    "remove_permission": APPROVAL_REQUIRED,
}

# Operations that trusted mode may NEVER auto-approve.
NEVER_TRUSTED = frozenset({
    "send", "reply", "reply_all", "forward", "send_draft",
    "delete_permanent", "empty_trash", "delete_label",
    "share", "update_permission", "remove_permission", "organize",
})

# Operations whose effect changes as the affected count grows.
BULK_SENSITIVE = frozenset({
    "trash", "untrash", "archive", "move_to_inbox", "mark_read",
    "mark_unread", "star", "unstar", "add_label", "remove_label",
    "delete_permanent", "move", "organize", "share",
})


def level_for(domain: str, operation: str,
              count: int = 1) -> str:
    """Permission level for (domain, operation, affected-count)."""
    table = DRIVE_LEVELS if str(domain).lower().startswith("drive") \
        else GMAIL_LEVELS
    op = str(operation or "").strip().lower()
    level = table.get(op, APPROVAL_REQUIRED)      # unknown = ask
    if op in BULK_SENSITIVE and int(count or 1) > BULK_THRESHOLD:
        return APPROVAL_REQUIRED
    return level


def needs_confirmation(domain: str, operation: str, count: int = 1,
                       trusted: bool = False) -> bool:
    """True when the user must explicitly confirm before this runs."""
    op = str(operation or "").strip().lower()
    level = level_for(domain, op, count)
    if level == BLOCKED:
        return True                                # blocked is not approval…
    if op in NEVER_TRUSTED:
        return True
    if trusted and level == APPROVAL_REQUIRED and op not in NEVER_TRUSTED:
        return False
    return security.needs_approval(level) or op in BULK_SENSITIVE and \
        int(count or 1) > BULK_THRESHOLD


def is_trusted_allowed(domain: str, operation: str, count: int = 1) -> bool:
    """Operations that a narrowly-scoped automation policy may run alone."""
    op = str(operation or "").strip().lower()
    return (op not in NEVER_TRUSTED
            and level_for(domain, op, count) != BLOCKED
            and int(count or 1) <= BULK_THRESHOLD)


# ── previews (shown before a confirmation) ──────────────────────────────────

_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
MAX_TITLE = 90


def _clean(text: str) -> str:
    """Untrusted subject/name → one short, quoted, control-free line."""
    value = _CTRL.sub(" ", str(text or "")).replace("\n", " ").strip()
    value = security.redact(value)
    if len(value) > MAX_TITLE:
        value = value[:MAX_TITLE - 1] + "…"
    return value


def email_preview(action: str, rows: Iterable[dict],
                  total: Optional[int] = None) -> str:
    """A human-verifiable list of the messages an action will affect.

    The subjects come from email — untrusted data — so they are quoted,
    control characters stripped and length capped. They are shown as data,
    never interpreted as instructions.
    """
    rows = list(rows or [])
    affected = total if total is not None else len(rows)
    lines = [f"About to {action} {affected} message"
             f"{'' if affected == 1 else 's'}:"]
    for row in rows[:10]:
        who = _clean(row.get("from") or row.get("sender") or "unknown sender")
        subj = _clean(row.get("subject") or "(no subject)")
        lines.append(f"  • “{subj}” — {who}")
    if affected > len(rows[:10]):
        lines.append(f"  … and {affected - len(rows[:10])} more.")
    lines.append("Nothing has been changed yet.")
    return "\n".join(lines)


def drive_preview(action: str, files: Iterable[dict],
                  total: Optional[int] = None) -> str:
    """Same contract for Drive file actions."""
    files = list(files or [])
    affected = total if total is not None else len(files)
    lines = [f"About to {action} {affected} file"
             f"{'' if affected == 1 else 's'}:"]
    for f in files[:10]:
        name = _clean(f.get("name") or f.get("id") or "untitled")
        size = f.get("size_human") or ""
        lines.append(f"  • “{name}”" + (f" ({size})" if size else ""))
    if affected > len(files[:10]):
        lines.append(f"  … and {affected - len(files[:10])} more.")
    lines.append("Nothing has been changed yet.")
    return "\n".join(lines)


def confirm_prompt(domain: str, operation: str, *, subject: str = "",
                   count: int = 1) -> str:
    """Short single-line question for the confirmation banner / chat."""
    op = str(operation or "").strip().lower()
    if op in ("send", "reply", "reply_all", "forward", "send_draft"):
        tail = f" “{_clean(subject)}”" if subject else ""
        return f"Send this email{tail}? This leaves your account."
    if op == "delete_permanent":
        return (f"Permanently delete {count} item"
                f"{'' if count == 1 else 's'}? This cannot be undone.")
    if op == "empty_trash":
        return "Permanently delete everything in Trash? This cannot be undone."
    if op in ("share", "update_permission", "remove_permission"):
        return "Change who can access this file?"
    if op == "trash":
        return (f"Move {count} item{'' if count == 1 else 's'} to Trash? "
                f"Recoverable for 30 days on Gmail.")
    return f"Proceed with {operation} on {count} item" \
           f"{'' if count == 1 else 's'}?"


__all__ = [
    "READ_ONLY", "SAFE_ACTION", "APPROVAL_REQUIRED", "BLOCKED",
    "BULK_THRESHOLD", "GMAIL_LEVELS", "DRIVE_LEVELS", "NEVER_TRUSTED",
    "level_for", "needs_confirmation", "is_trusted_allowed",
    "email_preview", "drive_preview", "confirm_prompt",
]
