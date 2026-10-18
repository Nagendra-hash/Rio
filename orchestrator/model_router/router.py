"""
orchestrator/model_router/router.py — the Model Router itself.

Sits BENEATH the Jarvis orchestrator: one prompt in, one answer out — served
by the cheapest configured + healthy + capable model, falling back down the
capability chain when a provider fails, and explaining itself to the user
when it had to.

The core loop (route → try → cool → next) in six rules:

    1. ROUTE from the CapabilityRegistry: capable models for the task,
       cheapest tier first. PRIVATE asks see only local models.
    2. SKIP what is not configured, not healthy, or cooling down — with a
       recorded reason, never a silent gap.
    3. TRY with a hard timeout (the per-call deadline shrinks by whatever
       the earlier hops already burned).
    4. COOL on failure, by failure kind — a 429 parks the provider for
       RATE_LIMIT_COOLDOWN_S (or its Retry-After), a 503 briefly.
    5. FALL BACK down the chain until something answers or everything failed.
    6. LOG every hop to the usage journal — no keys, no prompt text.

The result carries `explanation`: a user-visible one-liner whenever a hop
had to fall back, e.g.
    "Answered by openrouter_free — fell back because local_qwen3b was
     unavailable (not enough free RAM (210 MB available))."
When the FIRST choice answers, explanation is "" — no noise on the happy
path. Skips that happened BELOW the winner (models that were never reached)
are not fallbacks and are not explained.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from orchestrator.model_router.capabilities import (
    CapabilityRegistry,
    ModelSpec,
    ModelTask,
    Privacy,
    build_default_registry,
)
from orchestrator.model_router.health import HealthBoard
from orchestrator.model_router.providers import (
    AuthError,
    CompletionRequest,
    DeepSeekAdapter,
    EmptyAnswerError,
    FreeLLMAdapter,
    GeminiAdapter,
    LocalQwenAdapter,
    NotConfiguredError,
    NvidiaAdapter,
    OpenRouterAdapter,
    ProviderAdapter,
    ProviderError,
    ProviderTimeoutError,
    RateLimitedError,
    UnavailableError,
    classify_error,
    cooldown_for,
    load_provider_config,
)
from orchestrator.model_router.usage import UsageJournal

log = logging.getLogger("jarvis.model_router")

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "api_keys.json"

DEFAULT_TIMEOUT_S = 60.0
DEFAULT_DEADLINE_S = 90.0          # total budget across the whole chain

_HUMAN_TASK = {
    ModelTask.SIMPLE_CODING: "a simple coding task",
    ModelTask.TRANSFORMATION: "a transformation task",
    ModelTask.PRIVATE: "a private task",
    ModelTask.LIGHT_REASONING: "a lightweight reasoning task",
    ModelTask.RESEARCH: "a research task",
    ModelTask.DIFFICULT_REASONING: "a difficult reasoning task",
}


@dataclass
class RouteOptions:
    """Per-request knobs the orchestrator layer can set."""
    task: ModelTask = ModelTask.LIGHT_REASONING
    system: str = ""
    force_provider: str = ""            # e.g. "gemini" pins the specialist
    require_local: bool = False         # hard privacy: local models only
    timeout_s: float = DEFAULT_TIMEOUT_S
    deadline_s: float = DEFAULT_DEADLINE_S
    max_tokens: Optional[int] = None


@dataclass
class RouteHop:
    """One provider's story in this route: skipped, failed, or answered."""
    provider: str                       # registry model name (spec.name)
    model: str
    status: str                         # see _TERMINAL/_SKIP statuses below
    detail: str = ""

    def to_dict(self) -> dict:
        return {"provider": self.provider, "model": self.model,
                "status": self.status, "detail": self.detail[:160]}


@dataclass
class RouteResult:
    """One routed request — the full story, not just the text."""
    ok: bool
    text: str = ""
    provider: str = ""                  # spec.name that answered ("" when none)
    model: str = ""                     # model id that answered
    task: ModelTask = ModelTask.LIGHT_REASONING
    explanation: str = ""               # user-visible fallback explanation
    hops: list[RouteHop] = field(default_factory=list)
    latency_s: float = 0.0

    @property
    def fell_back(self) -> bool:
        return bool(self.explanation)


_FAILURE_STATUSES = frozenset({
    "unavailable", "transient", "timeout", "unhealthy", "rate_limited",
    "auth", "not_configured", "empty"})


def build_explanation(winner: Optional[str], hops: list[RouteHop]) -> str:
    """The user-visible WHY of a route. Empty when the first choice answered
    (no hops happened before it). Otherwise: who answered, and every hop
    before the winner with its reason."""
    if not hops:
        return ""
    whys = "; ".join(
        (f"{h.provider} unavailable ({h.status}: {h.detail})"
         if h.status in _FAILURE_STATUSES
         else f"{h.provider} skipped ({h.detail})")
        for h in hops)
    if winner:
        return f"Answered by {winner} — fell back because {whys}."
    return f"No provider could answer: {whys}."


class ModelRouter:
    """Cheapest-capable-first routing with automatic, explained fallback."""

    def __init__(self, registry: Optional[CapabilityRegistry] = None,
                 adapters: Optional[dict[str, ProviderAdapter]] = None,
                 health: Optional[HealthBoard] = None,
                 usage: Optional[UsageJournal] = None,
                 config_path: str | Path = DEFAULT_CONFIG_PATH,
                 mode: Optional[str] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.registry = registry or build_default_registry()
        self.health = health or HealthBoard(clock=clock)
        self.usage = usage or UsageJournal()
        self.config_path = Path(config_path)
        self._clock = clock
        self.mode = mode or self._detect_mode()
        if adapters is not None:
            # Fully injected (tests, custom installs): take them as-is.
            self.adapters: dict[str, ProviderAdapter] = dict(adapters)
        else:
            self.adapters = self._build_default_adapters()

    # ── construction helpers ──────────────────────────────────────────────
    def _detect_mode(self) -> str:
        raw = (os.environ.get("JARVIS_MODEL_ROUTER_MODE")
               or os.environ.get("JARVIS_ROUTER_MODE") or "smart").strip().lower()
        return raw if raw in ("local", "free", "smart", "gemini") else "smart"

    def _build_default_adapters(self) -> dict[str, ProviderAdapter]:
        """Wire the default adapters from config. A missing config file is
        normal (fresh install) — providers then simply report "not
        configured" and the router degrades honestly."""
        config = load_provider_config(self.config_path)
        adapters: dict[str, ProviderAdapter] = {"local": LocalQwenAdapter()}
        key = (config.get("freellmapi_api_key") or "").strip()
        if key:
            adapters["freellm"] = FreeLLMAdapter(api_key=key)
        entries = config.get("openrouter_keys") or []
        keys = [e.get("key") if isinstance(e, dict) else e for e in entries]
        keys = [k.strip() for k in keys if isinstance(k, str) and k.strip()]
        if keys:
            adapters["openrouter"] = OpenRouterAdapter(api_key=keys[0])
            # The working DeepSeek coder, via the same OpenRouter key.
            adapters["deepseek"] = DeepSeekAdapter(api_key=keys[0])
        nv = (config.get("nvidia_api_key") or "").strip()
        if nv:
            models = config.get("nvidia_models") or None
            if isinstance(models, str):
                models = [models]
            adapters["nvidia"] = NvidiaAdapter(api_key=nv, models=models)
        gem = GeminiAdapter.from_config(config)
        if gem.is_configured():
            adapters["gemini"] = gem
        return adapters

    # ── planning (pure — testable without any adapter) ────────────────────
    def _plan(self, prompt: str, options: RouteOptions
              ) -> tuple[list[ModelSpec], list[RouteHop], list[str]]:
        """Returns (try-in-order, skip-hops-for-excluded, full-order-names).

        Excluded = no adapter, not configured, mode-forbidden, prompt too
        long for the model's context cap, cooling down, or probe says
        unhealthy — each with a recorded reason. Never raises.
        """
        prompt_chars = len(prompt or "") + len(options.system or "")
        privacy = Privacy.LOCAL if (options.require_local
                                    or options.task is ModelTask.PRIVATE) \
            else None
        specs = self.registry.models_for(options.task, privacy=privacy)
        if self.mode == "gemini":
            # Forced mode: Gemini only, whatever the task.
            specs = [s for s in specs if s.provider == "gemini"]
        elif options.force_provider:
            specs = [s for s in specs if s.provider == options.force_provider]
        order = [s.name for s in specs]
        hops: list[RouteHop] = []
        plan: list[ModelSpec] = []

        for spec in specs:
            adapter = self.adapters.get(spec.provider)
            if adapter is None:
                hops.append(RouteHop(spec.name, spec.model_id, "not_configured",
                                     "no adapter configured"))
                continue
            try:
                configured = adapter.is_configured()
            except Exception as exc:
                configured = False
                log.debug("is_configured(%s) crashed: %s", spec.provider, exc)
            if not configured:
                hops.append(RouteHop(spec.name, spec.model_id, "not_configured",
                                     f"{spec.provider} is not configured"))
                continue
            if self.mode == "local" and spec.privacy is not Privacy.LOCAL:
                hops.append(RouteHop(spec.name, spec.model_id, "skipped",
                                     "local mode allows on-machine models only"))
                continue
            if self.mode == "free" and spec.cost_tier >= 3:
                hops.append(RouteHop(spec.name, spec.model_id, "skipped",
                                     "free mode never uses Gemini"))
                continue
            if not spec.accepts_length(prompt_chars):
                hops.append(RouteHop(
                    spec.name, spec.model_id, "skipped",
                    f"prompt too long for this model "
                    f"({prompt_chars} > {spec.context_chars} chars)"))
                continue
            if not self.health.is_available(spec.provider):
                hops.append(RouteHop(spec.name, spec.model_id, "cooling",
                                     self.health.reason_while_cooling(spec.provider)
                                     or "cooling down after a recent failure"))
                continue
            try:
                ok, reason = self.health.check(spec.provider, adapter.probe)
            except Exception as exc:              # probe exploded = down
                ok, reason = False, f"probe failed: {type(exc).__name__}"
            if not ok:
                hops.append(RouteHop(spec.name, spec.model_id, "unhealthy",
                                     reason or "health check failed"))
                continue
            plan.append(spec)

        # Smart mode on hard asks: try the reasoning specialist FIRST — a 3B
        # model or a free chain answering a genuinely difficult question is
        # how confidently-wrong answers get shipped. It still falls back to
        # the free chain (and the specialist is cooled out of the way if it
        # is parked), so cost only rises when the specialist is healthy.
        if (options.task is ModelTask.DIFFICULT_REASONING
                and self.mode == "smart"
                and any(s.provider == "gemini" for s in plan)
                and len(plan) > 1):
            plan.sort(key=lambda s: s.provider != "gemini")  # False < True
            order = [s.name for s in plan] + [
                n for n in order if n not in {s.name for s in plan}]

        return plan, hops, order

    # ── the main entry ────────────────────────────────────────────────────
    def route(self, prompt: str,
              options: Optional[RouteOptions] = None) -> RouteResult:
        """One prompt → RouteResult. NEVER raises: total failure is a
        RouteResult(ok=False) carrying the explanation."""
        options = options or RouteOptions()
        t0 = self._clock()
        prompt_chars = len(prompt or "") + len(options.system or "")
        hops: list[RouteHop] = []

        plan, skip_hops, order = self._plan(prompt, options)
        hops.extend(skip_hops)

        if not plan:
            result = RouteResult(
                ok=False, task=options.task, hops=hops,
                latency_s=self._clock() - t0,
                explanation=build_explanation(None, hops)
                or "No provider is configured or healthy for "
                   + _HUMAN_TASK.get(options.task, "this task") + ".")
            self._journal(options, result, t0, prompt_chars, error="no provider")
            return result

        deadline = t0 + max(options.deadline_s, options.timeout_s)
        last_error = ""

        for spec in plan:
            remaining = deadline - self._clock()
            if remaining <= 0.5:
                hops.append(RouteHop(spec.name, spec.model_id, "timeout",
                                     "chain deadline exhausted"))
                last_error = "deadline exhausted"
                break
            adapter = self.adapters[spec.provider]
            timeout = min(options.timeout_s, remaining)
            req = CompletionRequest(
                prompt=(f"{options.system}\n\n{prompt}" if options.system
                        else prompt),
                model_id=("auto" if spec.provider in ("local", "nvidia",
                                                       "deepseek")
                          else spec.model_id),
                max_tokens=options.max_tokens,
                timeout_s=timeout,
                metadata={"task": options.task.value},
            )
            try:
                text = adapter.complete(req)
            except NotConfiguredError as exc:
                hops.append(RouteHop(spec.name, spec.model_id,
                                     "not_configured", str(exc)))
                last_error = str(exc)
                continue
            except (RateLimitedError, AuthError) as exc:
                kind = classify_error(exc)
                if kind == "rate_limited":
                    retry_after = getattr(exc, "retry_after_s", None)
                    if retry_after:
                        self.health.note_rate_limited(spec.provider, retry_after)
                    else:
                        self.health.note_rate_limited(spec.provider)
                    hops.append(RouteHop(
                        spec.name, spec.model_id, "rate_limited",
                        f"rate limited (quota) — cooling "
                        f"{self.health.cooldown_remaining(spec.provider):.0f}s"))
                else:
                    self.health.cool(spec.provider, cooldown_for(kind),
                                     f"auth error: {exc}")
                    hops.append(RouteHop(spec.name, spec.model_id, "auth",
                                         "API key rejected"))
                last_error = str(exc)
                continue
            except (ProviderTimeoutError, UnavailableError,
                    EmptyAnswerError, ProviderError) as exc:
                kind = classify_error(exc)
                self.health.cool(spec.provider, cooldown_for(kind),
                                 f"{kind}: {exc}")
                hops.append(RouteHop(spec.name, spec.model_id, kind, str(exc)))
                last_error = str(exc)
                continue
            except Exception as exc:               # unknown — still fall back
                kind = classify_error(exc)
                self.health.cool(spec.provider, cooldown_for(kind),
                                 f"{kind}: {exc}")
                hops.append(RouteHop(spec.name, spec.model_id, kind,
                                     f"{type(exc).__name__}: {exc}"))
                last_error = str(exc)
                continue

            # ── success ──────────────────────────────────────────────────
            self.health.mark_healthy(spec.provider)
            # Walk-chain providers (the NVIDIA coding chain) expose which model
            # actually answered, so the journal names DeepSeek — or whichever
            # fallback took over when it was queued.
            model_used = spec.model_id
            if model_used == "auto":
                model_used = getattr(adapter, "last_model", "") or model_used
            # Only hops that happened BEFORE the winner are fallbacks; skips
            # recorded below the winner were never reached, not fallen back.
            before_winner = order.index(spec.name)
            relevant = [h for h in hops
                        if h.provider in order
                        and order.index(h.provider) < before_winner]
            result = RouteResult(
                ok=True, text=text, provider=spec.name, model=model_used,
                task=options.task, hops=relevant,
                latency_s=self._clock() - t0,
                explanation=build_explanation(spec.name, relevant),
            )
            self._journal(options, result, t0, prompt_chars)
            return result

        # everything failed
        # Total failure: the story must cover EVERY capable model, including
        # the ones this task's capability chain never reached (a DIFFICULT
        # ask does not consider the local 3B, a PRIVATE ask never leaves the
        # machine) — "no one answered" only means something when "no one"
        # is the whole registry. See test_never_raises_explanation_names_
        # every_hop.
        hopped: set[str] = {h.provider for h in hops}
        tried: set[str] = {spec.name for spec in plan}
        for spec in self.registry.all():
            if spec.name in hopped or spec.name in tried:
                continue
            hops.append(RouteHop(spec.name, spec.model_id, "skipped",
                                 "not capable for this task"))

        result = RouteResult(
            ok=False, task=options.task, hops=hops,
            latency_s=self._clock() - t0,
            explanation=build_explanation(None, hops)
            or f"No provider could serve this task ({last_error or 'unknown'}).")
        self._journal(options, result, t0, prompt_chars, error=last_error)
        return result

    # ── status ────────────────────────────────────────────────────────────
    def status(self) -> dict:
        """Snapshot for dashboards: mode, plan per task class, provider
        health. No secrets — names and states only."""
        providers = sorted({s.provider for s in self.registry.all()})
        return {
            "mode": self.mode,
            "providers": self.health.snapshot(providers),
            "models": self.registry.describe(),
        }

    # ── journaling ────────────────────────────────────────────────────────
    def _journal(self, options: RouteOptions, result: RouteResult,
                 t0: float, prompt_chars: int, error: str = "",
                 tokens: Optional[dict] = None) -> None:
        try:
            self.usage.record(
                task=options.task.value,
                mode=self.mode,
                provider=result.provider,
                model=result.model,
                ok=result.ok,
                latency_s=result.latency_s or (self._clock() - t0),
                prompt_chars=prompt_chars,
                tokens=tokens,
                hops=[h.to_dict() for h in result.hops],
                error=error,
            )
        except Exception as exc:                    # never break a turn
            log.debug("usage journal skipped: %s", exc)
