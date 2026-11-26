"""
tests/test_usage_dashboard_api.py — Offline tests for dashboard Usage API endpoints.

Tests the /api/usage/* endpoints added to dashboard/server.py for the
Usage & Budget dashboard page.
"""

import json
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def temp_base_dir():
    """Create a temporary directory structure for testing."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        (base / "config").mkdir()
        (base / "memory").mkdir()
        yield base


@pytest.fixture
def mock_config(temp_base_dir):
    """Create mock api_keys.json."""
    config_path = temp_base_dir / "config" / "api_keys.json"
    config_path.write_text(json.dumps({
        "gemini_api_key": "test-key",
        "router_mode": "smart",
        "nvidia_nim_enabled": True,
        "ollama_enabled": True,
    }))


@pytest.fixture
def mock_usage_journal(temp_base_dir):
    """Create mock llm_usage.jsonl with sample data."""
    usage_path = temp_base_dir / "memory" / "llm_usage.jsonl"
    today = date.today().isoformat()
    records = [
        {
            "ts": f"{today}T10:00:00",
            "provider": "gemini",
            "model": "gemini-1.5-flash",
            "ok": True,
            "latency_s": 1.5,
            "tokens": {"total_tokens": 5000, "prompt_tokens": 1000, "completion_tokens": 4000},
            "cost_usd": 0.001,
            "task": "general",
            "mode": "smart",
            "hops": [],
        },
        {
            "ts": f"{today}T11:00:00",
            "provider": "ollama",
            "model": "qwen2.5-coder:3b",
            "ok": True,
            "latency_s": 0.8,
            "tokens": {"total_tokens": 3000, "prompt_tokens": 500, "completion_tokens": 2500},
            "cost_usd": 0.0,
            "task": "code",
            "mode": "smart",
            "hops": [],
        },
        {
            "ts": f"{today}T12:00:00",
            "provider": "nvidia",
            "model": "nvidia/llama-3.1-70b-instruct",
            "ok": True,
            "latency_s": 2.0,
            "tokens": {"total_tokens": 8000, "prompt_tokens": 2000, "completion_tokens": 6000},
            "cost_usd": 0.0,
            "task": "general",
            "mode": "smart",
            "hops": [],
        },
    ]
    usage_path.write_text("\n".join(json.dumps(r) for r in records))


def _create_test_app(temp_base_dir):
    """Create a DashboardServer app for testing."""
    import os
    os.environ["JARVIS_HOME"] = str(temp_base_dir)

    # Create config with dashboard token
    config_dir = temp_base_dir / "config"
    config_dir.mkdir(exist_ok=True)
    config_file = config_dir / "api_keys.json"
    config_file.write_text(json.dumps({
        "dashboard_auth_token": "test-token-123",
    }))

    # Patch both dashboard.server.BASE_DIR and memory.config_manager.BASE_DIR
    with patch("dashboard.server.BASE_DIR", temp_base_dir), \
         patch("memory.config_manager.BASE_DIR", temp_base_dir), \
         patch("memory.config_manager.CONFIG_DIR", temp_base_dir / "config"), \
         patch("memory.config_manager.CONFIG_FILE", temp_base_dir / "config" / "api_keys.json"):
        from dashboard.server import DashboardServer
        server = DashboardServer()
        return server.app


def _auth_headers():
    """Return auth headers for test requests."""
    return {"Authorization": "Bearer test-token-123"}


class TestUsageSummaryEndpoint:
    """Test /api/usage/summary endpoint."""

    def test_returns_summary_for_all_providers(self, temp_base_dir, mock_config, mock_usage_journal):
        """Summary should return budget status for all cloud providers."""
        app = _create_test_app(temp_base_dir)
        from fastapi.testclient import TestClient
        client = TestClient(app)

        resp = client.get("/api/usage/summary", headers=_auth_headers())
        assert resp.status_code == 200

        data = resp.json()
        assert "gemini" in data
        assert "nvidia" in data
        # ollama is local, excluded by default (exclude_local=True)
        assert "ollama" not in data

        # Check gemini has budget status structure
        assert "daily" in data["gemini"]
        assert "monthly" in data["gemini"]
        assert "is_local" in data["gemini"]
        assert data["gemini"]["is_local"] is False

    def test_returns_budget_status_when_no_usage(self, temp_base_dir, mock_config):
        """Should return default budget status when no usage file exists."""
        app = _create_test_app(temp_base_dir)
        from fastapi.testclient import TestClient
        client = TestClient(app)

        resp = client.get("/api/usage/summary", headers=_auth_headers())
        assert resp.status_code == 200

        data = resp.json()
        assert "gemini" in data
        assert "nvidia" in data
        # Should have default budget structure
        assert "daily" in data["gemini"]
        assert "limit" in data["gemini"]["daily"]


class TestUsageDetailEndpoint:
    """Test /api/usage/detail endpoint."""

    def test_returns_all_records(self, temp_base_dir, mock_config, mock_usage_journal):
        """Should return all usage records."""
        app = _create_test_app(temp_base_dir)
        from fastapi.testclient import TestClient
        client = TestClient(app)

        resp = client.get("/api/usage/detail", headers=_auth_headers())
        assert resp.status_code == 200

        data = resp.json()
        assert "summary" in data
        assert "data" in data
        assert len(data["data"]) == 3

    def test_filters_by_provider(self, temp_base_dir, mock_config, mock_usage_journal):
        """Should filter records by provider."""
        app = _create_test_app(temp_base_dir)
        from fastapi.testclient import TestClient
        client = TestClient(app)

        resp = client.get("/api/usage/detail?provider=gemini", headers=_auth_headers())
        assert resp.status_code == 200

        data = resp.json()
        assert len(data["data"]) == 1
        assert data["data"][0]["provider"] == "gemini"

    def test_filters_by_days(self, temp_base_dir, mock_config, mock_usage_journal):
        """Should filter records by days."""
        app = _create_test_app(temp_base_dir)
        from fastapi.testclient import TestClient
        client = TestClient(app)

        resp = client.get("/api/usage/detail?days=1", headers=_auth_headers())
        assert resp.status_code == 200

        data = resp.json()
        assert len(data["data"]) == 3  # All today

    def test_returns_empty_for_future_days(self, temp_base_dir, mock_config, mock_usage_journal):
        """Should return empty for dates before cutoff."""
        app = _create_test_app(temp_base_dir)
        from fastapi.testclient import TestClient
        client = TestClient(app)

        # days=0 means today's records are included (cutoff = today)
        resp = client.get("/api/usage/detail?days=0", headers=_auth_headers())
        assert resp.status_code == 200

        data = resp.json()
        # With days=0, today's records should be included
        assert len(data["data"]) == 3

        # Test with a very small days value that excludes today
        resp = client.get("/api/usage/detail?days=-1", headers=_auth_headers())
        assert resp.status_code == 200
        data = resp.json()
        # With days=-1, cutoff is tomorrow, so today's records are excluded
        assert len(data["data"]) == 0


class TestUsageBudgetEndpoint:
    """Test /api/usage/budget endpoints."""

    def test_get_returns_budget_status(self, temp_base_dir, mock_config, mock_usage_journal):
        """GET should return budget status for all providers."""
        app = _create_test_app(temp_base_dir)
        from fastapi.testclient import TestClient
        client = TestClient(app)

        resp = client.get("/api/usage/budget", headers=_auth_headers())
        assert resp.status_code == 200

        data = resp.json()
        assert "providers" in data
        assert "config" in data
        assert "gemini" in data["providers"]
        assert "daily" in data["providers"]["gemini"]
        assert "monthly" in data["providers"]["gemini"]

    def test_post_updates_budget_limits(self, temp_base_dir, mock_config, mock_usage_journal):
        """POST should update budget limits."""
        app = _create_test_app(temp_base_dir)
        from fastapi.testclient import TestClient
        client = TestClient(app)

        new_limits = {
            "daily_limits": {"gemini": 100000},
            "monthly_limits": {"gemini": 1000000},
        }
        resp = client.post("/api/usage/budget", json=new_limits, headers=_auth_headers())
        assert resp.status_code == 200

        data = resp.json()
        assert data["ok"] is True
        assert data["config"]["daily_limits"]["gemini"] == 100000

    def test_post_updates_global_settings(self, temp_base_dir, mock_config, mock_usage_journal):
        """POST should update global budget settings."""
        app = _create_test_app(temp_base_dir)
        from fastapi.testclient import TestClient
        client = TestClient(app)

        new_settings = {
            "per_request_cap": 4000,
            "alert_thresholds": [0.6, 0.8, 0.95],
            "hard_block_at": 0.95,
            "exclude_local": False,
        }
        resp = client.post("/api/usage/budget", json=new_settings, headers=_auth_headers())
        assert resp.status_code == 200

        data = resp.json()
        assert data["ok"] is True
        assert data["config"]["per_request_cap"] == 4000
        assert data["config"]["alert_thresholds"] == [0.6, 0.8, 0.95]
        assert data["config"]["hard_block_at"] == 0.95
        assert data["config"]["exclude_local"] is False


class TestUsageRouterStatusEndpoint:
    """Test /api/usage/router-status endpoint."""

    def test_returns_router_status(self, temp_base_dir, mock_config, mock_usage_journal):
        """Should return router status with mode and models."""
        app = _create_test_app(temp_base_dir)
        from fastapi.testclient import TestClient
        client = TestClient(app)

        resp = client.get("/api/usage/router-status", headers=_auth_headers())
        assert resp.status_code == 200

        data = resp.json()
        assert "mode" in data
        assert "models" in data
        assert "budget" in data


if __name__ == "__main__":
    pytest.main([__file__, "-v"])