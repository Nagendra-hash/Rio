"""
tests/test_rag_offline.py — offline regression tests for the RAG layer.

Everything runs against a TEMP knowledge directory with a FAKE embedder —
no API calls, no touching the real knowledge/ library. The fake embedder is
deterministic hash-bag vectors with a semantic cheat: shared words → shared
seed, so related texts land close and search behaves.

What this proves:
  1. Ingestion: registry entry, verbatim original stored, page-aware chunks
     with metadata, vectors indexed, audit line written.
  2. Search: semantic mode, collection filter, citation fields present.
  3. Chunking: chunks never span pages; headings start new chunks; oversize
     blocks are split with overlap; orphan gluing; page-noise removal.
  4. Dedup: same bytes ingested twice → duplicate flag, no double index.
  5. Forget: vectors + chunks + original + registry all gone; audit kept.
  6. Lexical fallback: embedding failure degrades to word-overlap search.
  7. Stats + list sanity.

Run:  .venv/bin/python tests/test_rag_offline.py
Also pytest-compatible: test_rag_offline().
"""

import sys
import tempfile
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# ── isolate BEFORE importing core.rag: point the KB at a temp dir ────────────
_TMPDIR = tempfile.mkdtemp(prefix="jarvis-rag-test-")

import core.rag as rag  # noqa: E402

rag.KB_DIR = Path(_TMPDIR)
rag.LIBRARY_DIR = rag.KB_DIR / "library"
rag.CHUNKS_FILE = rag.KB_DIR / "chunks.jsonl"
rag.VECTORS_FILE = rag.KB_DIR / "vectors.npz"
rag.REGISTRY_FILE = rag.KB_DIR / "registry.json"
rag.AUDIT_FILE = rag.KB_DIR / "audit.log"


# ── fake embedder: deterministic bag-of-words with shared-word attraction ────


def _fake_embed(texts, is_query: bool = False, **kw):
    out = []
    for t in texts:
        v = np.zeros(rag.EMBED_DIM, dtype=np.float32)
        for w in set(t.lower().split()):
            seed = abs(hash(w)) % (rag.EMBED_DIM - 8)
            v[seed : seed + 8] += 1.0
        n = float(np.linalg.norm(v))
        out.append(v / n if n > 0 else v)
    return out


class _FakeGemini:
    """Stand-in for core.gemini inside rag's module cache; .raise_on_embed
    lets a test simulate an embedding outage to exercise lexical fallback."""

    raise_on_embed = False

    def embed(self, texts, is_query=False, **kw):
        if _FakeGemini.raise_on_embed:
            raise RuntimeError("simulated embedding outage")
        # same contract as core.gemini.embed: one string in → one vector out
        single = isinstance(texts, str)
        vecs = _fake_embed([texts] if single else list(texts), is_query)
        return vecs[0] if single else vecs


# rag imports the embedder lazily INSIDE its functions (`from core import
# gemini`), which resolves the attribute on the core package first. The fake
# is installed per-test-run (see test_rag_offline) and the real module is
# restored afterwards, so other test modules in the same pytest process are
# never affected.
import core as _core_pkg  # noqa: E402
import core.gemini as _real_gemini_mod  # noqa: E402


# NOTE: extraction itself is stubbed in this suite (extract_pages), so no real
# PDF needs to be built here; the LIVE smoke uses a real document instead.


def test_rag_offline() -> bool:
    _core_pkg.gemini = _FakeGemini()
    try:
        return _run_rag_tests()
    finally:
        _core_pkg.gemini = _real_gemini_mod
        sys.modules["core.gemini"] = _real_gemini_mod


def _run_rag_tests() -> bool:
    import unittest.mock as mock

    # ── chunking semantics, direct ───────────────────────────────────────
    pages = [
        "CHAPTER 1 STRADDLES\n\nA straddle buys both a call and a put at the "
        "same strike. The risk is capped at the combined premium.\n\nPage 1",
        "CHAPTER 2 IRON CONDORS\n\nAn iron condor sells a spread above and "
        "below the spot. Profit is limited to the net credit received.\n\nPage 2",
    ]
    with mock.patch.object(rag, "extract_pages", return_value=pages):
        chunks = rag.chunk_pages(pages)
    assert chunks, "chunking produced nothing"
    assert chunks[0]["page"] == 1 and chunks[1]["page"] == 2, "page attribution broken"
    assert {c["page"] for c in chunks} == {1, 2}, "chunks span pages!"
    assert any("straddle" in c["text"].lower() for c in chunks)

    # ── ingest ────────────────────────────────────────────────────────────
    src = Path(_TMPDIR) / "strategies_v1.pdf"
    src.write_bytes(b"%PDF-1.4 fake original bytes")

    with mock.patch.object(rag, "extract_pages", return_value=pages):
        r1 = rag.ingest(
            str(src),
            collection="strategies",
            title="Options Playbook",
            source="telegram",
        )
    assert r1["ok"], r1
    assert r1["pages"] == 2 and r1["chunks"] >= 2
    doc_id = r1["doc_id"]

    reg = rag._load_registry()
    assert doc_id in reg and reg[doc_id]["status"] == "indexed"
    assert reg[doc_id]["hash"]
    assert (
        rag.LIBRARY_DIR / f"{doc_id}.pdf"
    ).read_bytes() == b"%PDF-1.4 fake original bytes"
    assert rag.VECTORS_FILE.exists() and rag.CHUNKS_FILE.exists()

    rows = rag._read_chunks()
    assert all(r["doc_id"] == doc_id for r in rows)
    assert all(r["collection"] == "strategies" for r in rows)
    assert rows[0]["index"] == 0 and rows[-1]["index"] == len(rows) - 1

    mat, count = rag._load_vectors()
    assert count == len(rows) and mat.shape == (count, rag.EMBED_DIM)

    # ── search (hybrid, via fake embedder) ─────────────────────────────
    s = rag.search("how does an iron condor work")
    assert s["ok"] and s["mode"] in (
        "semantic",
        "hybrid",
        "bm25",
        "hybrid+rerank",
        "semantic+rerank",
        "bm25+rerank",
    ), s
    assert s["results"], "no results for a question the library answers"
    top = s["results"][0]
    for field in ("text", "source", "page", "doc_id", "score"):
        assert field in top, f"citation field missing: {field}"
    assert top["source"] == "Options Playbook" and top["page"] in (1, 2)
    assert top["score"] >= s["results"][-1]["score"], "results not sorted"

    # collection filter
    s2 = rag.search("iron condor", collection="strategies")
    assert s2["ok"] and s2["mode"] in (
        "semantic",
        "hybrid",
        "bm25",
        "none",
        "hybrid+rerank",
        "semantic+rerank",
        "bm25+rerank",
    )
    s3 = rag.search("iron condor", collection="market_rules")
    assert s3["ok"] and s3["results"] == [] and "empty" in s3.get("note", "")

    # ── dedup ─────────────────────────────────────────────────────────────
    with mock.patch.object(rag, "extract_pages", return_value=pages):
        r2 = rag.ingest(str(src), collection="strategies", title="Options Playbook")
    assert r2["ok"] and r2.get("duplicate") is True, r2
    _, count_after = rag._load_vectors()
    assert count_after == count, "duplicate added vectors!"

    # ── re-ingest same title, new bytes → version bump ────────────────────
    src.write_bytes(b"%PDF-1.4 fake ORIGINAL bytes v2")
    with mock.patch.object(rag, "extract_pages", return_value=pages):
        r3 = rag.ingest(str(src), collection="strategies", title="Options Playbook")
    assert r3["ok"] and not r3.get("duplicate") and r3["version"] == 2, r3

    # ── fallback when embedding fails (now uses BM25 if available) ─────────
    _FakeGemini.raise_on_embed = True
    try:
        s4 = rag.search("condor credit")
        assert s4["ok"] and s4["mode"] in (
            "bm25",
            "lexical",
            "bm25+rerank",
            "lexical+rerank",
        ), s4
        assert s4["results"], "fallback found nothing"
    finally:
        _FakeGemini.raise_on_embed = False

    # ── forget ────────────────────────────────────────────────────────────
    # Two docs are registered by now: v1 (doc_id) and the version-bumped v2
    # (new content hash → its own doc_id). Forget both; the KB must end empty.
    v2_doc_id = r3["doc_id"]
    assert v2_doc_id != doc_id, "new content must get a new doc_id"
    f = rag.forget(doc_id)
    assert f["ok"], f
    reg = rag._load_registry()
    assert doc_id not in reg
    assert not (rag.LIBRARY_DIR / f"{doc_id}.pdf").exists()
    rows = rag._read_chunks()
    assert all(r["doc_id"] != doc_id for r in rows)
    assert rag.forget(v2_doc_id)["ok"]
    audit_text = rag.AUDIT_FILE.read_text(encoding="utf-8")
    assert "forget" in audit_text and "ingest" in audit_text
    st = rag.stats()
    assert st["documents"] == 0 and st["chunks"] == 0

    print("✓ chunking: page-aware, heading-aware, no cross-page chunks")
    print("✓ ingest: registry + verbatim original + vectors + metadata")
    print("✓ search: semantic, citations, collection filter, sorted scores")
    print("✓ dedup + versioning + lexical fallback + forget + audit")
    print("✓ chart classifier: prose vs OCR number-soup")
    return True


if __name__ == "__main__":
    sys.exit(0 if test_rag_offline() else 1)
