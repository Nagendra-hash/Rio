"""
tests/test_dashboard_usage_e2e.py — Browser E2E tests for Usage & Budget dashboard.

Tests the /usage page in the PWA dashboard using Playwright.
"""

import tempfile
from pathlib import Path
import json

import pytest
from playwright.sync_api import sync_playwright, expect


class TestUsageDashboardE2E:
    """E2E tests for Usage & Budget dashboard page."""

    @pytest.fixture(scope="class")
    def temp_dir(self):
        """Create temporary directory structure."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "config").mkdir()
            (base / "memory").mkdir()
            (base / "dashboard" / "frontend" / "dist").mkdir(parents=True)
            yield base

    @pytest.fixture(scope="class")
    def mock_config(self, temp_dir):
        """Create mock api_keys.json."""
        config_path = temp_dir / "config" / "api_keys.json"
        config_path.write_text(json.dumps({
            "gemini_api_key": "test-key",
            "router_mode": "smart",
            "nvidia_nim_enabled": True,
            "ollama_enabled": True,
        }))

    @pytest.fixture(scope="class")
    def mock_usage_journal(self, temp_dir):
        """Create mock llm_usage.jsonl with sample data."""
        usage_path = temp_dir / "memory" / "llm_usage.jsonl"
        today = "2026-11-23"
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

    @pytest.fixture(scope="class")
    def mock_budget_config(self, temp_dir):
        """Create mock cloud_budget.json."""
        budget_path = temp_dir / "config" / "cloud_budget.json"
        budget_path.write_text(json.dumps({
            "daily_limits": {"gemini": 200000, "nvidia": 100000},
            "monthly_limits": {"gemini": 2000000, "nvidia": 1000000},
            "per_request_cap": 8000,
            "alert_thresholds": [0.5, 0.75, 0.9],
            "hard_block_at": 1.0,
            "exclude_local": True,
        }))

    @pytest.fixture(scope="class")
    def app_server(self, temp_dir, mock_config, mock_usage_journal, mock_budget_config):
        """Start FastAPI test server."""
        import uvicorn
        import threading
        import time
        from dashboard.server import create_app
        from unittest.mock import patch

        with patch("dashboard.server.BASE_DIR", temp_dir):
            app = create_app()

            config = uvicorn.Config(app, host="127.0.0.1", port=8765, log_level="error")
            server = uvicorn.Server(config)
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()

            # Wait for server to start
            time.sleep(1)

            yield "http://127.0.0.1:8765"

            server.should_exit = True
            thread.join(timeout=2)

    def test_usage_page_loads(self, app_server):
        """Usage page should load without errors."""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            page.goto(f"{app_server}/usage")

            # Check page title
            expect(page).to_have_title("Usage & Budget — RIO Dashboard")

            # Check main heading
            expect(page.locator("h1")).to_contain_text("Usage & Budget")

            browser.close()

    def test_usage_page_shows_summary_cards(self, app_server):
        """Should show summary cards for each provider."""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            page.goto(f"{app_server}/usage")

            # Wait for data to load
            page.wait_for_selector("[data-testid='summary-card']", timeout=5000)

            # Should have cards for gemini, ollama, nvidia
            cards = page.locator("[data-testid='summary-card']")
            expect(cards).to_have_count(3)

            # Check gemini card
            gemini_card = page.locator("[data-testid='summary-card']:has-text('gemini')")
            expect(gemini_card).to_be_visible()
            expect(gemini_card.locator("[data-testid='total-tokens']")).to_contain_text("5,000")

            browser.close()

    def test_usage_page_shows_charts(self, app_server):
        """Should render Recharts visualizations."""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            page.goto(f"{app_server}/usage")

            # Wait for charts to render
            page.wait_for_selector("svg.recharts-surface", timeout=5000)

            # Should have multiple charts
            charts = page.locator("svg.recharts-surface")
            expect(charts).to_have_count_greater_than(0)

            browser.close()

    def test_budget_progress_bars(self, app_server):
        """Should show budget progress bars with alerts."""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            page.goto(f"{app_server}/usage")

            # Wait for budget section
            page.wait_for_selector("[data-testid='budget-section']", timeout=5000)

            # Check budget progress bars
            progress_bars = page.locator("[data-testid='budget-progress']")
            expect(progress_bars).to_have_count_greater_than(0)

            # Check alert badges
            alerts = page.locator("[data-testid='budget-alert']")
            # Should show alerts for providers near thresholds

            browser.close()

    def test_router_status_table(self, app_server):
        """Should display router status table."""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            page.goto(f"{app_server}/usage")

            # Wait for router status
            page.wait_for_selector("[data-testid='router-status-table']", timeout=5000)

            table = page.locator("[data-testid='router-status-table']")
            expect(table).to_be_visible()

            # Check columns
            expect(table.locator("th:has-text('Provider')")).to_be_visible()
            expect(table.locator("th:has-text('Status')")).to_be_visible()
            expect(table.locator("th:has-text('Model')")).to_be_visible()

            browser.close()

    def test_recent_requests_table(self, app_server):
        """Should display recent requests table."""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            page.goto(f"{app_server}/usage")

            # Wait for requests table
            page.wait_for_selector("[data-testid='requests-table']", timeout=5000)

            table = page.locator("[data-testid='requests-table']")
            expect(table).to_be_visible()

            # Should have 3 rows (one per usage record)
            rows = table.locator("tbody tr")
            expect(rows).to_have_count(3)

            browser.close()

    def test_budget_edit_modal(self, app_server):
        """Should allow editing budget limits via modal."""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            page.goto(f"{app_server}/usage")

            # Click edit budget button
            page.click("[data-testid='edit-budget-btn']")

            # Modal should open
            modal = page.locator("[data-testid='budget-modal']")
            expect(modal).to_be_visible()

            # Update daily limit
            page.fill("[data-testid='daily-limit-gemini']", "150000")
            page.click("[data-testid='save-budget-btn']")

            # Should close and update
            expect(modal).to_be_hidden()

            browser.close()

    def test_time_range_filter(self, app_server):
        """Should filter data by time range."""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            page.goto(f"{app_server}/usage")

            # Select different time range
            page.select_option("[data-testid='time-range-select']", "7d")

            # Data should reload
            page.wait_for_selector("[data-testid='summary-card']", timeout=5000)

            browser.close()

    def test_provider_filter(self, app_server):
        """Should filter requests by provider."""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            page.goto(f"{app_server}/usage")

            # Select provider filter
            page.select_option("[data-testid='provider-filter']", "gemini")

            # Requests table should show only gemini
            page.wait_for_selector("[data-testid='requests-table']", timeout=5000)
            rows = page.locator("[data-testid='requests-table'] tbody tr")
            expect(rows).to_have_count(1)

            browser.close()

    def test_pwa_installable(self, app_server):
        """Usage page should be part of installable PWA."""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            page.goto(f"{app_server}/usage")

            # Check manifest link
            manifest_link = page.locator("link[rel='manifest']")
            expect(manifest_link).to_have_attribute("href", "/manifest.json")

            # Check service worker registration
            sw_script = page.locator("script:has-text('serviceWorker')")
            expect(sw_script).to_be_attached()

            browser.close()


class TestUsageAPIIntegration:
    """Integration tests for usage API endpoints."""

    @pytest.fixture
    def temp_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "config").mkdir()
            (base / "memory").mkdir()
            yield base

    def test_summary_endpoint_returns_json(self, temp_dir):
        """Summary endpoint should return valid JSON."""
        from dashboard.server import create_app
        from fastapi.testclient import TestClient
        from unittest.mock import patch

        with patch("dashboard.server.BASE_DIR", temp_dir):
            app = create_app()
            client = TestClient(app)

            resp = client.get("/api/usage/summary")
            assert resp.status_code == 200
            assert isinstance(resp.json(), dict)

    def test_detail_endpoint_pagination(self, temp_dir):
        """Detail endpoint should support pagination."""
        from dashboard.server import create_app
        from fastapi.testclient import TestClient
        from unittest.mock import patch

        with patch("dashboard.server.BASE_DIR", temp_dir):
            app = create_app()
            client = TestClient(app)

            resp = client.get("/api/usage/detail?limit=2&offset=0")
            assert resp.status_code == 200
            data = resp.json()
            assert "data" in data
            assert len(data["data"]) <= 2

    def test_budget_endpoint_validates_input(self, temp_dir):
        """Budget POST should validate input."""
        from dashboard.server import create_app
        from fastapi.testclient import TestClient
        from unittest.mock import patch

        with patch("dashboard.server.BASE_DIR", temp_dir):
            app = create_app()
            client = TestClient(app)

            # Invalid per_request_cap
            resp = client.post("/api/usage/budget", json={
                "per_request_cap": -1
            })
            assert resp.status_code == 422  # Validation error

            # Valid input
            resp = client.post("/api/usage/budget", json={
                "per_request_cap": 4000
            })
            assert resp.status_code == 200


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "browser_e2e"])