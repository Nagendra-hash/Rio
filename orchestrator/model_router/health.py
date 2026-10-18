"""
orchestrator/model_router/health.py — provider health: probes, cooldowns,
rate-limit parking.

Two independent mechanisms, because they answer different questions:

  * PROBE CACHE — "is it up right now?" A cheap health probe per provider,
    cached with a TTL so a dead provider costs one probe per TTL window, not
    one per request.
  * COOLDOWNS — "it just failed, back off." Set by the router after a real
    failure. Length depends on the failure kind: a quota/rate-limit hit parks
    the provider long (quota resets daily), a transient 503 parks it briefly
    (a one-second blip should not bench a provider for ten minutes), a
    timeout/auth error sits in between.

Cooldown numbers mirror core/llm_router.py's field-tested values.

Clock is injectable (time.monotonic by default) so tests can fast-forward
cooldowns without sleeping. stdlib-only, thread-safe, never raises.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger("jarvis.model_router")

# ── cooldown lengths (seconds) by failure kind ───────────────────────────────
RATE_LIMIT_COOLDOWN_S = 900     # quota exhausted — resets daily on free tiers
AUTH_COOLDOWN_S = 600           # bad key — will not fix itself quickly
TIMEOUT_COOLDOWN_S = 60         # hung call — give the provider a minute
TRANSIENT_COOLDOWN_S = 45       # 503 / overload — clears in seconds
UNAVAILABLE_COOLDOWN_S = 120    # refused / not configured — service down

# ── probe cache TTLs ─────────────────────────────────────────────────────────
PROBE_TTL_OK_S = 300            # a healthy provider is re-probed rarely
PROBE_TTL_FAIL_S = 60           # a dead one is re-probed, but not per request


@dataclass
class ProviderHealth:
    """Everything the board knows about one provider right now."""
    provider: str
    ok: Optional[bool] = None       # None = never probed
    reason: str = ""
    checked_at: float = 0.0         # monotonic
    cooldown_until: float = 0.0     # monotonic
    cooldown_reason: str = ""

    @property
    def cooling(self) -> bool:
        return self.cooldown_until > 0.0

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "probed_ok": self.ok,
            "probe_reason": self.reason,
            "cooling": self.cooling,
            "cooldown_reason": self.cooldown_reason,
        }


class HealthBoard:
    """Probe cache + cooldown board for named providers."""

    def __init__(self,
                 probe_ttl_ok: float = PROBE_TTL_OK_S,
                 probe_ttl_fail: float = PROBE_TTL_FAIL_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._probe_ttl_ok = float(probe_ttl_ok)
        self._probe_ttl_fail = float(probe_ttl_fail)
        self._clock = clock
        self._states: dict[str, ProviderHealth] = {}
        self._lock = threading.RLock()

    # ── probes ────────────────────────────────────────────────────────────
    def check(self, provider: str,
              probe: Callable[[], tuple[bool, str]]) -> tuple[bool, str]:
        """Run (or reuse) the provider's health probe.

        `probe` returns (ok, reason). It is only called when the cached
        result is older than the TTL — a healthy provider every
        probe_ttl_ok seconds, a dead one every probe_ttl_fail seconds.
        Never raises: a probe that throws is just an unhealthy provider.
        """
        now = self._clock()
        with self._lock:
            st = self._states.setdefault(provider, ProviderHealth(provider))
            if st.ok is not None:
                ttl = self._probe_ttl_ok if st.ok else self._probe_ttl_fail
                if now - st.checked_at < ttl:
                    return st.ok, st.reason
        try:
            ok, reason = probe()
        except Exception as exc:                    # a crashing probe = down
            ok, reason = False, f"probe crashed: {type(exc).__name__}: {exc}"
        with self._lock:
            st.ok = bool(ok)
            st.reason = str(reason)[:200]
            st.checked_at = now
        return st.ok, st.reason

    # ── cooldowns ─────────────────────────────────────────────────────────
    def cool(self, provider: str, seconds: float, reason: str = "") -> None:
        with self._lock:
            st = self._states.setdefault(provider, ProviderHealth(provider))
            until = self._clock() + max(0.0, float(seconds))
            if until >= st.cooldown_until:          # never shorten a parking
                st.cooldown_until = until
                st.cooldown_reason = str(reason)[:200]
        log.debug("provider %s cooling %.0fs (%s)", provider, seconds, reason)

    def note_rate_limited(self, provider: str,
                          retry_after_s: Optional[float] = None) -> None:
        """A 429/quota hit: park for the provider's Retry-After when it sent
        one, else the default long quota cooldown."""
        seconds = (float(retry_after_s) if retry_after_s
                   else RATE_LIMIT_COOLDOWN_S)
        self.cool(provider, seconds, "rate limited")

    def mark_healthy(self, provider: str) -> None:
        """A live answer clears any cooldown and refreshes the probe cache —
        the provider just proved it works."""
        with self._lock:
            st = self._states.setdefault(provider, ProviderHealth(provider))
            st.ok = True
            st.reason = "answered"
            st.checked_at = self._clock()
            st.cooldown_until = 0.0
            st.cooldown_reason = ""

    def is_available(self, provider: str) -> bool:
        with self._lock:
            st = self._states.get(provider)
            return not (st and st.cooling
                        and self._clock() < st.cooldown_until)

    def cooldown_remaining(self, provider: str) -> float:
        with self._lock:
            st = self._states.get(provider)
            if not st or not st.cooling:
                return 0.0
            return max(0.0, st.cooldown_until - self._clock())

    def reason_while_cooling(self, provider: str) -> str:
        """Human reason for a route plan's skipped list."""
        with self._lock:
            st = self._states.get(provider)
        if not st:
            return ""
        remaining = self.cooldown_remaining(provider)
        why = st.cooldown_reason or "recent failure"
        if remaining >= 90:
            return f"{why} — cooling down {remaining / 60:.0f}m more"
        return f"{why} — cooling down {remaining:.0f}s more"

    # ── snapshot ──────────────────────────────────────────────────────────
    def snapshot(self, providers: Optional[list[str]] = None) -> dict:
        """Status for dashboards. No secrets — provider names and states only."""
        with self._lock:
            names = providers or list(self._states)
            return {n: self._states.get(n, ProviderHealth(n)).to_dict()
                    for n in names}
