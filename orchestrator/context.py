"""
orchestrator/context.py — conversation / context handoff.

Each conversation session keeps a bounded, chronological turn history. When a
task is routed, the orchestrator attaches the recent history to the
AgentRequest, so any agent (today a stub, tomorrow a real one) sees the same
conversation view the user has. Handoff is by value: agents receive copies
and cannot mutate each other's view of the conversation.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from orchestrator.contracts import AgentRequest

DEFAULT_MAX_TURNS = 20
DEFAULT_SESSION_TTL_S = 6 * 60 * 60  # sessions outlive a chat, not a week


@dataclass
class SessionContext:
    session_id: str
    channel: str = ""
    chat_id: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    history: list[tuple[str, str]] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def add_turn(self, role: str, text: str, max_turns: int) -> None:
        text = (text or "").strip()
        if not text:
            return
        self.history.append((role, text))
        if len(self.history) > max_turns * 2:  # user+assistant pairs
            del self.history[:2]

    def snapshot(self, max_turns: int) -> list[tuple[str, str]]:
        """A copy of the last max_turns user/assistant pairs."""
        return list(self.history[-max_turns * 2 :])


class ContextStore:
    """Thread-safe, bounded in-memory session store (stdlib-only)."""

    def __init__(
        self,
        max_turns: int = DEFAULT_MAX_TURNS,
        session_ttl_s: float = DEFAULT_SESSION_TTL_S,
    ) -> None:
        self._sessions: dict[str, SessionContext] = {}
        self._lock = threading.RLock()
        self._max_turns = max(1, int(max_turns))
        self._ttl = max(60.0, float(session_ttl_s))

    # ── lifecycle ─────────────────────────────────────────────────────────
    def _prune(self, now: float) -> None:
        stale = [
            sid for sid, s in self._sessions.items() if now - s.updated_at > self._ttl
        ]
        for sid in stale:
            del self._sessions[sid]

    def get_or_create(
        self, session_id: str, channel: str = "", chat_id: str = ""
    ) -> SessionContext:
        now = time.time()
        with self._lock:
            self._prune(now)
            session = self._sessions.get(session_id)
            if session is None:
                session = SessionContext(
                    session_id=session_id, channel=channel, chat_id=chat_id
                )
                self._sessions[session_id] = session
            session.updated_at = now
            return session

    # ── recording ─────────────────────────────────────────────────────────
    def record_user(
        self, session_id: str, text: str, channel: str = "", chat_id: str = ""
    ) -> SessionContext:
        session = self.get_or_create(session_id, channel, chat_id)
        session.add_turn("user", text, self._max_turns)
        return session

    def record_assistant(self, session_id: str, text: str) -> None:
        session = self._sessions.get(session_id)
        if session is not None:
            session.add_turn("assistant", text, self._max_turns)

    # ── handoff ───────────────────────────────────────────────────────────
    def attach(self, request: AgentRequest, session_id: str) -> AgentRequest:
        """Return the request with the session's recent history attached.

        The orchestrator calls this between routing and dispatch — the
        "conversation/context handoff" step. History is copied, so agents
        can never corrupt the canonical session record.
        """
        session = self._sessions.get(session_id)
        history = session.snapshot(self._max_turns) if session else []
        extra = {}
        if session is not None:
            extra = {
                "session_id": session_id,
                "channel": session.channel,
                "chat_id": session.chat_id,
            }
        return request.with_context(history, extra)

    def clear(self, session_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)
