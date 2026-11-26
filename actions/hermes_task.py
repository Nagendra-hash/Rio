"""
hermes_task.py — bridge action: delegate tasks from JARVIS to the Hermes Agent.

Hermes (Nous Research) lives at ~/.hermes/ and is a self-improving agent: it has
100+ bundled skills (email, devops, web research, file work, note-taking, media,
autonomous agents), terminal + browser access, and it WRITES NEW SKILLS from
experience when it meets a task without one. JARVIS inherits all of that live —
there is nothing to sync, because every call runs the current Hermes.

Design:
  * `hermes_task` — run any task through Hermes' full agent loop (tools,
    skills, terminal, browser) and bring back the final answer text.
    (Skills are listed separately by actions/hermes_skills.py.)

Timeouts are conservative: one-shot agent turns are bounded by the CLI's own
limits, and a stuck call must never hang JARVIS' audio loop — the handler runs
in an executor thread, but the user is still waiting for speech back.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

_HOME = Path.home()
_HERMES = shutil.which("hermes") or str(_HOME / ".local" / "bin" / "hermes")
# Fixed, minimal env: the GUI process must never hand Hermes its own
# environment. PATH still has to reach the CLI and Hermes' bundled node.
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
# Timeouts are env-tunable: a full agent turn that GENERATES a long document
# and then DELIVERS it (Telegram) can legitimately run past the old fixed
# 240 s (observed 2026-10-13 — the meal-plan-to-Telegram task timed out at
# 240 and Hermes' work was lost). Still bounded: a stuck call must never hang
# JARVIS' audio loop — the handler runs in an executor thread, but the user
# is waiting for speech back.
_TIMEOUT_TASK = int(os.environ.get("HERMES_TASK_TIMEOUT_S", "360"))
_TIMEOUT_SEND = int(os.environ.get("HERMES_SEND_TIMEOUT_S", "60"))


def _run_hermes(args: list[str], timeout: int) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            [_HERMES, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_ENV,
        )
        return proc.returncode, (proc.stdout or "").strip()
    except FileNotFoundError:
        return 127, "Hermes CLI not found on this machine."
    except subprocess.TimeoutExpired:
        return 124, "Hermes did not finish in time — the task was cancelled."


def _read_env_var(name: str, path: Path | None = None) -> str | None:
    """Read a single env variable from ~/.hermes/.env (no parsing required)."""
    if path is None:
        path = _HOME / ".hermes" / ".env"
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


# Resolve the Telegram destination up-front so hermes_task never falls back to
# bare "telegram" (which fails with exit 1 when no home channel is configured).
_TG_CHAT_ID = (_read_env_var("TELEGRAM_CHAT_ID") or "").strip()
_HERMES_SEND_TARGET = f"telegram:{_TG_CHAT_ID}" if _TG_CHAT_ID else "telegram"


def hermes_task(parameters: dict, player=None) -> str:
    """Delegate one task to Hermes and return its final answer text."""
    parameters = parameters or {}
    task = str(parameters.get("task", "")).strip()
    deliver = str(parameters.get("deliver", "")).strip()

    rc, out = _run_hermes(["-z", task], _TIMEOUT_TASK)
    if rc == 0 and out:
        if (deliver or "").strip().lower() == "telegram":
            # Use the resolved target (e.g. telegram:7840222728) so hermes
            # doesn't hit "No home channel set" exit 1. Fallback to bare
            # "telegram" only when no chat id is configured — same behaviour
            # as before for setups that rely on it.
            target = _HERMES_SEND_TARGET
            send_rc, send_out = _run_hermes(
                ["send", "-t", target, out[:3500]], _TIMEOUT_SEND
            )
            if send_rc == 0 and ("sent" in send_out.lower()
                                 or "delivered" in send_out.lower()):
                return "Hermes finished — the full answer is on your Telegram."
            # Delivery failed (network/Telegram outage): never claim success —
            # fall back to reading the answer aloud so the user still gets it.
            reason = (send_out or f"exit {send_rc}").strip()[:120]
            return (
                f"Hermes finished, but the Telegram send failed ({reason}). "
                f"Here is the answer: {out[:800]}"
            )
        return out
    if rc == 124:
        return "Hermes ran out of time on that one. Try breaking it into smaller steps."
    return f"Hermes could not complete the task (exit {rc}). {out[:400]}".strip()


TOOL = {
    "name": "hermes_task",
    "description": (
        "Delegate a task to Hermes, a self-improving AI agent with 100+ skills "
        "(email, devops, web research, coding, file work, note-taking), terminal "
        "and browser access, and the ability to learn new skills on the fly. Use "
        "for complex multi-step work that benefits from an autonomous agent: "
        "'research X and write a report', 'organise my downloads folder', "
        "'automate this repetitive thing'. Do NOT use for instant actions "
        "(open app, volume, weather) — do those directly."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "task": {
                "type": "STRING",
                "description": "The task for Hermes, phrased as a complete self-contained instruction.",
            },
            "deliver": {
                "type": "STRING",
                "description": "Optional: 'telegram' sends Hermes' full answer to the user's Telegram instead of reading it aloud.",
            },
        },
        "required": ["task"],
    },
    "handler": hermes_task,
}
