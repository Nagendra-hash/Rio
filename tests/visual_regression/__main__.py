"""
Visual Regression Testing for RIO Dashboard - CLI Entry Point

Run:  .venv/bin/python -m tests.visual_regression --help
"""

from tests.visual_regression.visual_regression import _cli_main

if __name__ == "__main__":
    import sys
    sys.exit(_cli_main())