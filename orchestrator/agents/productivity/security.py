"""
orchestrator/agents/productivity/security.py — the Phase 12 security layer.

Two jobs, both baked into CODE (the trading-agent / health-agent pattern):

1. TOKEN REDACTION — the hard rule "never expose email tokens". Every
   user-facing string and every blob about to be persisted passes through
   redact(), which scrubs OAuth tokens, refresh tokens, bearer/authorization
   headers, api keys, and secret-ish key/value shapes. The agents run every
   reply and every store write through this.

2. PERMISSION LEVELS — the system_tools.py pattern: the level IS the answer.

    READ_ONLY          inspect/measure/list — inbox triage, summaries,
                       availability, conflict checks. No approval needed.
    SAFE_ACTION        bounded + reversible locally — saving a DRAFT,
                       proposing slots. Still nothing leaves the machine.
    APPROVAL_REQUIRED  anything that reaches the outside world: sending an
                       email, creating an event with attendees (a real
                       invite), moving/deleting messages. Runs ONLY after a
                       live human approval via the orchestrator's
                       ApprovalGate.
    BLOCKED            never runs, even approved: raw token/secret access,
                       permanent deletes outside the recoverable Gmail
                       trash path, wiring new OAuth scopes, disabling the
                       approval gate itself.

Trust levels (the "trusted automation" clause): the Calendar Agent may run
in `standard` mode (every APPROVAL_REQUIRED action parks) or `trusted` mode
(enabled EXPLICITLY per deployment via JARVIS_CALENDAR_TRUSTED=on). Trusted
mode never covers send/delete — only event creation on the user's own
calendar WITHOUT external attendees. Email always requires approval.
"""
from __future__ import annotations

import re

from orchestrator.contracts import TaskStatus

# ── permission levels ────────────────────────────────────────────────────────

READ_ONLY = "read_only"
SAFE_ACTION = "safe_action"
APPROVAL_REQUIRED = "approval_required"
BLOCKED = "blocked"


def is_blocked(level: str) -> bool:
    return level == BLOCKED


def needs_approval(level: str) -> bool:
    return level == APPROVAL_REQUIRED


# ── token redaction ──────────────────────────────────────────────────────────

_REDACTED = "[REDACTED]"

# Secret-ish key names: access_token, refreshToken, "client-secret",
# api_key, Authorization: … (case-insensitive, any quoting style).
_SECRET_KEY = r"[\w.-]*(?:token|secret|password|passwd|credential|" \
              r"api[_-]?key|auth(?:orization)?)\w*"
_SECRET_KEY_RE = re.compile(
    r'["\']?' + _SECRET_KEY + r'["\']?\s*[:=]\s*', re.IGNORECASE)

# Secret-ish key names in quoted JSON position: "refresh_token": "…"
_QUOTED_SECRET_RE = re.compile(
    r'(["\']' + _SECRET_KEY + r'["\']\s*:\s*)'
    r'("[^"]*"|\'[^\']*\'|[^\s,}]+)', re.IGNORECASE)

# Value shapes worth scrubbing even without a key name.
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+([A-Za-z0-9._~+/=-]{8,})")
_JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{2,}\b")
_GOOGLE_ACCESS_RE = re.compile(r"\bya29\.[A-Za-z0-9._-]{8,}")
_GOOGLE_REFRESH_RE = re.compile(r"\b1//[A-Za-z0-9_-]{8,}")
_GOOGLE_CLIENT_SECRET_RE = re.compile(r"\bGOCSPX-[A-Za-z0-9_-]{8,}")
_GCP_API_KEY_RE = re.compile(r"\bAIza[A-Za-z0-9_-]{20,}")


def _redact_line(line: str) -> str:
    line = _BEARER_RE.sub(r"\1 " + _REDACTED, line)
    line = _JWT_RE.sub(_REDACTED, line)
    line = _GOOGLE_ACCESS_RE.sub(_REDACTED, line)
    line = _GOOGLE_REFRESH_RE.sub(_REDACTED, line)
    line = _GOOGLE_CLIENT_SECRET_RE.sub(_REDACTED, line)
    line = _GCP_API_KEY_RE.sub(_REDACTED, line)
    # "secret_key": "value"  →  "secret_key": [REDACTED]
    line = _QUOTED_SECRET_RE.sub(lambda m: m.group(1) + _REDACTED, line)
    # secret_key: value / secret_key=value  →  secret_key: [REDACTED]
    line = _SECRET_KEY_RE.sub(lambda m: m.group(0) + _REDACTED, line)
    return line


def redact(text: str) -> str:
    """Scrub token-like material from any user-facing or persisted string.

    Runs on EVERY AgentResult text and EVERY store write. Normal prose
    (dates, meeting titles, long words) is untouched — only token-shaped
    material and secret-ish key/value pairs are replaced.
    """
    if not text:
        return ""
    return "\n".join(_redact_line(line)
                     for line in str(text).splitlines())


def redact_obj(obj):
    """Deep-redact dict/list structures (e.g. before persisting payloads)."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            key = str(k)
            if _SECRET_KEY_RE.fullmatch(key + ":") or \
                    re.fullmatch(_SECRET_KEY, key, re.IGNORECASE):
                out[key] = _REDACTED
            else:
                out[key] = redact_obj(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    if isinstance(obj, str):
        return redact(obj)
    return obj


# ── approvals ────────────────────────────────────────────────────────────────

def approval_summary(agent: str, action: str, detail: str) -> str:
    """One line for the orchestrator's ApprovalGate + journal."""
    return f"{agent}: {action} — {detail}"[:200]


def approval_result(request, agent: str, action: str, detail: str):
    """The standard NEEDS_APPROVAL envelope for an outside-world action.

    `request` is an AgentRequest. The text is what the human sees BEFORE
    anything runs: what will happen, to whom, and how to say no.
    """
    from orchestrator.contracts import AgentResult
    return AgentResult(
        task_id=request.task_id,
        status=TaskStatus.NEEDS_APPROVAL,
        text=(f"This needs your approval before anything is sent:\n"
              f"  What:   {detail}\n"
              f"  Action: {action}\n"
              f"  Say 'approve' (or resolve the approval) to let it run — "
              f"nothing has left your machine yet."),
        data={"approval_summary": approval_summary(agent, action, detail),
              "needs_approval": True, "action": action},
        agent=agent,
    )
