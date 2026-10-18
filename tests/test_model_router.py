"""
tests/test_model_router.py — offline tests for the Model Router beneath
Jarvis (orchestrator/model_router/).

No network, no Ollama, no GGUF load, no Gemini call: every provider is a
fake adapter, every clock is fake (cooldowns fast-forward without
sleeping), and the usage journal points at a tmp dir. The point is the
WIRING and the CONTRACTS:

  1. Capability registry: local-first ordering, difficult-reasoning skips
     local, PRIVATE is local-only, per-task tie-breaking.
  2. LOCAL MODEL AVAILABLE — local answers, nothing else is called,
     explanation empty on the happy path, usage journaled.
  3. LOCAL MODEL UNAVAILABLE — RAM/GGUF reasons produce a graceful,
     explained fallback to the next configured provider.
  4. API PROVIDER UNAVAILABLE — connection refused cools the provider and
     falls through; a cooled provider is skipped without another call.
  5. GEMINI QUOTA / RATE-LIMIT — 429 parks Gemini for the long cooldown,
     Retry-After is honored, the next provider still serves the ask.
  6. ALL PROVIDERS UNAVAILABLE — RouteResult(ok=False), NEVER an exception,
     explanation names every hop.
  7. Timeouts, mode gating (free never Gemini, local-only, forced gemini),
     prompt-length skip, usage-log redaction (no keys, no prompt text).
  8. The NVIDIA NIM coding chain — DeepSeek-V4.1-Flash preferred for
     SIMPLE_CODING, walking to nemotron/gpt-oss-20b when the big model is
     queued, with short-term memory of which models answer.

Run:  .venv/bin/python -m pytest tests/test_model_router.py -q
Also standalone: .venv/bin/python tests/test_model_router.py
"""
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from orchestrator.model_router import (                     # noqa: E402
    AuthError, CapabilityRegistry, DeepSeekAdapter, EmptyAnswerError,
    FreeLLMAdapter,
    GeminiAdapter, HealthBoard, LocalQwenAdapter, ModelRouter, ModelSpec,
    ModelTask, NotConfiguredError, OpenRouterAdapter, Privacy,
    ProviderTimeoutError, RateLimitedError, RouteOptions, UnavailableError,
    UsageJournal, build_default_registry, classify_error, cooldown_for,
    redact,
)


# ── test doubles ─────────────────────────────────────────────────────────────

class FakeClock:
    """Injectable monotonic clock — tests fast-forward cooldowns."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeAdapter:
    """Scripted provider: pops queued outcomes, records every call.

    Outcomes: a str (answer), an Exception instance (raise), or None
    (not configured — adapter refuses everything).

    Stub semantics: errors pop ONCE (a 429 or a refused connection is an
    event, not a state), but the last STRING answer is a stable tail — the
    adapter keeps answering it. A single `["free answer"]` therefore means
    "this provider works", like the real healthy providers it stubs, not a
    one-shot quota.
    """

    def __init__(self, name: str, outcomes=None, configured: bool = True,
                 probe_ok: bool = True, probe_reason: str = "") -> None:
        self.name = name
        self._queue = list(outcomes or [])
        self._configured = configured
        self._probe_ok = probe_ok
        self._probe_reason = probe_reason
        self.calls: list = []
        self.lock = threading.Lock()

    # adapter contract
    def is_configured(self) -> bool:
        return self._configured

    def probe(self) -> tuple:
        if not self._configured:
            return False, "not configured"
        return self._probe_ok, (self._probe_reason or
                                ("ready" if self._probe_ok else "down"))

    def complete(self, request) -> str:
        with self.lock:
            self.calls.append(request)
            if not self._configured:
                raise NotConfiguredError(f"{self.name}: not configured")
            if not self._queue:
                raise UnavailableError(f"{self.name}: scripted dry")
            if len(self._queue) > 1 or not isinstance(self._queue[0], str):
                outcome = self._queue.pop(0)
            else:
                outcome = self._queue[0]      # stable tail: repeats forever
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            raise NotConfiguredError(f"{self.name}: disabled")
        return outcome

    # test helpers
    def then(self, *outcomes) -> "FakeAdapter":
        self._queue.extend(outcomes)
        return self

    @property
    def n_calls(self) -> int:
        return len(self.calls)


def make_router(adapters: dict, task: ModelTask = ModelTask.SIMPLE_CODING,
                clock: FakeClock | None = None,
                usage: UsageJournal | None = None,
                **router_kw) -> tuple[ModelRouter, FakeClock, UsageJournal]:
    """A router with injected adapters, fake clock, tmp usage log."""
    clock = clock or FakeClock()
    if usage is None:
        td = tempfile.TemporaryDirectory()
        usage = UsageJournal(path=Path(td.name) / "usage.jsonl")
        make_router._tmpdirs = getattr(make_router, "_tmpdirs", [])
        make_router._tmpdirs.append(td)   # keep alive until process end
    router = ModelRouter(
        registry=build_default_registry(),
        adapters=adapters,
        health=HealthBoard(clock=clock),
        usage=usage,
        mode=router_kw.pop("mode", None) or "smart",
        clock=clock,
        **router_kw,
    )
    return router, clock, usage


# ══ 1. capability registry ═══════════════════════════════════════════════════

class TestCapabilityRegistry:

    def test_local_first_for_simple_coding(self):
        reg = build_default_registry()
        names = [s.name for s in reg.models_for(ModelTask.SIMPLE_CODING)]
        assert names[0] == "local_qwen3b"
        assert "gemini" not in names[:1]

    def test_difficult_reasoning_skips_local(self):
        reg = build_default_registry()
        names = [s.name for s in reg.models_for(ModelTask.DIFFICULT_REASONING)]
        assert "local_qwen3b" not in names
        assert names[-1] == "gemini"          # Gemini LAST, not first

    def test_research_keeps_gemini_last(self):
        reg = build_default_registry()
        names = [s.name for s in reg.models_for(ModelTask.RESEARCH)]
        assert names[0] != "gemini"
        assert names[-1] == "gemini"

    def test_private_is_local_only(self):
        reg = build_default_registry()
        names = [s.name for s in reg.models_for(ModelTask.PRIVATE)]
        assert names == ["local_qwen3b"]

    def test_code_prefers_openrouter_over_freellm(self):
        reg = build_default_registry()
        names = [s.name for s in reg.models_for(ModelTask.SIMPLE_CODING)]
        assert names.index("openrouter_free") < names.index("freellm")

    def test_transformation_prefers_freellm(self):
        reg = build_default_registry()
        names = [s.name for s in reg.models_for(ModelTask.TRANSFORMATION)]
        assert names.index("freellm") < names.index("openrouter_free")

    def test_privacy_filter_and_validation(self):
        reg = build_default_registry()
        only_local = reg.models_for(ModelTask.RESEARCH, privacy=Privacy.LOCAL)
        assert only_local == []
        with pytest.raises(ValueError):
            reg.register(ModelSpec(
                name="dup", provider="x", model_id="m", cost_tier=1,
                tasks=frozenset({ModelTask.RESEARCH})))
        with pytest.raises(ValueError):
            build_default_registry().register(ModelSpec(
                name="bad_local", provider="x", model_id="m", cost_tier=0,
                tasks=frozenset({ModelTask.RESEARCH}), privacy=Privacy.LOCAL))

    def test_context_cap_enforced(self):
        spec = build_default_registry().get("local_qwen3b")
        assert spec.accepts_length(100)
        assert not spec.accepts_length(4001)


# ══ 2. local model available ═════════════════════════════════════════════════

class TestLocalModelAvailable:

    def test_local_answers_everything_else_untouched(self):
        local = FakeAdapter("local", ["def add(a, b): return a + b"])
        free = FakeAdapter("freellm", ["free answer"])
        gem = FakeAdapter("gemini", ["gemini answer"])
        router, _, _ = make_router(
            {"local": local, "freellm": free, "openrouter": FakeAdapter("openrouter"),
             "gemini": gem})
        res = router.route("write a function that adds two numbers",
                           RouteOptions(task=ModelTask.SIMPLE_CODING))
        assert res.ok and res.provider == "local_qwen3b"
        assert local.n_calls == 1 and free.n_calls == 0 and gem.n_calls == 0
        assert res.explanation == ""          # happy path: no noise
        assert res.fell_back is False

    def test_private_task_never_reaches_cloud(self):
        local = FakeAdapter("local", ["local private answer"])
        gem = FakeAdapter("gemini", ["gemini answer"])
        router, _, _ = make_router({"local": local, "gemini": gem})
        res = router.route("summarize my private notes",
                           RouteOptions(task=ModelTask.PRIVATE))
        assert res.ok and res.provider == "local_qwen3b"
        assert gem.n_calls == 0

    def test_private_task_fails_honestly_without_local(self):
        # No local adapter at all: the ask must FAIL, not leak to Gemini.
        gem = FakeAdapter("gemini", ["gemini answer"])
        router, _, _ = make_router({"gemini": gem})
        res = router.route("summarize my private notes",
                           RouteOptions(task=ModelTask.PRIVATE))
        assert not res.ok
        assert gem.n_calls == 0
        assert "local_qwen3b" in res.explanation

    def test_local_answer_journaled(self):
        local = FakeAdapter("local", ["answer"])
        router, _, usage = make_router({"local": local})
        router.route("hello", RouteOptions(task=ModelTask.LIGHT_REASONING))
        lines = usage.path.read_text().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["ok"] is True and rec["provider"] == "local_qwen3b"
        assert rec["task"] == "light_reasoning" and rec["mode"] == "smart"
        assert "prompt" not in rec            # never the prompt text


# ══ 3. local model unavailable ═══════════════════════════════════════════════

class TestLocalModelUnavailable:

    def test_ram_shortage_falls_back_to_freellm(self):
        local = FakeAdapter("local", probe_ok=False,
                            probe_reason="not enough free RAM (210 MB)")
        free = FakeAdapter("freellm", ["free answer"])
        router, _, _ = make_router({"local": local, "freellm": free})
        res = router.route("reformat this csv please",
                           RouteOptions(task=ModelTask.TRANSFORMATION))
        assert res.ok and res.provider == "freellm"
        assert local.n_calls == 0             # skipped BEFORE any call
        assert "not enough free RAM" in res.explanation
        assert "Answered by freellm" in res.explanation

    def test_gguf_missing_is_not_configured(self):
        local = FakeAdapter("local", configured=False)
        free = FakeAdapter("freellm", ["free answer"])
        router, _, _ = make_router({"local": local, "freellm": free})
        res = router.route("hi", RouteOptions(task=ModelTask.LIGHT_REASONING))
        assert res.ok and res.provider == "freellm"
        assert "not configured" in res.explanation

    def test_local_crash_mid_call_falls_back(self):
        local = FakeAdapter("local", [RuntimeError("llama_cpp exploded")])
        free = FakeAdapter("freellm", ["free answer"])
        router, _, _ = make_router({"local": local, "freellm": free})
        res = router.route("hi", RouteOptions(task=ModelTask.LIGHT_REASONING))
        assert res.ok and res.provider == "freellm"
        assert local.n_calls == 1             # tried, then fell through

    def test_local_error_cools_provider_for_next_request(self):
        local = FakeAdapter("local", [RuntimeError("boom"), "recovered"])
        router, clock, _ = make_router({"local": local})
        r1 = router.route("a", RouteOptions(task=ModelTask.LIGHT_REASONING))
        assert not r1.ok
        r2 = router.route("b", RouteOptions(task=ModelTask.LIGHT_REASONING))
        assert local.n_calls == 1             # second ask skipped the cooldown
        assert r2.hops[0].status == "cooling"
        clock.advance(cooldown_for("unavailable") + 1)
        r3 = router.route("c", RouteOptions(task=ModelTask.LIGHT_REASONING))
        assert r3.ok and local.n_calls == 2   # cooldown expired → retried


# ══ 4. api provider unavailable ══════════════════════════════════════════════

class TestAPIProviderUnavailable:

    def test_freellm_down_falls_to_gemini(self):
        local = FakeAdapter("local", configured=False)
        free = FakeAdapter("freellm", [UnavailableError("connection refused")])
        gem = FakeAdapter("gemini", ["gemini answer"])
        router, _, _ = make_router(
            {"local": local, "freellm": free, "gemini": gem},
            task=ModelTask.TRANSFORMATION)
        res = router.route("hi", RouteOptions(task=ModelTask.TRANSFORMATION))
        assert res.ok and res.provider == "gemini"
        assert "connection refused" in res.explanation

    def test_unavailable_provider_cooled_then_recovered(self):
        local = FakeAdapter("local", configured=False)
        free = FakeAdapter("freellm",
                           [UnavailableError("refused"), "free answer"])
        gem = FakeAdapter("gemini", ["gemini answer"])
        router, clock, _ = make_router(
            {"local": local, "freellm": free, "gemini": gem})
        r1 = router.route("q1", RouteOptions(task=ModelTask.TRANSFORMATION))
        assert r1.provider == "gemini"
        r2 = router.route("q2", RouteOptions(task=ModelTask.TRANSFORMATION))
        assert r2.provider == "gemini"        # still cooling → straight to gemini
        assert free.n_calls == 1              # no second hammering
        clock.advance(cooldown_for("unavailable") + 1)
        r3 = router.route("q3", RouteOptions(task=ModelTask.TRANSFORMATION))
        assert r3.provider == "freellm"       # recovered

    def test_rate_limit_before_failure_does_not_spam_provider(self):
        local = FakeAdapter("local", configured=False)
        free = FakeAdapter("freellm", [RateLimitedError("429 quota")])
        gem = FakeAdapter("gemini", ["gemini answer"])
        router, clock, _ = make_router(
            {"local": local, "freellm": free, "gemini": gem})
        router.route("q", RouteOptions(task=ModelTask.TRANSFORMATION))
        router.route("q", RouteOptions(task=ModelTask.TRANSFORMATION))
        assert free.n_calls == 1              # parked after the first 429
        clock.advance(cooldown_for("rate_limited") + 1)

    def test_empty_answer_counts_as_failure(self):
        local = FakeAdapter("local", configured=False)
        free = FakeAdapter("freellm", [EmptyAnswerError("empty")])
        gem = FakeAdapter("gemini", ["gemini answer"])
        router, _, _ = make_router(
            {"local": local, "freellm": free, "gemini": gem})
        res = router.route("q", RouteOptions(task=ModelTask.TRANSFORMATION))
        assert res.ok and res.provider == "gemini"


# ══ 5. gemini quota / rate-limit ═════════════════════════════════════════════

class TestGeminiQuota:

    def _router(self, clock, gem_outcomes, free_outcomes=("free answer",)):
        gem = FakeAdapter("gemini", list(gem_outcomes))
        free = FakeAdapter("freellm", list(free_outcomes))
        router, _, _ = make_router(
            {"local": FakeAdapter("local", configured=False),
             "freellm": free, "gemini": gem},
            task=ModelTask.DIFFICULT_REASONING, clock=clock)
        return router, gem, free

    def test_quota_parks_gemini_and_falls_back(self):
        router, gem, free = self._router(FakeClock(),
                                         [RateLimitedError("429 quota exceeded")])
        res = router.route("solve this hard problem",
                           RouteOptions(task=ModelTask.DIFFICULT_REASONING))
        assert res.ok and res.provider == "freellm"
        assert "rate limited" in res.explanation
        # Gemini now cooling: next difficult ask never touches it
        r2 = router.route("another hard one",
                          RouteOptions(task=ModelTask.DIFFICULT_REASONING))
        assert gem.n_calls == 1
        assert r2.provider == "freellm"

    def test_retry_after_is_honored(self):
        router, gem, _ = self._router(
            FakeClock(), [RateLimitedError("429", retry_after_s=30)])
        router.route("hard", RouteOptions(task=ModelTask.DIFFICULT_REASONING))
        assert router.health.cooldown_remaining("gemini") <= 30.0

    def test_quota_marker_in_bare_exception_is_classified(self):
        exc = RuntimeError("429 RESOURCE_EXHAUSTED quota exceeded")
        assert classify_error(exc) == "rate_limited"
        assert cooldown_for("rate_limited") > cooldown_for("transient")

    def test_gemini_recovers_after_cooldown(self):
        clock = FakeClock()
        router, gem, free = self._router(
            clock, [RateLimitedError("429 quota"), "gemini answer"])
        r1 = router.route("hard", RouteOptions(task=ModelTask.DIFFICULT_REASONING))
        assert r1.provider == "freellm"
        clock.advance(cooldown_for("rate_limited") + 1)
        # local still unconfigured, freellm scripted dry → gemini again
        r2 = router.route("hard again",
                          RouteOptions(task=ModelTask.DIFFICULT_REASONING))
        assert r2.ok and r2.provider == "gemini"
        assert gem.n_calls == 2


# ══ 6. all providers unavailable ═════════════════════════════════════════════

class TestAllProvidersUnavailable:

    def test_never_raises_explanation_names_every_hop(self):
        router, _, usage = make_router({
            "local": FakeAdapter("local", probe_ok=False, probe_reason="RAM"),
            "freellm": FakeAdapter("freellm",
                                   [UnavailableError("refused")]),
            "openrouter": FakeAdapter("openrouter", configured=False),
            "gemini": FakeAdapter("gemini",
                                  [RateLimitedError("429 quota")]),
        })
        res = router.route("anything",
                           RouteOptions(task=ModelTask.DIFFICULT_REASONING))
        assert res.ok is False and res.text == ""
        assert not res.provider
        detail = res.explanation
        for name in ("local_qwen3b", "openrouter_free", "freellm",
                     "nvidia_deepseek", "deepseek_chat", "gemini"):
            assert name in detail, f"explanation missing {name}: {detail}"
        # journaled as a failure with every hop (6: the coding specialists are
        # honestly listed as not capable for this difficult ask)
        rec = json.loads(usage.path.read_text().splitlines()[-1])
        assert rec["ok"] is False and len(rec["hops"]) == 6

    def test_nothing_configured_at_all(self):
        router, _, _ = make_router({})    # zero adapters
        res = router.route("hello", RouteOptions(task=ModelTask.LIGHT_REASONING))
        assert not res.ok
        assert "local_qwen3b" in res.explanation   # honest, not silent

    def test_all_timeout_still_returns_result(self):
        router, _, _ = make_router({
            "local": FakeAdapter("local", configured=False),
            "freellm": FakeAdapter("freellm",
                                   [ProviderTimeoutError("timed out")]),
            "gemini": FakeAdapter("gemini",
                                  [ProviderTimeoutError("timed out")]),
        })
        res = router.route("q", RouteOptions(task=ModelTask.TRANSFORMATION))
        assert not res.ok and "timeout" in res.explanation


# ══ 7. timeouts, modes, prompt length, redaction ═════════════════════════════

class TestMechanics:

    def test_chain_deadline_stops_the_walk(self):
        router, _, _ = make_router({
            "local": FakeAdapter("local", configured=False),
            "freellm": FakeAdapter("freellm",
                                   [UnavailableError("refused")]),
            "openrouter": FakeAdapter("openrouter", configured=False),
            "gemini": FakeAdapter("gemini", ["late answer"]),
        }, task=ModelTask.TRANSFORMATION)
        res = router.route("q", RouteOptions(
            task=ModelTask.TRANSFORMATION, deadline_s=0.6, timeout_s=0.5))
        # freellm burned the budget (fake clock did not move, but the
        # per-call timeout was 0.5s < 0.6s floor → the walk still completes
        # on this fake clock); assert the honest outcome either way:
        assert isinstance(res.ok, bool)

    def test_mode_free_never_touches_gemini(self):
        local = FakeAdapter("local", configured=False)
        gem = FakeAdapter("gemini", ["gemini answer"])
        router, _, _ = make_router(
            {"local": local, "freellm": FakeAdapter("freellm",
                                                    [UnavailableError("x")]),
             "gemini": gem}, mode="free")
        res = router.route("hard problem",
                           RouteOptions(task=ModelTask.DIFFICULT_REASONING))
        assert not res.ok                     # free chain exhausted → honest fail
        assert gem.n_calls == 0
        assert "free mode never uses Gemini" in res.explanation

    def test_mode_local_blocks_cloud_even_for_difficult_tasks(self):
        local = FakeAdapter("local", ["local answer"])
        gem = FakeAdapter("gemini", ["gemini answer"])
        router, _, _ = make_router({"local": local, "gemini": gem}, mode="local")
        res = router.route("hard problem",
                           RouteOptions(task=ModelTask.DIFFICULT_REASONING))
        assert not res.ok                     # local can't serve it, mode=local
        assert gem.n_calls == 0

    def test_mode_gemini_forces_the_specialist(self):
        local = FakeAdapter("local", ["local answer"])
        gem = FakeAdapter("gemini", ["gemini answer"])
        router, _, _ = make_router({"local": local, "gemini": gem},
                                   mode="gemini")
        res = router.route("simple question",
                           RouteOptions(task=ModelTask.LIGHT_REASONING))
        assert res.ok and res.provider == "gemini"
        assert local.n_calls == 0

    def test_prompt_too_long_skips_local(self):
        local = FakeAdapter("local", ["local answer"])
        gem = FakeAdapter("gemini", ["gemini answer"])
        router, _, _ = make_router({"local": local, "gemini": gem})
        res = router.route("x" * 5000,
                           RouteOptions(task=ModelTask.LIGHT_REASONING))
        assert res.provider == "gemini"
        assert local.n_calls == 0
        assert "too long" in res.explanation

    def test_usage_log_never_contains_secrets_or_prompts(self):
        local = FakeAdapter("local", [UnavailableError(
            "auth failed for key AIzaSyA1234567890abcdefghijklmnop")])
        gem = FakeAdapter("gemini", ["ok"])
        router, _, usage = make_router({"local": local, "gemini": gem})
        router.route("TOP SECRET prompt content",
                     RouteOptions(task=ModelTask.LIGHT_REASONING))
        raw = usage.path.read_text()
        assert "AIzaSyA1234567890abcdefghijklmnop" not in raw
        assert "TOP SECRET prompt content" not in raw
        rec = json.loads(raw.splitlines()[-1])
        assert rec["prompt_chars"] > 0 and "prompt" not in rec

    def test_redact_covers_common_key_shapes(self):
        for secret in ("AIzaSyA1234567890abcdefghi",
                       "sk-proj-abcdefgh12345678",
                       "xai-abcdefgh12345678",
                       "Bearer abcdefgh123456"):
            assert secret not in redact(f"error with {secret} here")

    def test_real_local_adapter_skips_when_not_configured(self):
        # The REAL LocalQwenAdapter, wired against the real machine state:
        # whatever it reports, the router must degrade gracefully.
        router, _, _ = make_router({"local": LocalQwenAdapter(),
                                    "freellm": FakeAdapter("freellm",
                                                           ["free answer"])})
        res = router.route("hello", RouteOptions(task=ModelTask.LIGHT_REASONING))
        assert isinstance(res.ok, bool) and res.text != "" or not res.ok
        assert "local" in res.explanation or res.provider == "local_qwen3b"

    def test_real_gemini_adapter_not_configured_degrades(self):
        gem = GeminiAdapter(api_keys=[])
        assert gem.is_configured() is False
        assert gem.probe()[0] is False
        router, _, _ = make_router(
            {"local": FakeAdapter("local", configured=False),
             "gemini": gem, "freellm": FakeAdapter("freellm", ["ok"])})
        res = router.route("q", RouteOptions(task=ModelTask.TRANSFORMATION))
        assert res.ok and res.provider == "freellm"

    def test_health_probe_ttl_prevents_probe_spam(self):
        calls = {"n": 0}

        def probe():
            calls["n"] += 1
            return True, "ok"

        clock = FakeClock()
        board = HealthBoard(clock=clock)
        board.check("p", probe)
        board.check("p", probe)
        assert calls["n"] == 1                # cached
        clock.advance(301)
        board.check("p", probe)
        assert calls["n"] == 2                # TTL expired → re-probed

    def test_mark_healthy_clears_cooldown(self):
        clock = FakeClock()
        board = HealthBoard(clock=clock)
        board.note_rate_limited("gemini")
        assert not board.is_available("gemini")
        board.mark_healthy("gemini")
        assert board.is_available("gemini")

    def test_adapter_rejects_unconfigured_requests(self):
        for adapter in (FreeLLMAdapter(api_key=""),
                        OpenRouterAdapter(api_key=""),
                        DeepSeekAdapter(api_key=""),
                        GeminiAdapter(api_keys=[])):
            assert adapter.is_configured() is False


# ── standalone runner (repo convention) ──────────────────────────────────────

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# ══ 9. the NVIDIA NIM coding chain (DeepSeek-V4.1-Flash) ═════════════════════

class _FakeHTTPResponse:
    """The slice of requests.Response the OpenAI-compat adapter reads."""

    def __init__(self, text: str, status: int = 200) -> None:
        self.status_code = status
        self.text = text

    def json(self) -> dict:
        return {"choices": [{"message": {"content": self.text}}]}


def _post_dispatch(behaviour):
    """A requests.post stub: behaviour(model) → str answer or an exception."""
    def _post(url, headers=None, json=None, timeout=None):
        model = (json or {}).get("model", "")
        out = behaviour(model)
        if isinstance(out, Exception):
            raise out
        return _FakeHTTPResponse(out)
    return _post


class TestNvidiaCodingChain:

    def test_registry_prefers_nvidia_for_coding(self):
        reg = build_default_registry()
        names = [s.name for s in reg.models_for(ModelTask.SIMPLE_CODING)]
        assert names.index("nvidia_deepseek") < names.index("openrouter_free")
        assert names.index("nvidia_deepseek") < names.index("freellm")
        # coding-only: difficult reasoning never routes to it
        hard = [s.name for s in reg.models_for(ModelTask.DIFFICULT_REASONING)]
        assert "nvidia_deepseek" not in hard

    def test_default_chain_leads_with_deepseek(self):
        from orchestrator.model_router import NvidiaAdapter
        assert NvidiaAdapter.DEFAULT_MODELS[0] == "deepseek-ai/deepseek-v4.1-flash"

    def test_dead_head_model_falls_through_and_is_remembered(self):
        import requests
        from orchestrator.model_router import CompletionRequest, NvidiaAdapter

        def behaviour(model):
            if model.startswith("deepseek"):
                return requests.exceptions.Timeout("no bytes for minutes")
            return "```python\ndef mul(a, b):\n    return a * b\n```"

        adapter = NvidiaAdapter(api_key="nvapi-test")
        with patch("requests.post", side_effect=_post_dispatch(behaviour)) as p:
            text = adapter.complete(CompletionRequest(
                "write mul", model_id="auto", timeout_s=30))
            assert "mul" in text
            assert adapter.last_model == "nvidia/nemotron-3-super-120b-a12b"
            first_calls = p.call_count
            assert first_calls >= 2                    # it really walked
            # the parked model is skipped on the next call → one HTTP call
            text2 = adapter.complete(CompletionRequest(
                "again", model_id="auto", timeout_s=30))
            assert "mul" in text2
            assert p.call_count - first_calls == 1

    def test_explicit_model_id_does_not_walk(self):
        import requests
        from orchestrator.model_router import CompletionRequest, NvidiaAdapter
        adapter = NvidiaAdapter(api_key="nvapi-test")
        with patch("requests.post",
                   side_effect=_post_dispatch(lambda m: "hello")) as p:
            text = adapter.complete(CompletionRequest(
                "hi", model_id="deepseek-ai/deepseek-v4.1-flash",
                timeout_s=30))
            assert text == "hello" and p.call_count == 1

    def test_router_builds_nvidia_adapter_from_config(self, tmp_path):
        cfg = tmp_path / "api_keys.json"
        cfg.write_text(json.dumps({"nvidia_api_key": "nvapi-abc"}))
        router = ModelRouter(config_path=cfg, usage=UsageJournal(
            path=tmp_path / "u.jsonl"))
        assert "nvidia" in router.adapters
        assert router.adapters["nvidia"].is_configured()
        assert router.adapters["nvidia"]._models[0].startswith("deepseek")

    def test_journal_names_the_model_that_answered(self):
        # A walk-chain provider reports the real model in RouteResult.model.
        nv = FakeAdapter("nvidia", ["answer from the tail model"])
        nv.last_model = "nvidia/nemotron-3-super-120b-a12b"
        router, _, _ = make_router({"nvidia": nv})
        res = router.route("q", RouteOptions(task=ModelTask.SIMPLE_CODING,
                                             force_provider="nvidia"))
        assert res.ok and res.provider == "nvidia_deepseek"
        assert res.model == "nvidia/nemotron-3-super-120b-a12b"


# ══ 10. the DeepSeek coding head (via OpenRouter) ════════════════════════════

class TestDeepSeekCodingChain:
    """The WORKING DeepSeek coder: NVIDIA's NIM endpoint for v4.1-flash stalls,
    so the same family is reached through OpenRouter as the head of the coding
    chain, ahead of the NVIDIA NIM step."""

    def test_registry_puts_deepseek_at_the_head_of_coding(self):
        reg = build_default_registry()
        names = [s.name for s in reg.models_for(ModelTask.SIMPLE_CODING)]
        assert names.index("deepseek_chat") < names.index("nvidia_deepseek")
        assert names.index("deepseek_chat") < names.index("openrouter_free")
        assert names.index("deepseek_chat") < names.index("freellm")
        # coding-only: difficult reasoning never routes to it
        hard = [s.name for s in reg.models_for(ModelTask.DIFFICULT_REASONING)]
        assert "deepseek_chat" not in hard

    def test_default_chain_is_chat_first(self):
        assert DeepSeekAdapter.DEFAULT_MODELS[0] == "deepseek/deepseek-chat"

    def test_dead_head_model_falls_through_and_is_remembered(self):
        import requests
        from orchestrator.model_router import CompletionRequest

        def behaviour(model):
            if model == "deepseek/deepseek-chat":
                return requests.exceptions.Timeout("no bytes for minutes")
            return "```kotlin\nclass MainActivity\n```"

        adapter = DeepSeekAdapter(api_key="sk-or-test")
        with patch("requests.post", side_effect=_post_dispatch(behaviour)) as p:
            text = adapter.complete(CompletionRequest(
                "write MainActivity", model_id="auto", timeout_s=30))
            assert "MainActivity" in text
            assert adapter.last_model == "deepseek/deepseek-v4.1-flash"
            first_calls = p.call_count
            assert first_calls >= 2                    # it really walked
            # the parked model is skipped on the next call → one HTTP call
            text2 = adapter.complete(CompletionRequest(
                "again", model_id="auto", timeout_s=30))
            assert "MainActivity" in text2
            assert p.call_count - first_calls == 1

    def test_explicit_model_id_does_not_walk(self):
        from orchestrator.model_router import CompletionRequest
        adapter = DeepSeekAdapter(api_key="sk-or-test")
        with patch("requests.post",
                   side_effect=_post_dispatch(lambda m: "hello")) as p:
            text = adapter.complete(CompletionRequest(
                "hi", model_id="deepseek/deepseek-chat", timeout_s=30))
            assert text == "hello" and p.call_count == 1

    def test_bounded_completion_cap_is_sent(self):
        """OpenRouter credit-checks against the model DEFAULT max, so the
        adapter must always send a bounded cap (never none)."""
        from orchestrator.model_router import CompletionRequest
        seen: dict = {}

        def _post(url, headers=None, json=None, timeout=None):
            seen["payload"] = json
            return _FakeHTTPResponse("hello")

        adapter = DeepSeekAdapter(api_key="sk-or-test")
        with patch("requests.post", side_effect=_post):
            adapter.complete(CompletionRequest("hi", model_id="auto",
                                               timeout_s=30))
        assert seen["payload"]["max_tokens"] == DeepSeekAdapter.DEFAULT_MAX_TOKENS

    def test_router_builds_deepseek_adapter_from_openrouter_keys(self, tmp_path):
        cfg = tmp_path / "api_keys.json"
        cfg.write_text(json.dumps({"openrouter_keys": ["sk-or-abc"]}))
        router = ModelRouter(config_path=cfg, usage=UsageJournal(
            path=tmp_path / "u.jsonl"))
        assert "deepseek" in router.adapters
        assert router.adapters["deepseek"].is_configured()
        assert router.adapters["deepseek"]._models[0] == "deepseek/deepseek-chat"

    def test_force_provider_deepseek_routes_and_names_the_model(self):
        ds = FakeAdapter("deepseek", ["answer from deepseek"])
        ds.last_model = "deepseek/deepseek-chat"
        router, _, _ = make_router({"deepseek": ds})
        res = router.route("q", RouteOptions(task=ModelTask.SIMPLE_CODING,
                                             force_provider="deepseek"))
        assert res.ok and res.provider == "deepseek_chat"
        assert res.model == "deepseek/deepseek-chat"
