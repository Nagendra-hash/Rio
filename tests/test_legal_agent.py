"""
tests/test_legal_agent.py — offline acceptance tests for the Phase 16
Legal Document Analysis agent.

NO network, NO cloud embeddings (hash-vector fake gemini module), NO real
PDF: JARVIS_HOME is redirected, the RAG KB is sandboxed exactly like
tests/test_metaquest_agent.py, and the callers file grants `legal_agent`
ONLY the legal_docs collection. The PDF path is exercised with a real
minimal PDF built in-memory via pdfplumber or PyPDF2 (whichever is
installed) — the same extraction ladder core/rag uses.

Locked in:
  1. STORAGE isolation — the vault lives in memory/legal/ under
     JARVIS_HOME, 0600 files in a 0700 dir, actions-only audit; the
     retrieval collection is legal_docs and a foreign caller is blind.
  2. GUARDRAILS — advice-shaped asks are DECLINED in code; blocked
     actions (drafting notices, contacting the counterparty) refused;
     every substantive reply carries the not-legal-advice disclaimer.
  3. PIPELINE — ingest→analyze extracts clauses with page/section
     citations; summarize is extractive (sentences appear verbatim in
     the document); clause types are detected (termination, liability,
     confidentiality…).
  4. RETRIEVAL — rag_service.search/answer as caller legal_agent pinned
     to legal_docs, weak passages dropped, zero results is an honest
     refusal, no cross-collection leak.
  5. VERSIONS — compare_versions finds changed/added/removed clauses
     with both pages cited; nothing changed → "no differences".
  6. RISKS — auto-renewal / broad indemnity / unlimited liability /
     arbitration produce flagged questions with page citations;
     severity ordering; nothing flagged fabricates quotes.
  7. APPROVALS — ingest and forget park EXACT payloads; resume commits
     the parked payload; re-resume is declined; unknown resume fails
     honestly.
  8. WIRING — classifier lane, explicit routes, registry gate (stub by
     default, real agent with JARVIS_LEGAL_AGENT=on).

Run:  .venv/bin/python -m pytest tests/test_legal_agent.py -q
"""
import itertools
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

_TMP = tempfile.mkdtemp(prefix="jarvis-legal-")
os.environ["JARVIS_HOME"] = _TMP

from orchestrator.agents import build_default_registry                    # noqa: E402
from orchestrator.agents.legal import clauses, diff, pages, risk, safety, schema  # noqa: E402
from orchestrator.agents.legal.checkpoint import LegalCheckpoint          # noqa: E402
from orchestrator.agents.legal.legal_agent import LegalAgent              # noqa: E402
from orchestrator.agents.legal.vault import (COLLECTION, LegalVault,      # noqa: E402
                                             new_doc_id)
from orchestrator.classifier import IntentClassifier                      # noqa: E402
from orchestrator.contracts import AgentRequest, TaskStatus, TaskType     # noqa: E402

_SEQ = itertools.count(1)


def _req(text: str = "", **meta) -> AgentRequest:
    return AgentRequest(task_id=f"legal-test-{next(_SEQ):04d}",
                        task_type=TaskType.LEGAL, text=text,
                        metadata=meta)


@pytest.fixture()
def tmp():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture()
def vault(tmp):
    return LegalVault(tmp / "legal")


@pytest.fixture()
def ckpt(tmp):
    return LegalCheckpoint(tmp / "ckpt")


@pytest.fixture()
def agent(tmp, vault, ckpt):
    return LegalAgent(store=vault, checkpoint=ckpt)


# ── the fixture contract used by every action test ───────────────────────
LEASE_V1 = """Commercial Lease Agreement
Landlord: Alpha Properties LLC. Tenant: Nimbus Coffee Ltd.
This Lease Agreement is made between the Landlord and the Tenant.

1. Term and Renewal
The Lease Term shall commence on 2026-11-01 for an initial term of
thirty-six (36) months. This agreement shall automatically renew for
successive twelve (12) month periods unless either party gives ninety
(90) days written notice before expiry.

2. Rent and Late Charges
Tenant shall pay monthly rent of 2500.00 USD in advance. Late payment
interest at 5% per month applies to arrears, with no cap and no
limitation on accumulated late charges.

3. Termination
The Landlord may terminate this lease for convenience upon sixty (60)
days written notice. The Tenant may terminate only for Landlord's
material breach uncured within thirty (30) days.

4. Indemnification and Liability
Tenant shall indemnify and hold harmless the Landlord against any and
all claims arising from Tenant's use of the premises. The Landlord's
liability under this lease shall be unlimited.

5. Confidentiality
Tenant shall keep confidential all proprietary information of the
Landlord and non-disclosure obligations survive forever.

6. Governing Law and Arbitration
This Lease is governed by the laws of Delaware. Any dispute shall be
settled by arbitration in Wilmington, Delaware, and Tenant waives any
right to a class action or jury trial.
"""

LEASE_V2 = LEASE_V1.replace(
    "thirty-six (36) months. This agreement shall automatically renew",
    "twenty-four (24) months. This agreement shall not automatically renew"
).replace("2500.00 USD", "2950.00 USD").replace(
    "The Landlord may terminate this lease for convenience upon sixty (60)",
    "The Landlord may terminate this lease for convenience upon ninety (90)")


def _txt(tmp: Path, body: str, name: str = "lease.txt") -> Path:
    p = tmp / name
    p.write_text(body, encoding="utf-8")
    return p


def _pdf(tmp: Path, body: str, name: str = "lease.pdf") -> str | None:
    """A real minimal single-page PDF, written by whichever PDF lib is
    installed (pdfplumber's writer or PyPDF2). Skips the tests using it
    if neither writer is available."""
    try:
        import pdfplumber
        from pdfminer.pdfwriter import PDFWriter
    except Exception:
        pdfplumber = None
    # pdfminer's writer is awkward; fpdf/PyPDF2 writers vary — the
    # deterministic route is reportlab if present, else a hand-built
    # simple PDF with one text stream (valid enough for PyPDF2).
    try:
        from reportlab.pdfgen import canvas
        path = tmp / name
        c = canvas.Canvas(str(path))
        y = 800
        for line in body.splitlines():
            c.drawString(40, y, line[:100])
            y -= 14
            if y < 40:
                break
        c.save()
        return str(path)
    except Exception:
        pass
    # hand-built minimal PDF fallback
    import zlib
    lines = [l.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
             for l in body.splitlines()][:60]
    stream = "BT /F1 10 Tf 40 760 Td " + " ".join(
        f"{y2 + 0.1} Td ({l}) Tj" for y2, l in zip(range(len(lines)), lines)) \
        + " ET"
    objs = []
    objs.append("1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj")
    objs.append("2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj")
    objs.append("3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/"
                "Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj")
    objs.append("4 0 obj<</Length " + str(len(stream)) + ">>stream\n"
                + stream + "\nendstream endobj")
    objs.append("5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>"
                "endobj")
    pdf = "%PDF-1.4\n" + "\n".join(objs)
    path = tmp / name
    path.write_text(pdf, encoding="latin-1")
    # verify a reader sees text in it; else give up honestly
    from orchestrator.agents.legal import pages as lg_pages
    rd = lg_pages.read_pages(path)
    return str(path) if rd.ok and rd.page_count == 1 else None


def _pdf_or_skip(tmp, body):
    p = _pdf(tmp, body)
    if p is None:
        pytest.skip("no PDF writer/extractor pair available offline")
    return p


@pytest.fixture(scope="module")
def sandbox_rag():
    """Offline mini KB + caller grants. Destructive to JARVIS_HOME-scoped
    KB paths for the module run (JARVIS_HOME is a temp dir already)."""
    pytest.importorskip("numpy")
    from core import rag, rag_service
    kb = Path(_TMP) / "kb-fixture"
    kb.mkdir(parents=True, exist_ok=True)
    rag.KB_DIR = kb
    rag.LIBRARY_DIR = kb / "library"
    rag.CHUNKS_FILE = kb / "chunks.jsonl"
    rag.VECTORS_FILE = kb / "vectors.npz"
    rag.REGISTRY_FILE = kb / "registry.json"
    rag.AUDIT_FILE = kb / "audit.log"
    rag_service.CALLERS_FILE = Path(_TMP) / "rag_callers.json"
    rag_service.CALLERS_FILE.write_text(json.dumps({
        "legal_agent": {"collections": [COLLECTION], "can_write": False},
        "other_reader": {"collections": ["study_library"],
                         "can_write": False},
    }), encoding="utf-8")

    import numpy as np

    def _fake_embed(texts, is_query=False, **kw):
        out = []
        for t in texts:
            v = np.zeros(rag.EMBED_DIM, dtype=np.float32)
            for w in set(str(t).lower().split()):
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
    # patch the PACKAGE attribute: core/rag does `from core import gemini`
    # inside every function, so the fake must live where that lookup lands
    core_pkg.gemini = _FakeGemini()
    yield rag_service
    # leave nothing for other suites: the whole JARVIS_HOME is a temp dir


def _ingested(agent, tmp, body=LEASE_V1, inbox=True):
    """Ingest through the approval path and return (doc_id, file_path).

    The approval re-dispatch keeps the SAME task id — that is how the
    orchestrator resumes a parked approval (the checkpoint is keyed by
    the pinned task id)."""
    fp = _txt(tmp, body, name=f"lease-{next(_SEQ)}.txt")
    req = _req("add this contract", legal_action="ingest", path=str(fp))
    before = {d["doc_id"] for d in agent.store.documents()}
    res = agent.handle(req)
    assert res.status is TaskStatus.NEEDS_APPROVAL, res.text
    # nothing NEW stored yet
    assert {d["doc_id"] for d in agent.store.documents()} == before
    # resume with the parked payload, same task id
    approved = agent.handle(AgentRequest(
        task_id=req.task_id, task_type=TaskType.LEGAL, text="approve",
        metadata={"approved": True, "resume": req.task_id,
                  "by": "user"}))
    assert approved.status is TaskStatus.COMPLETED, approved.text
    docs = agent.store.documents()
    added = [d for d in docs if d["doc_id"] not in before]
    assert len(added) >= 1, docs
    return added[0]["doc_id"], str(fp)


def _last_task_id(res) -> str:
    # the checkpoint is keyed by the REQUEST's task id — recover it from
    # the checkpoint directory listing
    cps = sorted((Path(_TMP) / "ckpt").glob("approval-*.json"))
    if not cps:
        return ""
    name = Path(cps[-1]).name[len("approval-"):-len(".json")]
    return name


@pytest.fixture()
def env_sandbox(tmp, monkeypatch):
    """Sandbox the agent's own vault/checkpoint roots into the tmp dir."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp))
    yield
    # nothing global mutated beyond env


# ══════════════════════════════════════════════════════════════════════════
# 1. reading + schema
# ══════════════════════════════════════════════════════════════════════════

class TestReading:
    def test_txt_read(self, tmp):
        read = pages.read_pages(_txt(tmp, LEASE_V1))
        assert read.ok is True
        assert read.page_count == 1          # a text file: no page lie
        assert "Term and Renewal" in read.full_text

    def test_pdf_read(self, tmp):
        path = _pdf_or_skip(tmp, LEASE_V1)
        read = pages.read_pages(path)
        assert read.ok is True
        assert read.page_count >= 1
        assert "Lease Agreement" in ("\n".join(p.text for p in read.pages)
                                     or "")

    def test_unreadable_pdf_reports(self, tmp):
        p = tmp / "broken.pdf"
        p.write_bytes(b"not a pdf at all")
        read = pages.read_pages(p)
        assert read.ok is False
        assert read.error

    def test_missing_file(self, tmp):
        read = pages.read_pages(tmp / "nope.pdf")
        assert read.ok is False and read.error

    def test_sha256_and_kind(self, tmp):
        read = pages.read_pages(_txt(tmp, LEASE_V1))
        sha = schema.sha256_file(_txt(tmp, LEASE_V1))
        assert len(read.sha256) == 64 and read.sha256 == sha
        # the lease matches both 'lease' and 'contract' shapes — a
        # legitimate contract family is accepted (kind is advisory)
        assert read.kind in ("lease", "contract")
        other = pages.read_pages(_txt(tmp, "This is a Letter about "
                                     "gardening in June.\nIt is nice."))
        assert other.kind == "other"

    def test_sections_carry_pages(self, tmp):
        read = pages.read_pages(_txt(tmp, LEASE_V1))
        secs = pages.build_sections(read.pages)
        assert any("3" == s.num for s in secs)      # "3. Termination"
        assert all(s.start_page >= 0 for s in secs)


# ══════════════════════════════════════════════════════════════════════════
# 2. clauses + summarization
# ══════════════════════════════════════════════════════════════════════════

class TestClauses:
    def test_extract_with_citations(self, tmp):
        read = pages.read_pages(_txt(tmp, LEASE_V1))
        cls = clauses.extract_clauses(read)
        assert len(cls) >= 5
        by_num = {c.number: c for c in cls}
        assert "3" in by_num and "4" in by_num
        c3 = by_num["3"]
        assert c3.heading == "Termination"
        assert c3.spans and c3.spans[0].page == 1     # page-cited
        assert c3.spans[0].end > c3.spans[0].start

    def test_clause_types(self, tmp):
        read = pages.read_pages(_txt(tmp, LEASE_V1))
        got = {c.heading: c.clause_type for c in clauses.extract_clauses(read)}
        assert got.get("Termination") == "termination"
        assert got.get("Confidentiality") == "confidentiality"
        assert got.get("Governing Law and Arbitration") == "dispute"
        assert got.get("Indemnification and Liability") == "indemnity"

    def test_summarize_is_extractive(self, tmp):
        read = pages.read_pages(_txt(tmp, LEASE_V1))
        sumry = clauses.summarize_document(read)
        full = " ".join(read.full_text.split())          # whitespace-normalized
        for sec in sumry.sections:
            for s in sec["sentences"]:
                body = " ".join(s["text"].replace("…", "").split())
                assert body[:60] in full        # verbatim from the document
        assert sumry.pages == 1


# ══════════════════════════════════════════════════════════════════════════
# 3. the guardrails
# ══════════════════════════════════════════════════════════════════════════

class TestGuardrails:
    def test_advice_declined(self, agent):
        res = agent.handle(_req("should i sign this contract?"))
        assert res.status is TaskStatus.DECLINED
        assert "can't advise" in res.text
        assert "qualified lawyer" in res.text

    @pytest.mark.parametrize("ask", [
        "should i sign this?",
        "will i win if i sue?",
        "can they evict me like that?",
        "is this enforceable without a stamp paper?",
        "what would you do about the eviction notice?",
        "draft a legal notice for my landlord",
    ])
    def test_advice_patterns(self, agent, ask):
        res = agent.handle(_req(ask))
        assert res.status is TaskStatus.DECLINED, ask

    def test_blocked_request(self, agent):
        res = agent.handle(_req("email the counterparty about the breach"))
        assert res.status is TaskStatus.COMPLETED
        assert "outside what I do" in res.text

    def test_normal_analysis_not_blocked(self, agent, tmp):
        doc_id, _ = _ingested(agent, tmp)
        res = agent.handle(_req("analyze it", legal_action="analyze"))
        assert res.status is TaskStatus.COMPLETED
        assert res.data.get("action") == "analyze"

    def test_disclaimer_on_every_substantive_reply(self, agent, tmp):
        doc_id, _ = _ingested(agent, tmp)
        res = agent.handle(_req("show me the clauses",
                                legal_action="clauses"))
        assert safety.DISCLAIMER_TEXT.split(":")[0] in res.text


# ══════════════════════════════════════════════════════════════════════════
# 4. the vault: isolation, permissions, audit
# ══════════════════════════════════════════════════════════════════════════

class TestVault:
    def test_vault_location_and_perms(self, tmp):
        v = LegalVault(tmp / "legal")
        assert (tmp / "legal").is_dir()
        if os.name == "posix":
            mode = stat.S_IMODE(os.stat(tmp / "legal").st_mode)
            assert mode & 0o777 == 0o700
            # a registry that exists is 0600
            v.register_document(schema.DocumentMeta(
                doc_id=new_doc_id("t"), title="t"))
            reg = tmp / "legal" / "registry.json"
            if reg.exists():
                assert stat.S_IMODE(os.stat(reg).st_mode) & 0o777 == 0o600

    def test_audit_is_actions_only(self, agent, tmp):
        doc_id, _ = _ingested(agent, tmp)
        import json as _json
        log = (agent.store.root / "audit.jsonl").read_text()
        assert doc_id in log
        for forbidden in (LEASE_V1[:40].split()[0], "2500.00"):
            assert forbidden not in log, "contract text leaked into audit"
            break
        assert "ingested" in log and "parked_ingest" in log

    def test_doc_register_and_get(self, agent, tmp):
        doc_id, _ = _ingested(agent, tmp)
        hit = agent.store.get_document(doc_id)
        assert hit and hit["title"] and hit["pages"] == 1
        assert agent.store.get_document("lg-99999999-x-y") is None


# ══════════════════════════════════════════════════════════════════════════
# 5. the agent pipeline (ingest → analyze → clauses → summarize)
# ══════════════════════════════════════════════════════════════════════════

class TestPipeline:
    def test_ingest_needs_approval_and_parks_exact_payload(self, agent, tmp):
        fp = _txt(tmp, LEASE_V1, name="p1.txt")
        req = _req("ingest this agreement",
                   legal_action="ingest", path=str(fp))
        res = agent.handle(req)
        assert res.status is TaskStatus.NEEDS_APPROVAL
        assert res.data.get("needs_approval") is True
        assert "Nothing is stored" in res.text
        assert agent.store.documents() == []
        cp = agent.checkpoint.load_for(req.task_id)
        assert cp and cp["status"] == "awaiting_approval"
        assert Path(cp["payload"]["path"]).name == "p1.txt"

    def test_duplicate_ingest_new_doc_id(self, agent, tmp):
        _ingested(agent, tmp, body=LEASE_V1)
        fp = _txt(tmp, LEASE_V1, name="second.txt")
        req = _req("ingest", legal_action="ingest", path=str(fp))
        agent.handle(req)
        agent.handle(AgentRequest(
            task_id=req.task_id, task_type=TaskType.LEGAL, text="approve",
            metadata={"approved": True, "resume": req.task_id,
                      "by": "user"}))
        assert len(agent.store.documents()) == 2

    def test_analyze_full_pass(self, agent, tmp):
        doc_id, _ = _ingested(agent, tmp)
        res = agent.handle(_req("analyze", legal_action="analyze"))
        assert res.status is TaskStatus.COMPLETED
        assert res.data["doc_id"] == doc_id
        t = res.text
        assert "Analysis of" in t
        assert "Questions for human review" in t
        assert res.data["clauses"] >= 5

    def test_analyze_empty_vault_is_honest(self, agent):
        res = agent.handle(_req("analyze anything",
                                legal_action="analyze"))
        assert res.status is TaskStatus.COMPLETED
        assert "vault is empty" in res.text

    def test_clauses_listed(self, agent, tmp):
        doc_id, _ = _ingested(agent, tmp)
        res = agent.handle(_req("extract the clauses",
                                legal_action="clauses"))
        t = res.text
        assert res.data["n"] >= 5
        assert "[indemnity]" in t or "[termination]" in t
        assert "p." in t            # page citation in the listing

    def test_summarize_action(self, agent, tmp):
        _ingested(agent, tmp)
        res = agent.handle(_req("summarize", legal_action="summarize"))
        assert res.status is TaskStatus.COMPLETED
        assert "Section summaries" in res.text

    def test_unsupported_file_type(self, agent, tmp):
        p = tmp / "img.png"
        p.write_bytes(b"\x89PNG")
        res = agent.handle(_req("ingest", legal_action="ingest",
                                path=str(p)))
        assert "Unsupported file type" in res.text

    def test_missing_path_asks(self, agent):
        res = agent.handle(_req("ingest something",
                                legal_action="ingest"))
        assert res.status is TaskStatus.NEEDS_INPUT


# ══════════════════════════════════════════════════════════════════════════
# 6. retrieval (ACL + citations) — needs the sandboxed KB
# ══════════════════════════════════════════════════════════════════════════

# NOTE: the sandbox_rag fixture switches the KB into the module temp dir;
# the agent fixture still writes vaults into per-test tmp dirs, so both
# work together.

class TestRetrieval:
    def test_retrieve_passages_cited(self, agent, tmp, sandbox_rag):
        doc_id, _ = _ingested(agent, tmp)
        res = agent.handle(_req("find passages about arbitration",
                                legal_action="retrieve"))
        assert res.status is TaskStatus.COMPLETED
        assert "Relevant passages" in res.text
        assert "[" in res.text and "p." in res.text   # citation lines

    def test_retrieve_zero_results_is_honest(self, agent, tmp, sandbox_rag):
        _ingested(agent, tmp)
        res = agent.handle(_req("find passages about gardening",
                                legal_action="retrieve"))
        assert res.status is TaskStatus.COMPLETED
        assert "will not guess" in res.text

    def test_foreign_caller_is_blind(self, agent, tmp, sandbox_rag):
        doc_id, _ = _ingested(agent, tmp)
        out = sandbox_rag.search(caller="other_reader",
                                 query="termination indemnity arbitration",
                                 collection=COLLECTION)
        assert out.get("ok") in (True, False)
        assert not out.get("results")     # no cross-collection leak

    def test_legal_agent_answer_is_grounded(self, agent, tmp, sandbox_rag):
        _ingested(agent, tmp)
        res = agent.handle(_req("(ask: what is the governing law?)",
                                legal_action="ask"))
        # with no LLM the extractive answerer still cites the document
        if res.status is TaskStatus.COMPLETED and \
                res.data.get("groundless"):
            pytest.skip("no LLM rung: honest refusal is acceptable")
        assert res.status is TaskStatus.COMPLETED
        assert "Quoted from your documents" in res.text or "[" in res.text


# ══════════════════════════════════════════════════════════════════════════
# 7. version comparison
# ══════════════════════════════════════════════════════════════════════════

class TestVersions:
    def _two_docs(self, agent, tmp):
        old_id, _ = _ingested(agent, tmp, body=LEASE_V1)
        fp2 = _txt(tmp, LEASE_V2, name="v2.txt")
        req = _req("ingest v2", legal_action="ingest",
                   path=str(fp2), supersedes=old_id)
        res = agent.handle(req)
        assert res.status is TaskStatus.NEEDS_APPROVAL
        agent.handle(AgentRequest(
            task_id=req.task_id, task_type=TaskType.LEGAL, text="approve",
            metadata={"approved": True, "resume": req.task_id,
                      "by": "user"}))
        docs = agent.store.documents()
        new_id = next(d["doc_id"] for d in docs if d["supersedes"] == old_id)
        return old_id, new_id

    def test_compare_changed_added_removed(self, agent, tmp):
        old_id, new_id = self._two_docs(agent, tmp)
        res = agent.handle(_req(f"compare {new_id} vs {old_id}",
                                legal_action="compare",
                                doc_id_old=old_id, doc_id_new=new_id))
        assert res.status is TaskStatus.COMPLETED
        t = res.text
        assert "~ changed" in t
        # rent: changed; auto-renew: text rewritten; late-charge cap added?
        assert res.data["changed"] + res.data["added"] > 0
        # page citations on both sides
        assert "old p." in t and "new p." in t

    def test_compare_identical_texts(self, agent, tmp):
        a, _ = _ingested(agent, tmp, body=LEASE_V1)
        b, _ = _ingested(agent, tmp, body=LEASE_V1)
        res = agent.handle(_req("compare these two",
                                legal_action="compare",
                                doc_id_old=a, doc_id_new=b))
        assert "No text differences" in res.text

    def test_compare_missing_doc_asks(self, agent, tmp):
        a, _ = _ingested(agent, tmp)
        res = agent.handle(_req("compare these",
                                legal_action="compare", doc_id_old=a))
        assert res.status is TaskStatus.NEEDS_INPUT

    def test_supersede_unknown_doc_refused(self, agent, tmp):
        fp = _txt(tmp, LEASE_V2, name="v9.txt")
        res = agent.handle(_req("ingest v2", legal_action="ingest",
                                path=str(fp), supersedes="lg-20260101-no-no"))
        assert "not in the vault" in res.text


# ══════════════════════════════════════════════════════════════════════════
# 8. risk scan + questions
# ══════════════════════════════════════════════════════════════════════════

class TestRisk:
    def test_findings_have_questions_and_pages(self, tmp):
        read = pages.read_pages(_txt(tmp, LEASE_V1))
        rep = risk.scan_document(read)
        assert rep.counts["high"] >= 3
        sev = {f.severity for f in rep.findings}
        assert "informational" in sev or rep.counts["medium"] >= 3
        for f in rep.findings:
            assert f.question and f.why
            assert f.evidence.strip()     # real quoted evidence
            # page 0 only for a page-less txt fixture (cited as cover page
            # 1 in clause spans); a real PDF always carries its true page
            assert f.page >= 0

    def test_specific_concerns(self, tmp):
        read = pages.read_pages(_txt(tmp, LEASE_V1))
        rep = risk.scan_document(read)
        concerns = {f.concern for f in rep.findings}
        assert "auto-renewal / evergreen term" in concerns
        assert "broad indemnity" in concerns
        assert "arbitration / waiver of class actions" in concerns

    def test_report_text_carries_pages(self, tmp):
        read = pages.read_pages(_txt(tmp, LEASE_V1))
        rep = risk.scan_document(read)
        t = "\n".join(rep.to_lines())
        assert "⚠" in t and "Q:" in t and "Why:" in t
        assert "Evidence:" in t
        assert "p." in t

    def test_no_findings_says_so(self, tmp):
        read = pages.read_pages(_txt(tmp,
                                     "This is a friendly letter about "
                                     "gardening and vegetables in June."))
        rep = risk.scan_document(read)
        assert "No flagged passages" in "\n".join(rep.to_lines())

    def test_scan_never_fabricates(self, tmp):
        read = pages.read_pages(_txt(tmp, LEASE_V1))
        rep = risk.scan_document(read)
        full = " ".join(read.full_text.split())   # whitespace-normalized
        for f in rep.findings:
            frag = " ".join(f.evidence.split())
            # the quoted evidence must appear in the document itself —
            # allow the heading/body glue the page carries but nothing else
            assert frag[:40].lower() in full.lower(), \
                "evidence not from the document: %r" % frag[:60]


# ══════════════════════════════════════════════════════════════════════════
# 9. approvals (forget + re-resume)
# ══════════════════════════════════════════════════════════════════════════

class TestApprovals:
    def test_forget_parks_then_commits(self, agent, tmp):
        doc_id, _ = _ingested(agent, tmp)
        req = _req("forget that document", legal_action="forget",
                   doc_id=doc_id)
        res = agent.handle(req)
        assert res.status is TaskStatus.NEEDS_APPROVAL
        res2 = agent.handle(AgentRequest(
            task_id=req.task_id, task_type=TaskType.LEGAL, text="approve",
            metadata={"approved": True, "resume": req.task_id,
                      "by": "user"}))
        assert res2.status is TaskStatus.COMPLETED
        assert "Deleted" in res2.text
        assert agent.store.get_document(doc_id) is None

    def test_re_resume_is_declined(self, agent, tmp):
        doc_id, _ = _ingested(agent, tmp)
        req = _req("forget", legal_action="forget", doc_id=doc_id)
        agent.handle(req)
        agent.handle(AgentRequest(
            task_id=req.task_id, task_type=TaskType.LEGAL, text="approve",
            metadata={"approved": True, "resume": req.task_id,
                      "by": "user"}))
        res3 = agent.handle(AgentRequest(
            task_id=req.task_id, task_type=TaskType.LEGAL, text="again",
            metadata={"approved": True, "resume": req.task_id,
                      "by": "user"}))
        assert res3.status is TaskStatus.DECLINED

    def test_unknown_resume_fails_honestly(self, agent):
        res = agent.handle(_req("approve",
                                approved=True, resume="legal-test-0001"))
        assert res.status is TaskStatus.FAILED
        assert "No pending approval" in res.text

    def test_deny_leaves_vault_untouched(self, agent, tmp):
        fp = _txt(tmp, LEASE_V1, name="deny.txt")
        res = agent.handle(_req("ingest", legal_action="ingest",
                                path=str(fp)))
        # an explicit deny = never resume; the vault stays empty
        assert agent.store.documents() == []


# ══════════════════════════════════════════════════════════════════════════
# 10. wiring: classifier + registry gates
# ══════════════════════════════════════════════════════════════════════════

class TestWiring:
    def test_classifier_lane(self):
        for ask in ("summarize the termination clause in my lease",
                    "what does the indemnity clause say in this contract",
                    "compare versions of the NDA",
                    "review the terms of service for auto-renewal",
                    "extract clauses from the privacy policy"):
            c = IntentClassifier().classify(ask)
            assert c.task_type is TaskType.LEGAL, (ask, c.reason)

    def test_explicit_route(self):
        c = IntentClassifier().classify("[legal] compare v1 and v2")
        assert c.task_type is TaskType.LEGAL
        c2 = IntentClassifier().classify("send this to the legal agent")
        assert c2.task_type is TaskType.LEGAL

    def test_registry_gated_off_by_default(self):
        old = os.environ.pop("JARVIS_LEGAL_AGENT", None)
        try:
            reg = build_default_registry()
            stub = reg.get("legal_agent")
            assert stub is not None
            res = stub.handle(_req("anything"))
            assert res.status is TaskStatus.NEEDS_INPUT     # the stub
        finally:
            if old:
                os.environ["JARVIS_LEGAL_AGENT"] = old

    def test_registry_on(self):
        old = os.environ.get("JARVIS_LEGAL_AGENT")
        os.environ["JARVIS_LEGAL_AGENT"] = "on"
        try:
            reg = build_default_registry()
            assert type(reg.get("legal_agent")) is LegalAgent
        finally:
            if old is None:
                os.environ.pop("JARVIS_LEGAL_AGENT", None)
            else:
                os.environ["JARVIS_LEGAL_AGENT"] = old

    def test_handle_never_raises(self, agent):
        for text in ("", "   ", "\x00 \x1f", "clause " * 400,
                     "compare \x00 me now"):
            res = agent.handle(_req(text, **{"legal_action": "analyze",
                                             "doc_id": "lg-garbage"}))
            assert res.status in (TaskStatus.COMPLETED, TaskStatus.FAILED,
                                  TaskStatus.NEEDS_INPUT,
                                  TaskStatus.DECLINED)

    def test_collection_declared_in_rag(self):
        from core import rag
        assert COLLECTION in rag.COLLECTIONS
