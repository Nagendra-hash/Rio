"""
orchestrator/agents/finance/safety.py — the Phase 14 guardrails.

Three jobs, all in code (no prompt):

1. THE ADVICE REFUSAL — the agent never makes investment decisions or
   financial recommendations from raw transaction data. An advice-shaped
   ask is refused with a fixed, honest message; the agent can still
   report the underlying DATA (spend totals, budget state) when asked
   separately.

2. PERMISSION LEVELS — same vocabulary as productivity/system_tools:

    READ_ONLY           summaries, budget status, recurring list, receipt
                        parse PREVIEW (nothing written).
    SAFE_ACTION         bounded local config: set budgets, edit category
                        list. Still never touches records.
    APPROVAL_REQUIRED   anything that writes or changes a financial
                        RECORD: import, add, edit, delete, recategorize.
                        Runs only after a live human approval, and then
                        only on the EXACT parked payload.
    BLOCKED             never runs, even approved: outbound payments or
                        transfers, bank/card credential access, connecting
                        new financial accounts, disabling the approval
                        gate itself.

3. REDACTION reuse — financial blobs pass productivity.security.redact
   before any user-facing reply or disk write (amounts and vendors are
   user data, not secrets, but token-shaped material found in pasted
   receipts must never survive).
"""
from __future__ import annotations

import re

from orchestrator.agents.productivity.security import (  # noqa: F401  (re-export)
    APPROVAL_REQUIRED, BLOCKED, READ_ONLY, SAFE_ACTION, approval_result,
    is_blocked, needs_approval, redact, redact_obj)

ADVICE_REFUSAL = (
    "I can report and organize your recorded expenses, but I don't make "
    "investment decisions or financial recommendations from transaction "
    "data — that needs a qualified human advisor. I'm happy to give you "
    "the numbers: summaries, category totals, budget state, or recurring "
    "payments.")

_ADVICE_RE = re.compile(
    r"\b(?:invest|investing|investment|investments|stocks?|shares?\s+"
    r"(?:buy|sell|purchase)|mutual\s+funds?|sips?\b|equity|portfolio|"
    r"cryptocurrenc|crypto|bitcoin|fixed\s+deposit|fds?\b|"
    r"should\s+i\s+(?:buy|sell|invest|put)|financial\s+(?:advice|planning|"
    r"plan|goals)|retirement\s+(?:plan|planning)|tax\s+saving|"
    r"insurance\s+policy|wealth\s+management|where\s+to\s+put\s+my\s+money)",
    re.IGNORECASE)


def is_advice_request(text: str) -> bool:
    """True when the ask is for an investment/financial decision rather
    than for data the agent can honestly report."""
    return bool(_ADVICE_RE.search(str(text or "")))


def permission_for(action: str) -> str:
    """The level IS the answer — there is no try-and-see."""
    if action in ("summary", "budget_status", "recurring", "parse",
                  "categories", "budgets_view"):
        return READ_ONLY
    if action in ("budget_set", "category_add", "category_remove"):
        return SAFE_ACTION
    if action in ("import", "add", "edit", "delete", "recategorize"):
        return APPROVAL_REQUIRED
    if action in ("pay", "transfer", "send_money", "connect_bank",
                  "read_credentials", "disable_approvals"):
        return BLOCKED
    return READ_ONLY
