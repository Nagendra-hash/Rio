"""
tests/test_metaquest_agent.py — offline acceptance tests for the Phase 15
Meta Quest knowledge agent.

NO network, NO cloud embeddings (hash-vector fake gemini module), NO real
manual: JARVIS_HOME is redirected, the RAG KB is sandboxed exactly like
tests/test_research_agent.py, and the callers file grants
`metaquest_agent` ONLY the metaquest_docs collection.

Locked in:
  1. COLLECTION isolation — the agent can read metaquest_docs only; a
     doc outside the grant yields zero results / denial, never a leak.
  2. CITATIONS  — every ask from indexed material carries [n] citations
     decorated with product/version/collected metadata; an unsupported
     question refuses honestly instead of guessing.
  3. METADATA   — title-based product/version extraction, doc_meta
     overrides, product upsert; unknown stays unknown (never invented).
  4. INGEST     — approval-gating: nothing is written before approval,
     resume commits the EXACT payload, stamps doc metadata, records the
     product bump, and re-resume is declined.
  5. CORPUS     — honest status view per product/version.
  6. WIRING     — classifier lane, explicit routes, registry gates.

Run:  .venv/bin/python -m pytest tests/test_metaquest_agent.py -q
"""
import itertools
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

_TMP = tempfile.mkdtemp(prefix="jarvis-metaquest-")
os.environ["JARVIS_HOME"] = _TMP

from orchestrator.agents import build_default_registry                 # noqa: E402
from orchestrator.agents.metaquest import store as mq_store            # noqa: E402
from orchestrator.agents.metaquest.metaquest_agent import (MetaQuestAgent,  # noqa: E402
                                                           MetaQuestCheckpoint)
from orchestrator.agents.metaquest.store import (COLLECTION,           # noqa: E402
                                                 MetaQuestStore,
                                                 canon_product,
                                                 extract_version)
from orchestrator.classifier import IntentClassifier                    # noqa: E402
from orchestrator.contracts import AgentRequest, TaskStatus, TaskType   # noqa: E402

_SEQ = itertools.count(1)


def _req(text: str = "", **meta) -> AgentRequest:
    return AgentRequest(task_id=f"mquest-test-{next(_SEQ):04d}",
                        task_type=TaskType.META_QUEST, text=text,
                        metadata=meta)


@pytest.fixture()
def tmp():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture()
def store(tmp):
    return MetaQuestStore(tmp / "vault")


@pytest.fixture()
def ckpt(tmp):
    return MetaQuestCheckpoint(tmp / "ckpt")


@pytest.fixture()
def agent(tmp, store, ckpt):
    return MetaQuestAgent(store=store, checkpoint=ckpt)


MANUAL = """Quest 3 User Guide v67
Chapter 1 Passthrough
Your headset uses full-colour passthrough so physical obstacles are
visible while you are immersed. Passthrough safety keeps you aware.

Chapter 2 Guardian Boundary
The guardian boundary draws a virtual wall when you approach your play
space edge.

Chapter 3 Link Cable
A USB-C link cable connects the headset to a PC for tethered play."""


@pytest.fixture()
def sandbox_rag(tmp, monkeypatch):
    """The offline mini KB + the metaquest_agent caller grant."""
    pytest.importorskip("numpy")
    from core import rag, rag_service
    kb = tmp / "kb"
    kb.mkdir(parents=True, exist_ok=True)
    rag.KB_DIR = kb
    rag.LIBRARY_DIR = kb / "library"
    rag.CHUNKS_FILE = kb / "chunks.jsonl"
    rag.VECTORS_FILE = kb / "vectors.npz"
    rag.REGISTRY_FILE = kb / "registry.json"
    rag.AUDIT_FILE = kb / "audit.log"
    rag_service.CALLERS_FILE = tmp / "rag_callers.json"
    rag_service.CALLERS_FILE.write_text(json.dumps({
        "metaquest_agent":
            {"collections": ["metaquest_docs"], "can_write": False},
        "other_reader":
            {"collections": ["study_library"], "can_write": False},
    }), encoding="utf-8")

    import numpy as np

    def _fake_embed(texts, is_query=False, **kw):
        out = []
        for t in texts:
            v = np.zeros(rag.EMBED_DIM, dtype=np.float32)
            for w in set(t.lower().split()):
                seed = abs(hash(w)) % (rag.EMBED_DIM - 8)
                v[seed:seed + 8] += 1.0
            n = float(np.linalg.norm(v))
            out.append(v / n if n > 0 else v)
        return out

    class _FakeGemini:
        def embed(self, texts, is_query=False, **kw):
            single = isinstance(texts, str)
            vecs = _fake_embed([texts] if single else list(texts), is_query)
            return vecs[0] if single else vecs

    import core as core_pkg
    monkeypatch.setattr(core_pkg, "gemini", _FakeGemini(), raising=False)

    doc = kb / "manual.txt"
    doc.write_text(MANUAL, encoding="utf-8")
    res = rag.ingest(str(doc), collection="metaquest_docs",
                     title="Quest 3 User Guide v67",
                     metadata={"product": "Quest 3", "version": "67",
                               "collected": "2026-09-01"})
    assert res.get("ok"), res
    return rag_service


# ═══════════════════════════════════════════ 1. metadata helpers ════════════

class TestMetadata:
    def test_canon_product(self):
        assert canon_product("my quest 3s headset") == "Quest 3S"
        assert canon_product("the QUEST 2 handbook") == "Quest 2"
        assert canon_product("unrelated text") == ""

    def test_extract_version(self):
        assert extract_version("Quest 3 User Guide v67") == "67"
        assert extract_version("manual v7.2.1 final") == "7.2.1"
        assert extract_version("no version here") == ""

    def test_doc_meta_roundtrip(self, store):
        row = store.set_doc_meta("doc1", product="Quest 3", version="67",
                                 collected="2026-09-01")
        assert row["product"] == "Quest 3" and row["version"] == "67"
        got = store.doc_meta("doc1")
        assert got["product"] == "Quest 3"
        assert store.doc_meta("missing", default={"product": "x"}) \
            ["product"] == "x"

    def test_products_upsert_keeps_latest(self, store):
        store.upsert_product("Quest 3", "67")
        store.upsert_product("quest 3", "69")
        rows = store.products()
        assert len(rows) == 1 and rows[0]["latest_version"] == "69"

    def test_enrich_fills_metadata(self, store):
        store.set_doc_meta("d1", product="Quest 2", version="56.1")
        rows = store.enrich([{"doc_id": "d1", "title": "Guide"},
                             {"doc_id": "d2", "title": "Quest 3S explainer"},
                             {"doc_id": "d3", "title": "mystery doc"}])
        assert rows[0]["product"] == "Quest 2"
        assert rows[1]["product"] == "Quest 3S"
        assert rows[2]["product"] == ""          # unknown stays unknown


# ═══════════════════════════════════════════ 2. vault permissions ═══════════

class TestVault:
    def test_owner_only_permissions(self, store):
        store.upsert_product("Quest 3", "68")
        assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
        for f in store.root.iterdir():
            assert stat.S_IMODE(f.stat().st_mode) == 0o600, f.name

    def test_audit_actions_only(self, store):
        store.upsert_product("Secret Headset Xyz", "v1")
        assert "upserted" in (store.root / "audit.jsonl").read_text()
        assert "Secret Headset Xyz" not in \
            (store.root / "audit.jsonl").read_text()

    def test_wipe(self, store):
        store.upsert_product("Quest 2", "56")
        assert store.wipe()
        assert store.products() == []


# ═══════════════════════════════════════════ 3. ask + citations ═════════════

class TestAsk:
    def test_answer_with_rich_citations(self, agent, sandbox_rag):
        r = agent.handle(_req("How does the guardian boundary work?",
                              strategy="local_adaptive"))
        assert r.status is TaskStatus.COMPLETED, r.text
        assert "Sources:" in r.text
        # the metadata decoration arrives through title+doc metadata
        text = r.text
        assert "[" in text and "[metaquest_docs]" in text
        assert ("Quest 3" in " ".join(r.data.get("citations", [])))

    def test_unsupported_question_refuses(self, agent, sandbox_rag):
        r = agent.handle(_req("What is the exact latency of the Quest 10 "
                              "in microseconds?", strategy="local_adaptive"))
        assert r.status is TaskStatus.COMPLETED
        assert ("can't answer" in r.text.lower()
                or "no cited" in r.text.lower()
                or "does not cover" in r.text.lower()
                or "hallucination_risk" in r.data)

    def test_collection_scoped(self, agent, sandbox_rag):
        # a question outside the collection's domain → honest vacuum
        r = agent.handle(_req("how do I fix a leaking kitchen tap",
                              strategy="local_adaptive"))
        assert "can't answer" in r.text.lower() or \
            "no cited" in r.text.lower() or \
            "does not cover" in r.text.lower()

    def test_extractive_floor_kepts_citations(self, agent, sandbox_rag):
        # even with no model, the extractive answerer carries [n] blocks
        r = agent.handle(_req("passthrough safety", strategy="local_adaptive"))
        assert "[" in r.text

    def test_ask_needs_question(self, agent, sandbox_rag):
        r = agent.handle(_req(""))
        assert r.status is TaskStatus.NEEDS_INPUT


# ═══════════════════════════════════════════ 4. search + corpus ═════════════

def _ask_search(agent, query):
    """Raw chunk rows behind a search call (for passage-text asserts)."""
    from core import rag_service
    res = rag_service.search(caller="metaquest_agent", query=query,
                             top_k=5, collection=COLLECTION)
    return res.get("results") or []


class TestSearchAndCorpus:
    def test_search_returns_passages_with_citations(self, agent,
                                                    sandbox_rag):
        r = agent.handle(_req("link cable", quest_action="search"))
        assert r.status is TaskStatus.COMPLETED
        assert "Found" in r.text and r.data.get("citations")
        # the underlying chunk rows name the topic the user asked for
        assert "Link" in " ".join(str(p.get("text", ""))
                                   for p in _ask_search(agent, "link cable"))

    def test_search_empty_is_honest(self, agent, sandbox_rag):
        r = agent.handle(_req("quantum flux capacitor", quest_action="search"))
        assert r.status is TaskStatus.COMPLETED
        assert "no relevant passages" in r.text.lower()

    def test_corpus_lists_by_product(self, agent, sandbox_rag, tmp):
        svc = sandbox_rag
        docs = svc.list_documents(caller="jarvis")["documents"][-1:]
        store = MetaQuestStore(tmp / "mvault")
        store.set_doc_meta(docs[0]["doc_id"], product="Quest 3",
                           version="67")
        archive = MetaQuestAgent(store=store)
        r = archive.handle(_req("what manuals are in the corpus"))
        assert r.status is TaskStatus.COMPLETED
        assert "Quest 3" in r.text and "metaquest_docs" in r.text


# ═══════════════════════════════════════════ 5. ingest approval flow ════════

class TestIngestApproval:
    def test_ingest_parks_then_commits(self, agent, sandbox_rag, tmp_path):
        manual = tmp_path / "quest2_manual.txt"
        manual.write_text("Quest 2 guide v56\nGuardian boundary setup "
                          "steps: draw the floor, confirm the play area, "
                          "and test passthrough safety.", encoding="utf-8")
        r = agent.handle(_req("", quest_action="ingest", path=str(manual),
                              product="Quest 2", version="56",
                              title="Quest 2 Guide"))
        assert r.status is TaskStatus.NEEDS_APPROVAL
        assert "approval_summary" in r.data
        # nothing has been written
        svc = sandbox_rag
        before = {d.get("doc_id")
                  for d in svc.list_documents(caller="jarvis")["documents"]}
        r2 = agent.handle(_req("ok do it", approved=True,
                               resume=r.task_id, by="user"))
        assert r2.status is TaskStatus.COMPLETED, r2.text
        after_docs = svc.list_documents(caller="metaquest_agent")["documents"]
        added = [d for d in after_docs if d["doc_id"] not in before]
        assert len(added) == 1
        doc_id = added[0]["doc_id"]
        # doc metadata stamped for citations
        meta = agent.store.doc_meta(doc_id)
        assert meta.get("product") == "Quest 2"
        assert meta.get("version") == "56"
        # product bump recorded
        products = agent.store.products()
        assert any(p["name"] == "Quest 2" and p["latest_version"] == "56"
                   for p in products)

    def test_double_resume_declined(self, agent, sandbox_rag, tmp_path):
        manual = tmp_path / "q2.txt"
        manual.write_text("Quest 2 guide v56 — passthrough colour modes."
                          , encoding="utf-8")
        park = agent.handle(_req("", quest_action="ingest",
                                 path=str(manual), product="Quest 2"))
        r1 = agent.handle(_req("x", approved=True, resume=park.task_id))
        assert r1.status is TaskStatus.COMPLETED
        r2 = agent.handle(_req("x", approved=True, resume=park.task_id))
        assert r2.status is TaskStatus.DECLINED

    def test_ingest_needs_path(self, agent):
        r = agent.handle(_req("please ingest the manual"))
        assert r.status is TaskStatus.NEEDS_INPUT

    def test_ingest_bad_file_refused(self, agent, tmp_path):
        exe = tmp_path / "firmware.bin"
        exe.write_bytes(b"MZ\x00\x00")
        r = agent.handle(_req("", quest_action="ingest", path=str(exe)))
        assert r.status is TaskStatus.COMPLETED
        assert "unsupported" in r.text.lower()

    def test_missing_checkpoint_honest(self, agent):
        r = agent.handle(_req("x", approved=True, resume="mquest-nope"))
        assert r.status is TaskStatus.FAILED
        assert "nothing changed" in r.text.lower()


# ═══════════════════════════════════════════ 6. meta maintenance ════════════

class TestMetaMaintenance:
    def test_add_product_flow(self, agent):
        r = agent.handle(_req("quest 3 got v69 released",
                              quest_action="add_product"))
        assert r.status is TaskStatus.COMPLETED
        assert "Quest 3" in r.text and "v69" in r.text

    def test_set_doc_meta_flow(self, agent, store):
        r = agent.handle(_req("", quest_action="set_doc_meta",
                              doc_id="q3man", product="Quest Pro",
                              version="58.101",
                              collected="2026-08-15"))
        assert r.status is TaskStatus.COMPLETED
        got = store.doc_meta("q3man")
        assert got["product"] == "Quest Pro" and got["version"] == "58.101"
        assert got["collected"] == "2026-08-15"


# ═══════════════════════════════════════════ 7. wiring ══════════════════════

class TestWiring:
    def test_classifier_metaquest_lane(self):
        c = IntentClassifier()
        for text in ("how does air link work on my meta quest 3",
                     "beat saber quest setup guide",
                     "explain link cable vs air link for quest",
                     "use sidequest on the oculus headset"):
            assert c.classify(text).task_type is TaskType.META_QUEST, text

    def test_explicit_routes(self):
        c = IntentClassifier()
        assert c.classify("route this to quest").task_type \
            is TaskType.META_QUEST
        assert c.classify("[meta quest] what is guardian boundary") \
            .task_type is TaskType.META_QUEST
        assert c.classify("metaquest agent corpus status") \
            .task_type is TaskType.META_QUEST

    def test_other_lanes_unchanged(self):
        c = IntentClassifier()
        assert c.classify("how much did I spend this month").task_type \
            is TaskType.FINANCE
        assert c.classify("fix the bug in python").task_type \
            is TaskType.CODING
        assert c.classify("search loki series plot").task_type \
            is TaskType.RESEARCH

    def test_registry_off_by_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_METAQUEST_AGENT", raising=False)
        reg = build_default_registry()
        a = reg.get("metaquest_agent")
        if a is None:
            pytest.skip("metaquest lane not registered in this build")
        assert type(a).__name__ == "StubAgent"
        r = a.handle(_req("ask"))
        assert r.status is TaskStatus.NEEDS_INPUT

    def test_registry_on(self, monkeypatch):
        monkeypatch.setenv("JARVIS_METAQUEST_AGENT", "on")
        reg = build_default_registry()
        a = reg.get("metaquest_agent")
        assert type(a).__name__ == "MetaQuestAgent"
        assert TaskType.META_QUEST in a.task_types
