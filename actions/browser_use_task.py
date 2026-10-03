"""
browser_use_task.py — bridge action: JARVIS drives the browser-use agent.

browser-use (https://github.com/browser-use/browser-use) is an autonomous
browser agent: given a natural-language task it opens the system Chrome,
navigates, clicks, fills forms and reads pages until the task is done.
This action shells out to the shared runner (scripts/browser_use_run.py),
which picks the agent's LLM brain the same free way the rest of the house
does: FreeLLMAPI pool → Gemini → Ollama.

Scope: multi-step browsing JARVIS cannot do directly — "find the cheapest
flight on X site", "log into the dashboard and read the alerts", "scrape the
first three results and summarise". For plain page-open/navigate,
browser_control remains the right tool (faster, no agent loop).

Timeouts follow the hermes_task convention: the runner is bounded, and a
stuck call must never hang JARVIS' audio loop.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

_ROOT   = Path(__file__).resolve().parent.parent
_PYTHON = sys.executable or str(_ROOT / ".venv" / "bin" / "python")
_RUNNER = _ROOT / "scripts" / "browser_use_run.py"
_TIMEOUT_TASK = 300   # seconds; agent loops are slower than plain tools


def browser_use_task(parameters: dict, player=None, speak=None) -> str:
    """Run one browser task through the browser-use agent and speak the answer."""
    parameters = parameters or {}
    task      = str(parameters.get("task", "")).strip()
    max_steps = int(parameters.get("max_steps", 12) or 12)
    visible   = bool(parameters.get("visible", False))
    fresh     = bool(parameters.get("fresh_profile", False))

    if not task:
        return "Tell me what to do in the browser, sir."
    if not _RUNNER.exists():
        return "The browser-use agent is missing, sir — check scripts/browser_use_run.py."

    if player:
        player.write_log(f"[browser-use] task: {task[:80]}")
    if speak:
        speak("Opening the browser agent now, sir. This may take a minute.")

    cmd = [_PYTHON, str(_RUNNER), task, "--max-steps", str(max_steps)]
    if visible:
        cmd.append("--visible")
    if fresh:
        cmd.append("--no-persist")   # throwaway profile: skip saved logins

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=_TIMEOUT_TASK,
                              env={"HOME": str(Path.home()),
                                   "PATH": "/usr/local/bin:/usr/bin:/bin"})
    except FileNotFoundError:
        return "Python or the runner could not be started, sir."
    except subprocess.TimeoutExpired:
        return "The browser agent ran out of time — try a narrower task, sir."

    out = (proc.stdout or "").strip()
    if proc.returncode == 0 and out:
        return out[:3000]
    err = (proc.stderr or "").strip().splitlines()
    reason = err[-1] if err else f"exit {proc.returncode}"
    return f"The browser agent could not finish the task ({reason[:200]})."


TOOL = {
    "name": "browser_use_task",
    "description": (
        "Drive an autonomous browser agent (browser-use) to complete a multi-step "
        "web task: searching sites, clicking through results, filling forms, "
        "extracting and summarising page data. Use for browsing that needs "
        "several steps and judgement — 'find the cheapest X on site Y', 'check "
        "my dashboard and report alerts', 'research this topic across pages'. "
        "Do NOT use to simply open a URL or play media — use browser_control "
        "or youtube_video for those."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "task": {
                "type": "STRING",
                "description": "The browser task as one complete, self-contained instruction."
            },
            "max_steps": {
                "type": "INTEGER",
                "description": "Agent step budget (default 12). Raise for long journeys."
            },
            "visible": {
                "type": "BOOLEAN",
                "description": "Show the browser window instead of running headless (default false)."
            },
            "fresh_profile": {
                "type": "BOOLEAN",
                "description": "Use a throwaway browser profile, ignoring saved logins/cookies (default false = reuse the persistent profile)."
            }
        },
        "required": ["task"]
    },
    "handler": browser_use_task,
}
