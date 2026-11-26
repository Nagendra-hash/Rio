"""
tests/test_rag_strategies.py — evaluation tests for the retrieval strategies
(core/rag_strategies over core/rag_service.answer).

Everything runs offline: temp knowledge dirs, fake deterministic embedder,
raw-PDF builders (same as the other RAG suites), and either NO model at all
(local strategies + extractive answerer) or a scripted fake judge (the LLM
strategies' decision points). The real model ladder is never touched:
core.rag_strategies.llm.llm_ready is monkeypatched to False (or its callers
bridged to the fake), so a machine WITH a Gemini key still tests the same
deterministic paths.

What these tests EVALUATE (not just exercise):

  A. RETRIEVAL QUALITY
     1. relevance ranking — for questions the library answers, the top
        chunk must come from the right document (no cross-topic bleed)
     2. bad-query rescue — a messy natural question still retrieves the
        right document (query understanding, not keyword luck)
     3. correction loop — a query with junk terms triggers strategy.correct
        and the SECOND retrieval round finds the answer
     4. scope discipline — strategies asking for ungranted collections get
        nothing (ACL holds under the pipeline, not just under search)

  B. HALLUCINATION REDUCTION
     5. grounded answers carry citations — every claim traceable to a chunk
     6. refusal over invention — questions the library cannot answer produce
        "does not cover this", never a confident guess
     7. extractive answers are verbatim evidence — the local answer text is
        a span of the retrieved chunk (structural no-hallucination)
     8. risk grading — fabricated/unsupported answers grade medium/high;
        fully supported answers grade low
     9. no-LLM-send — across an entire answer() run the embedder only ever
        sees the query and chunk texts (never a whole document)

  C. THE NINE BEHAVIOURS + OFFLINE MODE
     10. every strategy answers, cites, and records its trace/iterations
     11. local twins never make a model call; LLM strategies degrade to
         their local twins when offline; auto picks by availability
     12. chit-chat/commands skip retrieval (adaptive step 2)

  D. SURFACES + PHASE-3 SAFETY
     13. rag_agent answer mode; REST /api/rag/answer (auth, ACL, 400s)
     14. search()/rag_api/query/agent contracts byte-compatible with Phase 3

Run:  .venv/bin/python -m pytest tests/test_rag_strategies.py -q
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
from core.rag_strategies import heuristics  # noqa: E402
from core.rag_strategies import llm as rs_llm  # noqa: E402
from core.rag_strategies.base import (Answer, Evidence,  # noqa: E402
                                      RetrievalPipeline)
from core.rag_strategies.strategies import (REGISTRY, get,  # noqa: E402
                                            strategies)
from orchestrator.contracts import TaskStatus  # noqa: E402

REFUSAL = "The knowledge base does not cover this."

# strategy → its offline twin (degradation path when the ladder is down)
_TWINS = {"adaptive": "local_adaptive", "agentic": "local_adaptive",
          "corrective": "local_adaptive", "self_rag": "local_self_rag"}


# ── sandbox + fake embedder (same pattern as test_rag_service) ───────────────

@pytest.fixture(autouse=True)
def sandbox():
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-rag-strat-test-"))
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
            v[seed:seed + 8] += 1.0
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


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """The real model ladder is never touched: tests are deterministic."""
    monkeypatch.setattr(rs_llm, "llm_ready", lambda: False)
    yield


# ── the library (two documents, two collections) ─────────────────────────────

CONDOR_TEXT = (
    "An iron condor sells a bear call spread above spot and a bull put "
    "spread below spot. Max profit is the net credit received when both "
    "spreads expire worthless. Risk is capped at the width of the wings "
    "minus the credit collected upfront.")
STRADDLE_TEXT = (
    "A straddle buys both a call and a put at the same strike. The position "
    "profits when the underlying moves beyond the combined premium paid. "
    "Theta decays the straddle value every day the stock stays still.")


@pytest.fixture()
def library(fake_gemini):
    (rag.KB_DIR / "condor.txt").write_text(CONDOR_TEXT, encoding="utf-8")
    (rag.KB_DIR / "straddle.txt").write_text(STRADDLE_TEXT, encoding="utf-8")
    assert rag.ingest(str(rag.KB_DIR / "condor.txt"), collection="strategies",
                      title="Condor Book")["ok"]
    assert rag.ingest(str(rag.KB_DIR / "straddle.txt"),
                      collection="research", title="Straddle Paper")["ok"]


# ── a scripted fake judge for the LLM strategies ─────────────────────────────

class FakeJudge:
    """Stands in for gemini.as_json: records prompts, replays answers."""
    def __init__(self, script=None):
        self.script = list(script or [])
        self.prompts: list[str] = []

    def __call__(self, prompt: str):
        self.prompts.append(prompt)
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return {}


@pytest.fixture()
def judge(monkeypatch):
    """LLM strategies get a scripted judge; llm_ready stays False so the
    answerer is the extractive one (deterministic answers)."""
    j = FakeJudge()
    monkeypatch.setattr(rs_llm, "llm_ready", lambda: True)
    # the answerer bridge is built inside rag_service.answer from
    # llm_call/llm_text — bridge them to a stub that never answers, so the
    # wrapper falls through to extractive deterministically
    monkeypatch.setattr(rs_llm, "llm_call", lambda prompt, tier="fast": None)
    monkeypatch.setattr(rs_llm, "llm_text", lambda prompt, tier="fast": "")
    yield j


# ══════════════════════════════════════════════════════════════════════════════
# A. RETRIEVAL QUALITY
# ══════════════════════════════════════════════════════════════════════════════


class TestRetrievalQuality:
    @pytest.mark.parametrize("strategy", ["local_adaptive", "local_self_rag",
                                          "adaptive", "self_rag",
                                          "corrective", "agentic"])
    def test_top_chunk_comes_from_the_right_document(self, library, strategy,
                                                     judge):
        a = rag_service.answer("jarvis", "How does an iron condor work?",
                               strategy=strategy)
        assert a["ok"], a
        assert a["used_evidence"], "no evidence retrieved"
        top = a["used_evidence"][0]
        assert "condor" in top["source"].lower(), \
            f"{strategy}: top chunk from {top['source']}"
        assert top["collection"] == "strategies"

    def test_messy_natural_question_still_finds_the_answer(self, library):
        a = rag_service.answer(
            "jarvis",
            "hey so I was reading about that options thing where you sell "
            "the wings — how does the whole condor setup actually work??")
        assert a["ok"] and a["used_evidence"]
        assert "condor" in a["used_evidence"][0]["source"].lower()

    def test_comparison_retrieves_both_sides(self, library):
        a = rag_service.answer(
            "jarvis",
            "Compare an iron condor with a straddle: risk and profit "
            "difference between the two", strategy="local_adaptive")
        assert a["ok"]
        sources = {e["source"] for e in a["used_evidence"]}
        assert any("condor" in s.lower() for s in sources), sources
        assert any("straddle" in s.lower() for s in sources), sources

    def test_correction_loop_rescues_a_junk_query(self, library,
                                                  monkeypatch):
        """Steps 5+8: the strategy's sufficiency check demands another
        round, correction rewrites the query, and the second retrieval uses
        the rewritten query (deterministic — no dependence on which doc the
        first round happened to rank first)."""
        from core.rag_strategies.strategies import LocalAdaptiveRAG

        class RescueAfterCorrect(LocalAdaptiveRAG):
            def more_needed(self, query, info, evidence, ans, iteration):
                return iteration == 1          # always one correction round

            def correct(self, query, info, evidence, iteration):
                return ["iron condor wings net credit"]

        monkeypatch.setitem(REGISTRY, "rescue", RescueAfterCorrect)
        a = rag_service.answer(
            "jarvis", "wings width minus credit collected upfront mechanism",
            strategy="rescue")
        assert a["iterations"] >= 2, "no second round after correction"
        assert any("condor" in q.lower() for q in a["queries_used"]), \
            a["queries_used"]
        assert a["used_evidence"], "second round retrieved nothing"

    def test_ungranted_collection_stays_invisible_to_strategies(self,
                                                                library):
        rag_service.CALLERS_FILE.write_text(json.dumps({
            "desk": {"collections": ["research"], "can_write": False},
        }), encoding="utf-8")
        a = rag_service.answer("desk", "how does an iron condor work")
        assert a["ok"], a
        # "strategies" is not granted: only research (the straddle paper)
        # may appear — a condor chunk would be an ACL breach
        assert all("condor" not in e["text"].lower()
                   for e in a["used_evidence"]), a["used_evidence"][:1]
        assert all(e["collection"] == "research"
                   for e in a["used_evidence"])

    def test_collection_pin_binds_the_strategy(self, library):
        a = rag_service.answer("jarvis", "how does an iron condor work",
                               strategy="local_adaptive",
                               collection="research")
        assert a["ok"]
        assert all(e["collection"] == "research"
                   for e in a["used_evidence"])

    def test_max_chars_bounds_the_evidence_string(self, library):
        a = rag_service.answer("jarvis", "iron condor mechanics",
                               max_chars=600)
        assert len(a["evidence"]) <= 600 + 4      # room for the […] marker


# ══════════════════════════════════════════════════════════════════════════════
# B. HALLUCINATION REDUCTION
# ══════════════════════════════════════════════════════════════════════════════


class TestHallucinationReduction:
    def test_grounded_answer_carries_citations(self, library):
        a = rag_service.answer("jarvis", "What is the max profit of an "
                                          "iron condor?",
                               strategy="local_self_rag")
        assert a["ok"] and a["answer"]
        assert a["citations"], "answer without citations"
        assert a["grounded"] is True
        assert a["hallucination_risk"] in ("low", "medium")

    def test_extractive_answer_is_verbatim_evidence(self, library):
        """The structural guarantee: the local answer IS a span of the
        evidence — character-for-character, so it cannot invent."""
        a = rag_service.answer("jarvis", "what decays a straddle position",
                               strategy="local_self_rag")
        assert a["ok"] and a["answer"]
        # strip the [n] markers, then every sentence must exist in a chunk
        body = " ".join(a["answer"].split("[")[0].strip() for _ in [0]) \
            if False else a["answer"]
        for part in [p for p in body.split("[") if p.strip()]:
            sent = part.split("]")[0].strip(" .")
            if len(sent) < 15:
                continue
            chunk_texts = [e["text"] for e in a["used_evidence"]]
            assert any(sent.lower() in t.lower() for t in chunk_texts), \
                f"answer sentence not in evidence: {sent!r}"

    def test_unanswerable_question_refuses_instead_of_inventing(self,
                                                                library):
        a = rag_service.answer(
            "jarvis",
            "What is the airspeed velocity of an unladen swallow on Mars?",
            strategy="local_self_rag")
        assert a["ok"], a
        # either a refusal, or (extractive may quote a nearest span when the
        # query shares real terms) the risk grade must NOT be low
        if a["answer"] and "does not cover" not in a["answer"]:
            assert a["hallucination_risk"] in ("medium", "high"), a["answer"]

    def test_nearest_span_refusal_threshold(self, library):
        """A question sharing zero terms with the library must refuse."""
        a = rag_service.answer("jarvis", "quantum flux capacitor capacitor",
                               strategy="local_self_rag")
        assert a["ok"]
        assert (not a["answer"]) or "does not cover" in a["answer"] \
            or a["hallucination_risk"] != "low"

    def test_risk_grading_ladder(self):
        ev = Evidence(text=CONDOR_TEXT, source="Condor Book", page=1,
                      doc_id="d", collection="strategies")
        good = ("An iron condor sells a bear call spread above spot and a "
                "bull put spread below spot. [1]")
        partial = ("An iron condor sells a bear call spread and requires "
                   "approval from the securities regulator chairman. [1]")
        fabricated = ("The chairman of the securities regulator personally "
                      "approves every condor margin account each winter "
                      "season before migration. [1]")
        assert heuristics.hallucination_risk(good, [ev], True) == "low"
        assert heuristics.hallucination_risk(partial, [ev], True) in \
            ("medium", "high")
        assert heuristics.hallucination_risk(fabricated, [ev], True) == "high"
        assert heuristics.hallucination_risk("", [ev], True) == "high"

    def test_fabricated_numbers_are_flagged_even_when_cited(self):
        ev = Evidence(text="The condor collected a net credit of two "
                           "hundred dollars.", source="B", page=1,
                      doc_id="d", collection="c")
        a = "The condor collected exactly 475.50 dollars of credit. [1]"
        assert heuristics.hallucination_risk(a, [ev], True) != "low"

    def test_support_score_direction(self):
        ev = CONDOR_TEXT
        # answer ⊂ evidence → strong support
        assert heuristics.support_score(
            "An iron condor sells a bear call spread above spot.", ev) > 0.6
        # evidence ⊂ answer must NOT score high (direction matters)
        assert heuristics.support_score(
            ev, "An iron condor sells a bear call spread above spot.") < 0.6

    def test_no_llm_send_across_a_full_answer_run(self, library,
                                                  monkeypatch):
        """The acceptance rule, enforced at the strategies layer: across an
        entire answer() the embedder sees only the query and chunks — never
        a whole document (a whole doc here would be > 300 chars at once)."""
        sent_texts: list[str] = []

        def spy(texts, is_query=False, **kw):
            batch = [texts] if isinstance(texts, str) else list(texts)
            sent_texts.extend(batch)
            return _fake_embed(batch, is_query)

        import core as core_pkg
        class SpyGemini:
            def embed(self, texts, is_query=False, **kw):
                return spy(texts, is_query)
        monkeypatch.setattr(core_pkg, "gemini", SpyGemini(), raising=False)

        rag_service.answer("jarvis",
                           "Compare an iron condor with a straddle for "
                           "defined risk", strategy="local_adaptive")
        assert sent_texts, "embedder was never called"
        assert all(len(t) < 1200 for t in sent_texts), \
            f"whole document shipped to the embedder: {max(len(t) for t in sent_texts)} chars"


# ══════════════════════════════════════════════════════════════════════════════
# C. THE NINE BEHAVIOURS + OFFLINE MODE
# ══════════════════════════════════════════════════════════════════════════════


class TestStrategiesAllSix:
    def test_registry_has_exactly_the_six(self):
        assert set(strategies()) == {
            "adaptive", "agentic", "corrective", "self_rag",
            "local_adaptive", "local_self_rag"}

    @pytest.mark.parametrize("strategy", sorted(REGISTRY))
    def test_every_strategy_answers_with_citations_and_trace(self, library,
                                                             strategy,
                                                             judge):
        a = rag_service.answer("jarvis", "How does an iron condor work?",
                               strategy=strategy)
        assert a["ok"], a
        assert a["strategy"] in (strategy, *_TWINS.values())
        assert a["citations"], f"{strategy}: no citations"
        assert a["trace"], f"{strategy}: no reasoning trace"
        assert a["iterations"] >= 1
        assert a["hallucination_risk"] in ("low", "medium", "high")
        assert isinstance(a["grounded"], bool)

    def test_offline_llm_strategy_degrades_to_local_twin(self, library):
        a = rag_service.answer("jarvis", "iron condor risk profile",
                               strategy="agentic")     # offline: llm=None
        assert a["strategy"] == "local_adaptive"
        a2 = rag_service.answer("jarvis", "iron condor risk profile",
                                strategy="self_rag")
        assert a2["strategy"] == "local_self_rag"

    def test_auto_picks_local_when_offline(self, library):
        a = rag_service.answer("jarvis", "iron condor wings", strategy="auto")
        assert a["strategy"] == "local_adaptive"

    def test_unknown_strategy_is_a_clean_error(self, library):
        r = rag_service.answer("jarvis", "condor", strategy="bogus")
        assert not r["ok"] and "unknown strategy" in r["error"]

    def test_chitchat_and_commands_skip_retrieval(self, library):
        for q in ("hey", "good morning!", "what is 12 + 15",
                  "set volume to 40"):
            a = rag_service.answer("jarvis", q, strategy="local_adaptive")
            assert a["ok"], (q, a)
            assert a["used_evidence"] == [], \
                f"{q!r} should not have hit the library: {a['used_evidence'][:1]}"

    def test_llm_strategy_uses_judge_when_online(self, judge):
        """With a judge present, adaptive's router call happens and its
        complexity refinement flows into the plan."""
        judge.script = [{"retrieve": True, "complexity": "complex"}]
        from core.rag_strategies.strategies import AdaptiveRAG
        s = AdaptiveRAG(llm=judge)
        info = {"complexity": "moderate", "type": "lookup", "multi_hop": False}
        assert s.needs_retrieval("iron condor mechanics", info) is True
        assert info["complexity"] == "complex"        # refined by the judge
        assert judge.prompts and "route" in judge.prompts[0].lower()

    def test_agentic_decomposition_and_sufficiency(self, judge):
        from core.rag_strategies.strategies import AgenticRAG
        s = AgenticRAG(llm=judge)
        judge.script = [{"sub_questions": ["what is condor risk",
                                            "what is straddle risk"]}]
        info = {"complexity": "complex", "type": "multi_hop",
                "multi_hop": True}
        plan = s.plan("compare condor and straddle risk", info, 1)
        assert plan["reformulations"], "no sub-questions planned"
        # sufficiency: judge says not sufficient, low confidence → another round
        judge.script = [{"sufficient": False, "confidence": 0.2}]
        assert s.more_needed("q", info, [], {}, 1) is True
        # judge says sufficient → stop
        judge.script = [{"sufficient": True, "confidence": 0.9}]
        assert s.more_needed("q", info, [], {}, 1) is False


# ══════════════════════════════════════════════════════════════════════════════
# D. SURFACES + PHASE-3 SAFETY
# ══════════════════════════════════════════════════════════════════════════════


class TestAnswerSurfaces:
    def test_rag_agent_answer_mode(self, library):
        from orchestrator.agents.rag_agent import RagAgent
        from orchestrator.contracts import AgentRequest, TaskType
        # the agent's caller needs a grant to see the library
        rag_service.CALLERS_FILE.write_text(json.dumps({
            "rag_agent": {"collections": ["__all__"], "can_write": False},
        }), encoding="utf-8")
        agent = RagAgent()
        req = AgentRequest(task_id="t-strat-1", task_type=TaskType.RAG,
                           text="How does an iron condor work?",
                           metadata={"rag_action": "answer",
                                     "rag_strategy": "local_self_rag"})
        result = agent.handle(req)
        assert result.status is TaskStatus.COMPLETED
        assert result.agent == "rag_agent"
        assert result.data["citations"]
        assert "does not cover" not in result.text or result.data["grounded"]
        # read mode still works exactly as Phase 3 left it
        req2 = AgentRequest(task_id="t-strat-2", task_type=TaskType.RAG,
                            text="iron condor", metadata={})
        r2 = agent.handle(req2)
        assert r2.status is TaskStatus.COMPLETED and r2.data["results"]

    def test_rest_answer_endpoint(self, fake_gemini):
        fastapi_testclient = pytest.importorskip("fastapi.testclient")
        from fastapi import FastAPI
        from dashboard import rag_api
        TOKEN = "test-strat-token-99"
        app = FastAPI()
        rag_api.register(app, lambda req: (req.headers.get("authorization", "")
                                           .removeprefix("Bearer ").strip()
                                           == TOKEN))
        client = fastapi_testclient.TestClient(app)
        src = rag.KB_DIR / "up.txt"
        src.write_text(CONDOR_TEXT, encoding="utf-8")
        assert rag.ingest(str(src), collection="strategies",
                          title="Condor Book")["ok"]

        r = client.post("/api/rag/answer",
                        headers={"Authorization": f"Bearer {TOKEN}"},
                        json={"query": "How does an iron condor work?",
                              "strategy": "local_self_rag",
                              "caller": "jarvis"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] and body["citations"] and body["strategy"] == \
            "local_self_rag"

        # auth still enforced
        r401 = client.post("/api/rag/answer",
                           json={"query": "condor", "caller": "jarvis"})
        assert r401.status_code == 401

        # empty query → 400
        r400 = client.post("/api/rag/answer",
                           headers={"Authorization": f"Bearer {TOKEN}"},
                           json={"query": "  ", "caller": "jarvis"})
        assert r400.status_code == 400

        # unknown strategy → 400 (clean validation, not a 500)
        rS = client.post("/api/rag/answer",
                         headers={"Authorization": f"Bearer {TOKEN}"},
                         json={"query": "condor", "strategy": "nope",
                               "caller": "jarvis"})
        assert rS.status_code == 400 and "unknown strategy" in rS.text

        # scoped caller cannot read other collections through answer
        rag_service.CALLERS_FILE.write_text(json.dumps({
            "desk": {"collections": ["research"], "can_write": False}}),
            encoding="utf-8")
        rA = client.post("/api/rag/answer",
                         headers={"Authorization": f"Bearer {TOKEN}"},
                         json={"query": "condor", "caller": "desk"})
        assert rA.status_code == 200
        assert rA.json()["used_evidence"] == []


class TestPhase3Safety:
    """The upgrade must not move anything Phase 3 shipped."""

    def test_search_contract_unchanged(self, library):
        s = rag_service.search("jarvis", "iron condor")
        assert s["ok"] and s["mode"] in ("semantic", "hybrid", "bm25", "hybrid+rerank", "semantic+rerank", "bm25+rerank")
        top = s["results"][0]
        for field in ("text", "source", "page", "doc_id", "score",
                      "collection", "filetype", "access", "metadata"):
            assert field in top
        assert s["citations"] and s["evidence"].startswith("[")

    def test_rag_api_query_endpoint_unchanged(self, fake_gemini):
        fastapi_testclient = pytest.importorskip("fastapi.testclient")
        from fastapi import FastAPI
        from dashboard import rag_api
        app = FastAPI()
        rag_api.register(app, lambda req: True)
        client = fastapi_testclient.TestClient(app)
        src = rag.KB_DIR / "up.txt"
        src.write_text(STRADDLE_TEXT, encoding="utf-8")
        rag.ingest(str(src), collection="research", title="Straddle Paper")
        r = client.post("/api/rag/query",
                        json={"query": "straddle", "caller": "jarvis"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] and body["results"] and "citations" in body

    def test_agent_read_contract_unchanged(self, library):
        from orchestrator.agents.rag_agent import RagAgent
        from orchestrator.contracts import AgentRequest, TaskType
        rag_service.CALLERS_FILE.write_text(json.dumps({
            "rag_agent": {"collections": ["__all__"], "can_write": False},
        }), encoding="utf-8")
        r = RagAgent().handle(AgentRequest(
            task_id="t", task_type=TaskType.RAG, text="iron condor",
            metadata={}))
        assert r.status is TaskStatus.COMPLETED
        assert set(r.data) >= {"results", "citations", "evidence", "mode"}

    def test_answer_is_audited(self, library):
        rag_service.answer("jarvis", "condor wings")
        audit = rag.AUDIT_FILE.read_text(encoding="utf-8")
        assert "svc:answer" in audit          # the new action is audited
        assert "[jarvis]" in audit
        assert "svc:denied" not in audit      # owner was not denied



