"""
orchestrator/agents/legal/ — the Phase 16 Legal Document Analysis agent.

A specialist on the standard BaseAgent contract that reads legal PDFs,
extracts clauses, summarizes sections, compares document versions,
retrieves passages with page citations, and flags items needing HUMAN
review — WITHOUT giving legal advice, and with every write (ingest,
forget) parked behind an approval and isolated legal-document storage:

    schema.py       document/clause shapes + validation
    pages.py        PDF (and txt/docx) reader: page-wise text, hashes,
                    heading/section structure with page attribution
    clauses.py      clause extraction + extractive summarization
    diff.py         version comparison (added/removed/changed clauses)
    risk.py         risk-pattern scan + open questions for human review
    vault.py        the isolated vault (memory/legal/), 0600 in 0700,
                    actions-only audit, version chains, clause cache
    safety.py       the advice guardrail (no legal advice in code),
                    permission levels, BLOCKED list, disclaimer
    checkpoint.py   crash-safe checkpoints (productivity contract)
    legal_agent.py  the BaseAgent wiring the pipeline

Registered behind JARVIS_LEGAL_AGENT=on (shadow-mode convention).
Documents with retrieval live ONLY in the legal_docs RAG collection,
granted exclusively to the `legal_agent` caller in
config/rag_callers.json — no other agent may read them.

DISCLAIMER — printed on every substantive reply:
    Not legal representation or definitive legal advice; a qualified
    legal review is encouraged before consequential decisions.
"""
