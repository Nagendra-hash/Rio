"""
Visual Regression Testing for RIO Dashboard

Uses Playwright to capture screenshots of dashboard routes and compares
them against baseline images to detect visual regressions.

Run:  .venv/bin/python -m pytest tests/visual_regression/ -v -m "visual_regression"
"""

from .visual_regression import VisualRegressionTester, compare_images, save_baseline

__all__ = ["VisualRegressionTester", "compare_images", "save_baseline"]