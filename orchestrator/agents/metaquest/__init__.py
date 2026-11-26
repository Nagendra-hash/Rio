"""
orchestrator/agents/metaquest/ — Phase 15 Meta Quest Knowledge agent.

A knowledge specialist on the standard BaseAgent contract, built on the
common RAG + Research infrastructure:

    store.py           the metadata vault (memory/metaquest/): known
                       products/versions, doc registry metadata.
    agent.py           the BaseAgent: ingest (approval-gated), search /
                       answer (citations), corpus status, version checks.

THE RULES
    1. DEDICATED COLLECTION — the agent reads ONLY the `metaquest_docs`
       collection (config/rag_callers.json: metaquest_agent →
       [metaquest_docs]); documents it ingests carry product + version
       metadata so every answer can say WHICH model/firmware it is
       grounded in.
    2. Citations ALWAYS — every answer carries the `[n]` citation list
       with `Doc (product vX, collected Y)` shapes; an answer without
       citations is refused, not guessed.
    3. INGEST NEEDS APPROVAL — a manual/PDF/changelog added to the
       collection parks the exact payload behind NEEDS_APPROVAL and is
       written by the owner caller only on resume.
"""
