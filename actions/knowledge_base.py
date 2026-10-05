"""
actions/knowledge_base.py — Jarvis's interface to the RAG knowledge layer.

POSITION (deliberate — see core/rag.py)
    One tool, four verbs. Jarvis's intent layer decides WHEN to call it (the
    decision tree lives in core/prompt.txt); this file only executes:

        search  → retrieve evidence for a question (returns chunks + citations)
        ingest  → REMEMBER a PDF permanently (read-once questions do NOT come
                  here — reading a file is what file_processor already does)
        forget  → remove a document from the library (audit keeps the deed)
        list    → what is remembered, and how much of it

    The tool formats every result with source + page so answers can cite WHERE
    a claim came from instead of asserting from thin air.
"""
from __future__ import annotations

import json
from pathlib import Path

from core import rag_service

# Jarvis IS the owner of the library: the service's CALLER_OWNER — all
# collections, full write. Every call goes through the service (audit + ACL
# engine), never straight to core/rag.py.
_CALLER = "jarvis"

_MAX_EVIDENCE_CHARS = 6000     # context budget for one answer's evidence


def knowledge_base(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    action = (params.get("action") or "search").strip().lower()

    if action == "search":
        return _do_search(params)
    if action == "ingest":
        return _do_ingest(params)
    if action == "forget":
        return _do_forget(params)
    if action == "list":
        return _do_list()
    return (f"Unknown knowledge_base action '{action}'. "
            "Use: search | ingest | forget | list")


# ── search ───────────────────────────────────────────────────────────────────

def _do_search(params: dict) -> str:
    query = (params.get("query") or "").strip()
    if not query:
        return "No query given for the knowledge search, sir."
    collection = (params.get("collection") or "").strip() or None

    result = rag_service.search(caller=_CALLER, query=query,
                                top_k=int(params.get("top_k", 5)),
                                collection=collection)
    if not result.get("ok"):
        return f"Knowledge search failed: {result.get('error')}"

    hits = result.get("results") or []
    if not hits:
        note = result.get("note")
        return (f"Nothing relevant in the knowledge base"
                f"{f' ({note})' if note else ''}, sir.")

    lines = []
    citations = result.get("citations") or []
    for i, r in enumerate(hits, start=1):
        cite = citations[i - 1] if i - 1 < len(citations) \
            else f"{r.get('source')}, page {r.get('page')}"
        lines.append(f"[{i}] {cite}  (score {r['score']})\n{r['text']}")

    evidence = "\n\n".join(lines)
    header = ("Evidence retrieved from your saved documents"
              f" (mode: {result['mode']}). "
              "Cite the source and page for any claim you make from it:\n\n")
    if len(header) + len(evidence) > _MAX_EVIDENCE_CHARS:
        evidence = evidence[:_MAX_EVIDENCE_CHARS - len(header)] + "\n[…]"
    return header + evidence


# ── ingest ───────────────────────────────────────────────────────────────────

def _do_ingest(params: dict) -> str:
    path = (params.get("path") or params.get("file_path") or "").strip()
    if not path:
        return ("No file path given, sir. I need the PDF's local path — "
                "ask file_processor to save it first if it arrived as an upload.")
    p = Path(path).expanduser()
    if not p.exists():
        return f"File not found: {path}"

    collection = (params.get("collection") or "").strip()
    if collection and collection not in rag_service.rag.COLLECTIONS:
        return (f"Unknown collection '{collection}'. Valid ones: "
                f"{', '.join(rag_service.rag.COLLECTIONS)}.")

    result = rag_service.ingest(
        caller=_CALLER, file_path=str(p),
        collection=collection or rag_service.rag.DEFAULT_COLLECTION,
        title=params.get("title"), source="jarvis")
    if not result.get("ok"):
        return f"Ingestion failed, sir: {result.get('error')}"

    if result.get("duplicate"):
        return (f"Already remembered, sir: '{result.get('title')}' "
                f"({result.get('pages')} pages). Nothing was duplicated.")
    label = (result.get("collection") or "").replace("_", " ")
    return (f"Added to {label}: {result.get('title')}. "
            f"Pages: {result.get('pages')}. Chunks: {result.get('chunks')}. "
            f"Searchable: yes.")


# ── forget ───────────────────────────────────────────────────────────────────

def _do_forget(params: dict) -> str:
    doc_id = (params.get("doc_id") or "").strip()
    if not doc_id:
        # Title fallback: forget by (partial) title, unambiguous wins only.
        needle = (params.get("title") or "").strip().lower()
        if not needle:
            return ("Give me a doc_id or title to forget, sir. "
                    "Call knowledge_base with action 'list' to see them.")
        matches = [d for d in rag_service.list_documents(caller=_CALLER)
                   .get("documents", [])
                   if needle in (d.get("title") or "").lower()]
        if not matches:
            return f"No remembered document matches '{needle}'."
        if len(matches) > 1:
            names = "; ".join(f"{m['doc_id']} = {m['title']}" for m in matches[:5])
            return (f"Several documents match — give me the exact doc_id: {names}")
        doc_id = matches[0]["doc_id"]

    result = rag_service.forget(caller=_CALLER, doc_id=doc_id)
    if not result.get("ok"):
        return f"Could not forget it: {result.get('error')}"
    return f"Forgotten: {result.get('forgotten')}. It is no longer searchable."


# ── list ─────────────────────────────────────────────────────────────────────

def _do_list() -> str:
    docs = rag_service.list_documents(caller=_CALLER).get("documents", [])
    if not docs:
        return ("The knowledge base is empty, sir. Send me a PDF and say "
                "'remember this document' to start the library.")
    s = rag_service.stats(caller=_CALLER).get("stats", {})
    coll = ", ".join(f"{k.replace('_', ' ')}: {v}"
                     for k, v in sorted(s["collections"].items()))
    lines = [f"Knowledge base: {s['documents']} documents, "
             f"{s['chunks']} searchable chunks ({coll})."]
    for d in docs[:12]:
        lines.append(f"• {d['doc_id']} — {d.get('title')} "
                     f"({d.get('page_count')}p, {d.get('collection')}, "
                     f"v{d.get('version', 1)}, {d.get('status')})")
    if len(docs) > 12:
        lines.append(f"… and {len(docs) - 12} more.")
    return "\n".join(lines)


# ── tool declaration (auto-discovered by core/action_loader.py) ─────────────

TOOL = {
    "name": "knowledge_base",
    "description": (
        "Searches and manages Jarvis's long-term document library (RAG) — "
        "trading books, strategies, research, personal notes the user has "
        "explicitly saved. Actions: 'search' (retrieve evidence chunks with "
        "source+page citations — use when a question is about the SAVED "
        "documents, and cite the source in your answer), 'ingest' (permanently "
        "REMEMBER a PDF the user asked to save; reading a PDF WITHOUT "
        "remembering it does not belong here — use file_processor instead), "
        "'forget' (remove one document), 'list' (show the library). "
        "NEVER present library content as current market data — old pages do "
        "not know today's price."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "search | ingest | forget | list"
            },
            "query": {
                "type": "STRING",
                "description": "Search mode: the question or topic to find in the saved documents"
            },
            "collection": {
                "type": "STRING",
                "description": "Optional: trading_books | strategies | research | personal_knowledge | market_rules | temporary"
            },
            "path": {
                "type": "STRING",
                "description": "Ingest mode: local path of the PDF to remember"
            },
            "title": {
                "type": "STRING",
                "description": "Ingest mode: optional display title (defaults to the file name); Forget mode: match by title"
            },
            "doc_id": {
                "type": "STRING",
                "description": "Forget mode: exact document id"
            },
            "top_k": {
                "type": "NUMBER",
                "description": "Search mode: max chunks to retrieve (default 5)"
            }
        },
        "required": ["action"]
    },
    "handler": knowledge_base,
}
