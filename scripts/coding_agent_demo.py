#!/usr/bin/env python3
"""
scripts/coding_agent_demo.py — Phase 5 sample workflow, fully offline.

Runs the coding agent end-to-end (REQUEST → INSPECT → PLAN → CHANGE →
TEST → VERIFY → REPORT) against a disposable sample repo with a scripted
LLM, so NO network, NO API key and NO model are needed:

    .venv/bin/python scripts/coding_agent_demo.py                # happy path
    .venv/bin/python scripts/coding_agent_demo.py --task fix     # fix loop
    .venv/bin/python scripts/coding_agent_demo.py --task delete  # approval flow
    .venv/bin/python scripts/coding_agent_demo.py --task rollback --task-id <id>

The rollback branch can undo a real agent task's changes from its
checkpoint, which is the documented rollback path from docs/CODING_AGENT.md.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from orchestrator.agents.coding_agent import CodingAgent, rollback_checkpoint
from orchestrator.contracts import AgentRequest, TaskType, new_task_id

GREEN, YELLOW, DIM, END = "\033[32m", "\033[33m", "\033[2m", "\033[0m"


def make_sample_repo(root: Path) -> Path:
    root = root / "weather_demo"
    (root / "weather").mkdir(parents=True)
    (root / "weather" / "__init__.py").write_text("", encoding="utf-8")
    (root / "weather" / "service.py").write_text(
        '"""Tiny weather helper the demo task lives on."""\n'
        "\n"
        "def heat_index(celsius, humidity):\n"
        '    """Rough feels-like temperature."""\n'
        "    return celsius + humidity / 100.0 * 5\n",
        encoding="utf-8")
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_service.py").write_text(
        "from weather.service import heat_index\n"
        "\n"
        "\n"
        "def test_heat_index():\n"
        "    assert heat_index(30, 50) == 32.5\n",
        encoding="utf-8")
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    return root


class ScriptedLLM:
    """Pops scripted answers: first call = plan, second = fix plan."""

    def __init__(self, answers: list[str]):
        self.answers = list(answers)

    def complete(self, prompt: str, *, system: str = ""):
        return self.answers.pop(0) if self.answers else None


PLAN_EDIT = json.dumps({
    "analysis": "heat_index should clamp sub-zero readings to 0 before use.",
    "steps": [{"action": "edit", "path": "weather/service.py",
               "old": "def heat_index(celsius, humidity):\n"
                      "    \"\"\"Rough feels-like temperature.\"\"\"\n"
                      "    return celsius + humidity / 100.0 * 5",
               "new": "def heat_index(celsius, humidity):\n"
                      "    \"\"\"Rough feels-like temperature; sub-zero reads as 0.\"\"\"\n"
                      "    return max(0, celsius) + humidity / 100.0 * 5",
               "why": "clamp negative temperatures"}],
    "tests_to_run": ["pytest", "-q"],
    "risks": ["changes behaviour for negative celsius only"],
})

PLAN_BROKEN = json.dumps({
    "analysis": "attempting min() instead of max() — tests will fail.",
    "steps": [{"action": "edit", "path": "weather/service.py",
               "old": "    return celsius + humidity / 100.0 * 5",
               "new": "    return min(0, celsius) + humidity / 100.0 * 5",
               "why": "demo of the failing-then-fixed loop"}],
    "tests_to_run": ["pytest", "-q"],
    "risks": ["breaks the existing test on purpose"],
})

PLAN_FIX = json.dumps({
    "analysis": "the previous step inverted the clamp — repair it.",
    "steps": [{"action": "edit", "path": "weather/service.py",
               "old": "    return min(0, celsius) + humidity / 100.0 * 5",
               "new": "    return max(0, celsius) + humidity / 100.0 * 5",
               "why": "correct clamp direction"}],
    "tests_to_run": ["pytest", "-q"],
    "risks": [],
})

PLAN_DELETE = json.dumps({
    "analysis": "sunsetting the pytest config — needs human approval (as designed).",
    "steps": [{"action": "delete", "path": "pytest.ini",
               "why": "demo of the approval gate"}],
    "tests_to_run": [],
    "risks": ["file deletion is always approval-gated"],
})


def show(result) -> str:
    print(f"\n{YELLOW}── agent report {'─' * 40}{END}")
    print(result.text)
    trace = result.data.get("stage_trace") or []
    print(f"{DIM}status={result.status.value} agent={result.agent}{END}")
    print(f"{DIM}stages: {' → '.join(s['stage'] for s in trace)}{END}")
    print(f"{DIM}checkpoint: {result.data.get('checkpoint')}  "
          f"(id={result.data.get('checkpoint_task')}){END}")
    return str(result.data.get("checkpoint_task") or "")


def run(repo: Path, plan: str, label: str) -> str:
    ag = CodingAgent(llm=ScriptedLLM([plan]))
    req = AgentRequest(task_id=new_task_id("task"), task_type=TaskType.CODING,
                       text=f"demo task: {label}",
                       metadata={"repo_root": str(repo)})
    return show(ag.handle(req))


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Offline sample workflow for the Phase-5 coding agent")
    ap.add_argument("--task", choices=["edit", "fix", "delete", "rollback"],
                    default="edit")
    ap.add_argument("--task-id", default="",
                    help="task id for --task rollback")
    args = ap.parse_args()

    if args.task == "rollback":
        if not args.task_id:
            print("pass --task-id <id> to roll a task back")
            return 2
        print(rollback_checkpoint(args.task_id))
        return 0

    with tempfile.TemporaryDirectory(prefix="coding_agent_demo_") as tmp:
        repo = make_sample_repo(Path(tmp))
        print(f"{GREEN}sample repo: {repo}{END}")
        if args.task == "edit":
            run(repo, PLAN_EDIT, "clamp sub-zero heat index")
        elif args.task == "fix":
            # first answer breaks the tests, second one repairs them —
            # exercises the failing→fix→re-test loop end to end.
            ag = CodingAgent(llm=ScriptedLLM([PLAN_BROKEN, PLAN_FIX]))
            req = AgentRequest(task_id=new_task_id("task"),
                               task_type=TaskType.CODING,
                               text="demo task: fix loop",
                               metadata={"repo_root": str(repo)})
            show(ag.handle(req))
        else:                                    # delete → approval flow
            run(repo, PLAN_DELETE, "remove pytest.ini")

        print(f"\n{GREEN}Checkpoints live under memory/coding_agent/ (JARVIS_HOME-aware). "
              f"Roll a run back with:{END}")
        print(f"  .venv/bin/python scripts/coding_agent_demo.py "
              f"--task rollback --task-id <task_id>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
