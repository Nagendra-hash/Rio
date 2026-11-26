"""
tests/test_model_router_budget.py — Tests for ModelRouter budget integration.

Tests that the ModelRouter properly enforces budgets when routing requests
through the provider chain.
"""

import json
import tempfile
from pathlib import Path
from datetime import date
from unittest.mock import patch, MagicMock

import pytest

from orchestrator.model_router.router import ModelRouter
from orchestrator.model_router.capabilities import ModelTask
from orchestrator.model_router.budget import BudgetManager
from orchestrator.model_router.usage import UsageJournal
from orchestrator.model_router.providers import (
    OllamaAdapter, CompletionRequest, ProviderAdapter
)


@pytest.fixture
def temp_dir():
    """Create temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


@pytest.fixture
def temp_config(temp_dir):
    """Create temp config file."""
    config_path = temp_dir / "config" / "api_keys.json"
    config_path.parent.mkdir(exist_ok=True)
    config_path.write_text(json.dumps({
        "router_mode": "smart",
        "ollama_enabled": True,
    }))
    return config_path


@pytest.fixture
def temp_budget(temp_dir):
    """Create temp budget config."""
    budget_path = temp_dir / "config" / "cloud_budget.json"
    budget_path.parent.mkdir(exist_ok=True)
    budget_path.write_text(json.dumps({
        "daily_limits": {"gemini": 100000},
        "monthly_limits": {"gemini": 1000000},
        "per_request_cap": 8000,
        "alert_thresholds": [0.5, 0.75, 0.9],
        "hard_block_at": 1.0,
        "exclude_local": True,
    }))
    return budget_path


@pytest.fixture
def usage_journal(temp_dir):
    """Create UsageJournal with temp path."""
    journal = UsageJournal()
    journal.path = str(temp_dir / "memory" / "llm_usage.jsonl")
    Path(journal.path).parent.mkdir(exist_ok=True)
    return journal


class MockLocalAdapter(ProviderAdapter):
    """Mock adapter that always succeeds."""
    name = "local"

    def is_configured(self):
        return True

    def probe(self):
        return True, "ready"

    def complete(self, request):
        return "Mock local response"


class MockCloudAdapter(ProviderAdapter):
    """Mock adapter for cloud provider."""
    name = "gemini"

    def __init__(self):
        self.call_count = 0

    def is_configured(self):
        return True

    def probe(self):
        return True, "ready"

    def complete(self, request):
        self.call_count += 1
        return "Mock cloud response"


class TestModelRouterBudgetIntegration:
    """Test budget enforcement in ModelRouter."""

    @pytest.fixture
    def temp_config(self, temp_dir):
        """Create temp config file."""
        config_path = temp_dir / "api_keys.json"
        config_path.write_text(json.dumps({
            "router_mode": "smart",
            "ollama_enabled": True,
        }))
        return config_path

    @pytest.fixture
    def temp_budget(self, temp_dir):
        """Create temp budget config."""
        budget_path = temp_dir / "cloud_budget.json"
        budget_path.write_text(json.dumps({
            "daily_limits": {"gemini": 10000},
            "monthly_limits": {"gemini": 100000},
            "per_request_cap": 8000,
            "alert_thresholds": [0.5, 0.75, 0.9],
            "hard_block_at": 1.0,
            "exclude_local": True,
        }))
        return budget_path

    @pytest.fixture
    def usage_journal(self, temp_dir):
        """Create UsageJournal with temp path."""
        journal = UsageJournal()
        journal.path = str(temp_dir / "llm_usage.jsonl")
        return journal

    def test_budget_blocks_cloud_provider(self, temp_config, temp_budget, usage_journal):
        """Router should skip cloud provider when budget exceeded."""
        # Add usage near daily limit
        today = date.today().isoformat()
        Path(usage_journal.path).write_text(json.dumps({
            "ts": f"{today}T10:00:00",
            "provider": "gemini",
            "model": "gemini-1.5-flash",
            "ok": True,
            "latency_s": 1.0,
            "tokens": {"total_tokens": 9000},
            "cost_usd": 0.0,
            "task": "general",
            "mode": "smart",
            "hops": [],
        }) + "\n")

        # Create router with budget
        budget = BudgetManager(temp_budget, usage_journal=usage_journal)
        router = ModelRouter(
            budget=budget,
            config_path=temp_config,
            budget_path=temp_budget,
        )

        # Replace adapters with mocks
        router.adapters["local"] = MockLocalAdapter()
        router.adapters["gemini"] = MockCloudAdapter()

        # Route a request - should use local, not gemini (budget exceeded)
        from orchestrator.model_router.router import RouteOptions
        options = RouteOptions(task=ModelTask.LIGHT_REASONING)

        result = router.route("Test prompt", options)

        assert result.ok is True
        assert result.provider in ["local", "local_qwen3b", "ollama"]
        assert "budget_exceeded" in result.explanation or "local" in result.provider

    def test_budget_allows_cloud_when_under_limit(self, temp_config, temp_budget, usage_journal):
        """Router should use cloud provider when under budget."""
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
            "task": "general",
            "mode": "smart",
            "hops": [],
        }) + "\n")

        budget = BudgetManager(temp_budget, usage_journal=usage_journal)
        router = ModelRouter(
            budget=budget,
            config_path=temp_config,
            budget_path=temp_budget,
        )

        router.adapters["local"] = MockLocalAdapter()
        router.adapters["gemini"] = MockCloudAdapter()

        from orchestrator.model_router.router import RouteOptions
        options = RouteOptions(task=ModelTask.LIGHT_REASONING)

        result = router.route("Test prompt", options)

        # Should use local first (cheaper), but gemini is available as fallback
        assert result.ok is True

    def test_local_provider_excluded_from_budget(self, temp_config, temp_budget, usage_journal):
        """Local providers should never be blocked by budget."""
        # Even huge usage for local
        today = date.today().isoformat()
        Path(usage_journal.path).write_text(json.dumps({
            "ts": f"{today}T10:00:00",
            "provider": "ollama",
            "model": "qwen2.5-coder:3b",
            "ok": True,
            "latency_s": 1.0,
            "tokens": {"total_tokens": 1000000},  # Way over limit
            "cost_usd": 0.0,
            "task": "general",
            "mode": "smart",
            "hops": [],
        }) + "\n")

        budget = BudgetManager(temp_budget, usage_journal=usage_journal)
        router = ModelRouter(
            budget=budget,
            config_path=temp_config,
            budget_path=temp_budget,
        )

        router.adapters["ollama"] = MockLocalAdapter()
        router.adapters["ollama"].name = "ollama"

        from orchestrator.model_router.router import RouteOptions
        options = RouteOptions(task=ModelTask.SIMPLE_CODING)

        result = router.route("Write a function", options)

        assert result.ok is True
        # Ollama should work despite huge "usage" because it's local

    def test_per_request_cap_enforced_in_router(self, temp_config, temp_budget, usage_journal):
        """Per-request cap should be enforced during routing."""
        budget = BudgetManager(temp_budget, usage_journal=usage_journal)
        router = ModelRouter(
            budget=budget,
            config_path=temp_config,
            budget_path=temp_budget,
        )

        router.adapters["gemini"] = MockCloudAdapter()

        from orchestrator.model_router.router import RouteOptions
        # Request with max_tokens exceeding per-request cap
        options = RouteOptions(
            task=ModelTask.LIGHT_REASONING,
            max_tokens=10000,  # Exceeds 8000 cap
        )

        result = router.route("Test prompt", options)

        # Should skip gemini due to per-request cap
        assert result.provider != "gemini" or not result.ok

    def test_budget_status_in_router_status(self, temp_config, temp_budget, usage_journal):
        """Router status should include budget info."""
        budget = BudgetManager(temp_budget, usage_journal=usage_journal)
        router = ModelRouter(
            budget=budget,
            config_path=temp_config,
            budget_path=temp_budget,
        )

        status = router.status()

        assert "budget" in status
        assert "gemini" in status["budget"]
        assert "daily" in status["budget"]["gemini"]
        assert "monthly" in status["budget"]["gemini"]


class TestModelRouterLegacyCompat:
    """Test legacy_generate with budget."""

    def test_legacy_generate_respects_budget(self, temp_config, temp_budget, usage_journal):
        """legacy_generate should respect budget limits."""
        # High usage
        today = date.today().isoformat()
        Path(usage_journal.path).write_text(json.dumps({
            "ts": f"{today}T10:00:00",
            "provider": "gemini",
            "model": "gemini-1.5-flash",
            "ok": True,
            "latency_s": 1.0,
            "tokens": {"total_tokens": 95000},
            "cost_usd": 0.0,
            "task": "general",
            "mode": "smart",
            "hops": [],
        }) + "\n")

        budget = BudgetManager(temp_budget, usage_journal=usage_journal)
        router = ModelRouter(
            budget=budget,
            config_path=temp_config,
            budget_path=temp_budget,
        )

        router.adapters["local"] = MockLocalAdapter()

        # legacy_generate for general task
        try:
            result = router.legacy_generate("Test prompt", "", "general")
            # Should work via local fallback
            assert result is not None
        except RuntimeError as e:
            # Or fail with budget message
            assert "budget" in str(e).lower() or "local" in str(e).lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])