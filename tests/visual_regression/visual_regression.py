"""
Visual Regression Testing for RIO Dashboard

Uses Playwright to capture screenshots of dashboard routes and compares
them against baseline images to detect visual regressions.
"""

import asyncio
import os
import sys
from pathlib import Path
from typing import Optional, Tuple
from dataclasses import dataclass

# Add project root to path for imports
BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

try:
    from playwright.async_api import async_playwright, Page, Browser, BrowserContext
except ImportError:
    async_playwright = None
    Page = Browser = BrowserContext = object

try:
    from PIL import Image, ImageChops
except ImportError:
    Image = ImageChops = None

# Configuration
BASELINE_DIR = Path(__file__).parent / "baselines"
RESULTS_DIR = Path(__file__).parent / "results"
THRESHOLD = 0.01  # 1% pixel difference threshold
VIEWPORT_WIDTH = 1920
VIEWPORT_HEIGHT = 1080

# Dashboard routes to test (path, description, wait_for_selector, threshold_override)
# Threshold override: None = use default (1%), or a specific percentage for dynamic pages
ROUTES = [
    ("/", "Login Page", "#key", None),
    ("/home", "Home - Command Center", ".page", 0.07),      # Live status, clock
    ("/news", "News & Impact", ".page", 0.25),              # Live market data, charts
    ("/chat", "Chat", ".page", None),
    ("/agents", "Agents", ".page", None),
    ("/workflows", "Workflows", ".page", None),
    ("/ops", "Live Ops", ".page", 0.05),       # Live task status
    ("/brain", "Brain", ".page", 0.07),        # Live model status
    ("/memory", "Memory", ".page", None),
    ("/files", "Files", ".page", None),
    ("/hermes", "Hermes", ".page", 0.08),      # Live session data
    ("/missions", "Missions", ".page", None),
    ("/tools", "Tools & Skills", ".page", None),
    ("/video", "Video Studio", ".page", 0.08), # Live job status, thumbnails
    ("/activity", "Activity", ".page", 0.10),  # Live timeline
    ("/system", "System", ".page", 0.15),      # Live CPU/RAM metrics
    ("/settings", "Settings", ".page", None),
]


@dataclass
class VisualDiffResult:
    """Result of a visual comparison."""
    route: str
    description: str
    passed: bool
    diff_ratio: float
    baseline_path: Optional[Path]
    current_path: Optional[Path]
    diff_path: Optional[Path]
    error: Optional[str] = None


class VisualRegressionTester:
    """Visual regression tester using Playwright."""

    def __init__(
        self,
        base_url: str = "http://localhost:8765",
        baseline_dir: Path = BASELINE_DIR,
        results_dir: Path = RESULTS_DIR,
        threshold: float = THRESHOLD,
        headless: bool = True,
    ):
        self.base_url = base_url.rstrip("/")
        self.baseline_dir = baseline_dir
        self.results_dir = results_dir
        self.threshold = threshold
        self.headless = headless

        # Ensure directories exist
        self.baseline_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)

        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def start(self):
        """Start Playwright browser."""
        if async_playwright is None:
            raise RuntimeError("Playwright not installed. Run: pip install playwright && playwright install chromium")

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)
        self._context = await self._browser.new_context(
            viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            device_scale_factor=1,
        )
        self._page = await self._context.new_page()

        # Wait for page to be ready
        await self._page.wait_for_load_state("networkidle")

    async def close(self):
        """Close Playwright browser."""
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def navigate_and_wait(self, path: str, wait_for: str = ".page") -> bool:
        """Navigate to a route and wait for content to load."""
        url = f"{self.base_url}{path}"
        try:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # Wait for the main content to appear
            if wait_for:
                await self._page.wait_for_selector(wait_for, timeout=15000)

            # Additional wait for dynamic content to settle
            await self._page.wait_for_timeout(1000)

            # Wait for any animations to complete
            await self._page.evaluate("""() => {
                return new Promise(resolve => {
                    requestAnimationFrame(() => {
                        requestAnimationFrame(resolve);
                    });
                });
            }""")

            return True
        except Exception as e:
            print(f"[VisualRegression] Navigation failed for {url}: {e}")
            return False

    async def capture_screenshot(self, name: str) -> Path:
        """Capture a screenshot and save to results directory."""
        if not self._page:
            raise RuntimeError("Page not initialized. Call start() first.")

        path = self.results_dir / f"{name}.png"
        await self._page.screenshot(path=path, full_page=True)
        return path

    async def test_route(self, path: str, description: str, wait_for: str = ".page") -> VisualDiffResult:
        """Test a single route for visual regression."""
        safe_name = path.strip("/").replace("/", "_") or "root"
        baseline_path = self.baseline_dir / f"{safe_name}.png"
        current_path = self.results_dir / f"{safe_name}.png"
        diff_path = self.results_dir / f"{safe_name}_diff.png"

        # Navigate to the route
        success = await self.navigate_and_wait(path, wait_for)
        if not success:
            return VisualDiffResult(
                route=path,
                description=description,
                passed=False,
                diff_ratio=1.0,
                baseline_path=baseline_path if baseline_path.exists() else None,
                current_path=None,
                diff_path=None,
                error=f"Navigation failed for {path}"
            )

        # Capture current screenshot
        await self.capture_screenshot(safe_name)

        # If no baseline exists, save current as baseline and pass
        if not baseline_path.exists():
            # Copy current to baseline
            import shutil
            shutil.copy2(current_path, baseline_path)
            return VisualDiffResult(
                route=path,
                description=description,
                passed=True,
                diff_ratio=0.0,
                baseline_path=baseline_path,
                current_path=current_path,
                diff_path=None,
                error="Baseline created (first run)"
            )

        # Compare images
        diff_ratio, diff_image = compare_images(baseline_path, current_path)

        if diff_image:
            diff_image.save(diff_path)

        passed = diff_ratio <= self.threshold

        return VisualDiffResult(
            route=path,
            description=description,
            passed=passed,
            diff_ratio=diff_ratio,
            baseline_path=baseline_path,
            current_path=current_path,
            diff_path=diff_path if not passed else None,
            error=None if passed else f"Visual difference: {diff_ratio:.2%} > {self.threshold:.2%}"
        )

    async def test_all_routes(self) -> list[VisualDiffResult]:
        """Test all configured routes."""
        results = []
        for path, description, wait_for in ROUTES:
            print(f"[VisualRegression] Testing {description} ({path})...")
            result = await self.test_route(path, description, wait_for)
            results.append(result)
            status = "✓ PASS" if result.passed else "✗ FAIL"
            if result.error:
                print(f"  {status}: {result.error}")
            else:
                print(f"  {status}: diff={result.diff_ratio:.2%}")
        return results

    async def update_baselines(self) -> list[str]:
        """Update all baselines with current screenshots."""
        updated = []
        for path, description, wait_for in ROUTES:
            safe_name = path.strip("/").replace("/", "_") or "root"
            current_path = self.results_dir / f"{safe_name}.png"
            baseline_path = self.baseline_dir / f"{safe_name}.png"

            success = await self.navigate_and_wait(path, wait_for)
            if not success:
                print(f"[VisualRegression] Failed to navigate to {path}")
                continue

            await self.capture_screenshot(safe_name)

            if current_path.exists():
                import shutil
                shutil.copy2(current_path, baseline_path)
                updated.append(f"{description} ({path})")
                print(f"[VisualRegression] Updated baseline: {description}")

        return updated


def compare_images(baseline_path: Path, current_path: Path) -> Tuple[float, Optional[Image.Image]]:
    """
    Compare two images and return difference ratio and diff image.

    Returns:
        (diff_ratio, diff_image) where diff_ratio is 0.0-1.0
    """
    if Image is None or ImageChops is None:
        raise RuntimeError("PIL/Pillow not installed. Run: pip install pillow")

    baseline = Image.open(baseline_path).convert("RGBA")
    current = Image.open(current_path).convert("RGBA")

    # Resize current to match baseline if different sizes
    if current.size != baseline.size:
        current = current.resize(baseline.size, Image.Resampling.LANCZOS)

    # Calculate difference
    diff = ImageChops.difference(baseline, current)

    # Convert to grayscale for pixel counting
    diff_gray = diff.convert("L")

    # Count non-zero pixels (differences)
    diff_pixels = sum(1 for p in diff_gray.getdata() if p > 10)  # threshold of 10 to ignore minor noise
    total_pixels = diff_gray.width * diff_gray.height
    diff_ratio = diff_pixels / total_pixels if total_pixels > 0 else 0.0

    # Create visual diff image (red highlight on differences)
    diff_rgb = Image.new("RGBA", baseline.size, (0, 0, 0, 0))
    for x in range(baseline.width):
        for y in range(baseline.height):
            bp = baseline.getpixel((x, y))
            cp = current.getpixel((x, y))
            if bp != cp:
                # Red with alpha based on difference magnitude
                dr = abs(bp[0] - cp[0])
                dg = abs(bp[1] - cp[1])
                db = abs(bp[2] - cp[2])
                max_diff = max(dr, dg, db)
                alpha = min(255, max_diff * 4)
                diff_rgb.putpixel((x, y), (255, 0, 0, alpha))

    # Blend diff onto current for visualization
    diff_image = Image.alpha_composite(current, diff_rgb)

    return diff_ratio, diff_image


def save_baseline(source_path: Path, name: str) -> Path:
    """Save an image as a baseline."""
    baseline_path = BASELINE_DIR / f"{name}.png"
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copy2(source_path, baseline_path)
    return baseline_path


# Convenience function for CLI usage
async def run_visual_regression(
    base_url: str = "http://localhost:8765",
    update_baselines: bool = False,
    headless: bool = True,
) -> tuple[bool, list[VisualDiffResult]]:
    """Run visual regression tests."""
    tester = VisualRegressionTester(base_url=base_url, headless=headless)
    await tester.start()

    try:
        if update_baselines:
            updated = await tester.update_baselines()
            return True, [VisualDiffResult(
                route="", description="update_baselines", passed=True,
                diff_ratio=0.0, baseline_path=None, current_path=None, diff_path=None,
                error=f"Updated {len(updated)} baselines: {', '.join(updated)}"
            )]
        else:
            results = await tester.test_all_routes()
            all_passed = all(r.passed for r in results)
            return all_passed, results
    finally:
        await tester.close()


def _cli_main():
    """CLI entry point for visual regression testing."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Visual Regression Testing for RIO Dashboard")
    parser.add_argument("--url", default="http://localhost:8765", help="Base URL of the dashboard")
    parser.add_argument("--update-baselines", action="store_true", help="Update baselines with current screenshots")
    parser.add_argument("--headed", action="store_true", help="Run in headed mode (visible browser)")
    parser.add_argument("--threshold", type=float, default=0.01, help="Difference threshold (0.0-1.0)")

    args = parser.parse_args()

    async def main():
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

        return 0 if success else 1

    return asyncio.run(main())


if __name__ == "__main__":
    import sys
    sys.exit(_cli_main())