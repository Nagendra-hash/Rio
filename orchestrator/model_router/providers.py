"""
orchestrator/model_router/providers.py — provider adapters.

Each adapter wraps one provider behind the same tiny contract:

    is_configured()  — is there anything to talk to? (config/files only,
                       NEVER a network call, NEVER an assumption)
    probe()          — cheap health check → (ok, reason)
    complete(req)    — one prompt → one answer; raises TYPED errors

Typed errors are the fallback language — the router reads them to decide
how long to cool the provider down:

    ProviderError          base
    ├── NotConfiguredError  nothing set up → skip silently, no cooldown
    ├── UnavailableError    service down / connection refused → short cooldown
    ├── TimeoutError        hung call → medium cooldown
    ├── RateLimitedError    429 / quota → long park (Retry-After honored)
    └── AuthError           bad key → long cooldown, skip other attempts

Secrets flow in via constructor/config only — nothing here reads, prints or
logs a key. Local adapter runs through core/llm_local.py (the RAM-guarded
Qwen2.5-Coder 3B GGUF); the OpenAI-compatible adapters reuse the wire format
core/llm_router.py already speaks.
"""
from __future__ import annotations

import json
import logging
import re
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("jarvis.model_router")


# ── typed errors (the fallback vocabulary) ───────────────────────────────────

class ProviderError(RuntimeError):
    """Base for every provider failure."""


class NotConfiguredError(ProviderError):
    """Nothing set up for this provider — skip, do not cool down."""


class UnavailableError(ProviderError):
    """The service is down / refused the connection / 5xx."""


class ProviderTimeoutError(ProviderError):
    """The call hung past its deadline."""


class RateLimitedError(ProviderError):
    """429 / quota / RESOURCE_EXHAUSTED. Carries Retry-After when sent."""

    def __init__(self, message: str, retry_after_s: Optional[float] = None):
        super().__init__(message)
        self.retry_after_s = retry_after_s


class AuthError(ProviderError):
    """401/403 — the credential is bad. Do not hammer the provider."""


class EmptyAnswerError(ProviderError):
    """Answered, but with nothing usable."""


# ── error classification (mirror core/llm_router's field-tested sets) ────────

_QUOTA_MARKERS = ("429", "resource_exhausted", "quota", "rate limit",
                  "rate_limit", "too many requests")
_TRANSIENT_MARKERS = ("500", "502", "503", "unavailable", "overloaded",
                      "high demand", "internal", "deadline", "timed out",
                      "timeout", "connection reset", "aborted")
_AUTH_MARKERS = ("401", "403", "api key", "api_key", "unregistered",
                 "permission", "unauthorized", "invalid_grant")


def classify_error(exc: BaseException) -> str:
    """"rate_limited" | "auth" | "timeout" | "transient" | "unavailable".

    Typed errors win; bare exceptions are classified by message markers so
    SDK exceptions land in the right cooldown bucket too."""
    if isinstance(exc, RateLimitedError):
        return "rate_limited"
    if isinstance(exc, AuthError):
        return "auth"
    if isinstance(exc, ProviderTimeoutError):
        return "timeout"
    if isinstance(exc, NotConfiguredError):
        return "not_configured"
    text = str(exc).lower()
    if any(m in text for m in _QUOTA_MARKERS):
        return "rate_limited"
    if any(m in text for m in _AUTH_MARKERS):
        return "auth"
    if any(m in text for m in ("timed out", "timeout", "deadline")):
        return "timeout"
    if any(m in text for m in _TRANSIENT_MARKERS):
        return "transient"
    return "unavailable"


def cooldown_for(kind: str) -> float:
    from orchestrator.model_router import health as H
    return {
        "rate_limited": H.RATE_LIMIT_COOLDOWN_S,
        "auth": H.AUTH_COOLDOWN_S,
        "timeout": H.TIMEOUT_COOLDOWN_S,
        "transient": H.TRANSIENT_COOLDOWN_S,
        "unavailable": H.UNAVAILABLE_COOLDOWN_S,
        "not_configured": 0.0,
    }.get(kind, H.UNAVAILABLE_COOLDOWN_S)


# ── the request envelope ─────────────────────────────────────────────────────

class CompletionRequest:
    """One prompt → one answer.

    Attributes:
        prompt: the full user prompt (system + ask already merged by the
            router; adapters stay dumb).
        model_id: which model the adapter should use (may be "auto" for
            pool-style providers).
        max_tokens: output budget hint.
        timeout_s: hard per-call deadline; adapters must honor it.
        metadata: free-form (task name, privacy label, …) — never logged raw.
    """

    def __init__(self, prompt: str, model_id: str = "auto",
                 max_tokens: Optional[int] = None, timeout_s: float = 60.0,
                 metadata: Optional[dict[str, Any]] = None) -> None:
        self.prompt = prompt
        self.model_id = model_id
        self.max_tokens = max_tokens
        self.timeout_s = float(timeout_s)
        self.metadata = metadata or {}


# ── adapters ─────────────────────────────────────────────────────────────────

class ProviderAdapter(ABC):
    """One provider. Subclasses set `name` and implement the three methods."""

    name: str = "provider"

    @abstractmethod
    def is_configured(self) -> bool: ...

    @abstractmethod
    def probe(self) -> tuple[bool, str]: ...

    @abstractmethod
    def complete(self, request: CompletionRequest) -> str: ...

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"<{type(self).__name__} {self.name}>"


class LocalQwenAdapter(ProviderAdapter):
    """The LOCAL rung: Qwen2.5-Coder 3B GGUF via core/llm_local.py.

    Configured = the GGUF is on disk. Probing does NOT load the model (a
    cold load costs ~30 s and ~2 GB RAM) — it only checks disk + RAM head
    room. RAM failure is reported as down-with-reason so the router can
    say WHY it fell back.
    """

    name = "local"

    def __init__(self) -> None:
        self._llm_local = None

    def _module(self):
        if self._llm_local is None:
            from core import llm_local            # lazy: stdlib-only import rule
            self._llm_local = llm_local
        return self._llm_local

    def is_configured(self) -> bool:
        try:
            return bool(self._module().is_configured())
        except Exception:
            return False

    def probe(self) -> tuple[bool, str]:
        try:
            llm_local = self._module()
        except Exception as exc:
            return False, f"core.llm_local import failed: {type(exc).__name__}"
        if not llm_local.is_configured():
            return False, "model GGUF not on disk"
        if not llm_local.ram_ok():
            return False, (f"not enough free RAM "
                           f"({llm_local._available_ram_mb()} MB available)")
        return True, "ready"

    def complete(self, request: CompletionRequest) -> str:
        llm_local = self._module()
        if not llm_local.is_configured():
            raise NotConfiguredError("local model GGUF not on disk")
        max_tokens = request.max_tokens or (
            700 if request.metadata.get("task") == "simple_coding" else 350)
        import threading
        result: dict = {}

        def _run() -> None:
            try:
                result["text"] = llm_local.generate(
                    request.prompt, max_tokens=max_tokens)
            except BaseException as exc:          # surfaced in the joiner
                result["error"] = exc

        # llm_local.generate has its own lock and no timeout knob; run it in
        # a watcher thread and abandon it past the deadline. The generation
        # may still finish in the background (harmless — result is discarded);
        # the ROUTER moves on on time.
        worker = threading.Thread(target=_run, daemon=True,
                                  name="model-router-local")
        worker.start()
        worker.join(request.timeout_s)
        if worker.is_alive():
            raise ProviderTimeoutError(
                f"local model exceeded {request.timeout_s:.0f}s deadline")
        if "error" in result:
            raise UnavailableError(
                f"local generation failed: {result['error']}")
        text = (result.get("text") or "").strip()
        if not text:
            raise EmptyAnswerError("local model returned nothing")
        return text


class _OpenAICompatAdapter(ProviderAdapter):
    """Shared OpenAI-compatible HTTP plumbing (FreeLLMAPI / OpenRouter)."""

    def __init__(self, api_key: str, base_url: str, models: list[str],
                 timeout_s: float = 60.0) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._models = list(models) or ["auto"]
        self._timeout_s = timeout_s

    # -- configured: key present? (never a network call) --
    def is_configured(self) -> bool:
        return bool((self._api_key or "").strip())

    def probe(self) -> tuple[bool, str]:
        if not self.is_configured():
            return False, "not configured (no API key)"
        try:
            import requests
        except Exception as exc:
            return False, f"requests unavailable: {type(exc).__name__}"
        try:
            r = requests.get(self._models_url(),
                             headers=self._headers(),
                             timeout=3.0)
            if r.status_code in (401, 403):
                return False, "auth rejected (key invalid)"
            r.raise_for_status()
            return True, "reachable"
        except Exception as exc:
            return False, f"unreachable ({str(exc)[:80]})"

    def _models_url(self) -> str:
        """Catalog endpoint for probes. The completion base ends in
        /chat/completions — stripping only the last path segment used to
        produce /v1/chat/models (404), which marked two healthy providers
        unreachable and starved the fallback chain (fixed 2026-10-08)."""
        base = self._base_url
        for suffix in ("/chat/completions", "/completions"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        else:
            base = base.rsplit("/", 1)[0]
        return base.rstrip("/") + "/models"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json"}

    def complete(self, request: CompletionRequest) -> str:
        if not self.is_configured():
            raise NotConfiguredError(f"{self.name}: not configured")
        import requests
        model = request.model_id if request.model_id and \
            request.model_id != "auto" else self._models[0]
        payload = {"model": model,
                   "messages": [{"role": "user", "content": request.prompt}]}
        if request.max_tokens:
            payload["max_tokens"] = request.max_tokens
        try:
            r = requests.post(self._base_url, headers=self._headers(),
                              json=payload, timeout=request.timeout_s)
        except requests.exceptions.Timeout as exc:
            raise ProviderTimeoutError(
                f"{self.name} timed out after {request.timeout_s:.0f}s") from exc
        except requests.exceptions.RequestException as exc:
            raise UnavailableError(
                f"{self.name} unreachable: {str(exc)[:120]}") from exc
        if r.status_code == 429:
            retry_after = None
            try:
                retry_after = float(r.headers.get("retry-after", ""))
            except (TypeError, ValueError):
                pass
            raise RateLimitedError(
                f"{self.name} rate limited (HTTP 429)",
                retry_after_s=(retry_after if retry_after and retry_after > 0
                               else None))
        if r.status_code in (401, 403):
            raise AuthError(f"{self.name} rejected the key (HTTP "
                            f"{r.status_code})")
        if r.status_code >= 500:
            raise UnavailableError(f"{self.name} server error "
                                   f"(HTTP {r.status_code})")
        if r.status_code >= 400:
            raise UnavailableError(f"{self.name} rejected the request "
                                   f"(HTTP {r.status_code}: {r.text[:120]})")
        try:
            data = r.json()
            text = (data.get("choices") or [{}])[0].get("message", {}) \
                .get("content", "")
        except Exception as exc:
            raise UnavailableError(
                f"{self.name} returned malformed JSON: {str(exc)[:80]}") from exc
        if not (text or "").strip():
            raise EmptyAnswerError(f"{self.name} returned an empty answer")
        return text


class FreeLLMAdapter(_OpenAICompatAdapter):
    """The local FreeLLMAPI free-endpoint pool (127.0.0.1:3001)."""

    name = "freellm"

    def __init__(self, api_key: str = "",
                 base_url: str = "http://127.0.0.1:3001/v1/chat/completions",
                 timeout_s: float = 60.0) -> None:
        super().__init__(api_key, base_url, ["auto"], timeout_s)


class OpenRouterAdapter(_OpenAICompatAdapter):
    """OpenRouter free models — a task-aware chain ending in the documented
    `openrouter/free` router id. Free only: `openrouter/auto` can pick PAID
    models and is never used here."""

    name = "openrouter"

    # Refreshed against the live catalog 2026-09 (same list core/llm_router
    # uses); stale ids simply 404 and the chain falls through.
    DEFAULT_FREE_CHAIN = [
        "z-ai/glm-5.2:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
        "nvidia/nemotron-3.5-lightning:free",
        "openrouter/free",
    ]

    def __init__(self, api_key: str = "", models: Optional[list[str]] = None,
                 timeout_s: float = 60.0) -> None:
        super().__init__(api_key, "https://openrouter.ai/api/v1/chat/completions",
                         models or list(self.DEFAULT_FREE_CHAIN), timeout_s)

    def _attempt_chain(self, request: CompletionRequest) -> str:
        """Walk the free-model chain, one HTTP call per model. Rate limits
        and auth errors are re-raised immediately — the router owns the
        cooldown decision, and hammering the next model on the same 429 is
        exactly what the cooldown exists to prevent."""
        last: Optional[ProviderError] = None
        for model in self._models:
            try:
                return self._send_one(request, model)
            except (RateLimitedError, AuthError):
                raise
            except ProviderError as exc:
                last = exc
                continue
        raise last or UnavailableError("openrouter chain empty")

    def _send_one(self, request: CompletionRequest, model: str) -> str:
        import requests
        payload = {"model": model,
                   "messages": [{"role": "user", "content": request.prompt}]}
        if request.max_tokens:
            payload["max_tokens"] = request.max_tokens
        try:
            r = requests.post(self._base_url, headers=self._headers(),
                              json=payload, timeout=request.timeout_s)
        except requests.exceptions.Timeout as exc:
            raise ProviderTimeoutError(
                f"openrouter {model} timed out") from exc
        except requests.exceptions.RequestException as exc:
            raise UnavailableError(
                f"openrouter unreachable: {str(exc)[:100]}") from exc
        if r.status_code == 429:
            try:
                retry_after = float(r.headers.get("retry-after", ""))
            except (TypeError, ValueError):
                retry_after = 0.0
            raise RateLimitedError(f"openrouter {model} rate limited (429)",
                                   retry_after_s=retry_after or None)
        if r.status_code in (401, 403):
            raise AuthError(f"openrouter rejected the key ({r.status_code})")
        if r.status_code >= 400:
            raise UnavailableError(
                f"openrouter {model}: HTTP {r.status_code}")
        try:
            data = r.json()
            text = (data.get("choices") or [{}])[0].get("message", {}) \
                .get("content", "")
        except Exception as exc:
            raise UnavailableError(
                f"openrouter malformed JSON: {str(exc)[:80]}") from exc
        if not (text or "").strip():
            raise EmptyAnswerError(f"openrouter {model} empty answer")
        return text

    def complete(self, request: CompletionRequest) -> str:
        if not self.is_configured():
            raise NotConfiguredError("openrouter: not configured")
        return self._attempt_chain(request)


class NvidiaAdapter(_OpenAICompatAdapter):
    """NVIDIA NIM (build.nvidia.com) — OpenAI-compatible, free dev tier.

    The preferred model is **DeepSeek-V4.1-Flash**
    (`deepseek-ai/deepseek-v4.1-flash`), NVIDIA's coding specialist and the
    house pick for the coding agent's SIMPLE_CODING asks. The free tier is
    genuinely flaky for the big models: DeepSeek-V4.1-Flash was in the live
    catalog on 2026-10-18 but returned zero bytes for minutes, so this adapter
    walks its chain with three safeguards:

      * equal time SLICES — one slow model cannot eat the whole deadline
        (the working tail still answers inside the router's timeout);
      * short-term model memory — a model that times out is skipped for
        `MODEL_COOLDOWN_S`, so only the FIRST call pays the dead-model tax;
        the order recovers by itself after the cooldown.
    """

    name = "nvidia"

    BASE_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
    DEFAULT_MODELS = [
        "deepseek-ai/deepseek-v4.1-flash",      # coding specialist (preferred)
        "nvidia/nemotron-3-super-120b-a12b",    # fast, reliable tail
        "openai/gpt-oss-20b",
    ]
    MODEL_COOLDOWN_S = 600.0

    def __init__(self, api_key: str = "", models: Optional[list[str]] = None,
                 timeout_s: float = 90.0) -> None:
        super().__init__(api_key, self.BASE_URL,
                         models or list(self.DEFAULT_MODELS), timeout_s)
        self._skip_until: dict[str, float] = {}
        self.last_model: str = self._models[0] if self._models else "auto"

    def _live_order(self) -> list[str]:
        """Configured models, responsive ones first. If everything is cooling
        the full list is returned anyway — a cooldown is a hint, not a wall."""
        now = time.monotonic()
        live = [m for m in self._models if self._skip_until.get(m, 0.0) <= now]
        cooling = [m for m in self._models if self._skip_until.get(m, 0.0) > now]
        return live + cooling if live else list(self._models)

    def complete(self, request: CompletionRequest) -> str:
        if not self.is_configured():
            raise NotConfiguredError("nvidia: not configured")
        # The router names one model: call exactly that (no walking).
        if request.model_id and request.model_id != "auto":
            return super().complete(request)

        order = self._live_order()
        # Equal slices bounded by the caller's deadline: a dead head model
        # costs 1/N of the budget, never all of it.
        slice_s = max(5.0, request.timeout_s / max(1, len(order)))
        last: Optional[ProviderError] = None
        for model in order:
            probe = CompletionRequest(
                request.prompt, model_id=model, max_tokens=request.max_tokens,
                timeout_s=min(slice_s, request.timeout_s),
                metadata=request.metadata)
            try:
                text = super().complete(probe)
                self.last_model = model
                self._skip_until.pop(model, None)   # responsive again
                return text
            except (RateLimitedError, AuthError):
                raise                                # provider-wide; router cools
            except ProviderError as exc:
                # A model that timed out / 404s is parked briefly so the NEXT
                # call goes straight to something that answers.
                self._skip_until[model] = time.monotonic() + self.MODEL_COOLDOWN_S
                last = exc
                continue
        raise last or UnavailableError("nvidia chain empty")


class DeepSeekAdapter(_OpenAICompatAdapter):
    """DeepSeek coders, reached through OpenRouter. The house coding head.

    NVIDIA NIM's `deepseek-ai/deepseek-v4.1-flash` trial endpoint stalls
    (0-byte responses for minutes — a service-side issue, not the key), while
    OpenRouter answers the same family in ~1–5 s. So the WORKING DeepSeek is
    reached here, ahead of the NVIDIA NIM step in the coding chain.

    `deepseek-chat` leads: it returns clean Kotlin/Python. The v4.1-flash id
    trails as best-effort — verified live 2026-10-18 it intermittently answers
    with empty content, and this adapter skips a model that answers empty.

    Two house safeguards, mirroring NvidiaAdapter:
      * a BOUNDED completion cap — OpenRouter credit-checks against a model's
        DEFAULT max (131072 for v4.1-flash) and 402s a low-balance key even
        when the request is modest (measured 2026-10-18);
      * short-term model memory — an empty/timed-out model is skipped for
        `MODEL_COOLDOWN_S`, so only the first call pays the dead-model tax.
    """

    name = "deepseek"

    BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
    DEFAULT_MODELS = [
        "deepseek/deepseek-chat",               # reliable coder (v3), verified live
        "deepseek/deepseek-v4.1-flash",         # best-effort; may answer empty
    ]
    MODEL_COOLDOWN_S = 600.0
    DEFAULT_MAX_TOKENS = 4096

    def __init__(self, api_key: str = "", models: Optional[list[str]] = None,
                 timeout_s: float = 75.0) -> None:
        super().__init__(api_key, self.BASE_URL,
                         models or list(self.DEFAULT_MODELS), timeout_s)
        self._skip_until: dict[str, float] = {}
        self.last_model: str = self._models[0] if self._models else "auto"

    def _live_order(self) -> list[str]:
        now = time.monotonic()
        live = [m for m in self._models if self._skip_until.get(m, 0.0) <= now]
        cooling = [m for m in self._models if self._skip_until.get(m, 0.0) > now]
        return live + cooling if live else list(self._models)

    def complete(self, request: CompletionRequest) -> str:
        if not self.is_configured():
            raise NotConfiguredError("deepseek: not configured")
        # The router names one model: call exactly that (no walking).
        if request.model_id and request.model_id != "auto":
            return super().complete(request)

        cap = request.max_tokens or self.DEFAULT_MAX_TOKENS
        order = self._live_order()
        slice_s = max(5.0, request.timeout_s / max(1, len(order)))
        last: Optional[ProviderError] = None
        for model in order:
            probe = CompletionRequest(
                request.prompt, model_id=model, max_tokens=cap,
                timeout_s=min(slice_s, request.timeout_s),
                metadata=request.metadata)
            try:
                text = super().complete(probe)
                self.last_model = model
                self._skip_until.pop(model, None)   # responsive again
                return text
            except (RateLimitedError, AuthError):
                raise                                # provider-wide; router cools
            except ProviderError as exc:
                self._skip_until[model] = time.monotonic() + self.MODEL_COOLDOWN_S
                last = exc
                continue
        raise last or UnavailableError("deepseek chain empty")


class GeminiAdapter(ProviderAdapter):
    """Gemini via google-genai. The SPECIALIST: difficult reasoning and
    research — and the last resort when everything cheaper failed.

    Configured = at least one API key in config/api_keys.json
    (gemini_api_key / gemini_api_keys). Never assumes availability;
    quota (429 / RESOURCE_EXHAUSTED) raises RateLimitedError with
    Retry-After honored, transient 503s raise UnavailableError.
    """

    name = "gemini"

    def __init__(self, api_keys: Optional[list[str]] = None,
                 model_id: str = "gemini-flash-latest",
                 timeout_s: float = 60.0) -> None:
        self._keys = [k for k in (api_keys or []) if (k or "").strip()]
        self._model_id = model_id
        self._timeout_s = timeout_s

    @classmethod
    def from_config(cls, config: dict) -> "GeminiAdapter":
        keys: list[str] = []
        primary = (config.get("gemini_api_key") or "").strip()
        if primary:
            keys.append(primary)
        extra = config.get("gemini_api_keys") or []
        if isinstance(extra, str):
            extra = [extra]
        for k in extra:
            k = (k or "").strip()
            if k and k not in keys:
                keys.append(k)
        return cls(api_keys=keys)

    def is_configured(self) -> bool:
        return bool(self._keys)

    def probe(self) -> tuple[bool, str]:
        if not self.is_configured():
            return False, "not configured (no Gemini API key)"
        try:
            import google.genai                  # noqa: F401
            return True, "configured"
        except Exception as exc:
            return False, f"google-genai not importable: {type(exc).__name__}"

    def complete(self, request: CompletionRequest) -> str:
        if not self.is_configured():
            raise NotConfiguredError("gemini: no API key configured")
        from google import genai
        from google.genai import types as genai_types
        last: Optional[Exception] = None
        for key in self._keys:
            try:
                client = genai.Client(api_key=key)
                resp = client.models.generate_content(
                    model=self._model_id,
                    contents=request.prompt,
                    config=genai_types.GenerateContentConfig(
                        max_output_tokens=request.max_tokens or 1024,
                    ),
                )
                text = (getattr(resp, "text", "") or "").strip()
                if not text:
                    raise EmptyAnswerError("gemini returned an empty answer")
                return text
            except RateLimitedError:
                raise
            except Exception as exc:
                kind = classify_error(exc)
                if kind == "rate_limited":
                    retry_after = _gemini_retry_after(exc)
                    last = RateLimitedError(
                        f"gemini quota exhausted: {str(exc)[:100]}",
                        retry_after_s=retry_after)
                    # Quota is per project/key — try the next key if one exists.
                    continue
                if kind == "auth":
                    last = AuthError(f"gemini rejected the key: {str(exc)[:100]}")
                    continue
                if kind == "transient":
                    last = UnavailableError(
                        f"gemini transient error: {str(exc)[:100]}")
                    continue
                raise
        if last:
            raise last
        raise UnavailableError("gemini: no key attempted")


def _gemini_retry_after(exc: Exception) -> Optional[float]:
    """Pull a Retry-After / retry delay out of a google-genai exception."""
    details = getattr(exc, "details", None)
    retry = None
    if details is not None:
        retry = getattr(details, "retry_delay", None)
        if retry is not None:
            try:
                retry = float(retry.seconds)
            except Exception:
                retry = None
    if retry is None:
        m = re.search(r"retry[^\d]{0,20}(\d+)", str(exc).lower())
        retry = float(m.group(1)) if m else None
    return retry


# ── config loader (shared by the router's default builder) ──────────────────

def load_provider_config(config_path: str | Path) -> dict:
    """Read config/api_keys.json — keys stay in the config file (gitignored),
    never in source code. Returns {} on any problem. NEVER logs contents."""
    try:
        return json.loads(Path(config_path).read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
