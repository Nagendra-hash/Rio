"""
hermes_skills.py — expose the Hermes agent's live skill inventory to JARVIS.

Hermes is self-improving: it starts with 100+ bundled skills and writes new
ones from experience. This action reads what Hermes has *right now*, so the
answer naturally grows over time. JARVIS uses it to answer "what can Hermes
do?" and to pick the right skill before delegating via hermes_task.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

_HOME = Path.home()
_HERMES = shutil.which("hermes") or str(_HOME / ".local" / "bin" / "hermes")
# Same fixed env as actions/hermes_task.py — see that file for why.
_ENV = {
    "PATH": os.pathsep.join(
        [
            str(_HOME / ".local" / "bin"),
            str(_HOME / ".hermes" / "node" / "bin"),
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
        ]
    ),
    "HOME": str(_HOME),
}
_TIMEOUT_LIST = 30  # seconds


def hermes_skills(parameters: dict, player=None) -> str:
    """Show Hermes' current skill inventory (optionally filtered by a query)."""
    q = str((parameters or {}).get("query", "")).strip()
    args = ["skills", "search", q] if q else ["skills", "list"]
    try:
        proc = subprocess.run(
            [_HERMES, *args],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_LIST,
            env=_ENV,
        )
    except FileNotFoundError:
        return "Hermes CLI not found on this machine."
    except subprocess.TimeoutExpired:
        return "Hermes took too long to list skills — try again."

    out = (proc.stdout or "").strip()
    if proc.returncode == 0 and out:
        lines = out.splitlines()
        if len(lines) > 40:
            lines = lines[:40] + [f"… and {len(out.splitlines()) - 40} more"]
        return "\n".join(lines)
    return f"Could not read Hermes skills right now (exit {proc.returncode})."


TOOL = {
    "name": "hermes_skills",
    "description": (
        "List the skills the Hermes agent currently has — the inventory grows "
        "as Hermes learns new skills from experience. Optionally filter by a "
        "topic. Use when the user asks what Hermes can do, or before calling "
        "hermes_task, to pick the right skill."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "query": {
                "type": "STRING",
                "description": "Optional filter, e.g. 'email', 'trading', 'pdf'.",
            }
        },
        "required": [],
    },
    "handler": hermes_skills,
}
