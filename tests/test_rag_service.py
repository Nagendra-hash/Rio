"""
tests/test_rag_service.py — offline tests for the RAG service layer
(core/rag_service.py over core/rag.py).

Everything runs against a TEMP knowledge directory with a FAKE embedder —
no API calls, no touching the real knowledge/ library. The PDF fixture is
built here with PIL (a genuine two-page PDF with a real text layer), so no
binary fixtures are committed.

What this proves (the deliverable's acceptance flow):
  1. INGESTION PIPELINE: a real PDF goes file → extract → clean → chunk →
     embed → index → registry, with verbatim original, metadata (filetype,
     access, source, metadata dict) and page-aware chunks.
  2. RETRIEVAL: "ask a question" → relevant chunks, sorted by score, full
     citations (title/page/collection/version), bounded evidence string.
  3. SOURCE TRACKING: citations name the document and page; scoped search
     (doc_id) returns only that document's chunks.
  4. COLLECTION SEPARATION + ACL: callers see only granted collections;
     unknown callers are blind; can_write gates every mutation; the owner
     sees everything.
  5. UPDATE: metadata patch (no reindex) and content replacement (new
     chunks replace old rows atomically; version bumps; old text gone).
  6. DELETE: forget removes vectors, chunks, original, registry entry.
  7. FORMATS: .txt/.md/.docx ingest through the same pipeline; a bogus
     suffix is refused by filetype detection.
  8. SERVICE-TEXT ingest: raw text → searchable, no file needed.
  9. Non-PDF originals are NOT sent to OCR (page-less formats pass through).

Run:  .venv/bin/python -m pytest tests/test_rag_service.py -q
"""

import json
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import core.rag as rag  # noqa: E402
from core import rag_service  # noqa: E402


@pytest.fixture(autouse=True)
def sandbox():
    """A FRESH knowledge directory + callers file for every test.

    core.rag keeps its paths as module globals, and every test module in this
    suite shares that module object — so the paths are (re-)pointed per test
    instead of once at import, keeping this file correct even when pytest runs
    several RAG test files in one process. The previous values are restored on
    teardown so a module that patched the paths at IMPORT time (e.g.
    tests/test_rag_offline.py, which runs after this file when both are
    listed on one pytest command line) gets its own temp KB back instead of
    this file's last one.
    """
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-rag-svc-test-"))
    saved = (
        rag.KB_DIR,
        rag.LIBRARY_DIR,
        rag.CHUNKS_FILE,
        rag.VECTORS_FILE,
        rag.REGISTRY_FILE,
        rag.AUDIT_FILE,
        rag_service.CALLERS_FILE,
    )
    rag.KB_DIR = tmp
    rag.LIBRARY_DIR = tmp / "library"
    rag.CHUNKS_FILE = tmp / "chunks.jsonl"
    rag.VECTORS_FILE = tmp / "vectors.npz"
    rag.REGISTRY_FILE = tmp / "registry.json"
    rag.AUDIT_FILE = tmp / "audit.log"
    rag_service.CALLERS_FILE = tmp / "rag_callers.json"
    yield tmp
    (
        rag.KB_DIR,
        rag.LIBRARY_DIR,
        rag.CHUNKS_FILE,
        rag.VECTORS_FILE,
        rag.REGISTRY_FILE,
        rag.AUDIT_FILE,
        rag_service.CALLERS_FILE,
    ) = saved


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
    def embed(self, texts, is_query=False, **kw):
        single = isinstance(texts, str)
        vecs = _fake_embed([texts] if single else list(texts), is_query)
        return vecs[0] if single else vecs


@pytest.fixture()
def fake_gemini(monkeypatch):
    # core imports gemini lazily ("from core import gemini" inside rag
    # functions resolves the attribute on the package first), so assigning
    # the attribute with raising=False is the same trick test_rag_offline
    # uses; monkeypatch restores the real module afterwards.
    import core as core_pkg

    monkeypatch.setattr(core_pkg, "gemini", _FakeGemini(), raising=False)
    yield


# ── fixture builders ─────────────────────────────────────────────────────────


def _make_pdf(path: Path, pages_text: list[str]) -> Path:
    """A genuine multi-page PDF WITH a real text layer, written in raw PDF
    syntax (no fixtures committed, no OCR detour: pdfplumber/PyPDF2 read it
    directly, so ingestion exercises the normal extract → chunk path)."""

    def esc(s: str) -> str:
        return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    n = len(pages_text)
    objs = [None] * (3 + 2 * n)
    objs[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = " ".join(f"{5 + 2 * i} 0 R" for i in range(n))
    objs[1] = f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode()
    objs[2] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    for i, text in enumerate(pages_text):
        lines = text.splitlines() or [""]
        parts = ["BT", "/F1 12 Tf", "72 720 Td"]
        for ln in lines:
            parts.append(f"({esc(ln)}) Tj")
            parts.append("0 -26 Td")
        parts.append("ET")
        stream = "\n".join(parts).encode("latin-1", "replace")
        cid = 4 + 2 * i
        objs[cid - 1] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"
        )
        objs[4 + 2 * i] = (
            f"<< /Type /Page /Parent 2 0 R "
            f"/MediaBox [0 0 612 792] /Contents {cid} 0 R "
            f"/Resources << /Font << /F1 3 0 R >> >> >>"
        ).encode()
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for idx, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{idx} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    path.write_bytes(bytes(out))
    return path


P1 = (
    "CHAPTER 1 STRADDLES\n\n"
    "A straddle buys both a call and a put at the same strike price. "
    "The maximum risk of a long straddle is capped at the combined premium "
    "paid for both legs.\n\n"
    "Traders use straddles when they expect a big move but cannot predict "
    "the direction of the move."
)
P2 = (
    "CHAPTER 2 IRON CONDORS\n\n"
    "An iron condor sells a bear call spread above the spot price and a "
    "bull put spread below it. The maximum profit is limited to the net "
    "credit received when the position is opened."
)

PDF_PAGES = [P1, P2]


# ── 1. ingestion pipeline ────────────────────────────────────────────────────


class TestIngestion:
    def test_pdf_upload_index_register(self, fake_gemini, tmp_path):
        src = _make_pdf(tmp_path / "options_playbook.pdf", PDF_PAGES)
        result = rag_service.ingest(
            caller="jarvis",
            file_path=str(src),
            collection="strategies",
            title="Options Playbook",
            source="telegram",
            metadata={"channel": "telegram", "chat_id": "12345"},
        )
        assert result["ok"], result
        assert result["pages"] == 2 and result["chunks"] >= 2
        doc_id = result["doc_id"]

        # verbatim original stored with the real suffix
        stored = rag.LIBRARY_DIR / f"{doc_id}.pdf"
        assert stored.exists()
        assert stored.read_bytes() == src.read_bytes()

        # registry: metadata + status
        reg = rag._load_registry()
        e = reg[doc_id]
        assert e["status"] == "indexed" and e["filetype"] == "pdf"
        assert e["collection"] == "strategies" and e["access"] == "private"
        assert e["metadata"]["channel"] == "telegram"
        assert e["version"] == 1

        # index rows: page-aware chunks with full metadata
        rows = rag._read_chunks()
        assert all(r["doc_id"] == doc_id for r in rows)
        assert {r["page"] for r in rows} == {1, 2}, "chunks span pages"
        assert all(r["filetype"] == "pdf" for r in rows)
        assert all(r["metadata"].get("chat_id") == "12345" for r in rows)

        # vectors: one per chunk
        mat, count = rag._load_vectors()
        assert count == len(rows) and mat.shape == (count, rag.EMBED_DIM)

    def test_duplicate_bytes_dedup(self, fake_gemini, tmp_path):
        src = _make_pdf(tmp_path / "dedup.pdf", PDF_PAGES)
        r1 = rag_service.ingest(
            caller="jarvis", file_path=str(src), collection="research", title="Dedup"
        )
        assert r1["ok"]
        r2 = rag_service.ingest(
            caller="jarvis", file_path=str(src), collection="research", title="Dedup"
        )
        assert r2["ok"] and r2.get("duplicate") is True
        _, count = rag._load_vectors()
        rows = [r for r in rag._read_chunks() if r["title"] == "Dedup"]
        assert count >= len(rows) and r2["doc_id"] == r1["doc_id"]

    def test_unknown_collection_refused(self, fake_gemini, tmp_path):
        src = _make_pdf(tmp_path / "x.pdf", PDF_PAGES)
        r = rag_service.ingest(
            caller="jarvis", file_path=str(src), collection="not_a_collection"
        )
        assert not r["ok"] and "unknown collection" in r["error"]


# ── 2/3. retrieval + citations ───────────────────────────────────────────────


class TestRetrieval:
    @pytest.fixture()
    def library(self, fake_gemini, tmp_path):
        src = _make_pdf(tmp_path / "playbook.pdf", PDF_PAGES)
        r = rag_service.ingest(
            caller="jarvis",
            file_path=str(src),
            collection="strategies",
            title="Options Playbook",
            source="telegram",
            metadata={"chat_id": "42"},
        )
        assert r["ok"], r
        return r["doc_id"]

    def test_question_returns_relevant_chunks_with_citations(self, library):
        s = rag_service.search(caller="jarvis", query="how does an iron condor work")
        assert s["ok"] and s["mode"] in (
            "semantic",
            "hybrid",
            "bm25",
            "hybrid+rerank",
            "semantic+rerank",
            "bm25+rerank",
        ), s
        hits = s["results"]
        assert hits, "no results for a question the library answers"
        assert all(
            h["score"] >= h2["score"] for h, h2 in zip(hits, hits[1:])
        ), "not sorted by score"
        top = hits[0]
        for field in (
            "text",
            "source",
            "page",
            "doc_id",
            "score",
            "collection",
            "filetype",
            "access",
            "metadata",
        ):
            assert field in top, f"citation field missing: {field}"
        assert top["source"] == "Options Playbook"
        assert top["page"] in (1, 2)

        # the citations block + bounded evidence string
        assert s["citations"] and "Options Playbook" in s["citations"][0]
        assert "p." in s["citations"][0]
        assert s["evidence"].startswith("[1] Options Playbook")
        assert len(s["evidence"]) <= 12000 + 10

    def test_scoped_search_only_returns_that_doc(self, library):
        s = rag_service.search(
            caller="jarvis", query="straddle premium", doc_id=library
        )
        assert s["ok"] and s["results"]
        assert {r["doc_id"] for r in s["results"]} == {library}

    def test_unknown_doc_is_error(self, library):
        s = rag_service.search(caller="jarvis", query="x", doc_id="nope123")
        assert not s["ok"]

    def test_no_llm_send(self, library, monkeypatch):
        """The acceptance criterion: retrieval must NOT ship whole documents
        to any model. Prove search embeds only the query (one text, short)."""
        sent = []

        def spy_embed(texts, is_query=False, **kw):
            items = [texts] if isinstance(texts, str) else list(texts)
            sent.extend(items)
            return _fake_embed(items, is_query)

        import core as core_pkg

        monkeypatch.setattr(
            core_pkg,
            "gemini",
            type("G", (), {"embed": staticmethod(spy_embed)})(),
            raising=False,
        )
        rag_service.search(caller="jarvis", query="iron condor max profit")
        assert sent == [
            "iron condor max profit"
        ], f"search sent {len(sent)} text(s) to the embedder, expected 1"


# ── 4. collections + ACL ─────────────────────────────────────────────────────


class TestAcl:
    @pytest.fixture(autouse=True)
    def _setup(self, fake_gemini, tmp_path):
        # three docs across two collections
        src = _make_pdf(tmp_path / "trading.pdf", PDF_PAGES)
        assert rag_service.ingest(
            caller="jarvis",
            file_path=str(src),
            collection="trading_books",
            title="Trading Book",
        )["ok"]
        # distinct text so sha256 dedup does not collapse it with trading.pdf
        src2 = _make_pdf(
            tmp_path / "research.pdf",
            [
                "QUARTERLY VOLATILITY RESEARCH\n\nRealized "
                "volatility clustered near historic lows while "
                "skew steepened across the front months."
            ],
        )
        assert rag_service.ingest(
            caller="jarvis",
            file_path=str(src2),
            collection="research",
            title="Research Report",
        )["ok"]
        assert rag_service.ingest_text(
            caller="jarvis",
            text="private journal note about my day",
            title="Journal",
            collection="personal_knowledge",
        )["ok"]

        # callers file: one scoped reader, one scoped writer, one blind
        rag_service.CALLERS_FILE.write_text(
            json.dumps(
                {
                    "research_reader": {
                        "collections": ["research"],
                        "can_write": False,
                    },
                    "notes_bot": {
                        "collections": ["personal_knowledge"],
                        "can_write": True,
                    },
                }
            ),
            encoding="utf-8",
        )
        yield
        if rag_service.CALLERS_FILE.exists():
            rag_service.CALLERS_FILE.unlink()

    def test_owner_sees_all_collections(self):
        s = rag_service.stats(caller="jarvis")
        assert s["ok"]
        assert set(s["stats"]["collections"]) >= {
            "trading_books",
            "research",
            "personal_knowledge",
        }
        docs = rag_service.list_documents(caller="jarvis")["documents"]
        assert len(docs) >= 3

    def test_scoped_caller_sees_only_granted_collection(self):
        docs = rag_service.list_documents(caller="research_reader")["documents"]
        assert {d["collection"] for d in docs} == {"research"}
        s = rag_service.search(caller="research_reader", query="iron condor net credit")
        assert s["ok"]
        assert {r["collection"] for r in s["results"]} <= {"research"}
        assert all("Trading Book" not in r["source"] for r in s["results"])

    def test_unknown_caller_is_blind(self):
        s = rag_service.search(caller="mallory", query="iron condor")
        assert s["ok"] and s["results"] == []
        docs = rag_service.list_documents(caller="mallory")["documents"]
        assert docs == []

    def test_scoped_caller_cannot_widen_scope(self):
        r = rag_service.search(
            caller="research_reader", query="x", collection="trading_books"
        )
        assert not r["ok"] and "access denied" in r["error"]

    def test_scoped_caller_cannot_read_out_of_scope_doc(self):
        # find the trading doc id as owner, then try to read it scoped
        docs = rag_service.list_documents(caller="jarvis")["documents"]
        trading = next(d for d in docs if d["collection"] == "trading_books")
        r = rag_service.search(
            caller="research_reader", query="x", doc_id=trading["doc_id"]
        )
        assert not r["ok"] and "access denied" in r["error"]

    def test_write_gate_blocks_read_only_caller(self, tmp_path):
        src = _make_pdf(tmp_path / "nope.pdf", PDF_PAGES)
        r = rag_service.ingest(caller="research_reader", file_path=str(src))
        assert not r["ok"] and "cannot write" in r["error"]
        f = rag_service.forget(caller="research_reader", doc_id="whatever")
        assert not f["ok"] and "cannot delete" in f["error"]
        u = rag_service.update(caller="research_reader", doc_id="whatever", title="New")
        assert not u["ok"] and "cannot modify" in u["error"]

    def test_write_capable_caller_can_ingest_into_granted_collection(self, tmp_path):
        src = _make_pdf(tmp_path / "note.pdf", PDF_PAGES)
        r = rag_service.ingest(
            caller="notes_bot",
            file_path=str(src),
            collection="personal_knowledge",
            title="Note",
        )
        assert r["ok"], r
        # …but not into a collection it was not granted
        r2 = rag_service.ingest(
            caller="notes_bot",
            file_path=str(src),
            collection="research",
            title="Sneaky",
        )
        assert not r2["ok"] and "access denied" in r2["error"]

    def test_denials_are_audited(self):
        rag_service.search(caller="mallory", query="condor")
        audit = rag.AUDIT_FILE.read_text(encoding="utf-8")
        assert "svc:denied" in audit and "[mallory]" in audit


# ── 5. update ────────────────────────────────────────────────────────────────


class TestUpdate:
    @pytest.fixture()
    def doc(self, fake_gemini, tmp_path):
        src = _make_pdf(tmp_path / "v1.pdf", PDF_PAGES)
        r = rag_service.ingest(
            caller="jarvis", file_path=str(src), collection="strategies", title="V1 Doc"
        )
        assert r["ok"], r
        return r["doc_id"]

    def test_metadata_patch_no_reindex(self, doc):
        before = rag.stats()["chunks"]
        r = rag_service.update(
            caller="jarvis",
            doc_id=doc,
            title="Renamed Doc",
            access="shared",
            metadata={"tag": "audit"},
        )
        assert r["ok"] and r.get("updated_fields")
        assert rag.stats()["chunks"] == before  # nothing re-indexed
        e = rag._load_registry()[doc]
        assert e["title"] == "Renamed Doc" and e["access"] == "shared"
        assert e["metadata"]["tag"] == "audit"
        # new metadata reaches search results
        s = rag_service.search(caller="jarvis", query="straddle")
        assert all(
            r2["access"] == "shared" for r2 in s["results"] if r2["doc_id"] == doc
        )

    def test_content_replacement_swaps_rows(self, doc, tmp_path):
        old_text_rows = [r for r in rag._read_chunks() if r["doc_id"] == doc]
        assert any("straddle" in r["text"].lower() for r in old_text_rows)

        new_src = _make_pdf(
            tmp_path / "v2.pdf",
            [
                "CHAPTER 9 BUTTERFLIES\n\n",
                "A butterfly uses three strikes. Wings are "
                "bought, the body sold. Risk is the debit paid.",
            ],
        )
        r = rag_service.update(
            caller="jarvis", doc_id=doc, new_path=str(new_src), title="V2 Doc"
        )
        assert r["ok"] and r["version"] == 2, r

        rows = [r2 for r2 in rag._read_chunks() if r2["doc_id"] == doc]
        assert rows and all(
            "straddle" not in r2["text"].lower() for r2 in rows
        ), "old chunks survived update"
        assert any("butterfl" in r2["text"].lower() for r2 in rows)
        # total chunk count unchanged by the swap (same doc, new content)
        mat, count = rag._load_vectors()
        assert count == len(rag._read_chunks())
        # searchable under the new content
        s = rag_service.search(caller="jarvis", query="butterfly wings debit")
        assert s["ok"] and any(r2["doc_id"] == doc for r2 in s["results"])

    def test_update_unknown_doc(self, doc):
        r = rag_service.update(caller="jarvis", doc_id="ghost", title="X")
        assert not r["ok"] and "unknown document" in r["error"]


# ── 6. delete ────────────────────────────────────────────────────────────────


class TestDelete:
    def test_forget_removes_everything(self, fake_gemini, tmp_path):
        src = _make_pdf(tmp_path / "gone.pdf", PDF_PAGES)
        r = rag_service.ingest(
            caller="jarvis",
            file_path=str(src),
            collection="temporary",
            title="Ephemeral",
        )
        assert r["ok"]
        doc_id = r["doc_id"]
        assert (rag.LIBRARY_DIR / f"{doc_id}.pdf").exists()

        f = rag_service.forget(caller="jarvis", doc_id=doc_id)
        assert f["ok"]
        assert doc_id not in rag._load_registry()
        assert not (rag.LIBRARY_DIR / f"{doc_id}.pdf").exists()
        assert all(row["doc_id"] != doc_id for row in rag._read_chunks())
        # audit keeps the deed
        assert "forget" in rag.AUDIT_FILE.read_text(encoding="utf-8")

    def test_forget_unknown_doc(self):
        f = rag_service.forget(caller="jarvis", doc_id="ghost")
        assert not f["ok"]


# ── 7. formats ───────────────────────────────────────────────────────────────


class TestFormats:
    def test_plain_text_ingest_and_search(self, fake_gemini, tmp_path):
        src = tmp_path / "setup_notes.md"
        src.write_text(
            "# Friday setup notes\n\nBuy the dip below the 20 EMA "
            "when RSI resets from overbought. Stop below the "
            "session low.",
            encoding="utf-8",
        )
        r = rag_service.ingest(
            caller="jarvis",
            file_path=str(src),
            collection="strategies",
            title="Setup Notes",
        )
        assert r["ok"] and r["filetype"] == "md", r
        stored = rag.LIBRARY_DIR / f"{r['doc_id']}.md"
        assert stored.exists()
        s = rag_service.search(caller="jarvis", query="RSI overbought reset")
        assert s["ok"] and any(r2["doc_id"] == r["doc_id"] for r2 in s["results"])

    def test_docx_ingest(self, fake_gemini, tmp_path):
        pytest.importorskip("docx")
        from docx import Document

        d = Document()
        d.add_paragraph(
            "The weekly review process checks every open position "
            "against its original thesis on Monday morning."
        )
        d.add_paragraph(
            "Positions that violated the thesis are closed "
            "regardless of profit or loss."
        )
        src = tmp_path / "review_process.docx"
        d.save(str(src))
        r = rag_service.ingest(
            caller="jarvis",
            file_path=str(src),
            collection="market_rules",
            title="Review Process",
        )
        assert r["ok"] and r["filetype"] == "docx", r
        assert r["chunks"] >= 1

    def test_unsupported_suffix_refused(self, fake_gemini, tmp_path):
        src = tmp_path / "binary.bin"
        src.write_bytes(b"\x00\x01\x02weird bytes")
        r = rag_service.ingest(caller="jarvis", file_path=str(src))
        assert not r["ok"] and "unsupported file type" in r["error"]

    def test_pageless_formats_skip_ocr(self, fake_gemini, tmp_path, monkeypatch):
        """A .md file must never reach OCR — prove by making any OCR call
        blow up the test."""

        def _boom(*a, **kw):
            raise AssertionError("OCR called for a page-less document")

        monkeypatch.setattr(rag, "ocr_pdf_pages", _boom)
        src = tmp_path / "plain.md"
        src.write_text("Just some plain markdown text. " * 10, encoding="utf-8")
        r = rag_service.ingest(
            caller="jarvis",
            file_path=str(src),
            collection="personal_knowledge",
            title="Plain",
        )
        assert r["ok"], r


# ── 8. text ingest ───────────────────────────────────────────────────────────


class TestTextIngest:
    def test_ingest_text_roundtrip(self, fake_gemini):
        r = rag_service.ingest_text(
            caller="jarvis",
            text="Morning checklist: check gap list, review overnight futures, "
            "place bracket orders after the first fifteen minutes.",
            title="Morning Checklist",
            collection="strategies",
            metadata={"origin": "whatsapp"},
        )
        assert r["ok"] and r["filetype"] == "txt", r
        s = rag_service.search(
            caller="jarvis", query="morning checklist bracket orders"
        )
        assert s["ok"] and any(c["doc_id"] == r["doc_id"] for c in s["results"])
        # citations carry collection + metadata
        top = next(c for c in s["results"] if c["doc_id"] == r["doc_id"])
        assert top["metadata"]["origin"] == "whatsapp"

    def test_ingest_text_empty_refused(self, fake_gemini):
        r = rag_service.ingest_text(caller="jarvis", text="   ", title="Empty")
        assert not r["ok"]


# ── 9. stats / profile ───────────────────────────────────────────────────────


class TestStatsProfile:
    def test_caller_profile_introspection(self):
        p = rag_service.caller_profile("jarvis")
        assert p["can_write"] is True and p["collections"] is None  # all
        p2 = rag_service.caller_profile("ghost")
        assert p2["can_write"] is False and p2["collections"] == []

    def test_stats_scoped(self, fake_gemini, tmp_path):
        src = _make_pdf(tmp_path / "s.pdf", PDF_PAGES)
        rag_service.ingest(
            caller="jarvis",
            file_path=str(src),
            collection="research",
            title="Scoped Stat",
        )
        s = rag_service.stats(caller="mallory")
        assert s["ok"] and s["stats"]["documents"] == 0
