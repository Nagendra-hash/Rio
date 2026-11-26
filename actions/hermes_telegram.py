"""hermes_telegram.py — Send messages to Telegram from JARVIS.

Talks to the Telegram Bot API directly. This is intentional: `hermes send`
runs the gateway's platform path and needs a live gateway, while a plain
notification from JARVIS must work even when Hermes is down. The bot token and
chat id are read from ~/.hermes/.env, so there is still one source of
credentials — nothing is hardcoded.

Use cases:
  * "Send a message to my Telegram"
  * "Tell Telegram hello"
  * "Message the bot: [text]"
"""
from __future__ import annotations

import json
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional


_TG_TOKEN = None
_TG_CHAT = None


def _load_tg_config() -> tuple[str, str]:
    """Read TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from ~/.hermes/.env."""
    global _TG_TOKEN, _TG_CHAT
    if _TG_TOKEN and _TG_CHAT:
        return _TG_TOKEN, _TG_CHAT
    
    env_file = Path.home() / ".hermes" / ".env"
    token, chat_id = "", ""
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                token = line.split("=", 1)[1].strip().strip('"\'')
            elif line.startswith("TELEGRAM_CHAT_ID=") or line.startswith("TELEGRAM_ALLOWED_USERS="):
                val = line.split("=", 1)[1].strip().strip('"\'')
                # Take first numeric user id
                for part in val.split(","):
                    part = part.strip()
                    if part.isdigit():
                        chat_id = part
                        break
                if chat_id:
                    break
    if not token or not chat_id:
        raise RuntimeError(
            "Telegram config missing — set TELEGRAM_BOT_TOKEN and "
            "TELEGRAM_CHAT_ID (or TELEGRAM_ALLOWED_USERS with a numeric id) "
            "in ~/.hermes/.env"
        )
    _TG_TOKEN, _TG_CHAT = token, chat_id
    return _TG_TOKEN, _TG_CHAT


def _send(text: str) -> dict:
    """POST to Telegram Bot API sendMessage endpoint."""
    token, chat_id = _load_tg_config()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # chat_id may be numeric user id or @channel username; only coerce when
    # it really is an integer, otherwise the API gets a string as-is.
    chat: object = int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id
    payload = json.dumps({
        "chat_id": chat,
        "text": text[:4096],
        "parse_mode": "Markdown",
    }).encode()
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"Telegram API HTTP {e.code}: {body[:200]}")


def hermes_telegram(parameters: dict, player=None, speak=None) -> str:
    parameters = parameters or {}
    message = str(parameters.get("message") or "").strip()
    if not message:
        return "error: missing 'message' parameter"
    try:
        result = _send(message)
        ok = result.get("ok", False)
        msg_id = result.get("result", {}).get("message_id", "?")
        return f"Sent to Telegram (msg {msg_id})" if ok else f"Telegram API error: {result}"
    except Exception as e:
        return f"error sending to Telegram: {e}"


TOOL = {
    "name": "hermes_telegram",
    "description": (
        "Send a direct notification message to the user's Telegram chat "
        "(the one configured in ~/.hermes/.env as TELEGRAM_BOT_TOKEN and "
        "TELEGRAM_CHAT_ID/TELEGRAM_ALLOWED_USERS). Use when the user asks "
        "JARVIS to message them on Telegram, tell Telegram something, or "
        "push a summary/notification to their phone."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "message": {
                "type": "STRING",
                "description": "The message text to send (Markdown supported, max 4096 chars).",
            },
        },
        "required": ["message"],
    },
    "handler": hermes_telegram,
}
