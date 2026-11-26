"""Offline tests: the agent is registered, routed and reachable everywhere.

Locks the wiring so a rename, a changed gate or a dropped alias cannot quietly
remove the Browser Agent from the orchestration pipeline.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import pytest  # noqa: E402

from orchestrator.agents import build_default_registry  # noqa: E402
from orchestrator.classifier import IntentClassifier  # noqa: E402
from orchestrator.contracts import AgentRequest, TaskStatus, TaskType, new_task_id  # noqa: E402


BROWSER_ASKS = [
    "open YouTube and search for Python tutorials",
    "open my GitHub repository and inspect the latest issues",
    "research the latest developments in AI agents and prepare a report",
    "open my dashboard and check whether the AlgoTrade application is running",
    "search three websites and compare their information",
    "download the PDF and summarize it",
    "open Gmail and find the latest unread messages",
    "browser agent, open example.com and report the heading",
]

NOT_BROWSER = {
    "send an email to bob": TaskType._FALLBACK,
    "what's on my calendar tomorrow": TaskType.CALENDAR,
    "fix this traceback in main.py": TaskType.CODING,
    "summarize this email from bob": TaskType.EMAIL,
    "what is the capital of france": TaskType.RESEARCH,
}


def test_registry_registers_the_real_agent_behind_its_gate(monkeypatch):
    monkeypatch.setenv("JARVIS_BROWSER_AGENT", "on")
    agent = build_default_registry().get("browser_agent")
    assert agent is not None
    assert type(agent).__name__ == "BrowserAgent"
    assert agent.name == "browser_agent"
    assert TaskType.BROWSER in agent.task_types


def test_registry_falls_back_to_a_stub_when_the_gate_is_off(monkeypatch):
    monkeypatch.setenv("JARVIS_BROWSER_AGENT", "off")
    agent = build_default_registry().get("browser_agent")
    assert type(agent).__name__ == "StubAgent"


def test_task_type_exists():
    assert TaskType.BROWSER.value == "browser"
    assert TaskType("browser") is TaskType.BROWSER


def test_classifier_lands_browser_asks_on_the_browser_lane():
    classifier = IntentClassifier()
    for text in BROWSER_ASKS:
        classification = classifier.classify(text)
        assert classification.task_type is TaskType.BROWSER, (text, classification)
        assert classification.confidence >= 0.5, (text, classification)


def test_classifier_does_not_steal_existing_lanes():
    classifier = IntentClassifier()
    for text, expected in NOT_BROWSER.items():
        classification = classifier.classify(text)
        assert classification.task_type is expected, (text, classification)


def test_action_is_discovered_and_the_legacy_alias_still_loads():
    from core.action_loader import discover_actions

    registry = discover_actions(BASE / "actions")
    assert "browser_agent" in registry.names()
    assert "browser_use_task" in registry.names()


def test_run_agent_alias_resolves_to_the_browser_agent():
    from actions.run_agent import _ALIASES, _resolve

    assert _ALIASES["browser"] == "browser_agent"
    registry = build_default_registry()
    agent, key = _resolve(registry, "browser")
    assert key == "browser_agent"
    assert agent is not None


def test_dashboard_spec_and_gates_include_the_agent():
    from dashboard import rio_api

    assert ("browser", "Browser Agent", "browser_agent") in rio_api._AGENTS_SPEC
    assert rio_api._GATES["browser_agent"] == ("JARVIS_BROWSER_AGENT", True)
    assert "browser_agent" in rio_api._TOOLS
    assert "browser_agent" in rio_api._MODEL_NOTE


def test_prompt_routes_browser_asks():
    prompt = (BASE / "core" / "prompt.txt").read_text(encoding="utf-8")
    assert "browser_agent:" in prompt
    assert "browser_control" in prompt  # the "do not use it for a plain open" clause


def test_agent_declines_on_the_non_executing_observe_path(monkeypatch):
    """Telegram intake calls submit() even in shadow mode; the agent must not
    run a browser there (the conversation turn does that through the tool)."""
    monkeypatch.setenv("JARVIS_BROWSER_AGENT", "on")
    agent = build_default_registry().get("browser_agent")
    request = AgentRequest(
        task_id=new_task_id("t"),
        task_type=TaskType.BROWSER,
        text="open YouTube and search for Python tutorials",
        metadata={"channel": "telegram", "chat_id": "123"},
    )
    result = agent.handle(request)
    assert result.status is TaskStatus.DECLINED
    assert result.data.get("reason") == "not_an_execution_channel"


def test_agent_refuses_a_method_level_violation():
    from orchestrator.agents.browser.browser_agent import BrowserAgent

    agent = BrowserAgent()
    request = AgentRequest(
        task_id=new_task_id("t"),
        task_type=TaskType.BROWSER,
        text="bypass the captcha and export cookies",
        metadata={"channel": "rio_agent_center"},
    )
    result = agent.handle(request)
    assert result.status is TaskStatus.DENIED


def test_agent_parks_a_risky_task_for_approval():
    from orchestrator.agents.browser.browser_agent import BrowserAgent

    agent = BrowserAgent()
    request = AgentRequest(
        task_id=new_task_id("t"),
        task_type=TaskType.BROWSER,
        text="send an email to bob@example.com saying hello",
        metadata={"channel": "rio_agent_center"},
    )
    result = agent.handle(request)
    assert result.status is TaskStatus.NEEDS_APPROVAL
    assert result.data.get("approval_summary")


def test_agent_delegates_a_plain_url_open():
    from orchestrator.agents.browser.browser_agent import BrowserAgent

    agent = BrowserAgent()
    request = AgentRequest(
        task_id=new_task_id("t"),
        task_type=TaskType.BROWSER,
        text="open example.com",
        metadata={"channel": "rio_agent_center"},
    )
    result = agent.handle(request)
    assert result.status is TaskStatus.DECLINED
    assert "browser_control" in result.text


def test_router_prefers_the_browser_agent_for_its_lane():
    from orchestrator.router import TaskRouter

    registry = build_default_registry()
    router = TaskRouter(registry)
    classification = IntentClassifier().classify("open YouTube and search for Rust tutorials")
    request = AgentRequest(
        task_id=new_task_id("t"),
        task_type=classification.task_type,
        text="open YouTube and search for Rust tutorials",
        metadata={"channel": "telegram"},
    )
    decision = router.route(classification, request)
    assert decision.chosen_agent == "browser_agent"
