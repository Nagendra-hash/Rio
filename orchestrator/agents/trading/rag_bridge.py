"""
orchestrator/agents/trading/rag_bridge.py — RAG for the owner's trading
books/rules, when configured.

The trading agent NEVER reads the knowledge base directly: it calls
core/rag_service as the caller identity "trading_agent", whose grants live in
config/rag_callers.json (the same ACL the rag_agent and coding_agent use).
A blind caller degrades to {ok: True, evidence: "", note: "..."} — analysis
continues without book rules, visibly, rather than failing.
"""
from __future__ import annotations


def books_context(query: str, top_k: int = 4,
                  collection: str | None = None,
                  max_chars: int = 4000) -> dict:
    """Fetch evidence from the owner's trading books for one question.

    Returns {ok, evidence, citations, error?, note?}. ok=True with empty
    evidence means "no configured access / nothing matched" — never an
    invented rule.
    """
    out = {"ok": True, "evidence": "", "citations": [], "note": ""}
    if not (query or "").strip():
        return {**out, "note": "empty query"}
    try:
        from core import rag_service
        res = rag_service.search(caller="trading_agent", query=query,
                                 top_k=int(top_k), collection=collection,
                                 max_chars=max_chars)
    except Exception as exc:
        return {**out, "note": f"knowledge base unavailable: "
                               f"{type(exc).__name__}: {exc}"}
    if not res.get("ok"):
        return {**out, "note": f"knowledge base denied: "
                               f"{res.get('error', 'unknown')}"}
    if not res.get("results"):
        return {**out, "note": "no relevant passages in the granted scope"}
    return {**out, "evidence": res.get("evidence", ""),
            "citations": res.get("citations", [])}


def rules_checklist(proposal: dict) -> dict:
    """One combined query for a proposal: pull the owner's rule reminders
    relevant to the direction + strategy, return them as a checklist block.

    This is EVIDENCE, not a verdict — the human reads the checklist alongside
    the proposal. The agent never claims compliance on the owner's behalf.
    """
    direction = str(proposal.get("direction") or "").lower()
    strategy = str(proposal.get("strategy") or "trend_follow").replace("_", " ")
    q = (f"{strategy} {direction} entry rules stop loss position sizing "
         f"risk management checklist")
    res = books_context(q)
    return {"ok": res["ok"], "evidence": res["evidence"],
            "citations": res["citations"], "note": res.get("note", ""),
            "usage": "attach to the approval card — human decides compliance"}
