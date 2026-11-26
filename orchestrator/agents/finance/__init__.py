"""
orchestrator/agents/finance/ — the Phase 14 Personal Finance agent.

A specialist on the standard BaseAgent contract that keeps its data in a
SEPARATE vault (memory/finance/ — never in general memory, never exposed
to the RAG layer) and wraps shared infrastructure instead of duplicating
it:

    schema.py         the expense schema: validation, normalization,
                      currency handling, dedupe keys.
    parser.py         deterministic receipt + statement-line parsing
                      (vendor, date, total, line items, confidence).
    engine.py         categorization (user-defined categories first),
                      monthly budget tracking, recurring-expense
                      detection, overspending/pace detection, summaries.
    safety.py         the advice guardrail (no investment decisions or
                      recommendations from transaction data, in code),
                      permission levels, BLOCKED list.
    store.py          the finance vault: 0600 files in a 0700 dir,
                      actions-only audit, wipe().
    checkpoint.py     crash-safe checkpoints (productivity contract).
    finance_agent.py  the BaseAgent: parses, previews, parks EVERY
                      import/edit behind an approval, executes the exact
                      approved payload on resume.

Disabled by default (shadow-mode convention): JARVIS_FINANCE_AGENT=on.

THE THREE HARD RULES
    1. NO ADVICE — the agent reports and organizes recorded data only;
       investment/financial-planning asks are refused in code, not by
       prompt.
    2. SEPARATE VAULT — financial records live in memory/finance/ with
       strict permissions and an actions-only audit; they never feed
       core/rag_service and never leave the vault unredacted.
    3. CONFIRMATION BEFORE WRITES — importing, adding, editing, deleting
       or recategorizing a financial record parks the EXACT payload
       behind NEEDS_APPROVAL and executes only that payload on resume.
"""
