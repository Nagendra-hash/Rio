"""
orchestrator/agents/youtube/llm.py — the model layer for the youtube agents.

The same two rungs the rest of JARVIS uses:
  1. the house Model Router (``ModelTask.SIMPLE_CODING``) — DeepSeek / NVIDIA /
     Gemini, whichever the router chains first;
  2. ``core/gemini``'s SMART ladder behind it.

A backend may be injected (``llm=...``) so tests and an offline demo run with a
scripted model and no network. Every helper returns None on failure and never
raises — a missing model degrades a stage to its deterministic path, it never
fails the run and never fabricates.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional


class LLM:
    """Protocol: text(prompt, system="") -> str | None. Injectable."""

    name = "llm"

    def text(self, prompt: str, *, system: str = "") -> Optional[str]:
        raise NotImplementedError


class HouseLLM(LLM):
    """Model Router first, core/gemini SMART behind it."""

    name = "model-router"

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
    data = _loads(raw, "{", "}")
    return data if isinstance(data, dict) else None


def extract_json_array(raw: str | None) -> list | None:
    """Tolerant JSON-array extraction (fences + prose tolerated)."""
    data = _loads(raw, "[", "]")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("topics", "plan", "items", "fixes"):
            if isinstance(data.get(key), list):
                return data[key]
    return None


def _loads(raw: str | None, open_ch: str, close_ch: str):
    if not raw:
        return None
    text = str(raw).strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    if open_ch in text and close_ch in text:
        inner = text[text.find(open_ch) : text.rfind(close_ch) + 1]
        try:
            return json.loads(inner)
        except Exception:
            return None
    return None
