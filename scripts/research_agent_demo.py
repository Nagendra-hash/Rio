#!/usr/bin/env python3
"""
scripts/research_agent_demo.py — Phase 6 sample workflow, fully offline.

Runs the research agent end-to-end (QUESTION → PLAN → SEARCH → COLLECT →
COMPARE → VERIFY → REPORT) on a small technical research question with a
scripted LLM and scripted sources, so NO network, NO API key and NO model
are needed:

    .venv/bin/python scripts/research_agent_demo.py                 # web
    .venv/bin/python scripts/research_agent_demo.py --task rag      # local KB
    .venv/bin/python scripts/research_agent_demo.py --task unfounded  # honesty
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from orchestrator.agents.research_agent import ResearchAgent  # noqa: E402
from orchestrator.contracts import AgentRequest, TaskType, new_task_id  # noqa: E402

GREEN, YELLOW, DIM, END = "\033[32m", "\033[33m", "\033[2m", "\033[0m"

QUESTION = ("What is the difference between optimistic and pessimistic "
            "concurrency control in databases, and when should each be "
            "preferred?")

PLAN = json.dumps({
    "understanding": "Compare two concurrency-control strategies and their "
                     "applicable workloads.",
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

SOURCES = {
    "optimistic concurrency control database": [
        {"title": "Optimistic concurrency control — Wikipedia",
         "snippet": "Optimistic concurrency control (OCC) assumes multiple "
                    "transactions can complete without interfering; before "
                    "commit, each transaction validates that its reads were "
                    "not modified by others, and aborts on conflict.",
         "url": "https://en.wikipedia.org/wiki/Optimistic_concurrency_control"},
        {"title": "PostgreSQL docs: concurrency control",
         "snippet": "PostgreSQL uses multiversion concurrency control; "
                    "readers do not block writers and writers do not block "
                    "readers, which suits read-heavy workloads.",
         "url": "https://www.postgresql.org/docs/current/mvcc.html"},
    ],
    "pessimistic locking database": [
        {"title": "Pessimistic locking — Wikipedia",
         "snippet": "Pessimistic concurrency control locks a resource "
                    "before use, guaranteeing exclusive access; it performs "
                    "best when conflicts are frequent and contention is "
                    "short.",
         "url": "https://en.wikipedia.org/wiki/Lock_(computer_science)"},
    ],
    "optimistic vs pessimistic concurrency control": [
        {"title": "Citus Data blog: MVCC and locking",
         "snippet": "Optimistic control wins on read-mostly workloads with "
                    "rare conflicts; pessimistic locking wins when "
                    "contention is high and retried transactions would be "
                    "expensive.",
         "url": "https://scalegrid.io/blog/mvcc-locking"},
    ],
}

REPORT = """Optimistic concurrency control lets transactions proceed without
locks and validates at commit: if another transaction modified the data,
the loser aborts and retries [1]. Pessimistic control takes the lock
before use, guaranteeing exclusive access [3]. PostgreSQL-style MVCC keeps
readers and writers from blocking each other, which suits read-heavy
workloads [2]. Preference: optimistic control fits read-mostly workloads
with rare conflicts; pessimistic locking fits high-contention workloads
where retries would be expensive [4].

Uncertainty: production guidance (e.g. exact contention thresholds)
varies by engine and was only partially corroborated across sources."""

SCRIPTED_REPORTS = [REPORT, "dummy", "dummy", "dummy", "dummy", "dummy"]


class ScriptedLLM:
    """Pops scripted answers: first call = plan, the report call = report."""

    def __init__(self, answers: list[str]):
        self.answers = list(answers)
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, system: str = ""):
        self.prompts.append(prompt)
        return self.answers.pop(0) if self.answers else None


class ScriptedWeb:
    """Offline stand-in for the web-search backend."""

    def __init__(self, by_query: dict[str, list[dict]]):
        self.by_query = {k.lower(): v for k, v in by_query.items()}
        self.calls: list[str] = []

    def __call__(self, query: str, max_results: int) -> list[dict]:
        self.calls.append(query)
        return self.by_query.get(query.strip().lower(), [])


def show(result) -> str:
    print(f"\n{YELLOW}── research report {'─' * 38}{END}")
    print(result.text)
    d = result.data
    print(f"{DIM}status={result.status.value} agent={result.agent}{END}")
    print(f"{DIM}stages: {' → '.join(s['stage'] for s in d['stage_trace'])}{END}")
    print(f"{DIM}sources: {len(d['sources'])}  "
          f"supported_claims: {d['report']['supported_claims']}  "
          f"unverified_claims: {len(d['report']['unverified_claims'])}{END}")
    print(f"{DIM}checkpoint: {d.get('checkpoint')}  "
          f"(id={d.get('checkpoint_task')}){END}")
    return str(d.get("checkpoint_task") or "")


def run(task: str, cdir: Path) -> str:
    if task == "rag":
        # Local-KB-only run against a real (offline) mini-library.
        from core import rag, rag_service

        import shutil
        kb = cdir.parent / "kb"
        kb.mkdir(parents=True, exist_ok=True)
        rag.KB_DIR = kb
        rag.LIBRARY_DIR = kb / "library"
        rag.CHUNKS_FILE = kb / "chunks.jsonl"
        rag.VECTORS_FILE = kb / "vectors.npz"
        rag.REGISTRY_FILE = kb / "registry.json"
        rag.AUDIT_FILE = kb / "audit.log"
        rag_service.CALLERS_FILE = cdir / "rag_callers.json"
        cdir.mkdir(parents=True, exist_ok=True)
        rag_service.CALLERS_FILE.write_text(json.dumps({
            "research_agent": {"collections": ["__all__"], "can_write": False},
        }), encoding="utf-8")
        for p in (rag.LIBRARY_DIR,):
            shutil.rmtree(p, ignore_errors=True)
        doc = (rag.KB_DIR / "occ.txt")
        doc.write_text(
            "Chapter 1. Optimistic concurrency control assumes conflicts are "
            "rare: transactions run without locks and validate at commit "
            "time. Pessimistic control locks resources before use, trading "
            "throughput for guaranteed exclusivity under high contention.\n"
            "Chapter 2. Choose optimistic methods for read-mostly workloads; "
            "choose pessimistic locking when conflicts are frequent and "
            "retrying a transaction is expensive.\n", encoding="utf-8")
        try:
            assert rag.ingest(str(doc), collection="research",
                              title="Concurrency Notes")["ok"]
        except Exception as exc:
            print(f"{YELLOW}local KB unavailable in this env ({exc}) — "
                  f"skipping the rag demo{END}")
            return ""

        class NoWeb:
            def __call__(self, query, max_results):
                from orchestrator.agents.research_tools import SourceUnavailable
                raise SourceUnavailable("offline demo: web tier disabled")

        kb_plan = json.loads(PLAN)
        kb_plan["use_local_kb"] = True
        llm = ScriptedLLM([json.dumps(kb_plan), REPORT, REPORT])
        agent = ResearchAgent(llm=llm, checkpoint_dir_=str(cdir))
        agent.toolbox.search_fn = NoWeb()          # web tier fails honestly
        req = AgentRequest(task_id=new_task_id("task"), task_type=TaskType.RESEARCH,
                           text=QUESTION,
                           metadata={"checkpoint_dir": str(cdir)})
        return show(agent.handle(req))

    with tempfile.TemporaryDirectory(prefix="research_demo_") as tmp:
        cdir = Path(tmp) / "checkpoints"
        llm = ScriptedLLM([PLAN, REPORT, REPORT])
        agent = ResearchAgent(llm=llm, checkpoint_dir_=str(cdir))
        agent.toolbox.search_fn = ScriptedWeb(SOURCES)
        req = AgentRequest(task_id=new_task_id("task"), task_type=TaskType.RESEARCH,
                           text=QUESTION,
                           metadata={"checkpoint_dir": str(cdir)})
        if task == "unfounded":
            # Ask something the scripted sources cannot answer: the agent
            # must return a report with zero claims and honest uncertainty,
            # never a confident guess.
            empty = ScriptedWeb({})
            agent.toolbox.search_fn = empty
            req.text = ("What is the exact thrust-to-weight ratio of the "
                        "unannounced Nimbus-9 engine variant?")
        return show(agent.handle(req))


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Offline sample workflow for the Phase-6 research agent")
    ap.add_argument("--task", choices=["web", "rag", "unfounded"],
                    default="web")
    args = ap.parse_args()

    cp = run(args.task, BASE / "memory" / "research_agent_demo")
    if cp:
        print(f"\n{GREEN}Checkpoint saved at memory/research_agent_demo/{cp}.json"
              f" — sources and verdicts are preserved for audit.{END}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
