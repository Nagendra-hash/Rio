"""
gemini_retry.py — shared exponential backoff for transient Gemini API failures.

The free tier fails frequently with 429 RESOURCE_EXHAUSTED and 503 UNAVAILABLE
("model is under high demand"). These are transient by nature: a short wait and
a retry succeeds where an immediate failure forced callers to fall back to
degraded backends (DDG) or to drop work entirely (session summaries).

Retry policy:
    • Retries only *transient* errors: 429 RESOURCE_EXHAUSTED, 500/503
      SERVER_ERROR/UNAVAILABLE, and network errors (ConnectionError, Timeout).
    • Never retries permanent errors (401/403/400) — those need human action,
      and retrying just burns the user's time.
    • Exponential backoff: 2s → 4s → 8s (3 attempts, ~14s worst case, within
      the action timeouts used elsewhere in the app).
"""
from __future__ import annotations

import functools
import random
import time
from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")

# Error substrings that mark a TRANSIENT failure worth retrying.
# Matching is case-insensitive (both lists are lowercase; the text is
# lowercased before comparison) because Python exception types like
# ConnectionError capitalize their message text.
_TRANSIENT_MARKERS = (
    "429",
    "resource_exhausted",
    "quota",
    "500",
    "503",
    "server_error",
    "unavailable",
    "high demand",
    "internal",
    "deadline_exceeded",
    "timed out",
    "timeout",
    "connection reset",
    "connection aborted",
    "connectionerror",
    "connectionrefusederror",
)

# Errors that must NEVER be retried — permanent, need human intervention.
_PERMANENT_MARKERS = (
    "400",
    "invalid_argument",
    "401",
    "unauthenticated",
    "403",
    "permission_denied",
    "api_key_invalid",
    "api key not valid",
    "404",
    "not_found",
)

MAX_ATTEMPTS = 3
BASE_DELAY = 2.0     # seconds
MAX_DELAY = 8.0      # seconds — caps the exponential curve

# A daily-quota 429 is not the transient burst the backoff exists for: every
# retry within the window fails identically, so the ~14 s wait buys nothing
# (measured in the logs — two 429s + backoff before DDG finally answered).
_DAILY_QUOTA_MARKERS = (
    "exceeded your current quota",
    "check your plan and billing details",
)
# Shared across call sites: once one call sees a daily-quota 429, everything
# else skips the retry wait too (the answer cannot change mid-window).
_daily_quota_deadline = 0.0


def in_daily_quota_cooldown() -> bool:
    """True while we believe the Gemini daily quota is exhausted. Backends that
    have a cheaper fallback (web_search → DDG) consult this to skip the retry
    chain entirely; the failover router's own cooldown is independent."""
    return time.monotonic() < _daily_quota_deadline


def _note_daily_quota() -> None:
    """Record a daily-quota 429. 10 min ≫ any backoff chain, ≪ a daily reset;
    a wrong guess only costs one extra Gemini attempt later."""
    global _daily_quota_deadline
    _daily_quota_deadline = time.monotonic() + 600


def is_transient_error(exc: BaseException) -> bool:
    """True if `exc` looks like a transient Gemini/network failure.

    Heuristic on str(exc): the google-genai SDK surfaces API errors as
    exceptions whose text embeds the HTTP code and gRPC status (e.g.
    "503 UNAVAILABLE. {...}"), so substring matching is how the rest of this
    codebase already classifies them.
    """
    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
        return False
    text = str(exc).lower()
    # A daily-quota 429 classifies as NOT transient: retrying inside the
    # decorator's backoff window is pure delay (checked before the generic
    # markers, which "429"/"quota" would otherwise match first). The exception
    # still surfaces to the caller, so backend fallbacks keep working.
    if any(m in text for m in _DAILY_QUOTA_MARKERS):
        _note_daily_quota()
        return False
    # A permanent marker wins over a transient one (e.g. "400" would also
    # substring-match inside some quota payloads).
    if any(m in text for m in _PERMANENT_MARKERS):
        return False
    return any(m in text for m in _TRANSIENT_MARKERS)


def _sleep_with_jitter(delay: float) -> None:
    """Sleep `delay` seconds with ±20% jitter to avoid synchronized retries."""
    time.sleep(delay * random.uniform(0.8, 1.2))


def with_gemini_retry(
    *,
    max_attempts: int = MAX_ATTEMPTS,
    base_delay: float = BASE_DELAY,
    label: str = "Gemini",
    on_final: Callable[[Exception], Any] | None = None,
):
    """Decorator: retry a sync function with exponential backoff on transient
    errors. Raises the last exception if all attempts fail.

    Args:
        label: name used in log lines, e.g. "Gemini headlines".
        on_final: optional callback invoked with the final exception after the
            last attempt fails (before re-raising) — lets callers log/telemetry.
    """

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs) -> T:
            last_exc: Exception | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except Exception as e:
                    if not is_transient_error(e):
                        raise                      # permanent — surface now
                    last_exc = e
                    if attempt == max_attempts:
                        break
                    delay = min(base_delay * (2 ** (attempt - 1)), MAX_DELAY)
                    print(f"[{label}] ⏳ attempt {attempt}/{max_attempts} failed "
                          f"({type(e).__name__}: {str(e)[:100]}) — retrying in {delay:.0f}s")
                    _sleep_with_jitter(delay)
            if last_exc is not None and on_final is not None:
                try:
                    on_final(last_exc)
                except Exception:
                    pass
            assert last_exc is not None
            raise last_exc

        return wrapper

    return decorator


def retry_gemini_async(
    fn: Callable[..., Awaitable[T]],
    *,
    max_attempts: int = MAX_ATTEMPTS,
    base_delay: float = BASE_DELAY,
    label: str = "Gemini",
) -> Callable[..., Awaitable[T]]:
    """Wrap an async callable with the same exponential backoff policy.
    Sleeps with asyncio.sleep so it never blocks the event loop."""
    import asyncio

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs) -> T:
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                return await fn(*args, **kwargs)
            except Exception as e:
                if not is_transient_error(e):
                    raise
                last_exc = e
                if attempt == max_attempts:
                    break
                delay = min(base_delay * (2 ** (attempt - 1)), MAX_DELAY)
                print(f"[{label}] ⏳ attempt {attempt}/{max_attempts} failed "
                      f"({type(e).__name__}: {str(e)[:100]}) — retrying in {delay:.0f}s")
                await asyncio.sleep(delay * random.uniform(0.8, 1.2))
        assert last_exc is not None
        raise last_exc

    return wrapper
