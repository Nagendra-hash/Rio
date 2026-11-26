"""
core/voice_session.py — Voice Session Context Manager
Maintains conversation context across voice commands for follow-ups, corrections, and chaining.
"""

from __future__ import annotations

import time
import logging
from dataclasses import dataclass, field
from typing import Any
from core.voice_router import VoiceIntent
from collections import deque
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


class ConfirmationState(Enum):
    """State of a pending confirmation."""
    NONE = "none"
    PENDING = "pending"
    CONFIRMED = "confirmed"
    DENIED = "denied"
    MODIFIED = "modified"


@dataclass
class VoiceTurn:
    """A single voice interaction turn."""
    timestamp: float
    transcript: str
    intent: str
    entities: dict[str, Any]
    action: str | None
    params: dict[str, Any]
    result: dict[str, Any] | None = None
    success: bool = False
    error: str | None = None


@dataclass
class PendingConfirmation:
    """A pending confirmation request."""
    prompt: str
    original_intent: VoiceIntent
    state: ConfirmationState = ConfirmationState.PENDING
    modified_params: dict[str, Any] | None = None


@dataclass
class VoiceSession:
    """Manages voice conversation context."""

    # Session metadata
    session_id: str
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)

    # Conversation history (last 20 turns)
    history: deque[VoiceTurn] = field(default_factory=lambda: deque(maxlen=20))

    # Context for follow-ups
    last_intent: str | None = None
    last_entities: dict[str, Any] = field(default_factory=dict)
    last_action: str | None = None
    last_params: dict[str, Any] = field(default_factory=dict)
    last_result: dict[str, Any] | None = None

    # Pending confirmation
    pending_confirmation: PendingConfirmation | None = None

    # User preferences (learned)
    preferences: dict[str, Any] = field(default_factory=dict)

    # Active macro execution
    active_macro: str | None = None
    macro_step: int = 0

    def add_turn(self, turn: VoiceTurn) -> None:
        """Add a turn to history and update context."""
        self.history.append(turn)
        self.last_activity = time.time()

        if turn.success:
            self.last_intent = turn.intent
            self.last_entities = turn.entities
            self.last_action = turn.action
            self.last_params = turn.params
            self.last_result = turn.result

    def get_context_for_followup(self) -> dict[str, Any]:
        """Get context for follow-up commands like 'the second one', 'again', etc."""
        return {
            "last_intent": self.last_intent,
            "last_entities": self.last_entities,
            "last_action": self.last_action,
            "last_params": self.last_params,
            "last_result": self.last_result,
            "history": list(self.history)[-5:]  # Last 5 turns
        }

    def resolve_followup(self, text: str, base_params: dict) -> dict:
        """Resolve follow-up references in parameters."""
        text_lower = text.lower()
        resolved = base_params.copy()

        # "again" / "repeat" -> use last params
        if any(w in text_lower for w in ["again", "repeat", "once more"]):
            resolved.update(self.last_params)
            logger.info(f"Followup 'again': using last params {self.last_params}")

        # "the first/second/third one" -> index into last result list
        ordinal_match = re.search(r"(first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th)", text_lower)
        if ordinal_match and self.last_result:
            ordinal_map = {"first": 0, "1st": 0, "second": 1, "2nd": 1,
                          "third": 2, "3rd": 2, "fourth": 3, "4th": 3,
                          "fifth": 4, "5th": 4}
            idx = ordinal_map.get(ordinal_match.group(1), 0)
            if isinstance(self.last_result, dict) and "results" in self.last_result:
                results = self.last_result["results"]
                if isinstance(results, list) and idx < len(results):
                    resolved["selected_index"] = idx
                    resolved["selected_item"] = results[idx]
                    # Persist in session for chained followups
                    self.last_params["selected_index"] = idx
                    logger.info(f"Followup ordinal: selected index {idx}")

        # "the other one" / "different one" -> cycle through results
        if any(p in text_lower for p in ["other", "different", "another"]):
            if isinstance(self.last_result, dict) and "results" in self.last_result:
                results = self.last_result["results"]
                if isinstance(results, list) and len(results) > 1:
                    # Use session's last selected_index if not in base_params
                    current_idx = resolved.get("selected_index", self.last_params.get("selected_index", -1))
                    next_idx = (current_idx + 1) % len(results)
                    resolved["selected_index"] = next_idx
                    resolved["selected_item"] = results[next_idx]
                    # Persist in session for chained followups
                    self.last_params["selected_index"] = next_idx
                    logger.info(f"Followup 'other': selected index {next_idx}")

        # "more like this" / "similar" -> use last query as base
        if any(p in text_lower for p in ["similar", "like this", "more like"]):
            if "query" in self.last_params:
                resolved["query"] = self.last_params["query"]
                logger.info(f"Followup 'similar': using last query")

        return resolved

    def request_confirmation(self, prompt: str, intent: VoiceIntent) -> None:
        """Request user confirmation for a sensitive action."""
        self.pending_confirmation = PendingConfirmation(
            prompt=prompt,
            original_intent=intent,
            state=ConfirmationState.PENDING
        )

    def confirm(self, confirmed: bool, modified_params: dict | None = None) -> VoiceIntent | None:
        """Process confirmation response."""
        if not self.pending_confirmation:
            return None

        if confirmed:
            self.pending_confirmation.state = ConfirmationState.CONFIRMED
            intent = self.pending_confirmation.original_intent
            if modified_params:
                intent.params.update(modified_params)
                self.pending_confirmation.state = ConfirmationState.MODIFIED
            self.pending_confirmation = None
            return intent
        else:
            self.pending_confirmation.state = ConfirmationState.DENIED
            self.pending_confirmation = None
            return None

    def start_macro(self, macro_name: str) -> None:
        """Start executing a macro."""
        self.active_macro = macro_name
        self.macro_step = 0

    def next_macro_step(self) -> int:
        """Advance macro step."""
        self.macro_step += 1
        return self.macro_step

    def end_macro(self) -> None:
        """End macro execution."""
        self.active_macro = None
        self.macro_step = 0

    def learn_preference(self, key: str, value: Any) -> None:
        """Learn a user preference from interactions."""
        self.preferences[key] = value

    def get_preference(self, key: str, default: Any = None) -> Any:
        """Get a learned preference."""
        return self.preferences.get(key, default)

    def to_dict(self) -> dict:
        """Serialize session for storage."""
        return {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "last_activity": self.last_activity,
            "last_intent": self.last_intent,
            "last_entities": self.last_entities,
            "last_action": self.last_action,
            "last_params": self.last_params,
            "preferences": self.preferences,
            "active_macro": self.active_macro,
            "macro_step": self.macro_step
        }

    @classmethod
    def from_dict(cls, data: dict) -> "VoiceSession":
        """Deserialize session from storage."""
        session = cls(session_id=data["session_id"])
        session.created_at = data.get("created_at", time.time())
        session.last_activity = data.get("last_activity", time.time())
        session.last_intent = data.get("last_intent")
        session.last_entities = data.get("last_entities", {})
        session.last_action = data.get("last_action")
        session.last_params = data.get("last_params", {})
        session.preferences = data.get("preferences", {})
        session.active_macro = data.get("active_macro")
        session.macro_step = data.get("macro_step", 0)
        return session


import re  # For followup resolution


class VoiceSessionManager:
    """Manages multiple voice sessions (per user/device)."""

    def __init__(self, storage_dir: str = "memory/voice_sessions"):
        self.sessions: dict[str, VoiceSession] = {}
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def get_session(self, session_id: str) -> VoiceSession:
        """Get or create a session."""
        if session_id not in self.sessions:
            # Try to load from disk
            session_file = self.storage_dir / f"{session_id}.json"
            if session_file.exists():
                try:
                    import json
                    data = json.loads(session_file.read_text())
                    self.sessions[session_id] = VoiceSession.from_dict(data)
                except Exception as e:
                    logger.warning(f"Could not load session {session_id}: {e}")
                    self.sessions[session_id] = VoiceSession(session_id=session_id)
            else:
                self.sessions[session_id] = VoiceSession(session_id=session_id)
        return self.sessions[session_id]

    def save_session(self, session: VoiceSession) -> None:
        """Save session to disk."""
        try:
            import json
            session_file = self.storage_dir / f"{session.session_id}.json"
            session_file.write_text(json.dumps(session.to_dict()))
        except Exception as e:
            logger.error(f"Could not save session {session.session_id}: {e}")

    def cleanup_old_sessions(self, max_age_hours: int = 24) -> int:
        """Remove sessions older than max_age_hours."""
        now = time.time()
        removed = 0
        for session_id, session in list(self.sessions.items()):
            if now - session.last_activity > max_age_hours * 3600:
                del self.sessions[session_id]
                session_file = self.storage_dir / f"{session_id}.json"
                if session_file.exists():
                    session_file.unlink()
                removed += 1
        return removed


# Singleton instance
_session_manager: VoiceSessionManager | None = None


def get_session_manager() -> VoiceSessionManager:
    """Get or create the session manager singleton."""
    global _session_manager
    if _session_manager is None:
        _session_manager = VoiceSessionManager()
    return _session_manager