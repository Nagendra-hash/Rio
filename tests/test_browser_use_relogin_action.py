"""Offline checks for the browser_use_relogin JARVIS action.

No network, no Chrome: the live paths (list / recover / unknown site) were
drilled at install time, 2026-09-26. These tests lock the wiring — TOOL
shape, loader registration, subprocess argument mapping, and the rc→speech
mapping (the part a rename would silently break).

Run:  .venv/bin/python -m pytest tests/test_browser_use_relogin_action.py -q
"""

import ast
import sys
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

ACTION_FILE = BASE / "actions" / "browser_use_relogin.py"
RELOGIN_SCRIPT = BASE / "scripts" / "browser_use_relogin.py"


def test_tool_declares_handler_and_optional_site():
    from actions.browser_use_relogin import TOOL, browser_use_relogin

    assert TOOL["name"] == "browser_use_relogin"
    assert TOOL["handler"] is browser_use_relogin
    assert (
        TOOL["parameters"]["required"] == []
    ), "site must be optional so the model can list sites"
    assert "visible" in TOOL["parameters"]["properties"]
    assert "2FA" in TOOL["description"] or "captcha" in TOOL["description"]


def test_action_registers_in_loader():
    from core.action_loader import discover_actions

    reg = discover_actions(BASE / "actions")
    assert "browser_use_relogin" in reg.names()
    assert "browser_use_task" in reg.names()


def test_empty_site_lists_sites_without_subprocess_login():
    """No site → list configured sites; must run --list, never a login."""
    from actions import browser_use_relogin as m

    with patch.object(m.subprocess, "run") as fake:
        fake.return_value = type(
            "P", (), {"returncode": 0, "stdout": "  localapp  http://x/", "stderr": ""}
        )()
        out = m.browser_use_relogin({})
        assert "localapp" in out and "Which site" in out
        assert fake.call_count == 1
        argv = fake.call_args.args[0]
        assert "--list" in argv, "empty site must use --list, not attempt a login"


def test_rc0_speaks_success():
    from actions import browser_use_relogin as m

    proc = type(
        "P",
        (),
        {
            "returncode": 0,
            "stdout": "",
            "stderr": "[relogin] x: logged in (url=http://x/priv)",
        },
    )
    with patch.object(m.subprocess, "run", return_value=proc):
        out = m.browser_use_relogin({"site": "x"})
    assert "Logged back into x" in out
    assert "[relogin]" not in out, "internal prefix must not be spoken"
    assert "http://x/priv" in out


def test_rc2_missing_credentials_never_asks_in_chat():
    from actions import browser_use_relogin as m

    proc = type(
        "P",
        (),
        {
            "returncode": 2,
            "stdout": "",
            "stderr": "no credentials for site 'x'. Known: (none)",
        },
    )
    with patch.object(m.subprocess, "run", return_value=proc):
        out = m.browser_use_relogin({"site": "x"})
    assert "no saved credentials" in out
    assert "never tell them to me in chat" in out


def test_rc1_failure_surfaces_evidence_and_visible_hint():
    from actions import browser_use_relogin as m

    proc = type(
        "P",
        (),
        {
            "returncode": 1,
            "stdout": "",
            "stderr": "[relogin] FAILED for x: url=http://x/login page='Sign in'",
        },
    )
    with patch.object(m.subprocess, "run", return_value=proc):
        out = m.browser_use_relogin({"site": "x"})
    assert "failed" in out and "Sign in" in out
    assert "visible" in out


def test_flags_forwarded_to_script():
    from actions import browser_use_relogin as m

    proc = type("P", (), {"returncode": 0, "stdout": "", "stderr": "ok"})
    with patch.object(m.subprocess, "run", return_value=proc) as fake:
        m.browser_use_relogin({"site": "x", "clear": True, "visible": True})
    argv = fake.call_args.args[0]
    assert str(RELOGIN_SCRIPT) == argv[1]
    assert "--clear" in argv and "--visible" in argv
    assert "x" in argv


def test_timeout_mentions_visible_option():
    from actions import browser_use_relogin as m
    import subprocess as sp

    def _raise(*a, **k):
        raise sp.TimeoutExpired(cmd="x", timeout=1)

    with patch.object(m.subprocess, "run", side_effect=_raise):
        out = m.browser_use_relogin({"site": "x"})
    assert "timed out" in out and "visible" in out


def test_script_never_prints_credentials():
    """The recovery script must not echo store values — scan string literals
    and print/write call sites for the store fields."""
    src = (BASE / "scripts" / "browser_use_relogin.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    printed = set()
    for n in ast.walk(tree):
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "print"
        ):
            for arg in n.args:
                printed.add(ast.unparse(arg))
    joined = " ".join(printed)
    for secret in (
        "entry['password']",
        'entry["password"]',
        "entry['username']",
        'entry["username"]',
    ):
        assert secret not in joined, f"script prints credential field: {secret}"


if __name__ == "__main__":
    fns = [
        (n, f)
        for n, f in sorted(globals().items())
        if n.startswith("test_") and callable(f)
    ]
    failed = 0
    for name, fn in fns:
        try:
            fn()
        except Exception as e:
            failed += 1
            print(f"✗ {name}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
