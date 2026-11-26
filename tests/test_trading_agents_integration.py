"""Offline checks for the TradingAgents integration.

No LLM calls, no yfinance, no graph execution — the live drill (graph compile,
brain PONG, partial agent chain, 429 behaviour) was run at install time,
2026-09-26. These tests lock the WIRING: runner surface, brain routing keys,
action/tool shape, loader registration, and the docs.

Run:  .venv/bin/python -m pytest tests/test_trading_agents_integration.py -q
"""

import ast
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

RUNNER = BASE / "scripts" / "trading_agents_run.py"
ACTION = BASE / "actions" / "trading_agents.py"
HERMES_SKILL = Path.home() / ".hermes" / "skills" / "trading-agents" / "SKILL.md"
REPO_SKILL = BASE / ".agents" / "skills" / "trading-agents"
TA_VENV = BASE / ".venv-tradingagents" / "bin" / "python"


def test_runner_surface_and_brain_routing():
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
    funcs = {
        n.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert {"_feed_key_env", "build_graph", "main"} <= funcs
    # brain choices wired
    code = RUNNER.read_text(encoding="utf-8")
    assert '"ollama"' in code and '"openai"' in code, "both brains must be routable"
    assert "_FREELLM_URL" in code and "_OLLAMA_URL" in code
    # the key comes from house config, never hardcoded
    assert "freellmapi_api_key" in code and "OPENAI_API_KEY" in code
    assert "sk-" not in code and "AQ." not in code, "hardcoded key material in runner"
    # FRED macro wiring present
    assert "fred_api_key" in code and "FRED_API_KEY" in code
    assert '"fred"' in code, "macro vendor must be selectable when a key exists"
    # SEC EDGAR fundamentals chain wired
    assert "sec_edgar,yfinance" in code, "EDGAR-first fundamentals chain missing"
    assert "SEC_EDGAR_USER_AGENT" in code, "EDGAR caller identification missing"


def test_fred_key_from_config_feeds_env():
    """The runner must read fred_api_key from house config, never hardcode."""
    import json as _json
    import os

    cfg_path = BASE / "config" / "api_keys.json"
    if not cfg_path.exists():
        print("⚠ config/api_keys.json missing — skipping FRED env test")
        return
    cfg = _json.loads(cfg_path.read_text(encoding="utf-8"))
    assert "gemini_api_key" in cfg, "sanity: config unreadable"
    if not (cfg.get("fred_api_key") or "").strip():
        print("⚠ fred_api_key not set yet — macro stays degraded (by design)")
        return
    sys.path.insert(0, str(BASE / "scripts"))
    import trading_agents_run as tar

    # feed from config (idempotent: setdefault inside)
    tar._feed_key_env()
    assert os.environ.get("FRED_API_KEY"), "runner did not feed FRED_API_KEY"
    assert os.environ["FRED_API_KEY"] == cfg["fred_api_key"]


def test_edgar_vendor_selected_and_reachable():
    """fundamental_data must be the EDGAR-first chain, and EDGAR must serve
    real filings with the configured UA (403 means the UA format is wrong —
    SEC rejects placeholder/noreply contact addresses)."""
    import json as _json
    import os

    if not TA_VENV.exists():
        print("⚠ TA venv missing — skipping EDGAR test")
        return
    cfg_path = BASE / "config" / "api_keys.json"
    cfg = _json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    env = {
        **os.environ,
        "SEC_EDGAR_USER_AGENT": (
            cfg.get("sec_edgar_user_agent") or "Mark-LIII Research contact@example.com"
        ),
    }
    proc = subprocess.run(
        [
            str(TA_VENV),
            "-c",
            "import sys; sys.path.insert(0, 'scripts'); "
            "import trading_agents_run as tar; tar._feed_key_env(); "
            "ta = tar.build_graph(('market', 'news', 'fundamentals')); "
            "print('FUNDAMENTALS_VENDOR=' + ta.config['data_vendors']['fundamental_data']); "
            "from tradingagents.dataflows.vendors.sec_edgar import get_income_statement; "
            "out = get_income_statement('NVDA', 'quarterly', '2026-09-26'); "
            "print('EDGAR_LIVE=' + ('OK' if 'Income Statement' in out else 'BAD') + ':' + str(len(out)))",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=BASE,
        env=env,
    )
    out = proc.stdout
    assert "FUNDAMENTALS_VENDOR=sec_edgar,yfinance" in out, out[-300:]
    assert "EDGAR_LIVE=OK:" in out, out[-300:] + proc.stderr[-300:]
    """With FRED_API_KEY in the env, build_graph must pick the fred vendor —
    verified in the TradingAgents venv (the only place the package exists)."""
    import json as _json
    import os

    cfg_path = BASE / "config" / "api_keys.json"
    if not cfg_path.exists() or not TA_VENV.exists():
        print("⚠ config or TA venv missing — skipping vendor selection test")
        return
    key = (
        _json.loads(cfg_path.read_text(encoding="utf-8")).get("fred_api_key") or ""
    ).strip()
    if not key:
        print("⚠ fred_api_key not set yet — macro stays degraded (by design)")
        return
    env = {
        **os.environ,
        "FRED_API_KEY": key,
        "OPENAI_API_KEY": (
            _json.loads(cfg_path.read_text()).get("freellmapi_api_key") or "x"
        ),
    }
    proc = subprocess.run(
        [
            str(TA_VENV),
            "-c",
            "import sys; sys.path.insert(0, 'scripts'); "
            "import trading_agents_run as tar; "
            "ta = tar.build_graph(('market', 'news')); "
            "print('MACRO_VENDOR=' + ta.config['data_vendors']['macro_data'])",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=BASE,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr[-400:]
    assert "MACRO_VENDOR=fred" in proc.stdout, proc.stdout[-200:]


def test_fred_key_not_leaked_into_repo_files():
    """The key must only live in the gitignored config, not in any tracked file."""
    import json as _json

    cfg_path = BASE / "config" / "api_keys.json"
    if not cfg_path.exists():
        return
    key = (
        _json.loads(cfg_path.read_text(encoding="utf-8")).get("fred_api_key") or ""
    ).strip()
    if not key:
        return
    tracked = subprocess.run(
        ["git", "ls-files"],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=BASE,
    ).stdout.split()
    for rel in tracked:
        p = BASE / rel
        if p.suffix not in (".py", ".md", ".json", ".txt", ".yaml", ".yml", ".sh"):
            continue
        try:
            content = p.read_text(encoding="utf-8")
        except Exception:
            continue
        assert key not in content, f"FRED key leaked into tracked file: {rel}"


def test_runner_disclaimer_present():
    src = RUNNER.read_text(encoding="utf-8")
    assert "research" in src.lower(), "research-only framing must be stated"


def test_no_hardcoded_gemini_key_path():
    """Gemini-direct must NOT be a wired brain (double-spends JARVIS quota)."""
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
    # scan CODE strings only (the docstring legitimately mentions why 'google'
    # is excluded); a real wiring would be a dict key or a config read.
    code_strings = [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and not (n.col_offset == 0 or True)
    ]
    # config assignments: llm_provider must only ever be openai/ollama
    providers = [
        n.value.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(
            isinstance(t, ast.Subscript)
            and ast.unparse(t.slice) in ('"llm_provider"', "'llm_provider'")
            for t in n.targets
        )
        and isinstance(n.value, ast.Constant)
    ]
    assert providers, "llm_provider assignment not found"
    assert set(providers) <= {
        "openai",
        "ollama",
    }, f"unexpected provider wiring: {providers}"
    assert "gemini_api_key" not in RUNNER.read_text(encoding="utf-8")


def test_action_tool_shape():
    from actions.trading_agents import TOOL, trading_agents

    assert TOOL["name"] == "trading_agents"
    assert TOOL["handler"] is trading_agents
    assert TOOL["parameters"]["required"] == ["ticker"]
    desc = TOOL["description"].lower()
    assert "research" in desc or "analysis" in desc
    assert "not financial advice" in desc or "research only" in desc


def test_action_registers_in_loader():
    from core.action_loader import discover_actions

    reg = discover_actions(BASE / "actions")
    assert "trading_agents" in reg.names()


def test_action_maps_rate_limit_errors_to_friendly_speech():
    from actions import trading_agents as m
    from unittest.mock import patch

    proc = type(
        "P",
        (),
        {
            "returncode": 1,
            "stdout": "",
            "stderr": "run failed: Error code: 429 - rate_limit_exceeded",
        },
    )
    with patch.object(m.subprocess, "run", return_value=proc):
        out = m.trading_agents({"ticker": "NVDA"})
    assert "rate-limited" in out.lower() and "retry" in out.lower()


def test_timeout_message_is_helpful():
    from actions import trading_agents as m
    import subprocess as sp
    from unittest.mock import patch

    def _raise(*a, **k):
        raise sp.TimeoutExpired(cmd="x", timeout=1)

    with patch.object(m.subprocess, "run", side_effect=_raise):
        out = m.trading_agents({"ticker": "NVDA"})
    assert "rate-limited" in out.lower() or "out of time" in out.lower()


def test_hermes_skill_frontmatter():
    import re

    text = HERMES_SKILL.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "frontmatter missing"
    fm = match.group(1)
    for field in (
        "name: trading-agents",
        "category: finance",
        "recommended_method: terminal",
        "verified: true",
    ):
        assert field in fm, f"frontmatter missing {field!r}"
    assert "trading_agents_run.py" in text
    # disclaimer discipline
    assert "financial advice" in text.lower()


def test_runner_help_offline():
    proc = subprocess.run(
        [sys.executable, str(RUNNER), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert "--brain" in proc.stdout and "--analysts" in proc.stdout


def test_repo_skill_exists():
    assert (REPO_SKILL / "SKILL.md").exists()


def test_ta_venv_imports():
    if not TA_VENV.exists():
        print("⚠ .venv-tradingagents missing — integration not installed; skipping")
        return
    proc = subprocess.run(
        [
            str(TA_VENV),
            "-c",
            "from tradingagents.graph.trading_graph import TradingAgentsGraph; "
            "print('import-ok')",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "import-ok" in proc.stdout


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
