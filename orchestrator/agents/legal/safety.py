"""
orchestrator/agents/legal/safety.py — the Phase 16 guardrail layer.

Two jobs, both baked into CODE (the finance-agent / health-agent pattern):

1. THE ADVICE GUARDRAIL — "do not present output as legal representation
   or definitive legal advice". is_advice_request() catches advice-shaped
   asks (should I sign, will I win, can they do this to me, is this
   enforceable, what would you do) and the agent DECLINES them with a
   fixed refusal. The agent reads, quotes, extracts, compares and FLAGS —
   it never tells you what you should do. DISCLAIMER_TEXT is appended to
   every substantive reply and says the same thing: this is document
   reading, not a lawyer, and a qualified legal review is encouraged
   before any consequential decision.

2. PERMISSION LEVELS — the system_tools.py pattern: the level IS the
   answer.

    READ_ONLY           read/analyze/summarize/compare/search existing
                        documents. No approval needed. Nothing is written.
    APPROVAL_REQUIRED   ingest a document INTO the vault (a write into an
                        isolated store), forget (delete) one. Parks the
                        EXACT payload behind NEEDS_APPROVAL.
    BLOCKED             signing/creating/modifying legal instruments,
                        drafting filings or notices to the other party,
                        contacting counterparties, accessing privileged
                        material, disabling the guardrail. Never runs,
                        even approved.

Every persisted blob passes redact_obj() first (same choke point as
every other vault).
"""
from __future__ import annotations

import re

from orchestrator.contracts import TaskStatus

# ── permission levels ────────────────────────────────────────────────────────

READ_ONLY = "read_only"
APPROVAL_REQUIRED = "approval_required"
BLOCKED = "blocked"


def is_blocked(level: str) -> bool:
    return level == BLOCKED


def needs_approval(level: str) -> bool:
    return level == APPROVAL_REQUIRED


# ── the advice guardrail ─────────────────────────────────────────────────────

ADVICE_REFUSAL = (
    "I can't advise on that. I read, quote, extract and compare legal "
    "documents — I don't make legal decisions or recommendations. For "
    "anything consequential (signing, terminating, suing, settling), "
    "please consult a qualified lawyer. I can still summarize terms, "
    "extract clauses, compare versions or flag passages worth a lawyer's "
    "attention.")

DISCLAIMER_TEXT = (
    "Not legal advice: this is document analysis only, not representation "
    "or a legal opinion. Have a qualified lawyer review anything "
    "consequential.")

_BLOCK_PATTERNS = (
    r"\b(?:should|shall|may)\s+(?:i|we|my|our)\s+(?:sign|accept|agree)\b",
    r"\b(?:should|would|can)\s+(?:i|we|they)\s+(?:sue|litigate|arbitrate)\b",
    r"\b(?:will|do)\s+i\s+win\b",
    r"\bis\s+(?:this|it|that)\s+(?:legal(ly)?\s+)?(?:enforceable|binding|valid|illegal|lawful)\b",
    r"\b(?:can|may)\s+they\s+(?:do|evict|terminate|keep|charge)\b",
    r"\bwhat\s+(?:should|would)\s+you\s+do\b",
    r"\b(?:draft|write|prepare)\s+.{0,30}\b(?:suit|complaint|petition|"
    r"legal\s+notice|cease\s+and\s+desist|will\b|power\s+of\s+attorney)\b",
    r"\bfight\s+(?:this|my)\s+case\b",
    r"\b(?:represent|defend)\s+(?:me|us|my)\b",
    r"\blesser\s+penalty\b",
)

_ADVICE_RE = re.compile("|".join(_BLOCK_PATTERNS), re.IGNORECASE)


def is_advice_request(text: str) -> bool:
    """Advice-shaped? Deterministic, in code — the guardrail does not
    depend on a model noticing."""
    t = " ".join(str(text or "").lower().split())
    return bool(_ADVICE_RE.search(t))


_BLOCKED_RE = re.compile(
    r"\b(?:draft|write|prepare)\b.{0,40}\b(?:legal\s+)?"
    r"(?:notice|will|power\s+of\s+attorney|affidavit|pleading)\b"
    + r"|\b(?:send|email|mail)\b.{0,30}\b(?:counterparty|other\s+party|"
      r"landlord|tenant|opposing)\b"
    + r"|\b(?:notari[sz]e|witness|sign)\b.{0,30}\b(?:this|that|the|my)\b"
    + r"|\baccess\s+(?:my\s+|the\s+)?privileged\b",
    re.IGNORECASE)


def is_blocked_request(text: str) -> bool:
    """BLOCKED-class asks (drafting instruments, contacting the other
    side, privileged access) — never run, even approved."""
    t = " ".join(str(text or "").lower().split())
    return bool(_BLOCKED_RE.search(t))


_BLOCKED_ACTIONS = {
    "sign", "execute_document", "send_notice", "contact_counterparty",
    "file_case", "withdraw_case", "notarize", "witness",
    "disable_guardrail", "access_privileged",
}


def action_level(action: str) -> str:
    """The level IS the answer."""
    a = str(action or "").strip().lower()
    if a in _BLOCKED_ACTIONS:
        return BLOCKED
    if a in ("ingest", "forget", "replace_version"):
        return APPROVAL_REQUIRED
    return READ_ONLY


# ── approvals (the productivity/security envelope) ──────────────────────────

def approval_summary(agent: str, action: str, detail: str) -> str:
    return f"{agent}: {action} — {detail}"[:200]


def approval_result(request, agent: str, action: str, detail: str):
    """The standard NEEDS_APPROVAL envelope for a vault write."""
    from orchestrator.contracts import AgentResult
    return AgentResult(
        task_id=request.task_id,
        status=TaskStatus.NEEDS_APPROVAL,
        text=(f"This needs your approval before anything is stored:\n"
              f"  What:   {detail}\n"
              f"  Action: {action}\n"
              f"  Say 'approve' (or resolve the approval) to let it run — "
              f"the vault has not been modified yet."),
        data={"approval_summary": approval_summary(agent, action, detail),
              "needs_approval": True, "action": action},
        agent=agent,
    )
