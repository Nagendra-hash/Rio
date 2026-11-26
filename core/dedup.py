"""
core/dedup.py — lightweight incoming-event deduplication.

Root cause it fixes (DUPLICATE_RESPONSE_ROOT_CAUSE.md, cause D1): the Hermes
intake (/api/message), the dashboard command relay (/api/command) and the
TradingView command path put messages straight onto the command queue with no
duplicate guard. A Hermes hook retry, a webhook replay or a user double-click
enqueueing the same text twice became two model turns and two replies sent to
the user.

Design constraints:
  • No database, no new dependency: an in-RAM, bounded dict with TTL expiry.
  • Channel-scoped keys: a Telegram message and a dashboard command with the
    same text are DIFFERENT events; only a replay of the same *event* dedupes.
  • Prefer an explicit event/message id; fall back to (channel, hash(text))
    only when no id exists — with a SHORT TTL so the same words spoken twice
    a few seconds apart both go through.
  • Bounded memory: expired entries are swept opportunistically; the dict is
    hard-capped by evicting the oldest.
  • Thread-safe (the queues have both asyncio and thread callers).

Never raises into the hot path: can_dedup() returns False (no record) when
the internal structure failed, so a broken dedup state can never silence
legitimate messages.
"""
from __future__ import annotations

import hashlib
import threading
import time

__all__ = ["DedupGuard"]

# Default window a replay is still considered the same event. Long enough to
# absorb a hook retry (they re-POST within ~1-2s), short enough that the same
# words deliberately sent again a few seconds later both succeed.
DEFAULT_TTL_S = 5.0

# Hard cap on tracked events. 1024 covers hours of bursty use at trivial RAM
# cost (~100 bytes/entry); sweeps and oldest-eviction keep it exact.
MAX_ENTRIES = 1024


class DedupGuard:
    """See-first, then-remember dedup store."

    Typical use inside a handler:

        if not guard.check(key, channel):        # replay → ignore
            return "duplicate"
        guard.remember(key, channel)             # first time: own it

    Or atomically: `acquire()` does check+remember under one lock and is what
    handlers with concurrent callers should use.
    """

    def __init__(self, ttl_s: float = DEFAULT_TTL_S, max_entries: int = MAX_ENTRIES):
        self._ttl = float(ttl_s)
        self._max = max(1, int(max_entries))
        self._lock = threading.Lock()
        # key -> monotonic expiry. Insertion order in a plain dict gives cheap
        # oldest-first eviction.
        self._events: dict[tuple[str, str], float] = {}

    # ── key building ─────────────────────────────────────────────────────────

    @staticmethod
    def key_for(channel: str, event_id: str = "", text: str = "") -> tuple[str, str]:
        """Build the dedup key. An explicit event/message id always wins;
        without one, a short content hash scoped to the channel is used (safe:
        the guard's TTL is what bounds the risk of two real identical messages
        colliding — 5s, not minutes)."""
        ch = (channel or "").strip()[:64]
        eid = (event_id or "").strip()
        if eid:
            return (ch, f"id:{eid[:128]}")      # ties to the chat-scoped prefix
        h = hashlib.blake2b((text or "").encode("utf-8", "replace"),
                            digest_size=12).hexdigest()
        return (ch, f"txt:{h}")

    # ── core operations (single global lock: contention here is negligible) ──

    def _sweep_locked(self, now: float) -> None:
        if not self._events:
            return
        expired = [k for k, exp in self._events.items() if exp <= now]
        for k in expired:
            del self._events[k]
        while len(self._events) >= self._max:
            # dict preserves insertion order → first key is the oldest.
            oldest = next(iter(self._events))
            del self._events[oldest]

    def seen(self, channel: str, event_id: str = "", text: str = "") -> bool:
        """True when this exact event was recorded within the TTL window."""
        key = self.key_for(channel, event_id, text)
        now = time.monotonic()
        try:
            with self._lock:
                exp = self._events.get(key)
                return exp is not None and exp > now
        except Exception:
            return False

    def record(self, channel: str, event_id: str = "", text: str = "") -> None:
        """Record an event as handled (or as seen for withhold). Never raises."""
        key = self.key_for(channel, event_id, text)
        now = time.monotonic()
        try:
            with self._lock:
                self._sweep_locked(now)
                self._events[key] = now + self._ttl
        except Exception:
            pass

    def acquire(self, channel: str, event_id: str = "", text: str = "") -> bool:
        """Atomically: unseen → record and return True (caller proceeds);
        seen → return False (caller drops the replay). The atomic form is the
        one concurrent intake paths must use: two simultaneous identical
        POSTs must produce exactly one winner."""
        key = self.key_for(channel, event_id, text)
        now = time.monotonic()
        try:
            with self._lock:
                self._sweep_locked(now)
                if self._events.get(key, 0.0) > now:
                    return False
                self._events[key] = now + self._ttl
                return True
        except Exception:
            return True          # fail open: never lose legitimate messages

    def size(self) -> int:
        try:
            return len(self._events)
        except Exception:
            return 0


# Process-wide guard used by the intake endpoints. One instance, bounded, so
# even a stuck loop stops growing the dict.
def shared_guard() -> DedupGuard:
    global _SHARED
    try:
        return _SHARED
    except NameError:
        pass
    with _INIT_LOCK:
        if "_SHARED" not in globals():
            _SHARED = DedupGuard()
        return _SHARED


_INIT_LOCK = threading.Lock()
