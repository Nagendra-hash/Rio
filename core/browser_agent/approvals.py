"""
core/browser_agent/approvals.py — confirmations for headless / chat channels.

`core/confirm.py` already gates risky actions on the HUD, and it deliberately
refuses when no HUD is bound. Telegram and WhatsApp have no HUD, so a risky
browser task arriving there needs a different, equally explicit path:

    request(channel, chat, task, reason)  -> a token, parked with a TTL
    pending_for(channel, chat)            -> what the user is being asked about
    resolve(token, approved, by)          -> the pending task, once

The token is bound to the exact `channel:chat` that asked, so a different chat
cannot approve somebody else's action. Nothing here executes anything — the
caller decides whether to start the job after `resolve()` says yes.

This is intentionally tiny and thread-safe: the reply arm reads it from the
asyncio loop while a tool thread may write it.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

DEFAULT_TTL_S = 600.0

_YES = re.compile(
    r"^(?:yes|yeah|yep|yup|ok(?:ay)?|sure|go\s+ahead|do\s+it|confirm(?:ed)?|"
    r"approve[ds]?|proceed|haan|ha|sí|si|oui|ja)\b",
    re.IGNORECASE,
)
_NO = re.compile(
    r"^(?:no|nope|nah|cancel|stop|don'?t|abort|decline[ds]?|reject(?:ed)?|"
    r"never\s?mind|nahi|नहीं)\b",
    re.IGNORECASE,
)


def parse_yes_no(text: str) -> Optional[bool]:
    """True/False for an unambiguous answer, else None."""
    stripped = str(text or "").strip()
    if not stripped:
        return None
    if _NO.search(stripped):
        return False
    if _YES.search(stripped):
        return True
    return None


def target_key(channel: str, chat: str) -> str:
    return f"{(channel or '').strip().lower()}:{str(chat or '').strip()}"


def split_target(target: str) -> tuple[str, str]:
    """`platform:chat` → (platform, chat). One parser, used by both sides so a
    request and its reply can never disagree about the key."""
    channel, _, chat = str(target or "").partition(":")
    return channel.strip(), chat.strip()


@dataclass
class Pending:
    token: str
    channel: str
    chat: str
    task: str
    reason: str
    created: float = field(default_factory=time.monotonic)
    ttl_s: float = DEFAULT_TTL_S

    @property
    def key(self) -> str:
        return target_key(self.channel, self.chat)

    @property
    def expired(self) -> bool:
        return (time.monotonic() - self.created) > self.ttl_s

    def public(self) -> dict:
        return {
            "token": self.token,
            "channel": self.channel,
            "chat": self.chat,
            "task": self.task[:200],
            "reason": self.reason,
            "expires_in_s": max(0, int(self.ttl_s - (time.monotonic() - self.created))),
        }


class ApprovalStore:
    """One pending browser approval per chat, newest wins."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_key: dict[str, Pending] = {}

    def request(
        self, channel: str, chat: str, task: str, reason: str, ttl_s: float = DEFAULT_TTL_S
    ) -> Pending:
        pending = Pending(
            token=uuid.uuid4().hex[:10],
            channel=channel or "",
            chat=str(chat or ""),
            task=str(task or ""),
            reason=str(reason or ""),
            ttl_s=float(ttl_s),
        )
        with self._lock:
            self._by_key[pending.key] = pending
        return pending

    def request_for_target(
        self, target: str, task: str, reason: str, ttl_s: float = DEFAULT_TTL_S
    ) -> Pending:
        channel, chat = split_target(target)
        return self.request(channel, chat, task, reason, ttl_s=ttl_s)

    def pending_for(self, channel: str, chat: str) -> Optional[Pending]:
        with self._lock:
            self._prune()
            pending = self._by_key.get(target_key(channel, chat))
            if pending and pending.expired:
                self._by_key.pop(pending.key, None)
                return None
            return pending

    def get(self, token: str) -> Optional[Pending]:
        with self._lock:
            self._prune()
            for pending in self._by_key.values():
                if pending.token == token:
                    return None if pending.expired else pending
        return None

    def resolve(
        self, token: str, approved: bool, by: str = ""
    ) -> tuple[Optional[Pending], str]:
        """Pop the pending task. Returns (pending, note).

        `note` is a caller-facing sentence; it never claims work was done.
        """
        with self._lock:
            self._prune()
            for key, pending in list(self._by_key.items()):
                if pending.token != token:
                    continue
                self._by_key.pop(key, None)
                if pending.expired:
                    return None, "that confirmation expired — ask me again"
                who = by or pending.key
                if approved:
                    return pending, f"approved by {who}"
                return pending, f"declined by {who}"
        return None, "no such pending confirmation"

    def resolve_for_chat(
        self, channel: str, chat: str, approved: bool, by: str = ""
    ) -> tuple[Optional[Pending], str]:
        pending = self.pending_for(channel, chat)
        if pending is None:
            return None, "nothing is waiting for your confirmation"
        return self.resolve(pending.token, approved, by=by)

    def resolve_for_target(
        self, target: str, approved: bool, by: str = ""
    ) -> tuple[Optional[Pending], str]:
        channel, chat = split_target(target)
        return self.resolve_for_chat(channel, chat, approved, by=by or target)

    def cancel(self, token: str) -> bool:
        with self._lock:
            for key, pending in list(self._by_key.items()):
                if pending.token == token:
                    self._by_key.pop(key, None)
                    return True
        return False

    def _prune(self) -> None:
        for key, pending in list(self._by_key.items()):
            if pending.expired:
                self._by_key.pop(key, None)

    def list_pending(self) -> list[dict]:
        with self._lock:
            self._prune()
            return [p.public() for p in self._by_key.values()]

    def __len__(self) -> int:
        with self._lock:
            self._prune()
            return len(self._by_key)


STORE = ApprovalStore()
