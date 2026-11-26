"""
orchestrator/agents/growth/safety.py — the Phase-18 guardrail layer.

Same contract as legal/safety.py: permission levels + refusals, all baked
into CODE (the level IS the answer; a prompt never decides).

PERMISSION LEVELS
    READ_ONLY           score/assess/analyze/plan/report. Never writes,
                        never contacts, never deletes.
    APPROVAL_REQUIRED   lead ingest (writes the vault), a consequential
                        lead ACTION (mark contacted / discard / edit),
                        saving rules into the vault (they change future
                        scores). Parks the EXACT payload.
    BLOCKED             sending campaigns/publishing/buying ads,
                        ordering outreach execution, deleting user data,
                        disabling the guardrail. Never runs, even
                        approved — marketing and self-eval produce
                        ANALYSIS and PLANS, not execution.

MARKETING TESTIMONY RULE
    Marketing output may only state what the research evidence shows —
    claims are cited [n]; a claim no evidence supports is labeled as a
    GUESS and is refused as a fact. The engine enforces this with the
    RAG strategies' support_score (same metric the research agent's
    verifier uses), so fabrication is structurally excluded.
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


# ── refusals ─────────────────────────────────────────────────────────────────

EXECUTION_REFUSAL = (
    "I produce plans, options and evidence — I don't execute outreach. "
    "Sending campaigns, publishing posts, buying ads and contacting leads "
    "are human-executed steps in the plan I hand you.")

DELETION_REFUSAL = (
    "Reflection reviews progress; it never deletes your data. Remove "
    "goals, leads or records yourself, or ask the system agent with "
    "explicit approval.")

PERMISSION_REFUSAL = (
    "That changed nothing: ingesting leads and saving scoring rules are "
    "approval-gated writes. Approve the parked action and it runs with "
    "the exact payload you saw.")


_DISCLOSURE_RE = re.compile(
    r"\b(?:buy|purchase|run|launch|schedule|send)\b.{0,30}"
    r"\b(?:ads?|campaign|post|e-?mails?|blast)\b"
    + r"|\b(?:send|blast)\b.{0,30}\b(?:leads?|outreach)\b"
    + r"|\bpost(?:ing)?\s+(?:this|it|that)\b.{0,30}\b(?:linkedin|x|twitter)\b"
    + r"|\b(?:delete|erase|drop|purge|wipe)\b.{0,30}"
      r"\b(?:goals?|activities|reviews?|data|records?)\b",
    re.IGNORECASE)


def is_execution_request(text: str) -> bool:
    """BLOCKED-class ask: run the outreach, not plan it."""
    t = " ".join(str(text or "").lower().split())
    return bool(_DISCLOSURE_RE.search(t))


# ── approvals (the productivity envelope) ────────────────────────────────────

def approval_summary(agent: str, action: str, detail: str) -> str:
    return f"{agent}: {action} — {detail}"[:200]


def approval_result(request, agent: str, action: str, detail: str):
    """The standard NEEDS_APPROVAL envelope for a gated write."""
    from orchestrator.contracts import AgentResult
    return AgentResult(
        task_id=request.task_id,
        status=TaskStatus.NEEDS_APPROVAL,
        text=(f"This needs your approval before anything changes:\n"
              f"  What:   {detail}\n"
              f"  Action: {action}\n"
              f"  Approve and the parked payload runs exactly as shown — "
              f"nothing has been written yet."),
        data={"approval_summary": approval_summary(agent, action, detail),
              "needs_approval": True, "action": action},
        agent=agent,
    )
