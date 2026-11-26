"""
tests/test_budget_manager.py — Tests for BudgetManager cloud token enforcement.

Tests the BudgetManager class that enforces daily/monthly token budgets
per provider with per-request caps and alert thresholds.
"""

import json
import tempfile
from pathlib import Path
from datetime import date, timedelta
from unittest.mock import patch

import pytest

from orchestrator.model_router.budget import BudgetManager, BudgetExceededError
from orchestrator.model_router.usage import UsageJournal


class TestBudgetManagerConfig:
    """Test budget configuration loading and defaults."""

    def test_loads_custom_config(self, temp_dir):
        """Should load custom config from file."""
        config_path = temp_dir / "cloud_budget.json"
        config_path.write_text(json.dumps({
            "daily_limits": {"gemini": 100000},
            "monthly_limits": {"gemini": 1000000},
            "per_request_cap": 4000,
        }))

        budget = BudgetManager(config_path)
        assert budget.get_daily_limit("gemini") == 100000
        assert budget.get_monthly_limit("gemini") == 1000000
        assert budget.get_per_request_cap() == 4000

    def test_uses_defaults_when_no_config(self, temp_dir):
        """Should use defaults when config file doesn't exist."""
        budget = BudgetManager(temp_dir / "nonexistent.json")
        assert budget.get_daily_limit("gemini") == 200000
        assert budget.get_per_request_cap() == 8000
        assert budget._config["alert_thresholds"] == [0.5, 0.75, 0.9]

    def test_saves_config(self, temp_dir):
        """Should persist config changes."""
        config_path = temp_dir / "cloud_budget.json"
        budget = BudgetManager(config_path)
        budget._config["daily_limits"]["gemini"] = 50000
        budget.save()

        # Reload and verify
        budget2 = BudgetManager(config_path)
        assert budget2.get_daily_limit("gemini") == 50000


class TestBudgetManagerLocalProvider:
    """Test local provider exclusion."""

    def test_local_providers_excluded(self, temp_dir):
        """Local providers should be excluded from budgets."""
        budget = BudgetManager(temp_dir / "budget.json")

        # Ollama is local
        assert budget.is_local_provider("ollama") is True
        assert budget.is_local_provider("ollama", "qwen2.5-coder:3b") is True

        # LocalQwen is local
        assert budget.is_local_provider("local") is True
        assert budget.is_local_provider("local", "qwen2.5-coder-3b") is True

        # Cloud providers are not local
        assert budget.is_local_provider("gemini") is False
        assert budget.is_local_provider("openrouter") is False

    def test_local_provider_budget_check_passes(self, temp_dir):
        """Budget check should pass for local providers regardless of limits."""
        budget = BudgetManager(temp_dir / "budget.json")

        # Even with huge token request, local passes
        allowed, reason = budget.check_budget("ollama", "qwen2.5-coder:3b", 1000000)
        assert allowed is True
        assert "local provider excluded" in reason


@pytest.fixture
def usage_journal(temp_dir):
    """Create UsageJournal with temp directory."""
    journal = UsageJournal()
    journal.path = str(temp_dir / "llm_usage.jsonl")
    Path(journal.path).parent.mkdir(exist_ok=True)
    return journal


class TestBudgetManagerLimits:
    """Test daily/monthly limit enforcement."""

    def test_daily_limit_enforced(self, temp_dir, usage_journal):
        """Should block when daily limit would be exceeded."""
        budget = BudgetManager(temp_dir / "budget.json", usage_journal=usage_journal)

        # Add usage for today (195000 used, limit 200000)
        today = date.today().isoformat()
        usage_file = Path(usage_journal.path)
        usage_file.write_text(json.dumps({
            "ts": f"{today}T10:00:00",
            "provider": "gemini",
            "model": "gemini-1.5-flash",
            "ok": True,
            "latency_s": 1.0,
            "tokens": {"total_tokens": 195000},
            "cost_usd": 0.0,
            "task": "general",
            "mode": "smart",
            "hops": [],
        }) + "\n")

        # Request that would exceed daily limit (6000 would make it 201000 > 200000)
        # But under per-request cap (8000)
        allowed, reason = budget.check_budget("gemini", "gemini-1.5-flash", 6000)
        assert allowed is False
        assert "daily budget exceeded" in reason

    def test_monthly_limit_enforced(self, temp_dir, usage_journal):
        """Should block when monthly limit would be exceeded."""
        budget = BudgetManager(temp_dir / "budget.json", usage_journal=usage_journal)

        # Add usage for this month (995000 used, limit 1000000)
        month_prefix = date.today().strftime("%Y-%m")
        usage_file = Path(usage_journal.path)
        usage_file.write_text(json.dumps({
            "ts": f"{month_prefix}-01T10:00:00",
            "provider": "openrouter",
            "model": "openrouter/free",
            "ok": True,
            "latency_s": 1.0,
            "tokens": {"total_tokens": 995000},
            "cost_usd": 0.0,
            "task": "general",
            "mode": "smart",
            "hops": [],
        }) + "\n")

        # Request that would exceed monthly limit (6000 would make it 1001000 > 1000000)
        # But under per-request cap (8000)
        allowed, reason = budget.check_budget("openrouter", "openrouter/free", 6000)
        assert allowed is False
        assert "monthly budget exceeded" in reason

    def test_per_request_cap_enforced(self, temp_dir, usage_journal):
        """Should block when per-request cap exceeded."""
        budget = BudgetManager(temp_dir / "budget.json", usage_journal=usage_journal)

        # Request exceeds per-request cap (default 8000)
        allowed, reason = budget.check_budget("gemini", "gemini-1.5-flash", 10000)
        assert allowed is False
        assert "per-request cap" in reason

    def test_request_under_limits_passes(self, temp_dir, usage_journal):
        """Request under all limits should pass."""
        budget = BudgetManager(temp_dir / "budget.json", usage_journal=usage_journal)

        allowed, reason = budget.check_budget("gemini", "gemini-1.5-flash", 1000)
        assert allowed is True
        assert reason == "ok"


class TestBudgetManagerStatus:
    """Test budget status reporting."""

    def test_get_budget_status(self, temp_dir, usage_journal):
        """Should return detailed status for a provider."""
        budget = BudgetManager(temp_dir / "budget.json", usage_journal=usage_journal)

        # Add some usage
        today = date.today().isoformat()
        usage_file = Path(usage_journal.path)
        usage_file.write_text(json.dumps({
            "ts": f"{today}T10:00:00",
            "provider": "gemini",
            "model": "gemini-1.5-flash",
            "ok": True,
            "latency_s": 1.0,
            "tokens": {"total_tokens": 50000},
            "cost_usd": 0.0,
            "task": "general",
            "mode": "smart",
            "hops": [],
        }) + "\n")

        status = budget.get_budget_status("gemini")

        assert status["provider"] == "gemini"
        assert status["daily"]["used"] == 50000
        assert status["daily"]["limit"] == 200000
        assert status["daily"]["percent"] == 25.0
        assert status["is_local"] is False

    def test_get_all_status(self, temp_dir, usage_journal):
        """Should return status for all configured providers."""
        budget = BudgetManager(temp_dir / "budget.json", usage_journal=usage_journal)

        status = budget.get_all_status()

        assert "gemini" in status
        assert "openrouter" in status
        assert "nvidia" in status
        assert "freellm" in status
        assert "deepseek" in status

    def test_alert_thresholds(self, temp_dir, usage_journal):
        """Should generate alerts when thresholds crossed."""
        budget = BudgetManager(temp_dir / "budget.json", usage_journal=usage_journal)

        # Add usage at 75% of daily limit (triggers 0.5 and 0.75 alerts)
        today = date.today().isoformat()
        usage_file = Path(usage_journal.path)
        usage_file.write_text(json.dumps({
            "ts": f"{today}T10:00:00",
            "provider": "gemini",
            "model": "gemini-1.5-flash",
            "ok": True,
            "latency_s": 1.0,
            "tokens": {"total_tokens": 150000},  # 75% of 200000
            "cost_usd": 0.0,
            "task": "general",
            "mode": "smart",
            "hops": [],
        }) + "\n")

        status = budget.get_budget_status("gemini")

        assert "alerts" in status
        assert len(status["alerts"]) >= 2  # 50% and 75% thresholds


class TestLegacyBudgetCompat:
    """Test legacy core/llm_router budget compatibility functions."""

    def test_legacy_gemini_calls_today(self, temp_dir, monkeypatch):
        """Legacy function should count Gemini calls from llm_usage.jsonl."""
        from orchestrator.model_router.budget import _legacy_gemini_calls_today

        # Set JARVIS_HOME to temp dir so function uses our test file
        monkeypatch.setenv("JARVIS_HOME", str(temp_dir))

        # Create usage file with today's calls
        usage_path = temp_dir / "memory" / "llm_usage.jsonl"
        usage_path.parent.mkdir()
        today = date.today().isoformat()
        usage_path.write_text("\n".join([
            json.dumps({"ts": f"{today}T10:00:00", "provider": "gemini", "ok": True}),
            json.dumps({"ts": f"{today}T11:00:00", "provider": "gemini", "ok": True}),
            json.dumps({"ts": f"{today}T12:00:00", "provider": "ollama", "ok": True}),  # Not gemini
            json.dumps({"ts": f"2020-01-01T10:00:00", "provider": "gemini", "ok": True}),  # Not today
        ]) + "\n")

        count = _legacy_gemini_calls_today()

        assert count == 2

    def test_legacy_gemini_budget_ok(self, temp_dir):
        """Legacy budget check should work."""
        from orchestrator.model_router.budget import _legacy_gemini_budget_ok

        # Vision task is exempt
        assert _legacy_gemini_budget_ok("vision", {"gemini_daily_call_budget": 1}) is True

        # Non-vision with budget not exceeded
        assert _legacy_gemini_budget_ok("general", {"gemini_daily_call_budget": 5}) is True

        # Non-vision with budget exceeded would return False (tested via calls_today mock)


@pytest.fixture
def temp_dir():
    """Create temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])