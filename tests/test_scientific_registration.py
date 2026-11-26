"""Offline tests: the Science Agent is registered, routed and reachable."""

from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import pytest  # noqa: E402

from orchestrator.agents import build_default_registry  # noqa: E402
from orchestrator.classifier import IntentClassifier  # noqa: E402
from orchestrator.contracts import (  # noqa: E402
    AgentRequest, AgentResult, TaskStatus, TaskType, new_task_id,
)

SCIENCE_ASKS = [
    "prepare a literature review on Alzheimer's biomarkers with references",
    "find research papers about reinforcement learning",
    "look up TP53 in UniProt",
    "find clinical trials for metformin",
    "analyze this CSV and generate statistical charts",
    "run a t-test on my dataset with effect sizes",
    "identify research gaps in this topic",
    "generate testable hypotheses about the microbiome",
    "explain this paper in simple English",
]

NOT_SCIENCE = {
    "research the latest developments in artificial intelligence": TaskType.RESEARCH,
    "open YouTube and search for Python tutorials": TaskType.BROWSER,
    "fix this traceback in main.py": TaskType.CODING,
    "what's on my calendar tomorrow": TaskType.CALENDAR,
}


def test_task_type_exists():
    assert TaskType.SCIENTIFIC.value == "scientific"


def test_registry_registers_the_real_agent_behind_its_gate(monkeypatch):
    monkeypatch.setenv("JARVIS_SCIENTIFIC_AGENT", "on")
    agent = build_default_registry().get("scientific_agent")
    assert agent is not None and type(agent).__name__ == "ScientificAgent"
    assert TaskType.SCIENTIFIC in agent.task_types


def test_registry_falls_back_to_a_stub_when_the_gate_is_off(monkeypatch):
    monkeypatch.setenv("JARVIS_SCIENTIFIC_AGENT", "off")
    assert type(build_default_registry().get("scientific_agent")).__name__ == "StubAgent"


@pytest.mark.parametrize("text", SCIENCE_ASKS)
def test_classifier_routes_science_asks(text):
    classification = IntentClassifier().classify(text)
    assert classification.task_type is TaskType.SCIENTIFIC, (text, classification)


@pytest.mark.parametrize("text,expected", list(NOT_SCIENCE.items()))
def test_classifier_does_not_steal_existing_lanes(text, expected):
    classification = IntentClassifier().classify(text)
    assert classification.task_type is expected, (text, classification)


def test_explicit_route_to_the_science_agent():
    classification = IntentClassifier().classify("scientific agent, review CRISPR evidence")
    assert classification.task_type is TaskType.SCIENTIFIC
    assert "explicit" in classification.reason


def test_action_is_discovered():
    from core.action_loader import discover_actions

    registry = discover_actions(BASE / "actions")
    assert "scientific_agent" in registry.names()


def test_chat_alias_resolves():
    from actions.run_agent import _ALIASES, _resolve

    assert _ALIASES["science"] == "scientific_agent"
    agent, key = _resolve(build_default_registry(), "science")
    assert key == "scientific_agent" and agent is not None


def test_dashboard_spec_and_gate():
    from dashboard import rio_api

    assert ("science", "Scientific Research Agent", "scientific_agent") in rio_api._AGENTS_SPEC
    assert rio_api._GATES["scientific_agent"] == ("JARVIS_SCIENTIFIC_AGENT", True)
    assert "scientific_agent" in rio_api._TOOLS
    assert "scientific_agent" in rio_api._MODEL_NOTE


def test_prompt_and_rag_caller():
    prompt = (BASE / "core" / "prompt.txt").read_text(encoding="utf-8")
    assert "scientific_agent:" in prompt
    callers = json.loads((BASE / "config" / "rag_callers.json").read_text())
    assert callers["scientific_agent"]["can_write"] is True
    assert "research" in callers["scientific_agent"]["collections"]


def _agent():
    from orchestrator.agents.scientific.scientific_agent import ScientificAgent

    return ScientificAgent()


def _request(text: str, **meta) -> AgentRequest:
    return AgentRequest(task_id=new_task_id("t"), task_type=TaskType.SCIENTIFIC,
                        text=text, metadata=meta)


def test_agent_refuses_clinical_and_financial_requests():
    agent = _agent()
    clinical = agent.handle(_request("what dose should I give my patient"))
    assert clinical.status is TaskStatus.DENIED
    financial = agent.handle(_request("place a trade for 1 btc"))
    assert financial.status is TaskStatus.DENIED


def test_agent_skills_action_lists_the_library():
    result = _agent().handle(_request("list skills", action="skills"))
    assert result.status is TaskStatus.COMPLETED
    assert "177" in result.text
    assert result.data["summary"]["enabled"] >= 10


def test_agent_analysis_without_a_dataset_asks_for_one():
    result = _agent().handle(_request("analyze my data with a t-test", action="analysis"))
    assert result.status is TaskStatus.NEEDS_INPUT
    assert "dataset" in result.text.lower()


def test_agent_database_action_uses_the_client(monkeypatch):
    from core.scientific import databases

    def fake_lookup(database, query, *, limit=10, budget=None):
        return databases.Result(
            True, database,
            records=[{"source": database, "id": "X", "title": "Cellular tumor antigen p53",
                      "accession": "P04637", "organism": "Homo sapiens",
                      "url": "https://example.org/P04637"}],
            endpoint="https://example.org",
            params={"query": query},
            retrieved_at="2026-01-01T00:00:00Z",
        )

    monkeypatch.setattr(databases, "lookup", fake_lookup)
    result = _agent().handle(_request("look up TP53 in UniProt", action="database",
                                      database="uniprot", query="P04637"))
    assert result.status is TaskStatus.COMPLETED
    assert "Cellular tumor antigen p53" in result.text
    assert result.data["provenance"]["count"] == 1


def test_agent_literature_without_records_is_honest(monkeypatch):
    from core.scientific import databases

    monkeypatch.setattr(databases, "literature", lambda *a, **k: {
        "ok": True, "query": "x", "records": [], "sources_ok": ["pubmed"],
        "sources_failed": [], "total_found": 0, "budget": {}, "retrieved_at": "2026-01-01",
    })
    result = _agent().handle(_request("find papers about a nonexistent topic",
                                      action="literature"))
    assert result.status is TaskStatus.COMPLETED
    assert result.data["records"] == 0
    assert "no records" in result.text.lower()


def test_agent_reports_all_sources_failed(monkeypatch):
    from core.scientific import databases

    monkeypatch.setattr(databases, "literature", lambda *a, **k: {
        "ok": False, "query": "x", "records": [],
        "sources_ok": [], "sources_failed": [{"source": "pubmed", "error": "HTTP 503"}],
        "total_found": 0, "budget": {}, "retrieved_at": "2026-01-01",
    })
    result = _agent().handle(_request("find papers on anything", action="literature"))
    assert result.status is TaskStatus.FAILED
    assert "503" in result.text


def test_report_never_cites_unretrieved_works():
    from core.scientific import report

    references = report.references_from_records([
        {"title": "Known", "doi": "10.1000/known", "year": "2024", "authors": ["A"]},
    ])
    claims = [
        report.Claim("supported", [1], report.ESTABLISHED),
        report.Claim("unsupported", [2], report.ESTABLISHED),
        report.Claim("fabricated doi 10.9999/fake", [], report.ESTABLISHED),
    ]
    checked, issues = report.verify_claims(claims, references)
    assert issues
    assert checked[1].status == report.UNVERIFIED
    markdown = report.render(question="q", references=references, claims=checked)
    assert "10.9999/fake" in markdown  # the text stays, but it is flagged:
    assert any("never retrieved" in issue for issue in report.check_fabrication(markdown, references))
    assert "[2]" not in markdown


def test_established_requires_two_sources():
    from core.scientific import report

    references = report.references_from_records([
        {"title": "One", "doi": "10.1000/1", "year": "2024"},
        {"title": "Two", "doi": "10.1000/2", "year": "2024"},
    ])
    checked, issues = report.verify_claims(
        [report.Claim("single", [1], report.ESTABLISHED),
         report.Claim("double", [1, 2], report.ESTABLISHED)],
        references,
    )
    assert checked[0].status == report.CONTESTED
    assert checked[1].status == report.ESTABLISHED
    assert any("single retrieved source" in issue for issue in issues)


def test_delivery_reports_failures_without_raising(monkeypatch):
    from core.scientific import delivery

    monkeypatch.setattr(delivery, "send_text", lambda target, text: True)
    monkeypatch.setattr(delivery, "send_file", lambda target, path, caption="": False)
    result = delivery.deliver("telegram:1", "done", files=["/no/such/file.md"])
    assert result["text_sent"] is True
    assert result["files_failed"] == ["/no/such/file.md"]
    assert delivery.deliver("", "nothing")["text_sent"] is False
