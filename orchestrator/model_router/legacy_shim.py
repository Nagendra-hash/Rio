"""
orchestrator/model_router/legacy_shim.py — Compatibility layer for core/llm_router.py.

This module provides a drop-in replacement for the legacy core/llm_router module.
It routes all calls through the new unified ModelRouter, preserving the same
API surface so existing callers don't need to change.

Usage:
    # In any file that currently does:
    #   from core.llm_router import generate, local_generate, install_router, router_status
    # Replace with:
    #   from orchestrator.model_router.legacy_shim import generate, local_generate, install_router, router_status
"""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("jarvis.model_router.legacy")

# Global ModelRouter instance (lazy initialization)
_router: Optional[Any] = None
_router_lock: Any = None


def _get_router():
    """Get or create the global ModelRouter instance."""
    global _router, _router_lock
    if _router is not None:
        return _router

    import threading
    if _router_lock is None:
        _router_lock = threading.Lock()

    with _router_lock:
        if _router is not None:
            return _router

        from orchestrator.model_router.router import ModelRouter
        from orchestrator.model_router.usage import UsageJournal
        from orchestrator.model_router.budget import BudgetManager

        # Build the router with default configuration
        _router = ModelRouter(
            usage=UsageJournal(),
            budget=BudgetManager(),
        )
        log.info("[LegacyShim] ModelRouter initialized for legacy compatibility")
        return _router


# ── Public API (matches core/llm_router exports) ───────────────────────────────


def install_router(verbose: bool = True) -> bool:
    """Compatibility: initialize the router. Always returns True now."""
    try:
        router = _get_router()
        if verbose:
            status = router.status()
            parts = []
            for model in status.get("models", []):
                if model.get("cost_tier") == 0:
                    parts.append(f"Local:{model['name']}")
            if parts:
                log.info(f"[LLM] Router active — {' → '.join(parts)}")
            else:
                log.info("[LLM] Router active (no local models configured)")
        return True
    except Exception as e:
        log.error(f"[LLM] Router initialization failed: {e}")
        return False


def router_status() -> dict:
    """Compatibility: return router status for dashboards."""
    try:
        router = _get_router()
        status = router.status()

        # Transform to legacy format
        legacy_status = {
            "mode": status.get("mode", "smart"),
            "order": [],
            "ollama": {
                "enabled": any(
                    m["provider"] == "ollama" and m.get("cost_tier") == 0
                    for m in status.get("models", [])
                ),
                "alive": False,
                "reason": "check not implemented in shim",
            },
            "llm_local": {
                "enabled": any(
                    m["provider"] == "local" and m.get("cost_tier") == 0
                    for m in status.get("models", [])
                ),
                "alive": False,
                "reason": "check not implemented in shim",
            },
            "freellmapi": {
                "enabled": any(
                    m["provider"] == "freellm" for m in status.get("models", [])
                ),
            },
            "openrouter": {
                "keys": len([
                    m for m in status.get("models", [])
                    if m["provider"] == "openrouter"
                ]),
            },
            "nvidia": {
                "enabled": any(
                    m["provider"] == "nvidia" for m in status.get("models", [])
                ),
            },
            "deepseek": {
                "enabled": any(
                    m["provider"] == "deepseek" for m in status.get("models", [])
                ),
            },
            "gemini": {
                "keys": 1 if any(
                    m["provider"] == "gemini" for m in status.get("models", [])
                ) else 0,
            },
        }

        # Add budget info
        if "budget" in status:
            legacy_status["budget"] = status["budget"]

        return legacy_status
    except Exception as e:
        log.error(f"[LegacyShim] router_status failed: {e}")
        return {"mode": "smart", "order": [], "error": str(e)}


def local_generate(prompt: str, system: str = "") -> tuple[str, str]:
    """Compatibility: local_generate(prompt, system) -> (text, engine_name).

    Used by core/gemini.py's LOCAL rung and other callers.
    """
    try:
        router = _get_router()
        result = router.legacy_generate(prompt, system, task="general")
        return result, "ModelRouter"
    except Exception as e:
        log.error(f"[LegacyShim] local_generate failed: {e}")
        raise RuntimeError(f"no local model available: {e}")


def generate(
    self,  # Models instance from google-genai
    *,
    model=None,
    contents=None,
    **kwargs,
):
    """Compatibility: patched generate_content replacement.

    This mimics the signature of genai.Models.generate_content but routes
    through the new ModelRouter. The legacy router patched the method at
    class level; this function can be used the same way.
    """
    router = _get_router()

    # Extract system prompt from kwargs (same logic as legacy)
    cfg = kwargs.get("config") or {}
    if isinstance(cfg, dict):
        system = cfg.get("system_instruction", "") or ""
    else:
        system = getattr(cfg, "system_instruction", "") or ""

    # Convert contents to prompt string (use local implementation)
    try:
        message = _contents_to_message(contents)
    except Exception:
        message = {"role": "user", "content": str(contents)}

    prompt = message.get("content", "")
    if isinstance(prompt, list):
        prompt = " ".join(p.get("text", "") for p in prompt if isinstance(p, dict))

    # Classify task (use local implementation)
    try:
        task = _classify_task(message)
    except Exception:
        task = "general"

    # Route through new ModelRouter
    try:
        result = router.legacy_generate(prompt, system, task)
        # Return a response object compatible with genai response surface
        return _ORResponse(result)
    except Exception as e:
        log.error(f"[LegacyShim] generate failed: {e}")
        raise


# ── Additional helper functions that legacy code might import ──────────────────

import base64
import io
from pathlib import Path
import os
import json


def _load_full_config() -> dict:
    """Legacy helper: load config from api_keys.json."""
    _home = os.environ.get("JARVIS_HOME", "").strip()
    base = Path(_home).expanduser().resolve() if _home else Path(__file__).resolve().parents[2]
    config_path = base / "config" / "api_keys.json"
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _get_mode(full_cfg: dict | None = None) -> str:
    """Legacy helper: get router mode."""
    cfg = full_cfg if full_cfg is not None else _load_full_config()
    raw = os.environ.get("JARVIS_ROUTER_MODE") or (cfg.get("router_mode") or "smart")
    m = str(raw).strip().lower()
    return m if m in ("local", "free", "smart", "gemini") else "smart"


def _classify_task(message: dict) -> str:
    """Legacy helper: classify task from message (inline implementation)."""
    content = message.get("content")
    if isinstance(content, list):
        has_image = any(
            isinstance(p, dict) and p.get("type") == "image_url" for p in content
        )
        if has_image:
            return "vision"
        text = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    else:
        text = str(content or "")
    low = text.lower()
    # Task hints (copied from legacy core/llm_router.py)
    _TASK_HINTS = [
        (
            "code",
            (
                "code",
                "function",
                "script",
                "debug",
                "traceback",
                "error:",
                "regex",
                "compile",
                "class ",
                "def ",
                "import ",
                "sql",
                "api call",
                "refactor",
                "```",
                "bug",
                "python",
                "javascript",
            ),
        ),
        (
            "reason",
            (
                "solve",
                "calculate",
                "math",
                "equation",
                "logic",
                "puzzle",
                "step by step",
                "prove",
                "how many",
                "probability",
                "reason",
            ),
        ),
        (
            "summarize",
            (
                "summarize",
                "summary",
                "tl;dr",
                "headlines",
                "news",
                "brief",
                "condense",
                "key points",
                "recap",
            ),
        ),
    ]
    for task, words in _TASK_HINTS:
        if any(w in low for w in words):
            return task
    return "general"


def _pil_to_data_url(img) -> str:
    """Convert PIL Image to data URL."""
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=82)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _contents_to_message(contents) -> dict:
    """Build one user message. Accepts: str | PIL.Image | genai Part-like |
    list of any of those — the shapes the call sites actually pass."""
    text_parts: list[str] = []
    image_parts: list[str] = []  # data URLs

    def _walk(item):
        if item is None:
            return
        if isinstance(item, str):
            text_parts.append(item)
            return
        # Image carriers FIRST — genai Part/Blob are pydantic models, which are
        # iterable, so the generic iter branch below would otherwise consume
        # them and str() the raw bytes into the text (the model then "describes"
        # binary garbage instead of seeing the picture).
        inline = getattr(item, "inline_data", None)
        if inline is not None and getattr(inline, "data", None):
            mime = getattr(inline, "mime_type", "image/png") or "image/png"
            b64 = base64.b64encode(bytes(inline.data)).decode()
            image_parts.append(f"data:{mime};base64,{b64}")
            return
        if type(item).__name__ == "Image":  # PIL Image (duck-typed)
            image_parts.append(_pil_to_data_url(item))
            return
        if hasattr(item, "parts") and item.parts:  # genai Content
            for sub in item.parts:
                _walk(sub)
            return
        if isinstance(item, (bytes, bytearray)):  # raw bytes: no mime known
            return
        if hasattr(item, "__iter__"):
            for sub in item:
                _walk(sub)
            return
        text_parts.append(str(item))

    _walk(contents)

    if image_parts:
        content: list = [
            {"type": "text", "text": "\n".join(text_parts) or "Describe this."}
        ]
        content += [{"type": "image_url", "image_url": {"url": u}} for u in image_parts]
        return {"role": "user", "content": content}
    return {"role": "user", "content": "\n".join(text_parts)}


class _ORResponse:
    """Mimics the slice of a genai response that call sites touch: `.text` and
    `candidates[0].content.parts[i].text` (actions/web_search.py iterates the
    latter to assemble the answer text)."""

    class _Part:
        def __init__(self, text: str):
            self.text = text

    class _Content:
        def __init__(self, text: str):
            self.parts = [_ORResponse._Part(text)]

    class _Candidate:
        def __init__(self, text: str):
            self.content = _ORResponse._Content(text)

    def __init__(self, text: str):
        self.text = text
        self.candidates = [_ORResponse._Candidate(text)]

    def __str__(self):
        return self.text


# ── Module-level attributes that legacy code might access ───────────────────────

# The legacy module had these as module globals
_local_rung_order = ["ollama", "llm_local"]
_cloud_rung_order = ["nvidia", "freellm", "openrouter", "deepseek", "gemini"]

__all__ = [
    "install_router",
    "router_status",
    "local_generate",
    "generate",
    "_load_full_config",
    "_get_mode",
    "_classify_task",
    "_contents_to_message",
    "_ORResponse",
    "_local_rung_order",
    "_cloud_rung_order",
]