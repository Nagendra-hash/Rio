"""
orchestrator/integration.py — zero-risk wiring into the existing system.

The orchestrator is OBSERVE-ONLY by default ("shadow mode"): classify +
journal each inbound turn, then return notrouted so the existing Gemini/Hermes
path proceeds exactly as today. Nothing about the existing flow can change
because the only call sites are inside a try/except that can never raise
outwards, and the default mode never intercepts a turn.

Modes (env JARVIS_ORCHESTRATOR):
    "shadow" (default) → observe + journal only; existing path always runs.
    "on"               → orchestrator answers tasks it routes; unknown/failed
                         ones still defer to the existing path (fallback).
    "off"              → orchestrator is bypassed entirely.

Usage (e.g. in dashboard/server.py where messages enter the command queue):

    from orchestrator import integration
    verdict = integration.observe_user_turn(text, channel=..., chat_id=...)
    # shadow: verdict["mode"] == "shadow" → enqueue as before (unchanged)
    # on:     verdict["mode"] == "routed" → dispatch via orchestrator instead
"""

from __future__ import annotations

import os
import threading
from typing import Any, Optional

from orchestrator.agents import build_default_registry
from orchestrator.contracts import TaskStatus
from orchestrator.journal import OrchestrationJournal
from orchestrator.orchestrator import JarvisOrchestrator

_LOCK = threading.Lock()
_ORCH: Optional[JarvisOrchestrator] = None


def _mode() -> str:
    raw = (os.environ.get("JARVIS_ORCHESTRATOR") or "shadow").strip().lower()
    return raw if raw in {"on", "shadow", "off"} else "shadow"


def _shared() -> JarvisOrchestrator:
    """One process-wide orchestrator; safe under concurrent intakes."""
    global _ORCH
    with _LOCK:
        if _ORCH is None:
            journal = OrchestrationJournal(
                enabled=os.environ.get("JARVIS_ORCHESTRATOR_JOURNAL", "1")
                .strip()
                .lower()
                not in {"0", "false", "no"}
            )
            _ORCH = JarvisOrchestrator(
                registry=build_default_registry(),
                journal=journal,
            )
        return _ORCH


def shared_orchestrator() -> JarvisOrchestrator:
    return _shared()


def reset_shared() -> None:
    """Test hook — drop the process-wide singleton."""
    global _ORCH
    with _LOCK:
        _ORCH = None


def observe_user_turn(
    text: str,
    *,
    session_id: str = "default",
    channel: str = "",
    chat_id: str = "",
    attachments: Optional[list[str]] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Classify one inbound turn and decide who answers it.

    Never raises. Returns a verdict dict:
      {"mode": "shadow"|"routed"|"off", "result": AgentResult|None,
       "task_id": str}
    Callers:
      * shadow → enqueue the turn for the existing assistant (unchanged),
        optionally log verdict["result"].data["task_type"].
      * routed → hand the turn to the orchestrator's result instead.
    """
    try:
        mode = _mode()
        if mode == "off":
            return {"mode": "off", "result": None, "task_id": ""}

        orch = _shared()
        result = orch.submit(
            text,
            session_id=session_id,
            channel=channel,
            chat_id=chat_id,
            attachments=attachments,
            metadata=metadata,
        )
        if mode == "shadow":
            return {
                "mode": "shadow",
                "result": result,
                "task_id": result.task_id if result else "",
            }

        # Active mode: was the turn answered by a routed agent?
        routed = (
            result is not None
            and not result.data.get("fallback")
            and result.status
            in {TaskStatus.COMPLETED, TaskStatus.NEEDS_APPROVAL, TaskStatus.NEEDS_INPUT}
        )
        return {
            "mode": "routed" if routed else "fallback",
            "result": result,
            "task_id": result.task_id if result else "",
        }
    except Exception as exc:  # never break the intake path
        try:
            from orchestrator.journal import get_logger

            get_logger().warning("orchestrator intake failed: %s", exc)
        except Exception:
            pass
        return {"mode": "fallback", "result": None, "task_id": ""}


def classify_only(text: str) -> dict[str, Any]:
    """Exposure for tests/dashboard: classify without routing."""
    try:
        result = _shared().submit(text, classify_only=True)
        return {
            "task_id": result.task_id,
            "task_type": result.text,
            "confidence": result.data.get("classification", {}).get("confidence", 0.0),
            "reason": result.data.get("classification", {}).get("reason", ""),
        }
    except Exception as exc:
        return {
            "task_id": "",
            "task_type": "fallback",
            "confidence": 0.0,
            "reason": f"error: {exc}",
        }
