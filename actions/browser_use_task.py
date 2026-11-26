"""
browser_use_task.py — backward-compatible name for the RIO Browser Agent.

The tool was introduced against browser-use 0.13 and is now a **thin alias**:
the implementation moved to `core/browser_agent/` (browser-use 1.0.5, job
registry, permission policy, routing) and `actions/browser_agent.py` is the
tool to prefer. Keeping this TOOL registered means an existing prompt, habit or
saved command that says "browser_use_task" still works — it goes through the
same policy and the same runner, never a second code path.

New work should call `browser_agent`.
"""

from __future__ import annotations

from actions.browser_agent import browser_agent as _browser_agent


def browser_use_task(parameters: dict, player=None, speak=None, session_memory=None) -> str:
    """Run one browser task through the RIO Browser Agent."""
    params = dict(parameters or {})
    params.setdefault("action", "run")
    params.setdefault("mode", "auto")
    if "task" not in params:
        params["task"] = str(params.pop("instruction", "") or "")
    return _browser_agent(params, player=player, speak=speak, session_memory=session_memory)


TOOL = {
    "name": "browser_use_task",
    "description": (
        "Legacy alias for browser_agent — prefer browser_agent. Runs one "
        "autonomous multi-step browser task (open, click, fill, extract, "
        "download) through the same RIO browser runtime and permission policy."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "task": {
                "type": "STRING",
                "description": "The browser task as one complete, self-contained instruction.",
            },
            "max_steps": {
                "type": "INTEGER",
                "description": "Agent step budget (default 25, capped at 60).",
            },
            "visible": {
                "type": "BOOLEAN",
                "description": "Show the browser window instead of running headless.",
            },
            "fresh_profile": {
                "type": "BOOLEAN",
                "description": "Accepted for compatibility; profile choice is managed by the runtime.",
            },
        },
        "required": ["task"],
    },
    "handler": browser_use_task,
}
