"""
tests/test_rag_learner.py — offline tests for the self-directed RAG learner.

No network, no model, no embeddings: search/fetch/summarize/store are injected,
and state/config live in a tmp dir. Covers the contract that matters:

  * a cycle searches, fetches, distils and stores a cited dossier;
  * the same URL is never re-learned the next day (no churn);
  * new sources UPDATE the existing dossier instead of adding a duplicate;
  * an unanswered RAG question becomes tomorrow's topic + exact query;
  * no sources / model failure degrade honestly (extractive note, no invention);
  * due() runs once a day after the configured hour.

Run:  .venv/bin/python -m pytest tests/test_rag_learner.py -q
"""
from __future__ import annotations

import copy
import json
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from core import rag_learner as rl  # noqa: E402


def _setup(tmp_path, monkeypatch, **over):
    monkeypatch.setattr(rl, "STATE_FILE", tmp_path / "rag_learning" / "state.json")
    monkeypatch.setattr(rl, "CONFIG_FILE", tmp_path / "config" / "rag_learning.json")
    # Isolate from the real memory/long_term.json and self-feeding gaps.
    monkeypatch.setattr(rl, "_ensure_seeded", lambda state, cfg: False)
    cfg = {
        "enabled": True, "hour": 0, "interval_hours": 0,
        "topics_per_run": 1, "queries_per_topic": 1,
        "results_per_query": 2, "deep_fetch": 1,
        "min_note_chars": 10, "collection": "web_knowledge",
        "caller": "rag_learner", "access": "private",
        "interests": [], "blocked_domains": [],
    }
    cfg.update(over)
    rl.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    rl.CONFIG_FILE.write_text(json.dumps(cfg), encoding="utf-8")
    return cfg


def _state_with_topic(topic: str, **fields) -> None:
    entry = rl._new_topic()
    entry.update(fields)
    rl.save_state({"topics": {topic: entry}})


def _search_for(urls):
    def _search(query, k):
        return [{"title": f"Source {i}", "snippet": f"Fact about {query} {i}",
                 "url": u} for i, u in enumerate(urls)]
    return _search


def _fetch(_url):
    return "A detailed page body with enough words to count as real material."


def _summary(topic, existing, material):
    # The digest makes the note differ when the source material differs —
    # a real summary would too; identical output means nothing new was learned.
    import hashlib
    tag = hashlib.sha256(material.encode("utf-8")).hexdigest()[:10]
    return (f"# {topic}\n\nA concise, cited summary of the topic ({tag}).\n\n"
            f"- A key fact from the sources [1].\n"
            f"- A second fact [2].\n\n## Sources\n1. source\n")


def _collect_store(calls, doc_id="doc-1"):
    def _store(text, title, collection, metadata, existing_doc_id):
        calls.append({"text": text, "title": title, "collection": collection,
                      "metadata": metadata, "doc_id": existing_doc_id})
        return {"ok": True, "doc_id": existing_doc_id or doc_id}
    return _store


# ── 1. a cycle learns and files a dossier ───────────────────────────────────
def test_cycle_searches_summarises_and_stores(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _state_with_topic("quantum computing")
    calls = []
    result = rl.run_cycle(force=True, topics=["quantum computing"],
                          search_fn=_search_for(["https://a.example/1",
                                                 "https://b.example/2"]),
                          fetch_fn=_fetch, summarize_fn=_summary,
                          store_fn=_collect_store(calls))
    assert result["ok"] and result["learned"] == 1, result
    assert len(calls) == 1 and calls[0]["doc_id"] == ""
    assert calls[0]["collection"] == "web_knowledge"
    assert calls[0]["metadata"]["learner"] is True
    assert calls[0]["metadata"]["source_count"] == 2
    # the working dossier landed on disk
    note = rl._notes_dir() / f"{rl._slug('quantum computing')}.md"
    assert note.exists() and "key fact" in note.read_text(encoding="utf-8")
    state = rl.load_state()
    entry = state["topics"]["quantum computing"]
    assert entry["doc_id"] == "doc-1" and entry["learned"] == 1
    assert entry["last_status"] == "learned"
    assert len(state["seen_urls"]) == 2


# ── 2. same URLs are not re-learned; new ones update the dossier ────────────
def test_same_sources_skip_then_new_sources_update(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _state_with_topic("quantum computing")
    urls = ["https://a.example/1"]
    calls = []
    kw = dict(fetch_fn=_fetch, summarize_fn=_summary,
              store_fn=_collect_store(calls))
    rl.run_cycle(force=True, topics=["quantum computing"],
                 search_fn=_search_for(urls), **kw)
    assert len(calls) == 1

    # Same URL again → no sources, no store call, weight fades a little.
    again = rl.run_cycle(force=True, topics=["quantum computing"],
                         search_fn=_search_for(urls), **kw)
    assert again["no_sources"] == 1 and len(calls) == 1
    assert rl.load_state()["topics"]["quantum computing"]["last_status"] == "no_sources"

    # A genuinely new URL → UPDATE the same doc (doc_id, not a duplicate).
    upd = rl.run_cycle(force=True, topics=["quantum computing"],
                       search_fn=_search_for(["https://c.example/3"]), **kw)
    assert upd["learned"] == 1 and len(calls) == 2
    assert calls[1]["doc_id"] == "doc-1"
    assert rl.load_state()["updated"] == 1


# ── 3. an unanswered question becomes a weighted topic with the exact query ─
def test_unanswered_question_becomes_tomorrows_research(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    question = "what is quantum entanglement and why does it matter"
    rl.note_gap(question, caller="jarvis", force=True)
    state = rl.load_state()
    entries = {k.lower(): (k, v) for k, v in state["topics"].items()}
    key, entry = entries[rl._topic_from_query(question).lower()]
    assert entry["origin"] == "gap"
    assert entry["weight"] >= 2.0
    assert entry["queries"] == [question]
    gaps = rl._gaps_file().read_text(encoding="utf-8").strip().splitlines()
    assert json.loads(gaps[-1])["caller"] == "jarvis"

    seen_queries = []

    def _search(query, k):
        seen_queries.append(query)
        return [{"title": "t", "snippet": "s", "url": "https://x.example/a"}]

    rl.run_cycle(force=True, topics=[key], search_fn=_search,
                 fetch_fn=_fetch, summarize_fn=_summary,
                 store_fn=_collect_store([]))
    # The user's exact question is researched first.
    assert question in seen_queries
    # Once satisfied, the curiosity queue is cleared.
    assert rl.load_state()["topics"][key]["queries"] == []


# ── 4. a pytest run cannot pollute the real curiosity store ────────────────
def test_note_gap_is_noop_under_pytest_without_force(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    rl.note_gap("what is quantum entanglement", caller="jarvis")
    assert rl.load_state()["topics"] == {}
    assert not rl._gaps_file().exists()


# ── 5. no sources → honest no-op, nothing invented ──────────────────────────
def test_no_sources_does_not_store_or_invent(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _state_with_topic("empty topic")
    calls = []
    result = rl.run_cycle(force=True, topics=["empty topic"],
                          search_fn=lambda q, k: [],
                          fetch_fn=_fetch, summarize_fn=_summary,
                          store_fn=_collect_store(calls))
    assert result["no_sources"] == 1 and result["learned"] == 0
    assert calls == []
    assert not (rl._notes_dir() / f"{rl._slug('empty topic')}.md").exists()
    assert rl.load_state()["topics"]["empty topic"]["last_status"] == "no_sources"


# ── 6. model down → extractive note, still cited/honest ─────────────────────
def test_model_failure_falls_back_to_extractive_note(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _state_with_topic("solar power")
    calls = []
    rl.run_cycle(force=True, topics=["solar power"],
                 search_fn=_search_for(["https://s.example/1"]),
                 fetch_fn=_fetch, summarize_fn=lambda *a: "",
                 store_fn=_collect_store(calls))
    assert len(calls) == 1
    text = calls[0]["text"]
    assert "Extractive dossier" in text
    assert "https://s.example/1" in text
    assert "[1]" in text          # snippets keep their citation index


# ── 7. a transient search failure is retried once ───────────────────────────
def test_transient_search_failure_is_retried(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(rl, "_SEARCH_RETRY_DELAY_S", 0)
    _state_with_topic("solar power")
    calls = {"n": 0}

    def _flaky(query, k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("No results found.")
        return [{"title": "t", "snippet": "s", "url": "https://x.example/1"}]

    result = rl.run_cycle(force=True, topics=["solar power"],
                          search_fn=_flaky, fetch_fn=_fetch,
                          summarize_fn=_summary,
                          store_fn=_collect_store([]))
    assert result["learned"] == 1 and calls["n"] >= 2


# ── 8. due(): once a day, only after the configured hour ────────────────────
def test_due_once_per_day_after_hour(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, hour=9)
    monkeypatch.setattr(rl, "_ensure_seeded", lambda state, cfg: False)
    rl.save_state({})
    assert rl.due(now=datetime(2026, 10, 19, 8, 0)) is False
    assert rl.due(now=datetime(2026, 10, 19, 9, 1)) is True
    state = rl.load_state()
    state["last_run_date"] = "2026-10-19"
    state["last_run"] = __import__("time").time()
    rl.save_state(state)
    assert rl.due(now=datetime(2026, 10, 19, 23, 59)) is False


# ── 9. a forgotten dossier is re-created, not retried forever ───────────────
def test_forgotten_dossier_is_recreated(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _state_with_topic("solar power", doc_id="gone-doc")
    calls = []

    def _store(text, title, collection, metadata, doc_id):
        calls.append(doc_id)
        if doc_id:
            return {"ok": False, "error": "unknown document: gone-doc"}
        return {"ok": True, "doc_id": "new-doc"}

    result = rl.run_cycle(force=True, topics=["solar power"],
                          search_fn=_search_for(["https://s.example/1"]),
                          fetch_fn=_fetch, summarize_fn=_summary,
                          store_fn=_store)
    assert result["learned"] == 1
    assert calls == ["gone-doc", ""]
    entry = rl.load_state()["topics"]["solar power"]
    assert entry["doc_id"] == "new-doc" and entry["last_status"] == "learned"


# ── 10. store failure keeps the note for a retry (no data loss) ─────────────
def test_store_failure_marks_pending_and_keeps_note(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _state_with_topic("rust async")

    def _boom(*a, **k):
        raise RuntimeError("embeddings unavailable")

    result = rl.run_cycle(force=True, topics=["rust async"],
                          search_fn=_search_for(["https://r.example/1"]),
                          fetch_fn=_fetch, summarize_fn=_summary,
                          store_fn=_boom)
    assert result["failed"] == 1
    entry = rl.load_state()["topics"]["rust async"]
    assert entry["pending"] is True and entry["last_status"].startswith("store_failed")
    assert (rl._notes_dir() / f"{rl._slug('rust async')}.md").exists()


# ── 11. a gap recorded mid-cycle survives the cycle's save ──────────────────
def test_merge_concurrent_preserves_midcycle_gap(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    mine = rl._default_state()
    mine["topics"]["solar power"] = rl._new_topic(weight=0.8)
    mine["runs"] = 1
    baseline = copy.deepcopy(mine)          # what was on disk at cycle start
    disk = copy.deepcopy(mine)
    disk["topics"]["solar power"]["weight"] = 3.0
    disk["topics"]["solar power"]["queries"] = ["what is solar power"]
    disk["topics"]["brand new gap"] = rl._new_topic(weight=2.0, origin="gap")
    disk["seen_urls"] = {"https://x.example/1": 1.0}
    rl._merge_concurrent(mine, disk, baseline)
    assert mine["topics"]["solar power"]["weight"] == 3.0
    assert mine["topics"]["solar power"]["queries"] == ["what is solar power"]
    assert "brand new gap" in mine["topics"]
    assert "https://x.example/1" in mine["seen_urls"]


# ── 12. dashboard endpoints delegate to the learner (no network) ────────────
def test_dashboard_learning_endpoints(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    rl.save_state({})

    import fastapi
    import fastapi.testclient
    from dashboard import rag_api

    app = fastapi.FastAPI()
    rag_api.register(app, lambda req: True)
    client = fastapi.testclient.TestClient(app)

    r = client.get("/api/rag/learning")
    assert r.status_code == 200 and r.json()["ok"] is True

    r = client.post("/api/rag/learning",
                    json={"action": "add_topic", "topic": "macro economics"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert any(t["topic"] == "macro economics" for t in rl.list_topics())

    calls: dict = {}

    def _fake_cycle(force=False, topics=None):
        calls.update(force=force, topics=topics)
        return {"ok": True, "learned": 0}

    monkeypatch.setattr(rl, "run_cycle", _fake_cycle)
    r = client.post("/api/rag/learning",
                    json={"action": "run", "topics": "ai, ml"})
    assert r.status_code == 200
    assert calls == {"force": True, "topics": ["ai", "ml"]}

    r = client.post("/api/rag/learning", json={"action": "config",
                                               "patch": {"hour": 4}})
    assert r.status_code == 200 and r.json()["config"]["hour"] == 4


if __name__ == "__main__":
    raise SystemExit("Run with pytest: .venv/bin/python -m pytest "
                     "tests/test_rag_learner.py -q")
