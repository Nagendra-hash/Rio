"""
orchestrator/agents/replica/llm.py — the model layer for the replica agents.

Deliberately the same two rungs the coding agent uses:
  1. the house Model Router (ModelTask.SIMPLE_CODING) — NVIDIA / Gemini;
  2. `core/gemini`'s SMART ladder behind it.

A backend may be injected (``llm=...``) so tests and the offline demo run
with a scripted model and no network. Every helper returns None on failure
and never raises — a missing model degrades a stage to its deterministic
path, it never fails the run.
"""

from __future__ import annotations

import json
from typing import Any, Optional


class LLM:
    """Protocol: text(prompt, system="") -> str | None. Injectable."""

    def text(self, prompt: str, *, system: str = "") -> Optional[str]:
        raise NotImplementedError


class HouseLLM(LLM):
    """Model Router first, core/gemini SMART behind it."""

    def __init__(self) -> None:
        self._router: Any = None

    def text(self, prompt: str, *, system: str = "") -> Optional[str]:
        try:
            from orchestrator.model_router import ModelRouter, ModelTask, RouteOptions

            if self._router is None:
                self._router = ModelRouter()
            res = self._router.route(
                prompt,
                RouteOptions(
                    task=ModelTask.SIMPLE_CODING,
                    system=system,
                    timeout_s=90,
                    deadline_s=120,
                ),
            )
            if res.ok and res.text:
                return res.text
        except Exception:
            pass
        try:
            from core import gemini

            return (
                gemini.text(
                    f"{system}\n\n{prompt}".strip(),
                    tier=gemini.SMART,
                    timeout_ms=90_000,
                )
                or None
            )
        except Exception:
            return None


def extract_json(raw: str | None) -> dict | None:
    """Tolerant JSON-object extraction (fences + prose tolerated)."""
    if not raw:
        return None
    raw = raw.strip()
    if "{" in raw and "}" in raw:
        raw = raw[raw.find("{") : raw.rfind("}") + 1]
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None
