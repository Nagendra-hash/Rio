"""Offline checks for the browser-use integration (JARVIS action + Hermes skill + runner).

No network, no Chrome launch, no LLM call — the live smoke test (example.com
in 2 steps via FreeLLMAPI) was run at install time, 2026-09-26. These tests
lock the WIRING so a rename or move can't silently break the bridge.

Run:  .venv/bin/python -m pytest tests/test_browser_use_skill.py -q
"""

import ast
import re
import subprocess
import sys
from pathlib import Path

import json as _json

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

RUNNER = BASE / "scripts" / "browser_use_run.py"
ACTION = BASE / "actions" / "browser_use_task.py"
RELOGIN = BASE / "scripts" / "browser_use_relogin.py"
CREDS_TEMPLATE = BASE / "scripts" / "site_credentials.template.json"
HERMES_SKILL = Path.home() / ".hermes" / "skills" / "browser-use" / "SKILL.md"
HERMES_RELOGIN_SKILL = (
    Path.home() / ".hermes" / "skills" / "browser-use-relogin" / "SKILL.md"
)
REPO_SKILL = BASE / ".agents" / "skills" / "browser-use" / "SKILL.md"


def test_runner_parses_and_has_stable_surface():
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
    funcs = {
        n.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert {"_build_llm", "_run", "main"} <= funcs, f"runner surface changed: {funcs}"


def test_runner_never_hardcodes_keys():
    src = RUNNER.read_text(encoding="utf-8")
    # No provider key material embedded (a comment may legitimately say
    # "task-level", so match real key shapes rather than a bare "sk-").
    assert re.search(r"\bsk-[A-Za-z0-9]{16,}", src) is None
    assert "AQ." not in src
    # keys must come from config, not env-only
    assert "_CONFIG" in src and "freellmapi_api_key" in src
    # and never ride on argv
    assert "--api-key" not in src


def test_persistence_uses_profile_dir_not_storage_state():
    """browser-use 1.0 removed `storage_state`. The supported primitive is a
    profile directory passed as `user_data_dir=`, shared with the relogin
    script. A 0.x runner would silently lose every login on 1.0."""
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    kwargs = {kw.arg for call in calls for kw in call.keywords}

    assert "user_data_dir" in kwargs, "1.0 persists via user_data_dir"
    assert "storage_state" not in kwargs, "storage_state is gone in 1.0"
    assert "browser_session" not in kwargs, "browser_session is the 0.x spelling"

    imported = {
        a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names
    }
    assert "BrowserSession" not in imported
    assert "SaveStorageStateEvent" not in imported

    constants = {
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    assert "--no-persist" in constants, "ephemeral escape hatch missing"
    assert "ANONYMIZED_TELEMETRY" in constants, "telemetry must be switched off"


def test_runner_never_takes_the_paid_defaults():
    """1.0's defaults are a paid cloud browser and a paid gateway model."""
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"))

    browser_calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "Browser"
    ]
    assert browser_calls, "runner must construct a Browser explicitly"
    for call in browser_calls:
        assert call.args, "Browser() with no kind is the CLOUD default"
        assert isinstance(call.args[0], ast.Constant)
        assert call.args[0].value in ("chromium", "chrome")

    agent_calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "Agent"
    ]
    assert agent_calls, "runner must construct an Agent explicitly"
    for call in agent_calls:
        kwargs = {kw.arg for kw in call.keywords}
        assert "llm" in kwargs, "llm=None is the PAID gateway default"


def test_persistence_path_is_outside_repo():
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
    assigned = {
        t.id: ast.unparse(n.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        for t in n.targets
        if isinstance(t, ast.Name)
    }
    profile_root = assigned.get("_PROFILE_ROOT", "")
    profile_dir = assigned.get("_PROFILE_DIR", "")
    assert profile_dir, "_PROFILE_DIR missing"
    assert (
        "Path.home()" in profile_root
    ), "profile must live under home (holds session cookies)"
    assert "_PROFILE_ROOT" in profile_dir
    assert "Mark-LIII" not in profile_dir + profile_root, (
        "profile must NOT live inside the repo"
    )


def test_action_tool_declares_handler():
    from actions.browser_use_task import TOOL, browser_use_task

    assert TOOL["name"] == "browser_use_task"
    assert TOOL["handler"] is browser_use_task
    assert "browser" in TOOL["description"].lower()
    assert TOOL["parameters"]["required"] == ["task"]


def test_runtime_forwards_fresh_profile_to_the_runner():
    """The legacy alias's fresh_profile flag must reach the runner as the
    1.0 no-persist switch, through the shared runtime (one implementation)."""
    runtime_src = (BASE / "core" / "browser_agent" / "runtime.py").read_text(encoding="utf-8")
    assert "--no-persist" in runtime_src
    assert "fresh_profile" in runtime_src
    alias_src = ACTION.read_text(encoding="utf-8")
    assert "fresh_profile" in alias_src
    assert "browser_agent" in alias_src


def test_action_registers_in_loader(tmp_path=None):
    from pathlib import Path as _P
    from core.action_loader import discover_actions

    reg = discover_actions(_P(BASE) / "actions")
    assert "browser_use_task" in reg.names()


def test_hermes_skill_frontmatter():
    if not HERMES_SKILL.exists():
        __import__("pytest").skip(f"Hermes skill not installed at {HERMES_SKILL}")
    text = HERMES_SKILL.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, "missing frontmatter"
    fm = m.group(1)
    for field in (
        "name: browser-use",
        "description:",
        "category: web",
        "recommended_method: terminal",
        "verified: true",
    ):
        assert field in fm, f"frontmatter missing {field!r}"
    assert "browser_use_run.py" in text, "skill must point at the runner"


def test_repo_skill_references_resolve():
    text = REPO_SKILL.read_text(encoding="utf-8")
    for rel in re.findall(r"`((?:actions|scripts|core)/[\w./-]+?\.py)`", text):
        assert (BASE / rel).exists(), f"SKILL.md references missing path: {rel}"


def test_runner_help_runs_offline():
    proc = subprocess.run(
        [sys.executable, str(RUNNER), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert "--brain" in proc.stdout and "--max-steps" in proc.stdout


# ── Session recovery (browser_use_relogin.py) ────────────────────────────────


def test_relogin_surface_and_safety():
    src = RELOGIN.read_text(encoding="utf-8")
    tree = ast.parse(src)
    code_tokens = (
        {
            n.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        }
        | {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    )
    # verification is mandatory: the script must CHECK success, not assume it
    assert "success_text" in code_tokens and "success_url_part" in code_tokens
    # credentials hygiene
    assert "chmod" in src.lower(), "must enforce store permissions"
    # 1.0 persistence primitive: the SAME profile dir the runner uses.
    assert "launch_persistent_context" in code_tokens
    assert "PROFILE_DIR" in code_tokens
    assert "storage_state" not in code_tokens, "1.0 removed storage_state"
    assert str(RELOGIN).endswith("browser_use_relogin.py")


def test_relogin_refuses_loose_permissions(tmp_path):
    import json as _json
    import os

    # the script resolves the store under HOME/.local/share/jarvis-browser-profile
    store_dir = tmp_path / ".local" / "share" / "jarvis-browser-profile"
    store_dir.mkdir(parents=True)
    store = store_dir / "site_credentials.json"
    store.write_text(_json.dumps({"x": {"login_url": "http://x/"}}))
    store.chmod(0o644)  # group/others readable
    proc = subprocess.run(
        [sys.executable, str(RELOGIN), "x", "--list"],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        },
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "REFUSING" in proc.stderr and "chmod 600" in proc.stderr


def test_relogin_template_is_safe_and_trackable():
    if not (BASE / ".git").exists():
        __import__("pytest").skip("git metadata absent in this checkout")
    data = _json.loads(CREDS_TEMPLATE.read_text(encoding="utf-8"))
    for key, value in data.items():
        assert key.startswith(
            "_"
        ), f"template must only contain _-prefixed example keys, got {key!r}"
        if isinstance(value, dict):
            assert "CHANGE_ME" in str(
                value.get("password", "")
            ), "example password must be a placeholder"
    proc = subprocess.run(
        ["git", "check-ignore", str(CREDS_TEMPLATE)], capture_output=True, timeout=15
    )
    assert proc.returncode == 1, "template must NOT be gitignored"


def test_hermes_relogin_skill_installed():
    if not HERMES_RELOGIN_SKILL.exists():
        __import__("pytest").skip(
            f"Hermes skill not installed at {HERMES_RELOGIN_SKILL}"
        )
    text = HERMES_RELOGIN_SKILL.read_text(encoding="utf-8")
    assert "name: browser-use-relogin" in text
    assert "browser_use_relogin.py" in text
    assert "verified: true" in text
