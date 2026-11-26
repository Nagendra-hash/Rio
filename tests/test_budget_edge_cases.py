"""
tests/test_budget_edge_cases.py — Edge case tests for BudgetManager.

Tests edge cases and boundary conditions for budget enforcement.
"""

import json
import tempfile
from pathlib import Path
from datetime import date, timedelta
from unittest.mock import patch, MagicMock

import pytest

from orchestrator.model_router.budget import BudgetManager, BudgetExceededError
from orchestrator.model_router.usage import UsageJournal


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


@pytest.fixture
def budget_path(temp_dir):
    path = temp_dir / "config" / "cloud_budget.json"
    path.parent.mkdir()
    path.write_text(json.dumps({
        "daily_limits": {"gemini": 100000, "openrouter": 50000},
        "monthly_limits": {"gemini": 1000000, "openrouter": 500000},
        "per_request_cap": 8000,
        "alert_thresholds": [0.5, 0.75, 0.9],
        "hard_block_at": 1.0,
        "exclude_local": True,
    }))
    return path


@pytest.fixture
def usage_journal(temp_dir):
    journal = UsageJournal()
    journal.path = str(temp_dir / "memory" / "llm_usage.jsonl")
    Path(journal.path).parent.mkdir()
    return journal


class TestBudgetEdgeCases:
    """Test edge cases for budget enforcement."""

    def test_zero_daily_limit_blocks_all(self, temp_dir, usage_journal):
        """Zero daily limit should block all requests."""
        budget = BudgetManager(temp_dir / "budget.json", usage_journal=usage_journal)
        budget._config["daily_limits"]["gemini"] = 0

        allowed, reason = budget.check_budget("gemini", "gemini-1.5-flash", 1)
        assert allowed is False
        assert "daily budget exceeded" in reason

    def test_zero_monthly_limit_blocks_all(self, temp_dir, usage_journal):
        """Zero monthly limit should block all requests."""
        budget = BudgetManager(temp_dir / "budget.json", usage_journal=usage_journal)
        budget._config["monthly_limits"]["gemini"] = 0

        allowed, reason = budget.check_budget("gemini", "gemini-1.5-flash", 1)
        assert allowed is False
        assert "monthly budget exceeded" in reason

    def test_exact_daily_limit_allows(self, temp_dir, usage_journal):
        """Request exactly at daily limit should be allowed."""
        budget = BudgetManager(temp_dir / "budget.json", usage_journal=usage_journal)

        today = date.today().isoformat()
        Path(usage_journal.path).write_text(json.dumps({
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

        # Request exactly remaining (50000 more = 100000 limit)
        allowed, reason = budget.check_budget("gemini", "gemini-1.5-flash", 50000)
        assert allowed is True

    def test_exact_monthly_limit_allows(self, temp_dir, usage_journal):
        """Request exactly at monthly limit should be allowed."""
        budget = BudgetManager(temp_dir / "budget.json", usage_journal=usage_journal)

        month_prefix = date.today().strftime("%Y-%m")
        Path(usage_journal.path).write_text(json.dumps({
            "ts": f"{month_prefix}-01T10:00:00",
            "provider": "gemini",
            "model": "gemini-1.5-flash",
            "ok": True,
            "latency_s": 1.0,
            "tokens": {"total_tokens": 950000},
            "cost_usd": 0.0,
            "task": "general",
            "mode": "smart",
            "hops": [],
        }) + "\n")

        # Request exactly remaining
        allowed, reason = budget.check_budget("gemini", "gemini-1.5-flash", 50000)
        assert allowed is True

    def test_negative_token_request_rejected(self, temp_dir, usage_journal):
        """Negative token count should be rejected."""
        budget = BudgetManager(temp_dir / "budget.json", usage_journal=usage_journal)

        allowed, reason = budget.check_budget("gemini", "gemini-1.5-flash", -100)
        assert allowed is False
        assert "invalid" in reason.lower() or "negative" in reason.lower()

    def test_zero_token_request_allowed(self, temp_dir, usage_journal):
        """Zero token request should be allowed."""
        budget = BudgetManager(temp_dir / "budget.json", usage_journal=usage_journal)

        allowed, reason = budget.check_budget("gemini", "gemini-1.5-flash", 0)
        assert allowed is True

    def test_provider_not_in_config_uses_defaults(self, temp_dir, usage_journal):
        """Unknown provider should use default limits."""
        budget = BudgetManager(temp_dir / "budget.json", usage_journal=usage_journal)

        # Provider not in config
        allowed, reason = budget.check_budget("unknown_provider", "model", 1000)
        assert allowed is True  # No limit configured = unlimited

    def test_case_insensitive_provider_matching(self, temp_dir, usage_journal):
        """Provider matching should be case-insensitive for local detection."""
        budget = BudgetManager(temp_dir / "budget.json", usage_journal=usage_journal)

        assert budget.is_local_provider("OLLAMA") is True
        assert budget.is_local_provider("Ollama") is True
        assert budget.is_local_provider("LOCAL") is True
        assert budget.is_local_provider("Local") is True
        assert budget.is_local_provider("GeMiNi") is False

    def test_multiple_entries_same_day_aggregated(self, temp_dir, usage_journal):
        """Multiple entries same day should be aggregated."""
        budget = BudgetManager(temp_dir / "budget.json", usage_journal=usage_journal)

        today = date.today().isoformat()
        Path(usage_journal.path).write_text("\n".join([
            json.dumps({"ts": f"{today}T10:00:00", "provider": "gemini", "ok": True, "tokens": {"total_tokens": 30000}}),
            json.dumps({"ts": f"{today}T11:00:00", "provider": "gemini", "ok": True, "tokens": {"total_tokens": 30000}}),
            json.dumps({"ts": f"{today}T12:00:00", "provider": "gemini", "ok": True, "tokens": {"total_tokens": 30000}}),
        ]))

        # Total = 90000, limit = 100000, request 20000 = 110000 > limit
        allowed, reason = budget.check_budget("gemini", "gemini-1.5-flash", 20000)
        assert allowed is False
        assert "daily budget exceeded" in reason

    def test_multiple_months_not_aggregated(self, temp_dir, usage_journal):
        """Entries from different months should not be aggregated."""
        budget = BudgetManager(temp_dir / "budget.json", usage_journal=usage_journal)

        this_month = date.today().strftime("%Y-%m")
        last_month = (date.today().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")

        Path(usage_journal.path).write_text("\n".join([
            json.dumps({"ts": f"{this_month}-01T10:00:00", "provider": "gemini", "ok": True, "tokens": {"total_tokens": 500000}}),
            json.dumps({"ts": f"{last_month}-15T10:00:00", "provider": "gemini", "ok": True, "tokens": {"total_tokens": 600000}}),
        ]))

        # This month only 500000, limit 1000000, request 400000 = 900000 < limit
        allowed, reason = budget.check_budget("gemini", "gemini-1.5-flash", 400000)
        assert allowed is True

    def test_malformed_usage_lines_skipped(self, temp_dir, usage_journal):
        """Malformed JSONL lines should be skipped, not crash."""
        budget = BudgetManager(temp_dir / "budget.json", usage_journal=usage_journal)

        today = date.today().isoformat()
        Path(usage_journal.path).write_text("\n".join([
            json.dumps({"ts": f"{today}T10:00:00", "provider": "gemini", "ok": True, "tokens": {"total_tokens": 50000}}),
            "not valid json",
            json.dumps({"ts": f"{today}T11:00:00", "provider": "gemini", "ok": True, "tokens": {"total_tokens": 30000}}),
            "{incomplete",
        ]))

        # Should parse valid lines only: 50000 + 30000 = 80000
        status = budget.get_budget_status("gemini")
        assert status["daily"]["used"] == 80000

    def test_missing_tokens_field_treated_as_zero(self, temp_dir, usage_journal):
        """Missing tokens field should be treated as 0."""
        budget = BudgetManager(temp_dir / "budget.json", usage_journal=usage_journal)

        today = date.today().isoformat()
        Path(usage_journal.path).write_text("\n".join([
            json.dumps({"ts": f"{today}T10:00:00", "provider": "gemini", "ok": True}),  # No tokens
            json.dumps({"ts": f"{today}T11:00:00", "provider": "gemini", "ok": True, "tokens": {}}),  # Empty tokens
            json.dumps({"ts": f"{today}T12:00:00", "provider": "gemini", "ok": True, "tokens": {"total_tokens": 50000}}),
        ]))

        status = budget.get_budget_status("gemini")
        assert status["daily"]["used"] == 50000

    def test_failed_requests_not_counted(self, temp_dir, usage_journal):
        """Failed requests (ok=false) should not count toward budget."""
        budget = BudgetManager(temp_dir / "budget.json", usage_journal=usage_journal)

        today = date.today().isoformat()
        Path(usage_journal.path).write_text("\n".join([
            json.dumps({"ts": f"{today}T10:00:00", "provider": "gemini", "ok": True, "tokens": {"total_tokens": 50000}}),
            json.dumps({"ts": f"{today}T11:00:00", "provider": "gemini", "ok": False, "tokens": {"total_tokens": 100000}}),  # Failed
        ]))

        status = budget.get_budget_status("gemini")
        assert status["daily"]["used"] == 50000  # Only successful counted


class TestBudgetAlertThresholds:
    """Test alert threshold edge cases."""

    def test_alert_at_exact_threshold(self, temp_dir, usage_journal):
        """Alert should trigger at exact threshold percentage."""
        budget = BudgetManager(temp_dir / "budget.json", usage_journal=usage_journal)
        budget._config["alert_thresholds"] = [0.5, 0.8]

        today = date.today().isoformat()
        # Exactly 50% of 100000 = 50000
        Path(usage_journal.path).write_text(json.dumps({
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
        alerts = [a for a in status.get("alerts", []) if a["threshold"] == 0.5]
        assert len(alerts) >= 1

    def test_alerts_dont_duplicate(self, temp_dir, usage_journal):
        """Same threshold should not alert multiple times."""
        budget = BudgetManager(temp_dir / "budget.json", usage_journal=usage_journal)
        budget._config["alert_thresholds"] = [0.5]

        today = date.today().isoformat()
        Path(usage_journal.path).write_text(json.dumps({
            "ts": f"{today}T10:00:00",
            "provider": "gemini",
            "model": "gemini-1.5-flash",
            "ok": True,
            "latency_s": 1.0,
            "tokens": {"total_tokens": 60000},  # 60% - past 50%
            "cost_usd": 0.0,
            "task": "general",
            "mode": "smart",
            "hops": [],
        }) + "\n")

        status1 = budget.get_budget_status("gemini")
        status2 = budget.get_budget_status("gemini")

        # Alerts should be consistent
        assert status1["alerts"] == status2["alerts"]

    def test_hard_block_at_zero_blocks_everything(self, temp_dir, usage_journal):
        """hard_block_at = 0 should block all requests."""
        budget = BudgetManager(temp_dir / "budget.json", usage_journal=usage_journal)
        budget._config["hard_block_at"] = 0.0

        allowed, reason = budget.check_budget("gemini", "gemini-1.5-flash", 1)
        assert allowed is False
        assert "hard block" in reason.lower()

    def test_hard_block_at_one_allows_up_to_limit(self, temp_dir, usage_journal):
        """hard_block_at = 1.0 should allow up to limit."""
        budget = BudgetManager(temp_dir / "budget.json", usage_journal=usage_journal)
        budget._config["hard_block_at"] = 1.0

        today = date.today().isoformat()
        Path(usage_journal.path).write_text(json.dumps({
            "ts": f"{today}T10:00:00",
            "provider": "gemini",
            "model": "gemini-1.5-flash",
            "ok": True,
            "latency_s": 1.0,
            "tokens": {"total_tokens": 99999},
            "cost_usd": 0.0,
            "task": "general",
            "mode": "smart",
            "hops": [],
        }) + "\n")

        # Request 1 more token = exactly at limit
        allowed, reason = budget.check_budget("gemini", "gemini-1.5-flash", 1)
        assert allowed is True


class TestBudgetConfigPersistence:
    """Test budget configuration persistence."""

    def test_save_creates_parent_directories(self, temp_dir):
        """Save should create parent directories."""
        budget_path = temp_dir / "deep" / "nested" / "budget.json"
        budget = BudgetManager(budget_path)
        budget._config["daily_limits"]["gemini"] = 50000
        budget.save()

        assert budget_path.exists()
        reloaded = BudgetManager(budget_path)
        assert reloaded.get_daily_limit("gemini") == 50000

    def test_save_preserves_all_fields(self, temp_dir):
        """Save should preserve all config fields."""
        budget_path = temp_dir / "budget.json"
        budget = BudgetManager(budget_path)

        original = budget._config.copy()
        budget._config["daily_limits"]["gemini"] = 75000
        budget._config["custom_field"] = "test"
        budget.save()

        reloaded = BudgetManager(budget_path)
        assert reloaded.get_daily_limit("gemini") == 75000
        assert reloaded._config.get("custom_field") == "test"
        # Check all original fields preserved
        for key in original:
            if key not in ["daily_limits", "monthly_limits"]:  # These are modified
                assert reloaded._config.get(key) == original[key]


class TestBudgetManagerConcurrency:
    """Test budget manager under concurrent access."""

    def test_check_budget_thread_safe(self, temp_dir, usage_journal):
        """Multiple threads checking budget should not corrupt state."""
        import threading

        budget = BudgetManager(temp_dir / "budget.json", usage_journal=usage_journal)

        results = []
        def check():
            allowed, _ = budget.check_budget("gemini", "gemini-1.5-flash", 1000)
            results.append(allowed)

        threads = [threading.Thread(target=check) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All should succeed (no budget used yet)
        assert all(results)

    def test_concurrent_journal_reads(self, temp_dir, usage_journal):
        """Concurrent journal reads should not crash."""
        import threading

        today = date.today().isoformat()
        Path(usage_journal.path).write_text("\n".join([
            json.dumps({"ts": f"{today}T10:00:00", "provider": "gemini", "ok": True, "tokens": {"total_tokens": 10000}})
            for _ in range(100)
        ]))

        budget = BudgetManager(temp_dir / "budget.json", usage_journal=usage_journal)

        results = []
        def get_status():
            status = budget.get_budget_status("gemini")
            results.append(status["daily"]["used"])

        threads = [threading.Thread(target=get_status) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All should return same value
        assert len(set(results)) == 1
        assert results[0] == 1000000  # 100 * 10000


class TestLegacyCompatEdgeCases:
    """Test legacy compatibility edge cases."""

    def test_legacy_budget_ok_with_zero_budget(self, temp_dir):
        """Legacy budget check with zero budget."""
        from orchestrator.model_router.budget import _legacy_gemini_budget_ok

        with patch("orchestrator.model_router.budget._legacy_gemini_calls_today", return_value=0):
            # Zero budget should block non-vision
            assert _legacy_gemini_budget_ok("general", {"gemini_daily_call_budget": 0}) is False
            # Vision exempt
            assert _legacy_gemini_budget_ok("vision", {"gemini_daily_call_budget": 0}) is True

    def test_legacy_budget_ok_missing_config(self, temp_dir):
        """Legacy budget check with missing config key."""
        from orchestrator.model_router.budget import _legacy_gemini_budget_ok

        with patch("orchestrator.model_router.budget._legacy_gemini_calls_today", return_value=5):
            # Missing key = unlimited
            assert _legacy_gemini_budget_ok("general", {}) is True

    def test_legacy_calls_today_missing_file(self, temp_dir):
        """Legacy calls today with missing usage file."""
        from orchestrator.model_router.budget import _legacy_gemini_calls_today

        with patch("orchestrator.model_router.budget.Path") as mock_path:
            mock_path.return_value.exists.return_value = False
            count = _legacy_gemini_calls_today()
            assert count == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])