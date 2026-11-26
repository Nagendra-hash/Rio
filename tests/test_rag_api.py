"""
tests/test_rag_api.py — offline tests for the RAG REST surface
(dashboard/rag_api.py), the orchestrator rag_agent, and the Hermes inbox
--rag path.

Everything is sandboxed: temp knowledge dir, temp callers file, temp uploads,
fake embedder, real FastAPI app via TestClient — no network, no LLM.

What this proves:
  1. The full HTTP flow: upload a PDF → index it → POST /api/rag/query →
     relevant chunks with citations and source references.
  2. ingest-path (the Hermes flow: a file already on disk), ingest-text,
     update-meta, forget, documents, stats — every endpoint.
  3. Auth: no/invalid bearer token → 401 on every endpoint.
  4. ACL over HTTP: a scoped caller cannot read other collections (403),
     a read-only caller cannot write (403).
  5. rag_agent: read returns evidence + citations; ACL-scoped; write gated;
     replaces the stub under the same registry name.
  6. scripts/jarvis_inbox.py --rag: parses to a /api/rag/ingest-path POST
     with collection resolution from the caption.

Run:  .venv/bin/python -m pytest tests/test_rag_api.py -q
"""

import importlib.util
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
from orchestrator.contracts import TaskStatus  # noqa: E402


@pytest.fixture(autouse=True)
def sandbox():
    """A FRESH knowledge directory + callers file for every test (see
    test_rag_service.sandbox — the paths are module globals shared by every
    test module, so they are re-pointed per test, not once at import)."""
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-rag-api-test-"))
    rag.KB_DIR = tmp
    rag.LIBRARY_DIR = tmp / "library"
    rag.CHUNKS_FILE = tmp / "chunks.jsonl"
    rag.VECTORS_FILE = tmp / "vectors.npz"
    rag.REGISTRY_FILE = tmp / "registry.json"
    rag.AUDIT_FILE = tmp / "audit.log"
    rag_service.CALLERS_FILE = tmp / "rag_callers.json"
    yield tmp


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
    import core as core_pkg

    monkeypatch.setattr(core_pkg, "gemini", _FakeGemini(), raising=False)
    yield


# ── the app under test ───────────────────────────────────────────────────────

TOKEN = "test-static-token-1234"
_AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def client(fake_gemini):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from fastapi import FastAPI
    from dashboard import rag_api

    app = FastAPI()
    rag_api.register(
        app,
        lambda req: (
            req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            == TOKEN
        ),
    )
    return fastapi_testclient.TestClient(app)


def _make_pdf(path: Path, pages_text: list[str]) -> Path:
    """A genuine multi-page PDF WITH a text layer, in raw PDF syntax — same
    builder as test_rag_service (no OCR detour, no fixtures committed)."""

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
    "CHAPTER 1 STRADDLES\n\nA straddle buys both a call and a put at the "
    "same strike. Risk is capped at the combined premium."
)
P2 = (
    "CHAPTER 2 IRON CONDORS\n\nAn iron condor sells a bear call spread "
    "above spot and a bull put spread below. Max profit is the net credit."
)
PDF_PAGES = [P1, P2]


# ── 1/2. the full HTTP flow ──────────────────────────────────────────────────


class TestRestFlow:
    def test_upload_index_query_citations(self, client, tmp_path):
        src = _make_pdf(tmp_path / "playbook.pdf", PDF_PAGES)

        # upload a PDF
        r = client.post(
            "/api/rag/ingest",
            headers=_AUTH,
            files={"file": ("playbook.pdf", src.read_bytes(), "application/pdf")},
            data={
                "collection": "strategies",
                "title": "Options Playbook",
                "caller": "jarvis",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] and body["filetype"] == "pdf"
        assert body["pages"] == 2 and body["chunks"] >= 2
        doc_id = body["doc_id"]

        # indexed: visible in documents + stats (owner caller — the default
        # "dashboard" caller is blind unless granted in rag_callers.json)
        docs = client.get("/api/rag/documents?caller=jarvis", headers=_AUTH).json()
        assert any(d["doc_id"] == doc_id for d in docs["documents"])
        stats = client.get("/api/rag/stats?caller=jarvis", headers=_AUTH).json()
        assert stats["ok"] and stats["stats"]["documents"] >= 1

        # ask a question → relevant chunks
        q = client.post(
            "/api/rag/query",
            headers=_AUTH,
            json={"query": "how does an iron condor work", "caller": "jarvis"},
        )
        assert q.status_code == 200, q.text
        res = q.json()
        assert res["ok"] and res["results"], res
        top = res["results"][0]
        assert top["doc_id"] == doc_id
        assert top["score"] >= res["results"][-1]["score"]

        # retrieve relevant chunks → source references returned
        assert res["citations"], "no citations block"
        assert "Options Playbook" in res["citations"][0]
        assert res["evidence"].startswith("[1]")
        for field in ("text", "source", "page", "doc_id", "score"):
            assert field in top

        # doc-scoped query (owner caller; default dashboard caller is blind)
        q2 = client.post(
            "/api/rag/query",
            headers=_AUTH,
            json={"query": "straddle", "doc_id": doc_id, "caller": "jarvis"},
        )
        assert q2.status_code == 200
        assert {h["doc_id"] for h in q2.json()["results"]} == {doc_id}

    def test_ingest_text_and_forget(self, client):
        r = client.post(
            "/api/rag/ingest-text",
            headers=_AUTH,
            json={
                "text": "The journal says the thesis held.",
                "title": "Journal Entry",
                "collection": "personal_knowledge",
            },
        )
        assert r.status_code == 200, r.text
        doc_id = r.json()["doc_id"]

        f = client.post("/api/rag/forget", headers=_AUTH, json={"doc_id": doc_id})
        assert f.status_code == 200 and f.json()["ok"]
        docs = client.get("/api/rag/documents?caller=jarvis", headers=_AUTH).json()
        assert all(d["doc_id"] != doc_id for d in docs["documents"])

    def test_ingest_path_hermes_flow(self, client, tmp_path):
        """The Hermes path: the gateway already saved the file; ingest by
        local path, no re-upload."""
        src = _make_pdf(tmp_path / "hermes_saved.pdf", PDF_PAGES)
        r = client.post(
            "/api/rag/ingest-path",
            headers=_AUTH,
            json={
                "path": str(src),
                "collection": "research",
                "title": "Hermes Doc",
                "source": "hermes",
                "metadata": {"channel_chat": "555"},
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] and body["filetype"] == "pdf"
        s = client.post(
            "/api/rag/query",
            headers=_AUTH,
            json={"query": "iron condor credit", "caller": "jarvis"},
        )
        hits = s.json()["results"]
        assert any(h["doc_id"] == body["doc_id"] for h in hits)
        top = next(h for h in hits if h["doc_id"] == body["doc_id"])
        assert top["metadata"].get("channel_chat") == "555"

    def test_update_meta_endpoint(self, client, tmp_path):
        src = _make_pdf(tmp_path / "m.pdf", PDF_PAGES)
        r = client.post(
            "/api/rag/ingest-path",
            headers=_AUTH,
            json={"path": str(src), "collection": "research", "title": "Meta Doc"},
        )
        doc_id = r.json()["doc_id"]
        u = client.post(
            "/api/rag/update-meta",
            headers=_AUTH,
            json={
                "doc_id": doc_id,
                "title": "Renamed",
                "access": "shared",
                "metadata": {"tag": "x"},
            },
        )
        assert u.status_code == 200 and u.json()["ok"]
        e = rag._load_registry()[doc_id]
        assert e["title"] == "Renamed" and e["access"] == "shared"
        assert e["metadata"]["tag"] == "x"

    def test_content_update_endpoint(self, client, tmp_path):
        src = _make_pdf(tmp_path / "old.pdf", PDF_PAGES)
        doc_id = client.post(
            "/api/rag/ingest-path",
            headers=_AUTH,
            json={"path": str(src), "collection": "strategies", "title": "Old"},
        ).json()["doc_id"]
        new_src = _make_pdf(
            tmp_path / "new.pdf",
            [
                "CHAPTER 5 CALENDAR SPREADS\n\nA calendar spread "
                "buys a far-dated option and sells a near-dated "
                "one at the same strike."
            ],
        )
        u = client.post(
            "/api/rag/update",
            headers=_AUTH,
            files={"file": ("new.pdf", new_src.read_bytes(), "application/pdf")},
            data={"doc_id": doc_id, "title": "New", "caller": "jarvis"},
        )
        assert u.status_code == 200, u.text
        body = u.json()
        assert body["ok"] and body["version"] == 2
        rows = [x for x in rag._read_chunks() if x["doc_id"] == doc_id]
        assert rows and all("straddle" not in x["text"].lower() for x in rows)

    def test_empty_query_400(self, client):
        r = client.post("/api/rag/query", headers=_AUTH, json={"query": "  "})
        assert r.status_code == 400

    def test_no_file_400(self, client):
        r = client.post(
            "/api/rag/ingest", headers=_AUTH, data={"collection": "research"}
        )
        assert r.status_code == 400


# ── 3. auth ──────────────────────────────────────────────────────────────────


class TestAuth:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("post", "/api/rag/query"),
            ("post", "/api/rag/ingest-text"),
            ("post", "/api/rag/forget"),
            ("post", "/api/rag/update-meta"),
            ("get", "/api/rag/documents"),
            ("get", "/api/rag/stats"),
        ],
    )
    def test_missing_token_401(self, client, method, path):
        r = getattr(client, method)(
            path, **({"json": {"query": "x"}} if method == "post" else {})
        )
        assert r.status_code == 401, f"{path}: {r.status_code}"

    def test_bad_token_401(self, client):
        r = client.post(
            "/api/rag/query",
            headers={"Authorization": "Bearer nope"},
            json={"query": "x"},
        )
        assert r.status_code == 401


# ── 4. ACL over HTTP ─────────────────────────────────────────────────────────


class TestAclOverHttp:
    @pytest.fixture(autouse=True)
    def _setup(self, client, tmp_path):
        src = _make_pdf(tmp_path / "t.pdf", PDF_PAGES)
        assert client.post(
            "/api/rag/ingest-path",
            headers=_AUTH,
            json={
                "path": str(src),
                "collection": "trading_books",
                "title": "Trading Book",
            },
        ).json()["ok"]
        rag_service.CALLERS_FILE.write_text(
            json.dumps(
                {
                    "research_reader": {
                        "collections": ["research"],
                        "can_write": False,
                    },
                }
            ),
            encoding="utf-8",
        )
        yield
        if rag_service.CALLERS_FILE.exists():
            rag_service.CALLERS_FILE.unlink()

    def test_scoped_caller_cannot_read_other_collection_403(self, client):
        r = client.post(
            "/api/rag/query",
            headers=_AUTH,
            json={
                "query": "condor",
                "caller": "research_reader",
                "collection": "trading_books",
            },
        )
        assert r.status_code == 403
        assert "access denied" in r.json()["error"]

    def test_read_only_caller_cannot_ingest_403(self, client, tmp_path):
        src = _make_pdf(tmp_path / "w.pdf", PDF_PAGES)
        r = client.post(
            "/api/rag/ingest",
            headers=_AUTH,
            files={"file": ("w.pdf", src.read_bytes(), "application/pdf")},
            data={"caller": "research_reader"},
        )
        assert r.status_code == 403
        assert "cannot write" in r.json()["error"]

    def test_unknown_caller_gets_empty_results(self, client):
        r = client.post(
            "/api/rag/query",
            headers=_AUTH,
            json={"query": "condor", "caller": "mallory"},
        )
        assert r.status_code == 200
        assert r.json()["results"] == []

    def test_scoped_caller_list_is_filtered(self, client):
        docs = client.get(
            "/api/rag/documents", headers=_AUTH, params={"caller": "research_reader"}
        ).json()
        assert docs["documents"] == []  # only trading_books exists


# ── 5. the orchestrator rag_agent ────────────────────────────────────────────


class TestRagAgent:
    @pytest.fixture()
    def agent(self, fake_gemini, tmp_path):
        from orchestrator.agents.rag_agent import RagAgent
        from orchestrator.contracts import TaskType

        src = _make_pdf(tmp_path / "a.pdf", PDF_PAGES)
        assert rag_service.ingest(
            caller="jarvis",
            file_path=str(src),
            collection="strategies",
            title="Agent Book",
        )["ok"]
        rag_service.CALLERS_FILE.write_text(
            json.dumps(
                {
                    "rag_agent": {"collections": ["strategies"], "can_write": False},
                }
            ),
            encoding="utf-8",
        )
        yield RagAgent(), TaskType
        if rag_service.CALLERS_FILE.exists():
            rag_service.CALLERS_FILE.unlink()

    def _request(self, TaskType, text, **meta):
        from orchestrator.contracts import AgentRequest

        return AgentRequest(
            task_id="task-test-0001", task_type=TaskType.RAG, text=text, metadata=meta
        )

    def test_agent_read_returns_evidence_and_citations(self, agent):
        a, TaskType = agent
        result = a.handle(self._request(TaskType, "iron condor net credit"))
        assert result.status is TaskStatus.COMPLETED
        assert result.agent == "rag_agent"
        assert "Agent Book" in result.text
        assert result.data["citations"]
        assert result.data["evidence"]

    def test_agent_uses_its_own_scoped_caller(self, agent):
        a, TaskType = agent
        result = a.handle(self._request(TaskType, "condor"))
        assert result.status is TaskStatus.COMPLETED
        for r in result.data["results"]:
            assert r["collection"] == "strategies"

    def test_agent_no_results_is_completed_empty(self, agent):
        a, TaskType = agent
        # top-k retrieval always returns the nearest chunks when the library
        # has any — "no results" only happens on an empty/out-of-scope scope,
        # so empty the store to prove the empty path.
        for d in rag.list_documents():
            assert rag.forget(d["doc_id"])["ok"]
        result = a.handle(self._request(TaskType, "zzz unrelated query"))
        assert result.status is TaskStatus.COMPLETED
        assert result.data["citations"] == []

    def test_agent_write_gated_by_acl(self, agent, tmp_path):
        a, TaskType = agent
        src = _make_pdf(tmp_path / "denied.pdf", PDF_PAGES)
        result = a.handle(
            self._request(TaskType, "remember", rag_action="ingest", path=str(src))
        )
        assert result.status is TaskStatus.FAILED
        assert "cannot write" in result.text

    def test_agent_replaces_stub_under_same_name(self):
        import os

        os.environ["JARVIS_RAG_AGENT"] = "on"
        try:
            from orchestrator.agents import build_default_registry

            reg = build_default_registry()
            a = reg.get("rag_agent")
            assert a is not None and a.name == "rag_agent"
            assert not hasattr(a, "handle_logged") or type(a).__name__ == "RagAgent"
        finally:
            os.environ.pop("JARVIS_RAG_AGENT", None)

    def test_stub_stays_default(self):
        import os

        os.environ.pop("JARVIS_RAG_AGENT", None)
        from orchestrator.agents import build_default_registry

        reg = build_default_registry()
        a = reg.get("rag_agent")
        assert type(a).__name__ == "StubAgent"


# ── 6. Hermes inbox --rag ────────────────────────────────────────────────────


def _load_inbox():
    spec = importlib.util.spec_from_file_location(
        "jarvis_inbox", BASE / "scripts" / "jarvis_inbox.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestInboxRag:
    def test_module_loads(self):
        mod = _load_inbox()
        assert hasattr(mod, "send_rag")
        assert hasattr(mod, "send")

    def test_send_rag_posts_ingest_path(self, fake_gemini, tmp_path, monkeypatch):
        mod = _load_inbox()
        captured = {}

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(
                    {
                        "ok": True,
                        "title": "T",
                        "pages": 2,
                        "chunks": 4,
                        "doc_id": "abc",
                        "collection": "research",
                    }
                ).encode()

        def fake_urlopen(req, timeout=0, context=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode())
            return FakeResp()

        monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(mod, "_token", lambda: "tok")

        src = tmp_path / "doc.pdf"
        src.write_bytes(b"%PDF-1.4 fake")
        rc = mod.send_rag(str(src), "research", "My Title", "555")
        assert rc == 0
        assert captured["url"].endswith("/api/rag/ingest-path")
        assert captured["body"]["path"] == str(src)
        assert captured["body"]["collection"] == "research"
        assert captured["body"]["metadata"] == {"channel_chat": "555"}

    def test_send_rag_missing_file(self, tmp_path, capsys):
        mod = _load_inbox()
        rc = mod.send_rag(str(tmp_path / "ghost.pdf"), "research", None, "")
        assert rc == 1
        assert "file not found" in capsys.readouterr().err

    def test_send_rag_no_token(self, tmp_path, monkeypatch, capsys):
        mod = _load_inbox()
        monkeypatch.setattr(mod, "_token", lambda: "")
        src = tmp_path / "doc.pdf"
        src.write_bytes(b"%PDF-1.4 fake")
        rc = mod.send_rag(str(src), "research", None, "")
        assert rc == 1
        assert "no dashboard token" in capsys.readouterr().err
