"""
core/browser_agent — the RIO Browser Agent's shared runtime.

One implementation used by every entry point:

    actions/browser_agent.py                  voice / dashboard tool
    actions/browser_use_task.py               legacy alias (same path)
    orchestrator/agents/browser/browser_agent.py   text / Telegram / Agent Center

    config.py     settings (defaults + memory/browser_agent/config.json)
    policy.py     allow / needs_approval / deny, and domain policy
    router.py     tool vs static http vs real browser, plus the static fetch
    runtime.py    job registry, subprocess runner, timeouts, cancellation, logs
    approvals.py  pending confirmations for headless / chat channels

`scripts/browser_use_run.py` is the only file that imports browser-use; it runs
in the isolated interpreter (~/.jarvis-browser-venv) so RIO's own dependency
tree is untouched.
"""

from __future__ import annotations

from core.browser_agent.approvals import (
    STORE as APPROVALS,
    ApprovalStore,
    Pending,
    parse_yes_no,
)
from core.browser_agent.config import (
    BrowserAgentConfig,
    load_config,
    reset_config_cache,
    save_config,
)
from core.browser_agent.policy import Decision, Verdict, classify, domain_policy
from core.browser_agent.router import Plan, fetch_static, plan_task
from core.browser_agent.runtime import (
    BrowserRunResult,
    BrowserRuntime,
    preflight,
    resolve_python,
    run_task,
    shared_runtime,
)

__all__ = [
    "APPROVALS",
    "ApprovalStore",
    "BrowserAgentConfig",
    "BrowserRunResult",
    "BrowserRuntime",
    "Decision",
    "Pending",
    "Plan",
    "Verdict",
    "classify",
    "domain_policy",
    "fetch_static",
    "load_config",
    "parse_yes_no",
    "plan_task",
    "preflight",
    "reset_config_cache",
    "resolve_python",
    "run_task",
    "save_config",
    "shared_runtime",
]
