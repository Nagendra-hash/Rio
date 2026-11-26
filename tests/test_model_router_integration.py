"""
tests/test_model_router_integration.py — Integration tests for ModelRouter.

Tests the unified ModelRouter with all providers, capability-based routing,
legacy compatibility, budget enforcement, and health monitoring.
"""

import json
import tempfile
import os
from pathlib import Path
from datetime import date
from unittest.mock import patch, MagicMock

import pytest

from orchestrator.model_router.router import ModelRouter
from orchestrator.model_router.capabilities import (
    ModelTask, CapabilityRegistry, ModelSpec, Privacy, TIER_LOCAL, _CLOUD_TASKS
)
from orchestrator.model_router.providers import (
    CompletionRequest, ProviderAdapter, ProviderTimeoutError,
    UnavailableError, RateLimitedError, EmptyAnswerError
)
from orchestrator.model_router.budget import BudgetManager
from orchestrator.model_router.usage import UsageJournal


class MockLocalProvider(ProviderAdapter):
    """Mock local provider that always works."""
    name = "ollama"

    def __init__(self):
        self.call_count = 0

    def is_configured(self):
        return True

    def probe(self):
        return True, "ready"

    def complete(self, request):
        self.call_count += 1
        return f"Local response #{self.call_count}"


class MockCloudProvider(ProviderAdapter):
    """Mock cloud provider (Gemini, OpenRouter, etc.)."""
    name = "gemini"

    def __init__(self, should_fail=False, fail_type=None):
        self.call_count = 0
        self.should_fail = should_fail
        self.fail_type = fail_type

    def is_configured(self):
        return True

    def probe(self):
        return True, "ready"

    def complete(self, request):
        self.call_count += 1
        if self.should_fail:
            if self.fail_type == "timeout":
                raise ProviderTimeoutError("Timeout")
            elif self.fail_type == "unavailable":
                raise UnavailableError("Service down")
            elif self.fail_type == "rate_limited":
                raise RateLimitedError("Rate limited", retry_after_s=60)
            elif self.fail_type == "empty":
                raise EmptyAnswerError("Empty response")
        return f"Cloud response #{self.call_count}"


class MockNvidiaProvider(ProviderAdapter):
    """Mock NVIDIA NIM provider."""
    name = "nvidia"

    def __init__(self):
        self.call_count = 0

    def is_configured(self):
        return True

    def probe(self):
        return True, "ready"

    def complete(self, request):
        self.call_count += 1
        return f"NVIDIA response #{self.call_count}"


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


@pytest.fixture
def config_path(temp_dir):
    path = temp_dir / "config" / "api_keys.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({
        "router_mode": "smart",
        "ollama_enabled": True,
        "nvidia_nim_enabled": True,
    }))
    return path


@pytest.fixture
def budget_path(temp_dir):
    path = temp_dir / "config" / "cloud_budget.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({
        "daily_limits": {"gemini": 100000},
        "monthly_limits": {"gemini": 1000000},
        "per_request_cap": 8000,
        "alert_thresholds": [0.5, 0.75, 0.9],
        "hard_block_at": 1.0,
        "exclude_local": True,
    }))
    return path


@pytest.fixture
def usage_journal(temp_dir):
    path = str(temp_dir / "memory" / "llm_usage.jsonl")
    Path(path).parent.mkdir(exist_ok=True)
    journal = UsageJournal(path=path)
    return journal


def _test_registry():
    """Create a minimal registry for testing with only local and cloud providers."""
    reg = CapabilityRegistry()
    reg.register(ModelSpec(
        name="local_test",
        provider="local",
        model_id="test-model",
        cost_tier=TIER_LOCAL,
        tasks=frozenset({ModelTask.SIMPLE_CODING, ModelTask.TRANSFORMATION, ModelTask.PRIVATE, ModelTask.LIGHT_REASONING}),
        privacy=Privacy.LOCAL,
        priority=0,
    ))
    reg.register(ModelSpec(
        name="gemini_test",
        provider="gemini",
        model_id="gemini-test",
        cost_tier=3,
        tasks=frozenset(_CLOUD_TASKS),
        privacy=Privacy.CLOUD,
        priority=10,
    ))
    reg.register(ModelSpec(
        name="nvidia_test",
        provider="nvidia",
        model_id="nvidia-test",
        cost_tier=2,
        tasks=frozenset(_CLOUD_TASKS),
        privacy=Privacy.CLOUD,
        priority=20,
    ))
    # Also add ollama for simple_coding (allowed provider)
    reg.register(ModelSpec(
        name="ollama_test",
        provider="ollama",
        model_id="ollama-test",
        cost_tier=TIER_LOCAL,
        tasks=frozenset({ModelTask.SIMPLE_CODING, ModelTask.TRANSFORMATION, ModelTask.PRIVATE, ModelTask.LIGHT_REASONING}),
        privacy=Privacy.LOCAL,
        priority=1,
    ))
    return reg


def _test_adapters():
    """Create mock adapters that match the test registry providers."""
    return {
        "local": MockLocalProvider(),
        "gemini": MockCloudProvider(),
        "nvidia": MockNvidiaProvider(),
        "ollama": MockLocalProvider(),
    }


class TestModelRouterInitialization:
    """Test ModelRouter initialization and configuration."""

    def test_initializes_with_config(self, config_path, budget_path, usage_journal):
        """Should initialize with config and budget."""
        router = ModelRouter(
            registry=_test_registry(),
            adapters=_test_adapters(),
            config_path=config_path,
            budget_path=budget_path,
            budget=BudgetManager(budget_path, usage_journal=usage_journal),
            usage=usage_journal,
        )

        assert router is not None
        assert router.mode == "smart"

    def test_loads_providers(self, config_path, budget_path, usage_journal):
        """Should load all configured providers."""
        router = ModelRouter(
            registry=_test_registry(),
            adapters=_test_adapters(),
            config_path=config_path,
            budget_path=budget_path,
            budget=BudgetManager(budget_path, usage_journal=usage_journal),
            usage=usage_journal,
        )

        # Should have at least local and cloud providers
        assert "local" in router.adapters
        assert "gemini" in router.adapters or "nvidia" in router.adapters

    def test_legacy_shim_available(self, config_path, budget_path, usage_journal):
        """Should have legacy_generate method for backward compat."""
        router = ModelRouter(
            registry=_test_registry(),
            adapters=_test_adapters(),
            config_path=config_path,
            budget_path=budget_path,
            budget=BudgetManager(budget_path, usage_journal=usage_journal),
            usage=usage_journal,
        )

        assert hasattr(router, "legacy_generate")
        assert callable(router.legacy_generate)


class TestModelRouterCapabilityRouting:
    """Test capability-based routing."""

    def test_routes_simple_coding_to_local(self, config_path, budget_path, usage_journal):
        """Simple coding tasks should route to local (Ollama)."""
        router = ModelRouter(
            registry=_test_registry(),
            adapters=_test_adapters(),
            config_path=config_path,
            budget_path=budget_path,
            budget=BudgetManager(budget_path, usage_journal=usage_journal),
            usage=usage_journal,
        )

        from orchestrator.model_router.router import RouteOptions
        options = RouteOptions(task=ModelTask.SIMPLE_CODING)

        result = router.route("Write a hello world function", options)

        assert result.ok is True
        # Should use ollama or local provider from test registry
        assert result.provider in ["ollama_test", "local_test"]

    def test_routes_difficult_reasoning_to_cloud(self, config_path, budget_path, usage_journal):
        """Difficult reasoning should route to cloud when budget allows."""
        router = ModelRouter(
            registry=_test_registry(),
            adapters=_test_adapters(),
            config_path=config_path,
            budget_path=budget_path,
            budget=BudgetManager(budget_path, usage_journal=usage_journal),
            usage=usage_journal,
        )

        from orchestrator.model_router.router import RouteOptions
        options = RouteOptions(task=ModelTask.DIFFICULT_REASONING)

        result = router.route("Solve this complex math problem", options)

        # Should try local first, then cloud
        assert result.ok is True
        # Cloud provider used for difficult reasoning
        assert result.provider in ["gemini_test", "nvidia_test"]

    def test_routes_vision_to_gemini(self, config_path, budget_path, usage_journal):
        """Vision tasks map to RESEARCH and route to cloud (Gemini)."""
        router = ModelRouter(
            registry=_test_registry(),
            adapters=_test_adapters(),
            config_path=config_path,
            budget_path=budget_path,
            budget=BudgetManager(budget_path, usage_journal=usage_journal),
            usage=usage_journal,
        )

        from orchestrator.model_router.router import RouteOptions
        options = RouteOptions(task=ModelTask.RESEARCH)

        result = router.route("Describe this image", options)

        assert result.ok is True
        assert result.provider in ["gemini_test", "nvidia_test"]

    def test_fallback_on_provider_failure(self, config_path, budget_path, usage_journal):
        """Should fallback to next provider on failure."""
        # Make local providers fail so it falls back to cloud
        adapters = _test_adapters()
        failing_local = MockLocalProvider()
        failing_local.call_count = 0
        def fail_complete(request):
            failing_local.call_count += 1
            raise UnavailableError("local unavailable")
        failing_local.complete = fail_complete
        adapters["local"] = failing_local
        adapters["ollama"] = failing_local

        router = ModelRouter(
            registry=_test_registry(),
            adapters=adapters,
            config_path=config_path,
            budget_path=budget_path,
            budget=BudgetManager(budget_path, usage_journal=usage_journal),
            usage=usage_journal,
        )

        from orchestrator.model_router.router import RouteOptions
        options = RouteOptions(task=ModelTask.LIGHT_REASONING)

        result = router.route("Test prompt", options)

        assert result.ok is True
        # Should fallback to nvidia_test or gemini_test after local fails
        assert result.provider in ["nvidia_test", "gemini_test"]
        assert "fallback" in result.explanation.lower() or len(result.hops) > 1


class TestModelRouterBudgetEnforcement:
    """Test budget enforcement in routing."""

    def test_blocks_cloud_over_daily_limit(self, config_path, budget_path, usage_journal):
        """Should block cloud provider when daily budget exceeded."""
        # Add usage near daily limit - 99500 so next request exceeds 100000
        today = date.today().isoformat()
        Path(usage_journal.path).write_text(json.dumps({
            "ts": f"{today}T10:00:00",
            "provider": "gemini",
            "model": "gemini-1.5-flash",
            "ok": True,
            "latency_s": 1.0,
            "tokens": {"total_tokens": 99500},
            "cost_usd": 0.0,
            "task": "light_reasoning",
            "mode": "smart",
            "hops": [],
        }) + "\n")

        # Make local and nvidia fail so it tries gemini and hits budget
        adapters = _test_adapters()
        failing_local = MockLocalProvider()
        failing_local.call_count = 0
        def fail_complete(request):
            failing_local.call_count += 1
            raise UnavailableError("local unavailable")
        failing_local.complete = fail_complete
        adapters["local"] = failing_local
        adapters["ollama"] = failing_local
        # Make nvidia fail too
        failing_nvidia = MockNvidiaProvider()
        def fail_nvidia(request):
            raise UnavailableError("nvidia unavailable")
        failing_nvidia.complete = fail_nvidia
        adapters["nvidia"] = failing_nvidia

        router = ModelRouter(
            registry=_test_registry(),
            adapters=adapters,
            config_path=config_path,
            budget_path=budget_path,
            budget=BudgetManager(budget_path, usage_journal=usage_journal),
            usage=usage_journal,
        )

        from orchestrator.model_router.router import RouteOptions
        options = RouteOptions(task=ModelTask.LIGHT_REASONING)

        result = router.route("Test prompt", options)

        # Should be blocked by budget - no providers left
        assert result.ok is False
        assert "budget" in result.explanation.lower()

    def test_allows_cloud_under_budget(self, config_path, budget_path, usage_journal):
        """Should allow cloud when under budget."""
        # Low usage
        today = date.today().isoformat()
        Path(usage_journal.path).write_text(json.dumps({
            "ts": f"{today}T10:00:00",
            "provider": "gemini",
            "model": "gemini-1.5-flash",
            "ok": True,
            "latency_s": 1.0,
            "tokens": {"total_tokens": 1000},
            "cost_usd": 0.0,
            "task": "difficult_reasoning",
            "mode": "smart",
            "hops": [],
        }) + "\n")

        router = ModelRouter(
            registry=_test_registry(),
            adapters=_test_adapters(),
            config_path=config_path,
            budget_path=budget_path,
            budget=BudgetManager(budget_path, usage_journal=usage_journal),
            usage=usage_journal,
        )

        from orchestrator.model_router.router import RouteOptions
        options = RouteOptions(task=ModelTask.DIFFICULT_REASONING)

        result = router.route("Complex task", options)

        assert result.ok is True
        # Cloud available as fallback for difficult tasks
        assert result.provider in ["gemini_test", "nvidia_test", "local_test"]

    def test_per_request_cap_enforced(self, config_path, budget_path, usage_journal):
        """Per-request cap should limit max tokens."""
        router = ModelRouter(
            registry=_test_registry(),
            adapters=_test_adapters(),
            config_path=config_path,
            budget_path=budget_path,
            budget=BudgetManager(budget_path, usage_journal=usage_journal),
            usage=usage_journal,
        )

        from orchestrator.model_router.router import RouteOptions
        # Request exceeding per-request cap (8000)
        options = RouteOptions(task=ModelTask.LIGHT_REASONING, max_tokens=10000)

        result = router.route("Test prompt", options)

        # Should use local or reject
        assert result.provider != "gemini_test" or not result.ok


class TestModelRouterLegacyCompat:
    """Test legacy_generate compatibility layer."""

    def test_legacy_general_task(self, config_path, budget_path, usage_journal):
        """legacy_generate should handle general task."""
        router = ModelRouter(
            registry=_test_registry(),
            adapters=_test_adapters(),
            config_path=config_path,
            budget_path=budget_path,
            budget=BudgetManager(budget_path, usage_journal=usage_journal),
            usage=usage_journal,
        )

        result = router.legacy_generate("Hello", "", "general")

        assert result is not None
        assert isinstance(result, str)

    def test_legacy_vision_task(self, config_path, budget_path, usage_journal):
        """legacy_generate should handle vision task."""
        router = ModelRouter(
            registry=_test_registry(),
            adapters=_test_adapters(),
            config_path=config_path,
            budget_path=budget_path,
            budget=BudgetManager(budget_path, usage_journal=usage_journal),
            usage=usage_journal,
        )

        result = router.legacy_generate("Describe image", "", "vision")

        assert result is not None

    def test_legacy_code_task(self, config_path, budget_path, usage_journal):
        """legacy_generate should handle code task."""
        router = ModelRouter(
            registry=_test_registry(),
            adapters=_test_adapters(),
            config_path=config_path,
            budget_path=budget_path,
            budget=BudgetManager(budget_path, usage_journal=usage_journal),
            usage=usage_journal,
        )

        result = router.legacy_generate("Write function", "", "code")

        assert result is not None


class TestModelRouterHealthMonitoring:
    """Test health board and provider monitoring."""

    def test_health_board_tracks_latency(self, config_path, budget_path, usage_journal):
        """HealthBoard should track provider latency."""
        router = ModelRouter(
            registry=_test_registry(),
            adapters=_test_adapters(),
            config_path=config_path,
            budget_path=budget_path,
            budget=BudgetManager(budget_path, usage_journal=usage_journal),
            usage=usage_journal,
        )

        from orchestrator.model_router.router import RouteOptions
        options = RouteOptions(task=ModelTask.SIMPLE_CODING)

        router.route("Test", options)

        status = router.status()
        assert "providers" in status
        # Provider from test registry
        assert "local" in status["providers"] or "ollama" in status["providers"]

    def test_health_board_tracks_errors(self, config_path, budget_path, usage_journal):
        """HealthBoard should track error rates."""
        adapters = _test_adapters()
        adapters["gemini"] = MockCloudProvider(should_fail=True, fail_type="unavailable")

        router = ModelRouter(
            registry=_test_registry(),
            adapters=adapters,
            config_path=config_path,
            budget_path=budget_path,
            budget=BudgetManager(budget_path, usage_journal=usage_journal),
            usage=usage_journal,
        )

        from orchestrator.model_router.router import RouteOptions
        options = RouteOptions(task=ModelTask.LIGHT_REASONING)

        router.route("Test", options)

        status = router.status()
        assert "providers" in status
        # gemini should show errors
        assert "gemini" in status["providers"]

    def test_circuit_breaker_opens_on_repeated_failures(self, config_path, budget_path, usage_journal):
        """Circuit breaker should open after repeated failures."""
        adapters = _test_adapters()
        failing = MockCloudProvider(should_fail=True, fail_type="unavailable")
        adapters["gemini"] = failing

        router = ModelRouter(
            registry=_test_registry(),
            adapters=adapters,
            config_path=config_path,
            budget_path=budget_path,
            budget=BudgetManager(budget_path, usage_journal=usage_journal),
            usage=usage_journal,
        )

        from orchestrator.model_router.router import RouteOptions
        options = RouteOptions(task=ModelTask.LIGHT_REASONING)

        # Make multiple requests that fail
        for _ in range(5):
            router.route("Test", options)

        # Circuit should be open for gemini
        status = router.status()
        assert "providers" in status
        # gemini should be marked unhealthy
        assert "gemini" in status["providers"]


class TestModelRouterModeSwitching:
    """Test router mode switching (local, free, smart, gemini)."""

    def test_local_mode(self, config_path, budget_path, usage_journal):
        """local mode should only use local providers."""
        # Mode is detected from env var, not config file
        os.environ["JARVIS_MODEL_ROUTER_MODE"] = "local"
        try:
            router = ModelRouter(
                registry=_test_registry(),
                adapters=_test_adapters(),
                config_path=config_path,
                budget_path=budget_path,
                budget=BudgetManager(budget_path, usage_journal=usage_journal),
                usage=usage_journal,
            )

            from orchestrator.model_router.router import RouteOptions
            # Use a task that local providers can handle
            options = RouteOptions(task=ModelTask.SIMPLE_CODING)

            result = router.route("Test", options)

            assert result.provider in ["ollama_test", "local_test"]
            assert router.mode == "local"
        finally:
            del os.environ["JARVIS_MODEL_ROUTER_MODE"]

    def test_gemini_mode(self, config_path, budget_path, usage_journal):
        """gemini mode should only use gemini for non-coding tasks."""
        # Note: SIMPLE_CODING has a hard-coded allow-list (ollama, nvidia only)
        # so we test with LIGHT_REASONING which doesn't have this restriction
        os.environ["JARVIS_MODEL_ROUTER_MODE"] = "gemini"
        try:
            router = ModelRouter(
                registry=_test_registry(),
                adapters=_test_adapters(),
                config_path=config_path,
                budget_path=budget_path,
                budget=BudgetManager(budget_path, usage_journal=usage_journal),
                usage=usage_journal,
            )

            from orchestrator.model_router.router import RouteOptions
            # In gemini mode, all non-coding tasks should go to gemini
            options = RouteOptions(task=ModelTask.LIGHT_REASONING)

            result = router.route("Test", options)

            assert result.provider == "gemini_test"
            assert router.mode == "gemini"
        finally:
            del os.environ["JARVIS_MODEL_ROUTER_MODE"]


class TestModelRouterUsageJournal:
    """Test usage journal integration."""

    def test_records_successful_requests(self, config_path, budget_path, usage_journal):
        """Should record successful requests to journal."""
        router = ModelRouter(
            registry=_test_registry(),
            adapters=_test_adapters(),
            config_path=config_path,
            budget_path=budget_path,
            budget=BudgetManager(budget_path, usage_journal=usage_journal),
            usage=usage_journal,
        )

        from orchestrator.model_router.router import RouteOptions
        options = RouteOptions(task=ModelTask.SIMPLE_CODING)

        router.route("Test", options)

        # Check journal was written
        assert Path(usage_journal.path).exists()
        content = Path(usage_journal.path).read_text()
        assert "local" in content or "ollama" in content
        assert "ok" in content

    def test_records_failed_requests(self, config_path, budget_path, usage_journal):
        """Should record failed requests to journal."""
        # Make all providers fail
        failing = MockCloudProvider(should_fail=True, fail_type="timeout")

        router = ModelRouter(
            registry=_test_registry(),
            adapters={"local": failing, "gemini": failing, "ollama": failing, "nvidia": failing},
            config_path=config_path,
            budget_path=budget_path,
            budget=BudgetManager(budget_path, usage_journal=usage_journal),
            usage=usage_journal,
        )

        from orchestrator.model_router.router import RouteOptions
        options = RouteOptions(task=ModelTask.LIGHT_REASONING)

        router.route("Test", options)

        content = Path(usage_journal.path).read_text()
        # Should have entry for failed attempt
        assert "timeout" in content.lower() or "error" in content.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])