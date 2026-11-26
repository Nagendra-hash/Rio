"""
core/scientific/delivery.py — send a result (and a report file) to the chat
that asked.

Two paths, same as the rest of the house:
    text   -> core.binance_ws.send_hermes (the shared outbound primitive)
    file   -> `hermes send -t <target> -f <path>` through the CLI, because the
              direct Bot API path cannot attach documents

If the CLI is unavailable, the file path is delivered as text rather than the
message being lost. Nothing here prints or forwards credentials; the child gets
a scrubbed environment.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_CLI_TIMEOUT_S = 60


def _env() -> dict:
    home = Path.home()
    return {
        "PATH": os.pathsep.join([str(home / ".local" / "bin"), "/usr/local/bin",
                                 "/usr/bin", "/bin"]),
        "HOME": str(home),
    }


def send_text(target: str, text: str) -> bool:
    if not target or not text:
        return False
    try:
        from core import binance_ws

        return bool(binance_ws.send_hermes(target, str(text)[:3500]))
    except Exception:
        return False


def send_file(target: str, path: str | Path, caption: str = "") -> bool:
    """Attach one file via the Hermes CLI. False when it could not be sent."""
    file_path = Path(path)
    if not target or not file_path.is_file():
        return False
    cli = "hermes"
    body = (caption or f"Report attached: {file_path.name}")[:1500]
    try:
        proc = subprocess.run(
            [cli, "send", "-t", target, body, "-f", str(file_path)],
            capture_output=True, text=True, timeout=_CLI_TIMEOUT_S, env=_env(),
        )
        return proc.returncode == 0
    except FileNotFoundError:
        return False
    except Exception:
        return False


def deliver(target: str, text: str, files: list[str] | None = None,
            caption: str = "") -> dict:
    """Text first, then attachments; a failure is reported, never silent."""
    result = {"target": target, "text_sent": False, "files_sent": [], "files_failed": []}
    if not target:
        return result
    if text:
        result["text_sent"] = send_text(target, text)
    for path in files or []:
        if send_file(target, path, caption=caption or text[:400]):
            result["files_sent"].append(str(path))
        else:
            result["files_failed"].append(str(path))
    if result["files_failed"] and not result["text_sent"]:
        # Last resort: at least tell the user where the artifact is.
        result["text_sent"] = send_text(
            target,
            "The research run finished but the attachment could not be sent. "
            "Files are on the machine at: " + ", ".join(result["files_failed"]),
        )
    return result
