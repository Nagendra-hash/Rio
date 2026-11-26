"""dashboard/hermes_events.py — Phase 7 bridge event journal + live push.

One event stream for the Hermes pipeline visualisation:

    message_received → processing → agent_selected →
    response_generated → response_sent

Every event is written to memory/hermes_events.jsonl (append-only, durable —
the dashboard's GET /api/rio/hermes/events reads the tail of exactly this
file) and, when a DashboardServer is available, broadcast on the EXISTING
WebSocket ({type: "hermes_event"}) so the /hermes page updates live. No new
transport: same /ws clients, same broadcast() the chat log already uses.

Events only ever describe things that actually happened: the intake endpoint
emits received/processing/agent_selected from the request it just handled,
and main.py's reply arm emits response_generated/response_sent from the real
delivery result. A stage that never fired simply has no event — nothing here
is synthesised.
"""
from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from memory.memory_manager import get_base_dir

EVENTS_FILE = get_base_dir() / "memory" / "hermes_events.jsonl"

# The five pipeline stages the UI visualises, in flow order.
STAGES = ("message_received", "processing", "agent_selected",
          "response_generated", "response_sent")

_lock = threading.Lock()


def _preview(text: str, limit: int = 160) -> str:
    return (text or "").strip().replace("\n", " ")[:limit]


def emit(server: Any, stage: str, **data: Any) -> dict:
    """Journal one pipeline event + best-effort live broadcast.

    Never raises — an event write must not break the message path it is
    describing. Returns the event dict (possibly without broadcast).
    """
    if stage not in STAGES:
        stage = str(stage)
    event = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "stage": stage,
    }
    for key in ("channel", "platform", "chat", "text", "file",
                "agent", "mode", "ok", "detail", "chars"):
        if key in data and data[key] is not None:
            event[key] = data[key]
    if "text" in event:
        event["text"] = _preview(str(event["text"]))
    try:
        EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False)
        with _lock:
            with EVENTS_FILE.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        pass
    if server is not None:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(server.broadcast(
                {"type": "hermes_event", "event": event}))
        except Exception:
            pass        # running in a worker thread — journal still holds it
    return event


def read_events(limit: int = 50) -> list[dict]:
    """Newest-first tail of the event journal (bounded read)."""
    try:
        if not EVENTS_FILE.exists():
            return []
        max_bytes = 512 * 1024
        with EVENTS_FILE.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            blob = fh.read().decode("utf-8", errors="replace")
        lines = blob.splitlines()
        if size > max_bytes and lines:
            lines = lines[1:]          # drop possibly-truncated first line
        out: list[dict] = []
        for line in lines[-limit * 2:]:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict) and obj.get("stage"):
                out.append(obj)
        return out[-limit:][::-1]
    except Exception:
        return []
