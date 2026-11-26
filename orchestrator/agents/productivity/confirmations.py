"""
orchestrator/agents/productivity/confirmations.py — one gate for every
Gmail/Drive action that reaches the outside world.

Two interfaces exist, and the same action must work through both:

  Desktop / voice / dashboard   → core.confirm (HUD banner, CONFIRM/CANCEL)
  Telegram / WhatsApp / Hermes  → a pending token bound to `channel:chat`,
                                  answered with "yes"/"no" in that chat

The model never writes the confirmation. On the HUD it cannot (the button is
the interface's). In chat the token is issued here and resolved by
`resolve(target, approved)`, which main.py calls before the model sees a
"yes"/"no" turn — so the model cannot fabricate either half.

Nothing executes until the user answers. `run` is a zero-argument callable
that performs exactly the action the user was shown.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

DEFAULT_TTL_S = 600.0


@dataclass
class PendingAction:
    token: str
    channel: str
    chat: str
    title: str
    detail: str
    run: Callable[[], str]
    created: float = field(default_factory=time.monotonic)
    ttl_s: float = DEFAULT_TTL_S

    @property
    def key(self) -> str:
        return f"{self.channel}:{self.chat}"

    @property
    def expired(self) -> bool:
        return (time.monotonic() - self.created) > self.ttl_s


class ActionStore:
    """One pending action per chat, newest wins. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_key: dict[str, PendingAction] = {}

    def _prune(self) -> None:
        for key, p in list(self._by_key.items()):
            if p.expired:
                self._by_key.pop(key, None)

    def request(self, target: str, title: str, detail: str,
                run: Callable[[], str], ttl_s: float = DEFAULT_TTL_S
                ) -> PendingAction:
        channel, _, chat = str(target or "").partition(":")
        pending = PendingAction(
            token=uuid.uuid4().hex[:10], channel=channel.strip(),
            chat=chat.strip(), title=str(title or ""),
            detail=str(detail or ""), run=run, ttl_s=float(ttl_s))
        with self._lock:
            self._by_key[pending.key] = pending
        return pending

    def pending_for(self, target: str) -> Optional[PendingAction]:
        channel, _, chat = str(target or "").partition(":")
        key = f"{channel.strip()}:{chat.strip()}"
        with self._lock:
            self._prune()
            return self._by_key.get(key)

    def resolve(self, target: str, approved: bool
                ) -> tuple[Optional[PendingAction], str]:
        channel, _, chat = str(target or "").partition(":")
        key = f"{channel.strip()}:{chat.strip()}"
        with self._lock:
            self._prune()
            pending = self._by_key.pop(key, None)
        if pending is None:
            return None, "nothing is waiting for confirmation in this chat"
        if approved:
            return pending, "approved"
        return pending, "declined"

    def cancel(self, target: str) -> bool:
        channel, _, chat = str(target or "").partition(":")
        with self._lock:
            return self._by_key.pop(f"{channel.strip()}:{chat.strip()}",
                                    None) is not None


STORE = ActionStore()


# ── interface helpers ────────────────────────────────────────────────────────

def target_of(player) -> str:
    """`channel:chat` for the inbound turn, or '' on the desktop."""
    for attr in ("inbound_target", "_inbound_target"):
        value = getattr(player, attr, None) if player is not None else None
        if value:
            return str(value)
    return ""


def send_chat(target: str, text: str) -> bool:
    if not target:
        return False
    try:
        from core import binance_ws
        return bool(binance_ws.send_hermes(target, str(text)[:3500]))
    except Exception as e:
        print(f"[Productivity] chat send failed: {e}")
        return False


# ── the gate ─────────────────────────────────────────────────────────────────

def gate(*, title: str, detail: str, run: Callable[[], str],
         player=None, key: str = "productivity") -> str:
    """Park `run` behind the right confirmation interface.

    Returns the sentence the model should speak. On desktop the action runs
    from the HUD's CONFIRM callback; in chat it runs from main.py's reply arm
    after the user types yes.
    """
    target = target_of(player)

    if target:
        existing = STORE.pending_for(target)
        if existing is not None:
            return ("There is already a confirmation waiting in this chat — "
                    "answer that one first with yes or no.")
        pending = STORE.request(target, title, detail, run)
        send_chat(target, "This needs your confirmation before I do it.\n"
                          f"{title}\n{detail}\n"
                          f"Reply “yes” to proceed or “no” to cancel "
                          f"(token {pending.token}).")
        return (f"[CONFIRMATION_PENDING] I have asked the user to confirm "
                f"“{title}” in this chat. Do not claim it is done.")

    # Desktop / voice: the non-blocking HUD banner.
    try:
        from core import confirm
    except Exception as e:
        return (f"I cannot confirm “{title}” because the confirmation "
                f"interface is unavailable, so I have not done it ({e}).")

    if confirm.pending_title():
        return ("There is already a confirmation waiting on screen — answer "
                "that one first.")
    return confirm.request(key=key, title=title, detail=detail, run=run)


__all__ = ["STORE", "ActionStore", "PendingAction", "gate", "target_of",
           "send_chat", "DEFAULT_TTL_S"]
