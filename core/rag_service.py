"""
core/rag_service.py — the RAG SERVICE layer: the one API other agents call.

POSITION (deliberate — see core/rag.py)
    core/rag.py is the LIBRARY (storage, chunking, vectors — no callers of its
    own). This module is the front desk: identity, permissions, verdicts. Any
    caller — Jarvis's knowledge_base action, the dashboard REST API, the
    orchestrator rag_agent, a Hermes-driven script — talks to THIS, never to
    core/rag.py directly, so the rules below are enforced exactly once:

        * who may read  → collections: list[str]   (empty = read nothing)
        * who may write → can_write                (else every mutation is denied)
        * chunk budget  → max_chars (evidence sent to an LLM is bounded here,
                          never the whole document)

ACCESS MODEL (practical, file-backed)
    Two axes, checked on every call:

        collections  — a caller sees ONLY these knowledge collections. This
                       is category separation as a security boundary, not
                       just a search filter: trading books and personal
                       notes never leak into an agent that was not granted
                       them. Empty list = blind.
        can_write    — ingest/update/forget additionally require write.
                       Read-only agents (research, summarizers) get False.

    Every verdict is audit-logged with the caller name, so "who asked the
    library for this" has an answer. The Jarvis action and dashboard admin
    use CALLER_OWNER (all collections, can write); capability callers are
    minted in config/rag_callers.json, re-read on each use so access can be
    revoked without a restart.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from core import rag
from memory.config_manager import BASE_DIR

CALLERS_FILE = BASE_DIR / "config" / "rag_callers.json"

# The built-in owner: Jarvis itself. All collections, full write. Not listed
# in the callers file — always present, cannot be revoked by editing config.
CALLER_OWNER = "jarvis"

ALL_COLLECTIONS = "__all__"

_DEFAULT_CALLERS: dict = {
    # Example shape — an empty file means "owner only", which is the safe
    # default. Agents minted here are read-only until can_write is set.
    # {
    #   "trading_desk": {"collections": ["trading_books", "market_rules"],
    #                    "can_write": false},
    #   "ingest_bot":   {"collections": ["__all__"], "can_write": true}
    # }
}

_callers_lock = threading.Lock()


# ── callers / ACL ────────────────────────────────────────────────────────────

def _load_callers() -> dict:
    try:
        data = json.loads(CALLERS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return dict(_DEFAULT_CALLERS)


def _caller_spec(caller: str) -> dict:
    if not caller or not str(caller).strip():
        caller = CALLER_OWNER
    caller = str(caller).strip()
    if caller == CALLER_OWNER:
        return {"collections": [ALL_COLLECTIONS], "can_write": True}
    spec = _load_callers().get(caller)
    if not isinstance(spec, dict):
        return {"collections": [], "can_write": False}   # unknown → blind
    cols = spec.get("collections")
    return {
        "collections": [str(c) for c in cols] if isinstance(cols, list) else [],
        "can_write": bool(spec.get("can_write", False)),
    }


def caller_profile(caller: str) -> dict:
    """Introspection for callers and tests: what may this identity do?"""
    spec = _caller_spec(caller)
    cols = None if ALL_COLLECTIONS in spec["collections"] else spec["collections"]
    return {"caller": caller, "collections": cols,
            "can_write": spec["can_write"]}


def _audit_service(caller: str, action: str, detail: str) -> None:
    rag._audit(f"svc:{action}", f"[{caller}] {detail}")


# ── verdicts ─────────────────────────────────────────────────────────────────

def _deny(why: str) -> dict:
    return {"ok": False, "error": why}


def _check_collection(caller: str, spec: dict, collection: str | None) -> str | None:
    """An explicit collection argument must be inside the caller's grant."""
    if collection is None:
        return None
    if ALL_COLLECTIONS in spec["collections"]:
        return None
    if collection not in spec["collections"]:
        _audit_service(caller, "denied", f"collection '{collection}'")
        return (f"access denied: caller '{caller}' may not read collection "
                f"'{collection}'")
    return None


def _allowed_docs(spec: dict, collection: str | None = None) -> set[str] | None:
    """The doc whitelist this caller may see under rag.search's intersection
    rule: granted collections ∩ requested collection, indexed only."""
    if ALL_COLLECTIONS in spec["collections"]:
        return None if collection is None else _docs_in(collection)
    cols = ([collection] if collection
            else spec["collections"])
    docs: set[str] = set()
    for c in cols:
        docs |= _docs_in(c)
    return docs


def _docs_in(collection: str) -> set[str]:
    return {d for d, e in rag._load_registry().items()
            if e.get("collection") == collection
            and e.get("status") == "indexed"}


# ── the API ──────────────────────────────────────────────────────────────────

def ingest(caller: str, file_path: str, collection: str = rag.DEFAULT_COLLECTION,
           title: str | None = None, source: str = "service",
           access: str = "private", metadata: dict | None = None) -> dict:
    """Remember a document. Write-capable callers only."""
    spec = _caller_spec(caller)
    if not spec["can_write"]:
        _audit_service(caller, "denied", f"ingest '{Path(file_path).name}'")
        return _deny(f"access denied: caller '{caller}' cannot write to the "
                     "knowledge base")
    denied = _check_collection(caller, spec, collection)
    if denied:
        return _deny(denied)
    if collection not in rag.COLLECTIONS:
        return _deny(f"unknown collection '{collection}'. Valid: "
                     f"{', '.join(rag.COLLECTIONS)}")
    result = rag.ingest(file_path, collection=collection, title=title,
                        source=source, access=access, metadata=metadata)
    if result.get("ok") and not result.get("duplicate"):
        _audit_service(caller, "ingest",
                       f"{result.get('doc_id')} '{result.get('title')}' "
                       f"[{collection}:{access}]")
    return result


def ingest_text(caller: str, text: str, title: str,
                collection: str = rag.DEFAULT_COLLECTION,
                source: str = "service-text", suffix: str = "txt",
                access: str = "private",
                metadata: dict | None = None) -> dict:
    """Remember raw text (notes, transcripts, captions) without a file.

    A temp file keeps one extraction path: rag.ingest. The suffix is caller-
    chosen so markdown keeps its markdown, csv keeps its csv.
    """
    import tempfile
    spec = _caller_spec(caller)
    if not spec["can_write"]:
        _audit_service(caller, "denied", f"ingest_text '{title[:60]}'")
        return _deny(f"access denied: caller '{caller}' cannot write to the "
                     "knowledge base")
    denied = _check_collection(caller, spec, collection)
    if denied:
        return _deny(denied)
    if collection not in rag.COLLECTIONS:
        return _deny(f"unknown collection '{collection}'. Valid: "
                     f"{', '.join(rag.COLLECTIONS)}")
    if not (text or "").strip():
        return _deny("nothing to ingest — empty text")
    suffix = suffix if suffix in ("txt", "md", "log", "csv") else "txt"
    with tempfile.TemporaryDirectory(prefix="jarvis-rag-svc-") as td:
        tmp = Path(td) / f"{title[:60].strip().replace(' ', '_') or 'note'}.{suffix}"
        tmp.write_text(text, encoding="utf-8")
        result = rag.ingest(str(tmp), collection=collection, title=title,
                            source=source, access=access, metadata=metadata)
    if result.get("ok") and not result.get("duplicate"):
        _audit_service(caller, "ingest_text",
                       f"{result.get('doc_id')} '{result.get('title')}' "
                       f"[{collection}]")
    return result


def update(caller: str, doc_id: str, new_path: str | None = None,
           text: str | None = None, title: str | None = None,
           collection: str | None = None, access: str | None = None,
           metadata: dict | None = None) -> dict:
    """Update a document — metadata patch, or content replacement + reindex."""
    spec = _caller_spec(caller)
    if not spec["can_write"]:
        _audit_service(caller, "denied", f"update {doc_id}")
        return _deny(f"access denied: caller '{caller}' cannot modify the "
                     "knowledge base")
    denied = _check_collection(caller, spec, collection)
    if denied:
        return _deny(denied)
    result = rag.update(doc_id, new_path=new_path, text=text, title=title,
                        collection=collection, access=access, metadata=metadata)
    if result.get("ok"):
        _audit_service(caller, "update",
                       f"{doc_id} → '{result.get('title')}' v{result.get('version')}")
    return result


def forget(caller: str, doc_id: str) -> dict:
    """Remove a document from the library. Write-capable callers only."""
    spec = _caller_spec(caller)
    if not spec["can_write"]:
        _audit_service(caller, "denied", f"forget {doc_id}")
        return _deny(f"access denied: caller '{caller}' cannot delete from the "
                     "knowledge base")
    result = rag.forget(doc_id)
    if result.get("ok"):
        _audit_service(caller, "forget", f"{doc_id} '{result.get('forgotten')}'")
    return result


def search(caller: str, query: str, top_k: int = 5,
           collection: str | None = None, doc_id: str | None = None,
           max_chars: int = 12000) -> dict:
    """Retrieve evidence with citations, scoped to the caller's grants.

    Returns rag.search's result plus the citations block and a bounded
    evidence string ready to drop into a prompt. max_chars caps the evidence
    size (never the whole document — chunks only).
    """
    spec = _caller_spec(caller)
    if not spec["collections"]:
        # A caller with no grant probing the library is a security signal —
        # log it even though the search itself returns nothing.
        _audit_service(caller, "denied",
                       f"search (blind caller) q={query[:60]!r}")
    denied = _check_collection(caller, spec, collection)
    if denied:
        return _deny(denied)
    if doc_id:
        # Reading one document requires it to be inside the caller's grant.
        entry = rag._load_registry().get(doc_id)
        if entry is None:
            return _deny(f"unknown document: {doc_id}")
        if (ALL_COLLECTIONS not in spec["collections"]
                and entry.get("collection") not in spec["collections"]):
            _audit_service(caller, "denied", f"search doc {doc_id}")
            return _deny(f"access denied: document '{doc_id}' is outside "
                         f"caller '{caller}''s collections")

    result = rag.search(query, top_k=top_k, collection=collection,
                        doc_id=doc_id, allowed_docs=_allowed_docs(spec, collection))
    if result.get("ok") and result.get("results"):
        result["citations"] = format_citations(result["results"])
        evidence = "\n\n".join(
            f"[{i}] {c}\n{r['text']}"
            for i, (r, c) in enumerate(zip(result["results"], result["citations"]), 1))
        if len(evidence) > max_chars:
            evidence = evidence[:max_chars] + "\n[…]"
        result["evidence"] = evidence
        _audit_service(caller, "search",
                       f"k={len(result['results'])} mode={result.get('mode')} "
                       f"q={query[:60]!r}")
    return result


def list_documents(caller: str, collection: str | None = None) -> dict:
    """The library as this caller may see it."""
    spec = _caller_spec(caller)
    denied = _check_collection(caller, spec, collection)
    if denied:
        return _deny(denied)
    if ALL_COLLECTIONS in spec["collections"]:
        docs = rag.list_documents()
    else:
        docs = [d for d in rag.list_documents()
                if d.get("collection") in spec["collections"]]
    if collection:
        docs = [d for d in docs if d.get("collection") == collection]
    return {"ok": True, "documents": docs}


def stats(caller: str) -> dict:
    """Library stats scoped to the caller's collections."""
    spec = _caller_spec(caller)
    s = rag.stats()
    if ALL_COLLECTIONS not in spec["collections"]:
        s["collections"] = {c: n for c, n in s.get("collections", {}).items()
                            if c in spec["collections"]}
        s["documents"] = sum(s["collections"].values())
    return {"ok": True, "stats": s}


# ── citation formatting ──────────────────────────────────────────────────────

def format_citations(results: list[dict]) -> list[str]:
    """Human/LLM-readable citation per result: title, page, collection."""
    out: list[str] = []
    for r in results:
        cite = f"{r.get('source') or 'unknown source'}"
        if r.get("page"):
            cite += f", p.{r['page']}"
        if r.get("collection"):
            cite += f" [{r['collection']}]"
        if r.get("version") and r["version"] > 1:
            cite += f" (v{r['version']})"
        if r.get("vision"):
            cite += " (chart page — described visually)"
        out.append(cite)
    return out
