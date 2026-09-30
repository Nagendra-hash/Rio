"""
tests/conftest.py — shared pytest setup for the JARVIS test suite.

Provides:
  * Marker registration: `browser_e2e` (spins up a real headless Chrome via
    Playwright against the live dashboard server) and `e2e` (end-to-end test
    against a live server, browser or not). Registered in pytest.ini, so
    `-m "not browser_e2e"` and `--strict-markers` both work.
  * Environment detection: browser-E2E tests are SKIPped (not errored) when
    Playwright or system Chrome is missing, with the exact fix in the reason.
  * A result adapter: this repo's standalone test functions return
    `(ok: bool, message: str)` per the repo convention (see the __main__
    runners). Plain pytest would ignore those returns — failures would show
    as PASS plus a PytestReturnNotNoneWarning. The adapter turns a
    `(False, msg)` return into a real assertion failure and a `(True, msg)`
    into a pass, while letting normal `assert`-style/None-returning tests
    through untouched.

Run everything with one command:
    .venv/bin/python -m pytest tests/ -q
Unit-only fast pass:
    .venv/bin/python -m pytest tests/ -q -m "not browser_e2e"
Only the browser E2E suite:
    .venv/bin/python -m pytest tests/ -q -m "browser_e2e"
"""
import functools
import shutil

import pytest

# The repo's browser-E2E tests (launch real Chrome; each also has cheap
# static sibling checks that should always run).
BROWSER_TEST_NAMES = {
    "test_offline_flow",        # tests/test_dashboard_offline.py
    "test_pwa_install_flow",    # tests/test_dashboard_pwa_install.py
    "test_voice_keepalive",     # tests/test_voice_keepalive.py
}

# (flag_name, message) set once the environment has been checked.
_ENV_CACHE: dict[str, str] = {}


def _browser_env_missing() -> str:
    """Reason the browser E2E suite can't run here ('' when it can).
    Checks the import inside a SUBPROCESS — a module-level import here would
    be cached in sys.modules of this process, so a successful earlier import
    (or a test that blocked it) would poison the check for every item."""
    if "reason" not in _ENV_CACHE:
        import subprocess, sys
        code = (
            "try:\n"
            "    import playwright  # noqa\n"
            "    print('OK')\n"
            "except Exception as e:\n"
            "    print(type(e).__name__)\n"
        )
        try:
            r = subprocess.run([sys.executable, "-c", code],
                               capture_output=True, text=True, timeout=30)
            if r.stdout.strip() != "OK":
                _ENV_CACHE["reason"] = (
                    "playwright not importable — .venv/bin/pip install playwright"
                    " && .venv/bin/playwright install chromium")
                return _ENV_CACHE["reason"]
        except Exception as e:
            _ENV_CACHE["reason"] = f"could not probe the python env: {e}"
            return _ENV_CACHE["reason"]
        chrome = (shutil.which("google-chrome")
                  or shutil.which("google-chrome-stable")
                  or shutil.which("chromium")
                  or shutil.which("chromium-browser"))
        if not chrome:
            _ENV_CACHE["reason"] = (
                "no system Chrome for channel='chrome' — install Google Chrome"
                " (or adapt the tests to the bundled chromium)")
        else:
            _ENV_CACHE["reason"] = ""
    return _ENV_CACHE["reason"]


def pytest_collection_modifyitems(config, items):
    for item in items:
        # Only plain module-level test functions; leave unittest.TestCase
        # methods alone (pytest runs them through its unittest support and
        # their assertions already work natively).
        if not isinstance(item, pytest.Function):
            continue
        if item.getparent(pytest.Class) is not None:
            continue

        if item.name in BROWSER_TEST_NAMES:
            item.add_marker(pytest.mark.e2e)
            missing = _browser_env_missing()
            if missing:
                item.add_marker(
                    pytest.mark.skip(reason=f"browser E2E unavailable: {missing}"))
            else:
                item.add_marker(pytest.mark.browser_e2e)

        # ── adapt (ok, msg)-returning test functions ────────────────────────
        obj = item.obj
        if getattr(obj, "_jarvis_result_adapted", False):
            continue

        @functools.wraps(obj)
        def _adapted(*args, _fn=obj, **kwargs):
            result = _fn(*args, **kwargs)
            if (isinstance(result, tuple) and len(result) == 2
                    and isinstance(result[0], bool)
                    and isinstance(result[1], str)):
                ok, msg = result
                assert ok, msg
                return None          # swallowed — pytest wants None from tests
            # assert-style tests and anything else pass through untouched
            return result

        _adapted._jarvis_result_adapted = True
        item.obj = _adapted
