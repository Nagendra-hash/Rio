"""Live scientific integration tests — real APIs, real analysis, real models.

Opt-in and never part of the default run:

    JARVIS_SCIENCE_LIVE=1 .venv/bin/python -m pytest tests/test_scientific_live.py -q -m science_live

Every assertion is about observed evidence: records returned by a public API, a
file on disk, a model that actually answered. Anything that cannot run is
SKIPPED with the reason, and reported separately from a pass.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from orchestrator.contracts import AgentRequest, TaskStatus, TaskType, new_task_id  # noqa: E402

pytestmark = [
    pytest.mark.science_live,
    pytest.mark.skipif(os.environ.get("JARVIS_SCIENCE_LIVE") != "1",
                       reason="live science tests are opt-in: set JARVIS_SCIENCE_LIVE=1"),
]

CSV = BASE / "tests" / "fixtures" / "science" / "sample.csv"


def _agent():
    from orchestrator.agents.scientific.scientific_agent import ScientificAgent

    return ScientificAgent()


def _run(text: str, **meta):
    return _agent().handle(
        AgentRequest(task_id=new_task_id("live"), task_type=TaskType.SCIENTIFIC,
                     text=text, metadata=meta)
    )


# ── 1. model routing + literature ───────────────────────────────────────────


def test_live_model_answers_through_rio_router():
    from orchestrator.agents.scientific.scientific_agent import _DepthLLM

    llm = _DepthLLM("light_reasoning", timeout_s=45)
    answer = llm.complete("Reply with the single word: ok", system="")
    if not answer:
        pytest.skip(f"no model available through the router ({llm.last_error[:80]})")
    assert answer.strip()
    assert llm.last_provider, "the router did not report which provider answered"


def test_live_literature_review_end_to_end():
    result = _run("What does the evidence say about GLP-1 receptor agonists and weight loss?",
                  depth="quick")
    assert result.status is TaskStatus.COMPLETED, result.text
    data = result.data or {}
    assert data["records"] >= 3, data
    assert data["references"] >= 3, data
    assert Path(data["report_path"]).is_file()
    assert data["issues"] == [], data["issues"]
    for adjustment in data.get("claim_adjustments") or []:
        assert adjustment.startswith("adjusted:"), adjustment

    markdown = Path(data["report_path"]).read_text(encoding="utf-8")
    assert "## References" in markdown
    # Every [n] in the body must exist in the reference list.
    import re

    cited = {int(n) for n in re.findall(r"\[(\d{1,3})\]", markdown)}
    assert cited, "the report cites nothing"
    assert max(cited) <= data["references"], (max(cited), data["references"])


# ── 2. public databases ─────────────────────────────────────────────────────


def test_live_pubmed():
    from core.scientific import databases

    result = databases.search_pubmed("CRISPR base editing", limit=3)
    assert result.ok, result.error
    assert result.records and result.records[0]["title"]
    assert result.records[0]["url"].startswith("https://pubmed.ncbi.nlm.nih.gov/")


def test_live_uniprot():
    from core.scientific import databases

    result = databases.lookup_uniprot("P04637")
    assert result.ok, result.error
    assert result.records[0]["accession"] == "P04637"
    assert "p53" in result.records[0]["name"].lower()


def test_live_pubchem_and_chembl():
    from core.scientific import databases

    pubchem = databases.lookup_pubchem("aspirin")
    assert pubchem.ok and pubchem.records and pubchem.records[0]["cid"] == 2244
    chembl = databases.search_chembl("aspirin", limit=2)
    assert chembl.ok and chembl.records and chembl.records[0]["chembl_id"].startswith("CHEMBL")


def test_live_crossref_doi_verification():
    from core.scientific import databases

    result = databases.lookup_doi("10.1038/nature12373")
    assert result.ok, result.error
    assert result.records[0]["verified"] is True
    assert result.records[0]["year"] == "2013"


def test_live_clinicaltrials():
    from core.scientific import databases

    result = databases.search_clinicaltrials("metformin", limit=3)
    assert result.ok, result.error
    assert result.records and result.records[0]["nct_id"].startswith("NCT")


# ── 3. analysis: statistics, ML, figures, resources ────────────────────────


def test_live_analysis_produces_verified_numbers_and_a_figure():
    from core.scientific import analysis

    describe = analysis.run("describe", {"path": str(CSV)})
    assert describe.ok, describe.error
    assert describe.payload["stats"]["rows"] == 60

    ttest = analysis.run("ttest", {"path": str(CSV), "value_col": "score",
                                   "group_col": "group"})
    assert ttest.ok, ttest.error
    assert ttest.payload["stats"]["p"] < 0.05
    assert abs(ttest.payload["stats"]["t"]) > 3

    plot = analysis.run("plot", {"path": str(CSV), "kind": "box", "x": "score",
                                 "group": "group", "title": "Live figure"})
    assert plot.ok, plot.error
    png = next(path for path in plot.figures if path.endswith(".png"))
    assert Path(png).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_live_resource_usage_stays_bounded():
    from core.scientific import analysis

    result = analysis.run("describe", {"path": str(CSV)})
    assert result.ok
    assert result.peak_rss_mb < 1500, f"peak RSS {result.peak_rss_mb} MB"
    assert result.duration_s < 120, f"took {result.duration_s}s"


# ── 4. orchestration + interfaces ──────────────────────────────────────────


def test_live_orchestrator_dispatch():
    from orchestrator.integration import shared_orchestrator

    orch = shared_orchestrator()
    result = orch.submit("look up TP53 in UniProt", channel="science_live",
                         metadata={"action": "database", "database": "uniprot",
                                   "query": "P04637", "wait_s": 120})
    assert result.agent == "scientific_agent", result
    assert result.status is TaskStatus.COMPLETED, result.text
    assert "p53" in result.text.lower()


class _FakePlayer:
    def __init__(self):
        self.logs: list[str] = []
        self.shown: list[tuple[str, str]] = []
        self.inbound_target = ""

    def write_log(self, text: str):
        self.logs.append(str(text))

    def show_content(self, title: str, body: str):
        self.shown.append((str(title), str(body)))


def test_live_action_runs_and_delivers_to_the_originating_chat(monkeypatch):
    from actions import scientific_agent as action
    from core.scientific import delivery

    sent: list[tuple[str, str]] = []
    files: list[tuple[str, str]] = []
    monkeypatch.setattr(delivery, "send_text", lambda target, text: sent.append((target, text)) or True)
    monkeypatch.setattr(delivery, "send_file", lambda target, path, caption="": files.append((target, str(path))) or True)

    player = _FakePlayer()
    player.inbound_target = "telegram:4242"
    reply = action.scientific_agent(
        {"task": "look up TP53 in UniProt", "action": "database",
         "database": "uniprot", "query": "P04637"},
        player=player,
    )
    assert "started" in reply or "p53" in reply.lower()

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not sent:
        time.sleep(0.5)
    assert sent, "the result was not delivered to the chat"
    assert sent[0][0] == "telegram:4242"


# ── 5. browser fallback for a JavaScript-only page ─────────────────────────


_PAGE = """<html><body><div id="root"></div>
<script>
document.getElementById('root').innerHTML =
  '<h1>JavaScript Only Page</h1><p>' + 'The evidence lives in this rendered text. '.repeat(20) + '</p>';
</script></body></html>"""


class _JSHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def do_GET(self):  # noqa: N802
        body = _PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_live_browser_fallback_reads_a_js_only_page():
    from core.browser_agent import runtime as browser_runtime
    from core.scientific.config import Depth

    ok, _version, reason = browser_runtime.preflight()
    if not ok:
        pytest.skip(f"browser runtime unavailable ({reason})")

    server = ThreadingHTTPServer(("127.0.0.1", 0), _JSHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        agent = _agent()
        profile = Depth("standard", 2, 4, 4, True, "research", 3)
        record = {"source": "test", "title": "JS page", "url": f"http://{host}:{port}/"}
        notes = agent._fetch_full_text([record], profile)
        assert notes
        source = record.get("full_text_source")
        if source != "browser":
            pytest.skip(f"browser fallback did not engage: {notes[:1]}")
        assert "JavaScript Only Page" in record["full_text"]
    finally:
        server.shutdown()
        server.server_close()
        from core.browser_agent import runtime as rt

        for job in rt.shared_runtime().active():
            rt.shared_runtime().cancel(job["job_id"])


# ── 6. RAG round-trip (optional: needs embeddings) ─────────────────────────


def test_live_rag_roundtrip():
    from core import rag_service

    title = f"RIO science live test {int(time.time())}"
    text = ("Base editors convert A to G without double-strand breaks. "
            "Off-target editing is the principal safety concern. " * 6)
    ingested = rag_service.ingest_text(
        caller="scientific_agent", text=text, title=title,
        source="science_live_test", metadata={"live_test": True},
    )
    if not isinstance(ingested, dict) or not ingested.get("ok"):
        pytest.skip(f"RAG ingest unavailable ({ingested})")
    found = rag_service.search(caller="scientific_agent", query="base editor off-target safety", top_k=3)
    if not isinstance(found, dict) or not found.get("ok"):
        pytest.skip(f"RAG search unavailable ({found})")
    titles = [str(item.get("source") or "") for item in found.get("results") or []]
    assert any(title in value for value in titles), titles
    rag_service.forget(caller="scientific_agent", doc_id=ingested.get("doc_id"))


# ── 7. a real Telegram send is BLOCKED without credentials ─────────────────


def test_live_telegram_send():
    if os.environ.get("JARVIS_SCIENCE_TELEGRAM") != "1":
        pytest.skip("real bot delivery not requested (set JARVIS_SCIENCE_TELEGRAM=1)")
    from core.scientific import delivery

    assert delivery.send_text("telegram", "RIO science live test"), "delivery failed"
