"""
orchestrator/retry_policy.py — the retry policy for reflexive loops.

One authority for "how many times, for how long, at what cost" so the
reflection and reflexion loops can never spin without a ceiling. Policies
are data (clamped by hard constants), not scattered magic numbers.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable


def _default_clock() -> float:
    return time.monotonic()

# ── hard ceilings (policy is clamped to these) ──────────────────────────────
MAX_RETRIES = 3                    # reflexive retries per attempt cycle
MAX_ROUNDS = 3                     # reflection rounds
MAX_TOTAL_TOKENS = 60_000          # rough token budget (~4 chars/token)
MAX_WALL_SECONDS = 300.0           # wall-clock budget per loop
MAX_BACKOFF_SECONDS = 8.0


def rough_tokens(text: str) -> int:
    """Cheap token estimate (~4 chars/token) for budget accounting."""
    return max(1, len(text or "") // 4)


def _clamp(value, lo, hi, default):
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return float(default)


@dataclass
class RetryPolicy:
    """Tunable retry/budget policy, clamped by the hard ceilings above."""
    max_retries: int = 2               # retries AFTER the first attempt
    max_total_tokens: int = MAX_TOTAL_TOKENS
    max_wall_seconds: float = MAX_WALL_SECONDS
    backoff_seconds: float = 0.0       # linear backoff between retries
    clock: Callable[[], float] = field(default=_default_clock,
                                       repr=False, compare=False)

    def __post_init__(self) -> None:
        try:
            self.max_retries = int(self.max_retries)
        except (TypeError, ValueError):
            self.max_retries = 2
        self.max_retries = max(0, min(MAX_RETRIES, self.max_retries))
        if not callable(self.clock):
            self.clock = _default_clock
        self.max_total_tokens = int(_clamp(self.max_total_tokens, 1,
                                           MAX_TOTAL_TOKENS,
                                           MAX_TOTAL_TOKENS))
        self.max_wall_seconds = _clamp(self.max_wall_seconds, 1.0,
                                       MAX_WALL_SECONDS, MAX_WALL_SECONDS)
        self.backoff_seconds = _clamp(self.backoff_seconds, 0.0,
                                      MAX_BACKOFF_SECONDS, 0.0)

    # ── budget bookkeeping ───────────────────────────────────────────────
    def attempts_allowed(self) -> int:
        """Total attempts = 1 first try + retries."""
        return self.max_retries + 1

    def to_dict(self) -> dict[str, Any]:
        return {"max_retries": self.max_retries,
                "max_total_tokens": self.max_total_tokens,
                "max_wall_seconds": self.max_wall_seconds,
                "backoff_seconds": self.backoff_seconds}


@dataclass
class BudgetState:
    """Mutable budget ledger threaded through a loop."""
    tokens_used: int = 0
    started_at: float = field(default_factory=time.monotonic)
    attempts_used: int = 0

    def spend(self, tokens: int) -> None:
        self.tokens_used += max(0, int(tokens))

    def attempt(self) -> int:
        """Record one attempt, return its 1-based number."""
        self.attempts_used += 1
        return self.attempts_used

    def exhausted(self, policy: RetryPolicy) -> str:
        """Which budget ran out, or '' — checked BEFORE each attempt."""
        if self.tokens_used >= policy.max_total_tokens:
            return "tokens"
        if (policy.clock() - self.started_at) >= policy.max_wall_seconds:
            return "time"
        return ""

    def wall_seconds(self) -> float:
        return time.monotonic() - self.started_at


def backoff_delay(policy: RetryPolicy, attempt_number: int) -> float:
    """Linear backoff capped by the policy."""
    if policy.backoff_seconds <= 0:
        return 0.0
    return min(MAX_BACKOFF_SECONDS,
               policy.backoff_seconds * max(1, attempt_number))
