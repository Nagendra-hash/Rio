"""
browser_use_relogin.py — bridge action: voice-triggered session recovery.

When a browser_use_task lands on a login wall, the saved session for that
site has expired. This action shells out to the deterministic recovery
script (scripts/browser_use_relogin.py) — real form fill via Playwright,
NO LLM, NO agent loop, NO quota — using selectors from the local chmod-600
credentials store, then writes the fresh session back into the shared
storage_state.json the browser-use agent uses.

Voice flow: "JARVIS, log back into mybank" → this action → spoken result.
For 2FA/captcha sites the caller must pass visible=true AND stay present;
the action says so instead of attempting an unattended interactive login.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PYTHON = sys.executable or str(_ROOT / ".venv" / "bin" / "python")
_SCRIPT = _ROOT / "scripts" / "browser_use_relogin.py"
_TIMEOUT = (
    180  # seconds; headless logins take ~5 s, headed ones as long as the user takes
)


def _list_sites() -> str:
    proc = subprocess.run(
        [_PYTHON, str(_SCRIPT), "--list"],
        capture_output=True,
        text=True,
        timeout=30,
        env={"HOME": str(Path.home()), "PATH": "/usr/local/bin:/usr/bin:/bin"},
    )
    out = (proc.stdout or "").strip()
    return out or "(no sites configured)"


def browser_use_relogin(parameters: dict, player=None, speak=None) -> str:
    """Re-login one site into the persistent browser profile, by voice."""
    parameters = parameters or {}
    site = str(parameters.get("site", "")).strip()
    clear = bool(parameters.get("clear", False))
    visible = bool(parameters.get("visible", False))

    if not _SCRIPT.exists():
        return "The session recovery script is missing, sir."

    if not site:
        sites = _list_sites()
        return "Which site should I log back into, sir? Configured: " + sites.replace(
            "\n", "; "
        )

    if player:
        player.write_log(f"[relogin] site: {site} clear={clear} visible={visible}")
    if speak:
        speak("Re-authenticating now, sir. One moment.")

    cmd = [_PYTHON, str(_SCRIPT), site]
    if clear:
        cmd.append("--clear")
    if visible:
        cmd.append("--visible")

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            env={"HOME": str(Path.home()), "PATH": "/usr/local/bin:/usr/bin:/bin"},
        )
    except subprocess.TimeoutExpired:
        return (
            f"The login for {site} timed out, sir. If it needs a code or "
            "captcha, ask me again with the visible option and finish it yourself."
        )

    err = (proc.stderr or "").strip()
    if proc.returncode == 0:
        # last stderr line is the canonical "[relogin] site: logged in (url=…)"
        detail = err.splitlines()[-1] if err else f"{site} ok"
        return f"Logged back into {site}, sir. {detail.removeprefix('[relogin] ')}"

    if proc.returncode == 2:
        if "credentials store missing" in err or "no credentials" in err:
            return (
                f"I have no saved credentials for {site}, sir. Add them to "
                "the site credentials store first — never tell them to me in chat."
            )
        return f"Cannot recover {site}: {err.splitlines()[-1][:200] if err else 'configuration problem'}."

    # rc == 1: login attempted and failed — surface the evidence, suggest eyes-on
    reason = err.splitlines()[-1][:200] if err else "unknown failure"
    return (
        f"The re-login for {site} failed, sir. {reason} "
        "The site's login flow may have changed — or try again with the "
        "visible option and complete it yourself."
    )


TOOL = {
    "name": "browser_use_relogin",
    "description": (
        "Re-authenticate a website whose saved browser session expired, so "
        "browser_use_task works logged-in again. Deterministic form fill from "
        "the local credential store — no agent loop. Use when a browser task "
        "ends on a login page or the user says a site 'logged me out'. Use "
        "visible=true ONLY when the login needs a code or captcha and the "
        "user is present. Do NOT use for sites with no saved credentials."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "site": {
                "type": "STRING",
                "description": "Site key from the credentials store (omit to list configured sites).",
            },
            "clear": {
                "type": "BOOLEAN",
                "description": "Drop the site's stale cookies before logging in (default false).",
            },
            "visible": {
                "type": "BOOLEAN",
                "description": "Show the browser for interactive steps like 2FA or captcha — user must be present.",
            },
        },
        "required": [],
    },
    "handler": browser_use_relogin,
}
