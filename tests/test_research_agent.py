"""
tests/test_research_agent.py — offline tests for the Phase-6 research agent.

What this proves (no network, no real LLM, no dashboard):
  1. Source policy: private/loopback/credential-carrying URLs can never run;
     blocked search hits are dropped, not surfaced.
  2. Source permissions: web / local-KB tiers switch off per task; the
     blind-caller KB degrades to zero results, never an error.
  3. Secret masking: a leaked key shape inside a snippet or fetched page is
     masked before anything sees it.
  4. Claim verification: the support/corroboration ladder — supported,
     contested, unverified — and its use in the report (unverified claims
     listed separately, never presented as facts).
  5. Full workflow with a SCRIPTED LLM + SCRIPTED SOURCES (offline):
     QUESTION → PLAN → SEARCH → COLLECT → COMPARE → VERIFY → REPORT on a
     small technical research question, including: nothing-found honesty,
     single-source uncertainty, KB integration, deep fetch (permitted URLs
     only), checkpoint/resume, and registry integration.
  6. Report format: citations preserved as [n] and a references list.

Run:  .venv/bin/python -m pytest tests/test_research_agent.py -q
"""
import json
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from orchestrator.agents import build_default_registry  # noqa: E402
from orchestrator.agents.research_agent import (  # noqa: E402
    ResearchAgent, _extract_json, _verify_claims, _Claims,
    load_checkpoint, save_checkpoint)
from orchestrator.agents.research_tools import (  # noqa: E402
    SourcePermissions, SourceToolbox, SourceUnavailable, mask_secrets,
    strip_html, url_allowed)
from orchestrator.contracts import AgentRequest, TaskStatus, TaskType  # noqa: E402


# ── fixtures ─────────────────────────────────────────────────────────────────

QUESTION = ("What is the difference between optimistic and pessimistic "
            "concurrency control in databases, and when should each be "
            "preferred?")

PLAN = json.dumps({
    "understanding": "compare two concurrency-control strategies",
    "sub_questions": [
        "what is optimistic concurrency control in databases",
        "what is pessimistic locking in databases",
        "when to prefer optimistic vs pessimistic concurrency control",
    ],
    "search_queries": [
        "optimistic concurrency control database",
        "pessimistic locking database",
        "optimistic vs pessimistic concurrency control",
    ],
    "use_local_kb": False,
    "deep_fetch": False,
    "risks": ["vendor blogs may oversell one strategy"],
})

S_OCC = ("Optimistic concurrency control assumes conflicts are rare: "
         "transactions proceed without locks and validate at commit time, "
         "aborting when another transaction modified the data.")
S_PES = ("Pessimistic concurrency control locks a resource before use and "
         "guarantees exclusive access; it performs best when conflicts are "
         "frequent and lock hold times are short.")
S_PREF = ("Prefer optimistic control for read-mostly workloads with rare "
          "conflicts; prefer pessimistic locking when contention is high "
          "and retried transactions would be expensive.")

WEB_SOURCES = {
    "optimistic concurrency control database": [
        {"title": "Optimistic concurrency control — Wikipedia",
         "snippet": S_OCC,
         "url": "https://en.wikipedia.org/wiki/Optimistic_concurrency_control"},
        {"title": "PostgreSQL docs: MVCC",
         "snippet": "PostgreSQL uses multiversion concurrency control; "
                    "readers do not block writers and writers do not block "
                    "readers, which suits read-heavy workloads.",
         "url": "https://www.postgresql.org/docs/current/mvcc.html"},
    ],
    "pessimistic locking database": [
        {"title": "Lock (computer science) — Wikipedia",
         "snippet": S_PES,
         "url": "https://en.wikipedia.org/wiki/Lock_(computer_science)"},
    ],
    "optimistic vs pessimistic concurrency control": [
        {"title": "Citus blog: MVCC vs locking",
         "snippet": S_PREF,
         "url": "https://blog.citusdata.com/mvcc-vs-locking"},
    ],
}

REPORT = (
    "Optimistic control lets transactions run without locks and validates "
    "at commit; conflicts abort the loser [1]. Pessimistic control takes "
    "the lock before use [3]. Prefer optimistic control for read-mostly "
    "workloads with rare conflicts, pessimistic locking under high "
    "contention where retries are expensive [4].")

SCRIPTED_REPORTS = [REPORT, "dummy", "dummy", "dummy", "dummy", "dummy",
                    "dummy", "dummy"]


class ScriptedLLM:
    """Offline stand-in for the LLM layer: pops scripted answers."""

    def __init__(self, answers: list[str]):
        self.answers = list(answers)
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, system: str = ""):
        self.prompts.append(prompt)
        return self.answers.pop(0) if self.answers else None


class ScriptedWeb:
    """Offline stand-in for the web-search backend."""

    def __init__(self, by_query: dict[str, list[dict]], fail: bool = False):
        self.by_query = {k.lower(): v for k, v in by_query.items()}
        self.calls: list[str] = []
        self.fail = fail

    def __call__(self, query: str, max_results: int) -> list[dict]:
        self.calls.append(query)
        if self.fail:
            raise SourceUnavailable("offline: search unreachable")
        return self.by_query.get(query.strip().lower(), [])


def make_agent(tmp_path, plan=PLAN, report=REPORT,
               web: ScriptedWeb | None = None,
               llm_answers: list[str] | None = None) -> tuple:
    cdir = tmp_path / "checkpoints"
    llm = ScriptedLLM(llm_answers if llm_answers is not None
                      else [plan, report, report])
    agent = ResearchAgent(llm=llm, checkpoint_dir_=str(cdir))
    agent.toolbox.search_fn = web or ScriptedWeb(WEB_SOURCES)
    return agent, cdir, llm


def make_request(text: str = QUESTION, **meta) -> AgentRequest:
    return AgentRequest(task_id="task-test-research001",
                        task_type=TaskType.RESEARCH, text=text,
                        metadata=meta)


@pytest.fixture()
def sandbox_rag(tmp_path, monkeypatch):
    """A real (offline) mini knowledge base with the rag_agent caller grant,
    exactly like test_rag_strategies does."""
    pytest.importorskip("numpy")
    from core import rag, rag_service
    tmp = tmp_path / "kb"
    tmp.mkdir()
    rag.KB_DIR = tmp
    rag.LIBRARY_DIR = tmp / "library"
    rag.CHUNKS_FILE = tmp / "chunks.jsonl"
    rag.VECTORS_FILE = tmp / "vectors.npz"
    rag.REGISTRY_FILE = tmp / "registry.json"
    rag.AUDIT_FILE = tmp / "audit.log"
    rag_service.CALLERS_FILE = tmp / "rag_callers.json"
    rag_service.CALLERS_FILE.write_text(json.dumps({
        "research_agent": {"collections": ["__all__"], "can_write": False},
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

    doc = tmp / "occ.txt"
    doc.write_text(
        "Chapter 1. Optimistic concurrency control assumes conflicts are "
        "rare: transactions run without locks and validate at commit time. "
        "Chapter 2. Pessimistic control locks resources before use, "
        "trading throughput for guaranteed exclusivity under high "
        "contention.\n", encoding="utf-8")
    assert rag.ingest(str(doc), collection="research",
                      title="Concurrency Notes")["ok"]
    return rag_service


# ── 1. source policy ─────────────────────────────────────────────────────────

class TestSourcePolicy:
    @pytest.mark.parametrize("url", [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "data:text/html,hi",
        "http://user:pass@example.com/x",
        "http://localhost/x",
        "http://127.0.0.1/x",
        "http://192.168.1.10/admin",
        "http://10.0.0.1/",
        "http://169.254.169.254/latest/meta-data",
        "http://box.internal/",
        "http://no-such-host-xyz.invalid/",
    ])
    def test_blocked_urls(self, url):
        ok, _why = url_allowed(url)
        assert not ok, url

    def test_public_urls_pass(self):
        ok, why = url_allowed("https://en.wikipedia.org/wiki/RAG")
        assert ok, why

    def test_blocked_search_hits_are_dropped(self):
        tb = SourceToolbox(
            search_fn=lambda q, k: [
                {"title": "ok", "snippet": "fine",
                 "url": "https://example.com/ok"},
                {"title": "evil", "snippet": "private",
                 "url": "http://169.254.169.254/meta"},
                {"title": "local", "snippet": "loop",
                 "url": "http://localhost:8000/x"},
            ])
        hits = tb.web_search("q")
        urls = [h["url"] for h in hits]
        assert urls == ["https://example.com/ok"]


# ── 2. permissions / degradation ─────────────────────────────────────────────

class TestPermissionsAndDegradation:
    def test_web_tier_disabled(self):
        tb = SourceToolbox(SourcePermissions(allow_web=False),
                           search_fn=lambda q, k: [])
        with pytest.raises(SourceUnavailable):
            tb.web_search("anything")

    def test_search_backend_failure_raises(self):
        def boom(q, k):
            raise OSError("no network")
        tb = SourceToolbox(search_fn=boom)
        with pytest.raises(SourceUnavailable):
            tb.web_search("anything")

    def test_blind_kb_caller_degrades_to_empty(self, tmp_path, monkeypatch):
        from core import rag_service
        rag_service.CALLERS_FILE = tmp_path / "rag_callers.json"
        (tmp_path / "rag_callers.json").write_text("{}", encoding="utf-8")
        tb = SourceToolbox()
        res = tb.local_kb("iron condor")
        assert res.get("ok") is True
        assert res.get("results") in (None, [],) or not res.get("results")

    def test_kb_search_failure_is_recorded_not_raised(self):
        tb = SourceToolbox(kb_search=lambda q, k, c: (_ for _ in ()).throw(
            RuntimeError("kb down")))
        res = tb.local_kb("q")
        assert not res.get("ok") and "kb down" in res.get("error", "")


# ── 3. secret masking ────────────────────────────────────────────────────────

class TestSecretMasking:
    def test_snippet_keys_are_masked(self):
        tb = SourceToolbox(search_fn=lambda q, k: [{
            "title": "leak",
            "snippet": "the key is AIzaSyA1234567890abcdefghijklmnopQRstu",
            "url": "https://example.com/k",
        }])
        hits = tb.web_search("q")
        assert "AIzaSy" not in hits[0]["snippet"]

    def test_mask_secrets_passthrough(self):
        out, kinds = mask_secrets("password: 'hunter2hunter2'")
        assert "hunter2" not in out and kinds


# ── 4. claim verification ladder ─────────────────────────────────────────────

class TestClaimVerification:
    def _sources(self):
        return [
            {"index": 1, "title": "A", "url": "https://a.example.com/x",
             "host": "a.example.com", "snippet": S_OCC + " " + S_PREF,
             "tier": "web", "query": "q"},
            {"index": 2, "title": "B", "url": "https://b.example.com/y",
             "host": "b.example.com", "snippet": S_PREF,
             "tier": "web", "query": "q"},
            {"index": 3, "title": "C", "url": "https://c.example.com/z",
             "host": "c.example.com", "snippet": S_PES,
             "tier": "web", "query": "q"},
        ]

    def test_corroborated_claim_is_supported(self):
        sources = self._sources()
        claims = _Claims.from_evidence(sources)
        verdicts = _verify_claims(claims, sources)
        supported = [v for v in verdicts if v["verdict"] == "supported"]
        assert supported, verdicts
        assert all(v["support"] >= 0.6 for v in supported)

    def test_single_source_claim_stays_unverified(self):
        sources = self._sources()
        lone = {"claim": "The Zephyr engine prototype flew in June 2031 "
                         "above the Baltic sea.", "source_idx": 3,
                "url": "https://c.example.com/z", "title": "C"}
        verdicts = _verify_claims([lone], sources)
        assert verdicts[0]["verdict"] in ("unverified", "contested")

    def test_contested_beats_unverified_when_conflict_marked(self):
        sources = self._sources()
        sources.append({
            "index": 4, "title": "D", "url": "https://d.example.com/w",
            "host": "d.example.com",
            "snippet": "However, prefer pessimistic locking when conflicts "
                       "are frequent and contention is high.",
            "tier": "web", "query": "q"})
        claim = {"claim": S_PREF, "source_idx": 1,
                 "url": "https://a.example.com/x", "title": "A"}
        verdicts = _verify_claims([claim], sources)
        assert verdicts[0]["verdict"] in ("supported", "contested")

    def test_empty_sources_give_no_verdicts(self):
        verdicts = _verify_claims(
            [{"claim": "x", "source_idx": 1,
              "url": "", "title": ""}], [])
        assert verdicts == []


# ── 5. the full offline workflow ─────────────────────────────────────────────

class TestWorkflow:
    def test_happy_path_stages_and_citations(self, tmp_path):
        agent, cdir, llm = make_agent(tmp_path)
        result = agent.handle(make_request())
        assert result.status is TaskStatus.COMPLETED, result.text
        assert result.agent == "research_agent"
        stages = [s["stage"] for s in result.data["stage_trace"]]
        for stage in ("question", "plan", "search", "collect", "compare",
                      "verify", "report"):
            assert stage in stages, stages
        # citations preserved: [n] markers + references list
        assert "[1]" in result.text or "[2]" in result.text
        assert "Sources:" in result.text
        # structured report present
        rep = result.data["report"]
        assert rep["supported_claims"] >= 1
        assert rep["confidence"] in ("low", "medium", "high")
        # planner prompt + report prompt both went to the (scripted) model
        assert "research question" in llm.prompts[0].lower()
        assert any("research report" in p.lower() for p in llm.prompts[1:])

    def test_plan_gates_what_is_searched(self, tmp_path):
        web = ScriptedWeb(WEB_SOURCES)
        agent, _c, llm = make_agent(tmp_path, web=web)
        agent.handle(make_request())
        searched = " ".join(q.lower() for q in web.calls)
        assert "optimistic concurrency control database" in searched
        # plan asked for exactly its queries — nothing invented
        assert len(web.calls) == 3

    def test_unfounded_question_never_becomes_facts(self, tmp_path):
        web = ScriptedWeb({})                # nothing out there
        agent, _c, llm = make_agent(
            tmp_path, web=web,
            llm_answers=[PLAN, "The Nimbus-9 engine has ratio 77.3 [1].",
                         "The Nimbus-9 engine has ratio 77.3 [1]."])
        result = agent.handle(make_request(
            "What is the exact thrust-to-weight ratio of the unannounced "
            "Nimbus-9 engine variant?"))
        assert result.status is TaskStatus.COMPLETED
        rep = result.data["report"]
        assert rep["supported_claims"] == 0
        # the fabricated number must not enter the answer body as fact
        assert "77.3" not in (rep["summary"] or "")
        assert "no evidence" in result.text.lower()

    def test_search_failure_is_honest_not_invented(self, tmp_path):
        web = ScriptedWeb({}, fail=True)
        agent, _c, _l = make_agent(tmp_path, web=web)
        result = agent.handle(make_request())
        assert result.status is TaskStatus.COMPLETED
        assert "no evidence found" in result.text.lower()
        assert result.data["failed_sources"], "failures not recorded"

    def test_single_source_uncertainty_is_reported(self, tmp_path):
        single = {"only query": [WEB_SOURCES[
            "optimistic concurrency control database"][0]]}
        plan = json.loads(PLAN)
        plan["search_queries"] = ["only query"]
        web = ScriptedWeb(single)
        agent, _c, _l = make_agent(tmp_path, plan=json.dumps(plan), web=web)
        result = agent.handle(make_request())
        rep = result.data["report"]
        joined = " ".join(rep["uncertainties"]).lower()
        assert "single host" in joined or rep["supported_claims"] == 0

    def test_deep_fetch_only_permitted_urls(self, tmp_path):
        fetched: list[str] = []
        plan = json.loads(PLAN)
        plan["deep_fetch"] = True
        plan["search_queries"] = ["optimistic concurrency control database"]

        def fake_fetch(url: str) -> dict:
            ok, _why = url_allowed(url)
            assert ok, f"fetch attempted a blocked URL: {url}"
            fetched.append(url)
            return {"ok": True, "url": url,
                    "text": "fuller page text about optimistic "
                            "concurrency control validation at commit time "
                            "with much more detail " * 30}

        agent, _c, _l = make_agent(tmp_path, plan=json.dumps(plan))
        agent.toolbox.fetch_fn = fake_fetch
        agent.handle(make_request())
        assert fetched, "deep fetch never ran"

    def test_kb_tier_integrates_rag_service(self, tmp_path, sandbox_rag):
        plan = json.loads(PLAN)
        plan["use_local_kb"] = True
        plan["search_queries"] = ["optimistic concurrency control"]
        web = ScriptedWeb({})
        agent, _c, _l = make_agent(tmp_path, plan=json.dumps(plan), web=web)
        result = agent.handle(make_request())
        assert result.status is TaskStatus.COMPLETED, result.text
        kb_sources = [s for s in result.data["sources"]
                      if s["tier"] == "kb"]
        assert kb_sources, "local KB contributed nothing"
        assert kb_sources[0]["url"].startswith("kb://")

    def test_kb_failure_recorded_not_fatal(self, tmp_path):
        plan = json.loads(PLAN)
        plan["use_local_kb"] = True
        plan["search_queries"] = ["q1"]
        agent, _c, _l = make_agent(tmp_path, plan=json.dumps(plan),
                                   web=ScriptedWeb({}))
        agent.toolbox.kb_search = lambda q, k, c: {"ok": False,
                                                   "error": "kb down"}
        result = agent.handle(make_request())
        assert result.status is TaskStatus.COMPLETED
        assert any("kb" in f for f in result.data["failed_sources"])


# ── 6. report format + checkpoint plumbing ───────────────────────────────────

class TestReportAndCheckpoints:
    def test_unverified_section_in_structured_report(self, tmp_path):
        agent, _c, _l = make_agent(tmp_path)
        result = agent.handle(make_request())
        rep = result.data["report"]
        assert "unverified_claims" in rep
        assert isinstance(rep["unverified_claims"], list)

    def test_checkpoint_roundtrip_and_resume(self, tmp_path):
        agent, cdir, _l = make_agent(tmp_path)
        first = agent.handle(make_request())
        assert first.status is TaskStatus.COMPLETED
        cp = load_checkpoint("task-test-research001", cdir)
        assert cp and cp["status"] == "completed"
        assert cp["sources"], "sources not preserved"

        # resume reuses the stored evidence without re-planning
        agent2, _c2, llm2 = make_agent(tmp_path,
                                       llm_answers=["", "dummy", "dummy"])
        second = agent2.handle(make_request(resume="task-test-research001"))
        assert second.status is TaskStatus.COMPLETED
        assert len(second.data["sources"]) == len(first.data["sources"])

    def test_missing_checkpoint_is_honest_failure(self, tmp_path):
        agent, _c, _l = make_agent(tmp_path)
        r = agent.handle(make_request(resume="task-test-nothere0001"))
        assert r.status is TaskStatus.FAILED
        assert "No checkpoint" in r.text

    def test_save_load_roundtrip(self, tmp_path):
        cdir = tmp_path / "cp"
        save_checkpoint({"task_id": "task-test-x0000000001", "status": "x"},
                        cdir)
        loaded = load_checkpoint("task-test-x0000000001", cdir)
        assert loaded and loaded["status"] == "x"


# ── 7. misc units + registry integration ─────────────────────────────────────

class TestUnitsAndRegistry:
    def test_extract_json_tolerates_fences(self):
        raw = "```json\n{\"a\": 1}\n```"
        assert _extract_json(raw) == {"a": 1}

    def test_extractive_report_from_supported_claims_only(self, tmp_path):
        agent, _c, _l = make_agent(tmp_path, llm_answers=[PLAN, None, None])
        result = agent.handle(make_request())
        assert result.status is TaskStatus.COMPLETED
        # extractive floor: the body must come from evidence sentences
        body = result.data["report"]["summary"] or result.text
        assert "[1]" in body or "[2]" in body or "[3]" in body

    def test_stub_by_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_RESEARCH_AGENT", raising=False)
        reg = build_default_registry()
        assert type(reg.get("research_agent")).__name__ == "StubAgent"

    def test_real_agent_when_enabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_RESEARCH_AGENT", "on")
        reg = build_default_registry()
        agent = reg.get("research_agent")
        assert type(agent).__name__ == "ResearchAgent"
        assert TaskType.RESEARCH in agent.task_types

    def test_empty_question_needs_input(self, tmp_path):
        agent, _c, _l = make_agent(tmp_path)
        r = agent.handle(make_request(""))
        assert r.status is TaskStatus.NEEDS_INPUT
        assert r.data["needs"] == "question"

    def test_strip_html(self):
        raw = "<html><head><style>x{}</style></head><body><h1>Title</h1>" \
              "<p>Body &amp; text.</p><script>evil()</script></body></html>"
        text = strip_html(raw)
        assert "Title" in text and "Body & text." in text
        assert "evil()" not in text and "x{}" not in text
