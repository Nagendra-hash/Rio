# Finance Agent (Mark III / JARVIS) — Phase 14

Personal finance and expense specialist on the standard BaseAgent
contract. Registered under the canonical name `finance_agent`; replaces
its Phase-1 stub when `JARVIS_FINANCE_AGENT=on` (shadow-mode default,
same convention as every other phase).

Files:

    orchestrator/agents/finance/schema.py         # expense schema + validation
    orchestrator/agents/finance/parser.py         # receipt + statement parsing
    orchestrator/agents/finance/engine.py         # budgets, recurring, overspend, summaries
    orchestrator/agents/finance/safety.py         # advice refusal + permission levels
    orchestrator/agents/finance/store.py          # the finance vault (memory/finance/)
    orchestrator/agents/finance/checkpoint.py     # crash-safe checkpoints
    orchestrator/agents/finance/finance_agent.py  # the BaseAgent
    tests/test_finance_agent.py                   # 65 offline tests

## The three hard rules

1. **No advice.** Investment decisions and financial recommendations are
   refused in code (`safety.is_advice_request` → `TaskStatus.DECLINED`
   with a fixed message). The agent reports and organizes RECORDED data
   — summaries, totals, budget state, recurring payments — and says so
   in every summary footer. It never turns transaction data into a
   recommendation.
2. **Separate vault.** Financial records live only in `memory/finance/`
   (relocated by `JARVIS_HOME`) — a 0700 directory of 0600 files that
   nothing else in the system reads. Nothing is ever fed to
   `core/rag_service` or general memory. Every blob passes
   `productivity.security.redact_obj()` before disk; the audit log holds
   ACTIONS ONLY (counts, ids, categories) — never amounts or vendors.
3. **Confirmation before writes.** Importing, adding, editing, deleting
   or recategorizing a financial record returns `NEEDS_APPROVAL` with
   the EXACT payload parked in the checkpoint (`save_for`). The resume
   path executes only that parked payload — never a fresh parse of the
   user's words — and a second resume is declined.

## Capabilities

| Ask | Behavior |
|---|---|
| paste a receipt / statement | deterministic parse (vendor, date, total, line items, qty, currency, confidence) as a PREVIEW — nothing written |
| import | validate → categorize → dedupe-check → park the exact rows for approval → resume commits (duplicates skipped, invalid rows reported) |
| add / edit / delete / recategorize | park the exact record/patch for approval → resume applies |
| categorize | user-defined categories first (word-boundary keyword match), then built-ins, then `other` |
| monthly budget | `budget_set` (immediate config, audited) + budget status: spent vs limit, `over` / `ahead of pace` labels |
| recurring | vendors with ≥3 stable-amount payments at ~monthly spacing → candidate with median amount and an estimated next-due |
| overspending | callouts for categories over limit and spending ahead of the month-to-date pace |
| summaries | monthly summary: totals by category, budget state, recurring list, "reports only" footer |

## Expense schema

One shape for every record: `id`, `date` (YYYY-MM-DD), `vendor`,
`amount` (>0, own currency — no FX conversion is ever invented),
`currency` (default INR; unknown codes are rejected), `category`,
`note`, `source` (receipt/statement/manual), optional `items`
(name/qty/price), `imported_at`. Dedupe key = date + vendor + amount +
currency.

## Approval flow

    user: import this receipt: …
    agent: NEEDS_APPROVAL — "import into the finance vault: 1 record(s) …
           Records: BIG BAZAR 1053.80 INR on 2026-10-07 (category: other).
           Nothing is saved until you approve."
    user: approve
    agent: resume → executes the parked rows → "Imported (approved by user):
           1 record(s) added, 0 duplicate(s) skipped, 0 invalid row(s) dropped."

## Permission levels

| Level | Actions |
|---|---|
| READ_ONLY | summary, budget status, recurring list, receipt parse preview, category/budget views |
| SAFE_ACTION | set budgets, add/remove user categories (config, audited, checkpointed) |
| APPROVAL_REQUIRED | import, add, edit, delete, recategorize |
| BLOCKED | payments/transfers, bank or card credential access, connecting financial accounts, disabling the approval gate — never runs, even approved |

## Wiring

- `TaskType.FINANCE` lane in the classifier (+ explicit routes
  "finance agent", "route this to finance", "[finance]").
- Registry gate `JARVIS_FINANCE_AGENT=on`; broken dependencies leave the
  stub registered instead of taking the router down.
- Checkpoint snapshots on every state change; parked approvals live in
  `memory/finance/checkpoints/approval-<task_id>.json`.
