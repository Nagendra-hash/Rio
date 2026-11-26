"""
Visual Regression Tests for RIO Dashboard

These tests capture screenshots of dashboard routes and compare them
against baseline images to detect visual regressions.

Run:  .venv/bin/python -m pytest tests/test_visual_regression.py -v -m "visual_regression"

To update baselines: .venv/bin/python -m tests.visual_regression --update-baselines
"""

import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import pytest

# Add project root to path
BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from tests.visual_regression.visual_regression import (
    VisualRegressionTester,
    ROUTES,
    run_visual_regression,
    compare_images,
    BASELINE_DIR,
    RESULTS_DIR,
    THRESHOLD,
)

# Check if Playwright is available
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# Check if browsers are installed
def _browsers_installed() -> bool:
    cache_dir = Path.home() / ".cache" / "ms-playwright"
    if not cache_dir.exists():
        return False
    # Check for chromium
    for item in cache_dir.iterdir():
        if item.is_dir() and item.name.startswith("chromium"):
            return True
    return False

BROWSERS_AVAILABLE = _browsers_installed()

# Visual regression tests require Playwright and browsers
visual_regression_skip = pytest.mark.skipif(
    not (PLAYWRIGHT_AVAILABLE and BROWSERS_AVAILABLE),
    reason="Playwright or browsers not available. Run: pip install playwright && playwright install chromium"
)

# This marker is used by conftest to identify browser E2E tests
pytestmark = [pytest.mark.visual_regression, pytest.mark.e2e]


def _free_port() -> int:
    """Find a free port for the test server."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait(url: str, timeout: float = 30) -> bool:
    """Wait for a URL to become available."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except Exception:
            time.sleep(0.2)
    return False


class _TestServer:
    """Test dashboard server for visual regression tests."""

    def __init__(self):
        self.port = _free_port()
        self.srv = None
        self.server = None
        self.thread = None
        self._tmpdir = None
        self._tmp_config = None
        self._tmp_devices = None

    def start(self) -> None:
        """Start a fresh dashboard server instance."""
        # Create temp directory for config isolation
        self._tmpdir = tempfile.mkdtemp(prefix="jarvis-visual-e2e-")
        self._tmp_config = Path(self._tmpdir) / "api_keys.json"
        self._tmp_devices = Path(self._tmpdir) / "dashboard_devices.json"

        # Write test config
        self._tmp_config.write_text(json.dumps({"dashboard_auth_token": "e2e-static-token-1234"}))

        # Import and configure after temp paths are set
        import memory.config_manager as cfg
        cfg.CONFIG_FILE = self._tmp_config

        from dashboard.server import DashboardServer
        self.srv = DashboardServer()          # fresh instance every run
        # Note: DashboardServer doesn't persist devices to file; it keeps them in memory
        # We pair a fresh device in each test run anyway
        self.srv._device_sessions = {}

        import uvicorn
        config = uvicorn.Config(self.srv.app, host="127.0.0.1", port=self.port,
                                log_level="error")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()

        if not _wait(f"http://127.0.0.1:{self.port}/login"):
            raise RuntimeError("test server did not come up")

    def stop(self) -> None:
        """Stop the test server."""
        if self.server:
            self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=10)
        # Wait until port is truly free
        end = time.time() + 10
        while time.time() < end:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/login", timeout=0.5)
                time.sleep(0.2)
            except Exception:
                return

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def _pair_device(base: str) -> tuple[str, str]:
    """Pair a device exactly like a real phone (pair API → auto-login page)."""
    import re
    req = urllib.request.Request(
        f"{base}/api/phone/pair", method="POST",
        headers={"Authorization": "Bearer e2e-static-token-1234"})
    pair = json.load(urllib.request.urlopen(req))
    key = re.search(r"key=([A-Z2-9]+)", pair["link"]).group(1)
    body = urllib.request.urlopen(urllib.request.Request(
        f"{base}/auto-login?key={key}",
        headers={"User-Agent": "Mozilla/5.0 (E2E)"})).read().decode()
    dev = re.search(r"localStorage\.setItem\('jarvis_device_token','([^']+)'\)",
                    body).group(1)
    ses = re.search(r"sessionStorage\.setItem\('jarvis_token','([^']+)'\)",
                    body).group(1)
    return dev, ses


def _run_visual_regression_sync(
    base_url: str,
    device_tokens: tuple[str, str],
    update_baselines: bool = False,
) -> tuple[bool, list]:
    """Run visual regression tests synchronously."""
    from playwright.sync_api import sync_playwright

    dev_tok, ses_tok = device_tokens

    results = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True, channel="chrome",
            args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(
            storage_state=None,
            service_workers="block",
            viewport={"width": 1920, "height": 1080},
        )

        for route_tuple in ROUTES:
            path, description, wait_for, threshold_override = route_tuple
            threshold = threshold_override if threshold_override is not None else THRESHOLD

            safe_name = path.strip("/").replace("/", "_") or "root"
            baseline_path = BASELINE_DIR / f"{safe_name}.png"
            current_path = RESULTS_DIR / f"{safe_name}.png"
            diff_path = RESULTS_DIR / f"{safe_name}_diff.png"

            page = ctx.new_page()

            # Set authentication tokens for non-login pages
            if path != "/":
                page.add_init_script(
                    f"localStorage.setItem('jarvis_device_token','{dev_tok}');"
                    f"sessionStorage.setItem('jarvis_token','{ses_tok}');"
                )

            try:
                page.goto(f"{base_url}{path}", wait_until="domcontentloaded", timeout=30000)
                if wait_for:
                    page.wait_for_selector(wait_for, timeout=15000)
                page.wait_for_timeout(1000)

                # Capture screenshot
                page.screenshot(path=str(current_path), full_page=True)

                if update_baselines:
                    import shutil
                    shutil.copy2(current_path, baseline_path)
                    results.append({
                        "route": path,
                        "description": description,
                        "passed": True,
                        "diff_ratio": 0.0,
                        "error": "Baseline updated"
                    })
                    continue

                # If no baseline exists, save current as baseline and pass
                if not baseline_path.exists():
                    import shutil
                    shutil.copy2(current_path, baseline_path)
                    results.append({
                        "route": path,
                        "description": description,
                        "passed": True,
                        "diff_ratio": 0.0,
                        "error": "Baseline created (first run)"
                    })
                    continue

                # Compare images
                diff_ratio, diff_image = compare_images(baseline_path, current_path)

                if diff_image:
                    diff_image.save(diff_path)

                passed = diff_ratio <= threshold

                results.append({
                    "route": path,
                    "description": description,
                    "passed": passed,
                    "diff_ratio": diff_ratio,
                    "error": None if passed else f"Visual difference: {diff_ratio:.2%} > {threshold:.2%}"
                })

            except Exception as e:
                results.append({
                    "route": path,
                    "description": description,
                    "passed": False,
                    "diff_ratio": 1.0,
                    "error": f"Test error: {e}"
                })
            finally:
                page.close()

        browser.close()

    all_passed = all(r["passed"] for r in results)
    return all_passed, results


@visual_regression_skip
class TestVisualRegression:
    """Visual regression tests for dashboard routes."""

    @pytest.fixture(scope="class")
    def test_server(self):
        """Start a test server for the test class."""
        server = _TestServer()
        server.start()
        yield server
        server.stop()

    @pytest.fixture(scope="class")
    def base_url(self, test_server):
        """Base URL for the test server."""
        return test_server.base_url

    @pytest.fixture(scope="class")
    def device_tokens(self, base_url):
        """Pair a device and return tokens."""
        return _pair_device(base_url)

    @pytest.mark.parametrize("route_tuple", ROUTES)
    def test_route_visual_regression(self, base_url, device_tokens, route_tuple):
        """Test a single route for visual regression."""
        path, description, wait_for, threshold_override = route_tuple
        threshold = threshold_override if threshold_override is not None else THRESHOLD

        dev_tok, ses_tok = device_tokens

        safe_name = path.strip("/").replace("/", "_") or "root"
        baseline_path = BASELINE_DIR / f"{safe_name}.png"
        current_path = RESULTS_DIR / f"{safe_name}.png"
        diff_path = RESULTS_DIR / f"{safe_name}_diff.png"

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True, channel="chrome",
                args=["--no-sandbox", "--disable-dev-shm-usage"])
            ctx = browser.new_context(
                storage_state=None,
                service_workers="block",
                viewport={"width": 1920, "height": 1080},
            )
            page = ctx.new_page()

            # Set authentication tokens for non-login pages
            if path != "/":
                page.add_init_script(
                    f"localStorage.setItem('jarvis_device_token','{dev_tok}');"
                    f"sessionStorage.setItem('jarvis_token','{ses_tok}');"
                )

            try:
                page.goto(f"{base_url}{path}", wait_until="domcontentloaded", timeout=30000)
                if wait_for:
                    page.wait_for_selector(wait_for, timeout=15000)
                page.wait_for_timeout(1000)

                # Capture screenshot
                page.screenshot(path=str(current_path), full_page=True)

                # If no baseline exists, save current as baseline and pass
                if not baseline_path.exists():
                    import shutil
                    shutil.copy2(current_path, baseline_path)
                    assert True, f"Baseline created for {description} ({path})"
                    return

                # Compare images
                diff_ratio, diff_image = compare_images(baseline_path, current_path)

                if diff_image:
                    diff_image.save(diff_path)

                passed = diff_ratio <= threshold

                if not passed:
                    pytest.fail(
                        f"Visual regression detected for {description} ({path}): "
                        f"diff={diff_ratio:.2%} > threshold={threshold:.2%}. "
                        f"Diff image: {diff_path}"
                    )

            except Exception as e:
                if "Baseline created" in str(e):
                    raise  # Re-raise our assertion
                pytest.fail(f"Test error for {description} ({path}): {e}")
            finally:
                page.close()
                browser.close()

    def test_all_routes_summary(self, base_url, device_tokens):
        """Run all routes and provide a summary."""
        success, results = _run_visual_regression_sync(base_url, device_tokens)

        passed = sum(1 for r in results if r["passed"])
        total = len(results)
        failed = total - passed

        print(f"\n{'='*60}")
        print(f"Visual Regression Summary: {passed}/{total} passed")
        print(f"{'='*60}")

        for r in results:
            status = "✓" if r["passed"] else "✗"
            print(f"  {status} {r['description']} ({r['route']}): diff={r['diff_ratio']:.2%}")

        # This assertion will show the summary in pytest output
        assert failed == 0, f"{failed} visual regression(s) detected out of {total} routes"


@visual_regression_skip
class TestVisualRegressionCLI:
    """Test the CLI interface for visual regression."""

    def test_cli_help(self):
        """Test that the CLI help works."""
        import subprocess
        result = subprocess.run(
            [sys.executable, "-m", "tests.visual_regression", "--help"],
            capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0
        assert "Visual Regression Testing" in result.stdout


def test_visual_regression_module_imports():
    """Test that the visual regression module can be imported."""
    from tests.visual_regression import VisualRegressionTester, compare_images, save_baseline
    assert VisualRegressionTester is not None
    assert compare_images is not None
    assert save_baseline is not None


def test_routes_defined():
    """Test that routes are properly defined."""
    from tests.visual_regression.visual_regression import ROUTES
    assert len(ROUTES) > 0
    for route_tuple in ROUTES:
        path, description, wait_for, threshold_override = route_tuple
        assert isinstance(path, str)
        assert isinstance(description, str)
        assert isinstance(wait_for, str)
        assert path.startswith("/")
        # threshold_override can be None or a float
        assert threshold_override is None or isinstance(threshold_override, float)


def test_compare_images():
    """Test the compare_images function with identical images."""
    from tests.visual_regression.visual_regression import compare_images
    from PIL import Image

    # Create a test image
    img = Image.new("RGBA", (100, 100), (255, 0, 0, 255))
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f1:
        img.save(f1.name)
        path1 = Path(f1.name)
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f2:
        img.save(f2.name)
        path2 = Path(f2.name)

    try:
        diff_ratio, diff_image = compare_images(path1, path2)
        assert diff_ratio == 0.0
        assert diff_image is not None
    finally:
        path1.unlink(missing_ok=True)
        path2.unlink(missing_ok=True)


# Standalone runner for manual execution
if __name__ == "__main__":
    import asyncio

    async def main():
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--url", default="http://localhost:8765")
        parser.add_argument("--update-baselines", action="store_true")
        parser.add_argument("--headed", action="store_true")
        args = parser.parse_args()

        success, results = await run_visual_regression(
            base_url=args.url,
            update_baselines=args.update_baselines,
            headless=not args.headed,
        )

        print("\n" + "=" * 60)
        print("VISUAL REGRESSION TEST RESULTS")
        print("=" * 60)

        for r in results:
            status = "PASS" if r.passed else "FAIL"
            print(f"  [{status}] {r.description} ({r.route})")
            if r.error and not r.passed:
                print(f"    Error: {r.error}")
            elif r.error:
                print(f"    Info: {r.error}")

        if not args.update_baselines:
            passed = sum(1 for r in results if r.passed)
            total = len(results)
            print(f"\n  Summary: {passed}/{total} passed")

        sys.exit(0 if success else 1)

    asyncio.run(main())