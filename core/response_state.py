"""
core/response_state.py — lightweight per-request response lifecycle tracker.

Gives every dashboard/bridge request a correlation ID and a small state
machine so the system can tell apart — in logs and in code —

    RECEIVED → PROCESSING → GENERATING → (SPEAKING) → COMPLETED
                                            ↘ FAILED / CANCELLED

Design constraints (small-RAM box, no new dependencies):
  • In-RAM only, bounded: max 256 tracked requests, TTL sweep (15 min).
  • Thread-safe: one lock; the conversation queues have both asyncio and
    thread callers.
  • Never raises into the hot path: every method swallows its own errors —
    a broken tracker must never break a reply.
  • One line of stdout per state change (systemd journal captures it):
        [REQ a1b2c3] GENERATING channel=telegram:123 model=gemini-live
    No message bodies are logged — lengths and channels only.
  • Text generation and voice playback are tracked separately (`spoken`
    flag) — completed text does not imply completed audio.
"""
from __future__ import annotations

import threading
import time

__all__ = ["ResponseTracker", "tracker"]

# States (kept as plain strings — cheap to log, no enum import cost).
RECEIVED   = "RECEIVED"
PROCESSING = "PROCESSING"
GENERATING = "GENERATING"
SPEAKING   = "SPEAKING"
COMPLETED  = "COMPLETED"
FAILED     = "FAILED"
CANCELLED  = "CANCELLED"

# Legal forward transitions (enforced loosely: illegal moves are logged and
# ignored, never raised — a request cannot be un-completed by a stray update).
_TRANSITIONS = {
    RECEIVED:   {PROCESSING, FAILED, CANCELLED},
    PROCESSING: {GENERATING, FAILED, CANCELLED, COMPLETED},
    GENERATING: {SPEAKING, COMPLETED, FAILED, CANCELLED},
    SPEAKING:   {COMPLETED, FAILED, CANCELLED},
    COMPLETED:  set(),
    FAILED:     set(),
    CANCELLED:  set(),
}

# A finished request is kept for correlation lookups for 15 minutes, then
# swept. 256 entries × ~200 bytes ≈ 50 KB worst case.
_TTL_S = 900.0
_MAX_ENTRIES = 256


class _Request:
    __slots__ = ("id", "channel", "state", "created", "updated",
                 "spoken", "finish", "error", "model")

    def __init__(self, rid: str, channel: str):
        self.id = rid
        self.channel = channel
        self.state = RECEIVED
        self.created = time.monotonic()
        self.updated = self.created
        self.spoken = False          # voice playback completion (separate)
        self.finish = ""             # provider finish reason, when known
        self.error = ""              # failure detail, when known
        self.model = ""              # model/provider actually used


class ResponseTracker:
    """Create → transition → finish. See module docstring for the rules."""

    def __init__(self, ttl_s: float = _TTL_S, max_entries: int = _MAX_ENTRIES):
        self._ttl = float(ttl_s)
        self._max = max(1, int(max_entries))
        self._lock = threading.Lock()
        self._reqs: dict[str, _Request] = {}
        self._counter = 0

    # ── correlation ids ──────────────────────────────────────────────────
    def _new_id(self) -> str:
        # Short, unique-enough, human-copyable: req_<base36 ticks><rand>
        self._counter += 1
        return f"req_{int(time.time() * 1000):x}_{self._counter % 0xffff:04x}" \
               f"{int(time.monotonic() * 1e6) & 0xffff:04x}"

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self, channel: str = "", source: str = "") -> str:
        """Register a new request in RECEIVED. Returns its correlation ID."""
        try:
            now = time.monotonic()
            with self._lock:
                self._sweep_locked(now)
                rid = self._new_id()
                self._reqs[rid] = _Request(rid, (channel or "")[:64])
            print(f"[REQ {rid}] RECEIVED channel={self._reqs[rid].channel}"
                  + (f" source={source}" if source else ""))
            return rid
        except Exception:
            return ""

    def update(self, rid: str, state: str, *, model: str = "",
               finish: str = "", error: str = "") -> bool:
        """Move a request forward. Illegal/backward moves are logged and
        ignored (a stray late event can never resurrect a done request)."""
        if not rid:
            return False
        try:
            with self._lock:
                req = self._reqs.get(rid)
                if req is None:
                    print(f"[REQ {rid}] ⚠️ update for unknown request "
                          f"(state={state})")
                    return False
                allowed = _TRANSITIONS.get(req.state, set())
                if state != req.state and state not in allowed:
                    print(f"[REQ {rid}] ⚠️ illegal transition "
                          f"{req.state} → {state} ignored")
                    return False
                req.state = state
                req.updated = time.monotonic()
                if model:
                    req.model = model
                if finish:
                    req.finish = finish
                if error:
                    req.error = error
            extra = []
            if model:
                extra.append(f"model={model}")
            if finish:
                extra.append(f"finish={finish}")
            if error:
                extra.append(f"error={error[:120]}")
            print(f"[REQ {rid}] {state} " + " ".join(extra))
            return True
        except Exception:
            return False

    def mark_spoken(self, rid: str) -> None:
        """Voice playback for this request finished (independent of text
        state — completed text never implies completed audio)."""
        if not rid:
            return
        try:
            with self._lock:
                req = self._reqs.get(rid)
                if req is not None:
                    req.spoken = True
                    req.updated = time.monotonic()
            print(f"[REQ {rid}] TTS done")
        except Exception:
            pass

    def get(self, rid: str) -> dict | None:
        try:
            with self._lock:
                req = self._reqs.get(rid)
                if req is None:
                    return None
                return {
                    "id": req.id,
                    "channel": req.channel,
                    "state": req.state,
                    "spoken": req.spoken,
                    "finish": req.finish,
                    "error": req.error,
                    "model": req.model,
                    "age_s": round(time.monotonic() - req.created, 1),
                }
        except Exception:
            return None

    # ── housekeeping ─────────────────────────────────────────────────────
    def _sweep_locked(self, now: float) -> None:
        if not self._reqs:
            return
        for rid in [r for r, q in self._reqs.items()
                    if now - q.updated > self._ttl]:
            del self._reqs[rid]
        while len(self._reqs) >= self._max:
            oldest = min(self._reqs.values(), key=lambda q: q.updated).id
            del self._reqs[oldest]

    def size(self) -> int:
        try:
            with self._lock:
                return len(self._reqs)
        except Exception:
            return 0


def tracker() -> ResponseTracker:
    """Process-wide singleton (lazy, import-safe)."""
    global _SHARED
    try:
        return _SHARED                                  # type: ignore[name-defined]
    except NameError:
        pass
    with _INIT_LOCK:
        if "_SHARED" not in globals():
            _SHARED = ResponseTracker()
        return _SHARED


_INIT_LOCK = threading.Lock()
