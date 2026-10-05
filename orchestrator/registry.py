"""
orchestrator/registry.py — the agent registry.

One authoritative place where routed capabilities are registered and looked
up. The orchestrator (and only the orchestrator) decides whether a lookup
miss means "no agent" — the registry itself just answers questions and never
invents a default agent.
"""
from __future__ import annotations

import threading
from typing import Iterator, Optional

from orchestrator.base import BaseAgent
from orchestrator.contracts import AgentRequest, TaskType


class AgentRegistry:
    """Thread-safe registry of routed capabilities."""

    def __init__(self) -> None:
        self._agents: dict[str, BaseAgent] = {}
        self._lock = threading.RLock()

    # ── registration ──────────────────────────────────────────────────────
    def register(self, agent: BaseAgent) -> BaseAgent:
        if not isinstance(agent, BaseAgent):
            raise TypeError(f"not an agent: {agent!r}")
        if not agent.name or not agent.name.strip():
            raise ValueError("agent.name must be non-empty")
        with self._lock:
            existing = self._agents.get(agent.name)
            if existing is not None and existing is not agent:
                raise ValueError(f"agent name already registered: {agent.name}")
            self._agents[agent.name] = agent
        return agent

    def unregister(self, name: str) -> bool:
        with self._lock:
            return self._agents.pop(name, None) is not None

    # ── lookup ────────────────────────────────────────────────────────────
    def get(self, name: str) -> Optional[BaseAgent]:
        with self._lock:
            return self._agents.get(name)

    def find(self, agent_or_name: "BaseAgent | str") -> Optional[BaseAgent]:
        if isinstance(agent_or_name, BaseAgent):
            return agent_or_name
        return self.get(str(agent_or_name))

    def by_task_type(self, task_type: TaskType) -> list[BaseAgent]:
        with self._lock:
            return [a for a in self._agents.values()
                    if task_type in a.task_types]

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._agents)

    def __len__(self) -> int:
        with self._lock:
            return len(self._agents)

    def __iter__(self) -> Iterator[BaseAgent]:
        with self._lock:
            return iter(list(self._agents.values()))

    def __contains__(self, name: object) -> bool:
        with self._lock:
            return name in self._agents

    def describe(self) -> list[dict]:
        """Registry manifest — for logs, dashboards, and router prompts."""
        with self._lock:
            return [
                {
                    "name": a.name,
                    "description": a.description,
                    "task_types": [t.value for t in a.task_types],
                }
                for a in self._agents.values()
            ]
