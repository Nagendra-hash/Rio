"""
orchestrator/model_router — the Model Router beneath Jarvis.

GOAL
    Use the cheapest suitable model first and preserve Gemini for difficult
    work. Every inbound ask is classified into a ModelTask, matched against
    the capability registry, and served by the cheapest CONFIGURED + HEALTHY
    + CAPABLE provider, with automatic — and user-visibly EXPLAINED —
    fallback down the chain:

        LOCAL (Qwen2.5-Coder 3B)  →  free APIs (NVIDIA NIM DeepSeek-V4.1-Flash /
                                   FreeLLMAPI / OpenRouter free)
                                  →  Gemini (difficult reasoning / research,
                                     or the last resort)

    Hard rules:
      * PRIVATE asks never leave the machine (local models only, or an
        honest failure).
      * Never assume an API is available — configuration is detected, never
        assumed, and absence degrades gracefully with a reason.
      * Rate limits park a provider (Retry-After aware), timeouts cool it,
        health probes are cached, and every route is journaled without
        secrets.

LAYERS
    capabilities.py  — ModelSpec registry: what each model CAN do, cost,
                       privacy (pure data, offline).
    providers.py     — one adapter per provider, typed errors, config-only
                       secrets.
    health.py        — probe cache + cooldown board (rate-limit parking).
    usage.py         — JSONL usage journal, redacted, secret-free.
    router.py        — ModelRouter: plan → try → cool → fall back → explain.

    The Jarvis orchestrator (orchestrator/orchestrator.py) stays the front
    door; this package is the model-selection layer it calls into.

NOT INSTALLED BY DEFAULT
    Wiring this into the live Jarvis turn path is an explicit opt-in — the
    existing Gemini path and core/llm_router.py are untouched until
    something calls ModelRouter.route() directly (same shadow-by-default
    contract as the Phase-1 orchestrator).
"""

from orchestrator.model_router.capabilities import (
    CapabilityRegistry,
    ModelSpec,
    ModelTask,
    Privacy,
    TIER_FREE,
    TIER_GEMINI,
    TIER_LOCAL,
    TIER_PAID,
    build_default_registry,
    resolve_rio_task,
)
from orchestrator.model_router.health import (
    HealthBoard,
    ProviderHealth,
)
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
    OllamaAdapter,
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
from orchestrator.model_router.router import (
    DEFAULT_DEADLINE_S,
    DEFAULT_TIMEOUT_S,
    ModelRouter,
    RouteHop,
    RouteOptions,
    RouteResult,
    build_explanation,
)
from orchestrator.model_router.usage import UsageJournal, redact

__version__ = "1.0.0"

__all__ = [
    # capabilities
    "CapabilityRegistry",
    "ModelSpec",
    "ModelTask",
    "Privacy",
    "TIER_LOCAL",
    "TIER_FREE",
    "TIER_PAID",
    "TIER_GEMINI",
    "build_default_registry",
    # health
    "HealthBoard",
    "ProviderHealth",
    # providers
    "AuthError",
    "CompletionRequest",
    "EmptyAnswerError",
    "FreeLLMAdapter",
    "GeminiAdapter",
    "LocalQwenAdapter",
    "NotConfiguredError",
    "NvidiaAdapter",
    "OllamaAdapter",
    "DeepSeekAdapter",
    "OpenRouterAdapter",
    "ProviderAdapter",
    "ProviderError",
    "ProviderTimeoutError",
    "RateLimitedError",
    "UnavailableError",
    "classify_error",
    "cooldown_for",
    "load_provider_config",
    # router
    "DEFAULT_DEADLINE_S",
    "DEFAULT_TIMEOUT_S",
    "ModelRouter",
    "RouteHop",
    "RouteOptions",
    "RouteResult",
    "build_explanation",
    # usage
    "UsageJournal",
    "redact",
    "__version__",
]
