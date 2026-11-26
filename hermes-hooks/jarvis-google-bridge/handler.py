"""
jarvis-google-bridge — Hermes gateway hook (event: agent:end).

WHEN IT FIRES
    After every Hermes agent turn that came from a messaging platform, exactly
    like jarvis-pdf-forward. context carries platform, user_id, chat_id,
    session_id, message (the user's text), response, model, provider. Errors
    never break the gateway — the registry swallows them.

WHY THIS EXISTS
    Gmail and Google Calendar live in Jarvis, not in Hermes. Jarvis holds the
    OAuth tokens (config/gmail_token.json, config/calendar_token.json) and the
    two skills (actions/gmail_control.py, actions/calendar_control.py). Hermes
    has no Google account of its own, so "check my mail" typed on Telegram
    would otherwise be answered by an agent that cannot possibly read it.

    This hook spots the mail/schedule intent in the user's message and hands
    the message to Jarvis's normalized inbox:

        <jarvis>/scripts/jarvis_inbox.py --channel <platform> \
            --chat <chat_id> --text "<message>"

    Jarvis runs gmail_control / calendar_control, and its reply arm delivers
    the answer back through the Telegram bot API on the same chat. Round trip:
    Telegram → Hermes gateway → this hook → inbox → dashboard → Jarvis →
    Gmail/Calendar API → answer back to Telegram.

THE INTENT LIST IS DELIBERATELY CONSERVATIVE
    Only clear mail/calendar asks are forwarded. A false positive here is worse
    than a miss: it sends an unrelated message ("delete that file") into Jarvis
    and gets a second, confusing answer in the chat on top of Hermes' own. So
    every trigger needs an actual mail/calendar noun — "mail" alone or "what's
    up" does not qualify. Common phrasings in the languages this assistant is
    used in are included because a miss means the user simply gets no answer.

    Absent-intent turns exit on the first regex, before any work.

DUPLICATE-SUPPRESSION
    Hermes may itself answer "I can't read your email" before this hook fires,
    which would leave the user with Hermes' denial followed by the real answer.
    A per-chat cooldown (default 15 min) keeps one ask to one forward, so a
    follow-up like "and tomorrow?" inside the window is answered by Hermes
    alone rather than doubling up. State lives in the hooks dir.

THIS HOOK IS DUMB BY DESIGN
    No intent decisions beyond "is this a mail/calendar ask". Which operation,
    which message, which date — all of that is decided in Jarvis's prompt and
    tools, which are the only layer that holds the credentials.

INSTALL
    mkdir -p ~/.hermes/hooks/jarvis-google-bridge
    rsync -a --delete hermes-hooks/jarvis-google-bridge/ \
                 ~/.hermes/hooks/jarvis-google-bridge/
    hermes gateway restart        # hooks load at gateway startup only

    The trailing slash and the explicit mkdir matter. `cp -r src
    ~/.hermes/hooks/` looks equivalent and is not: it never removes files that
    no longer exist in the repo, so a deleted module (and __pycache__/) lingers
    and can be loaded instead of the new code. rsync --delete converges on
    exactly the repo contents and is safe to re-run.

FINDING THE JARVIS CHECKOUT
    This hook is COPIED into ~/.hermes/hooks/, so __file__ points at ~/.hermes.
    _find_inbox() locates the checkout instead: JARVIS_HOME first, then the
    repo this file lives in (source tree / tests), then a Mark-* directory in
    $HOME — each candidate accepted only if scripts/jarvis_inbox.py is really
    there, so a renamed checkout is still found.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

_INBOX_REL = ("scripts", "jarvis_inbox.py")


def _find_inbox() -> Path | None:
    """Locate scripts/jarvis_inbox.py in the Jarvis checkout.

    Order: JARVIS_HOME (portable mode) → the repo this file lives in (source
    tree / tests) → a Mark-* checkout in $HOME. A candidate is accepted only
    when the script is actually there, so a rename or move of the checkout is
    picked up automatically.
    """
    candidates: list[Path] = []
    env_home = os.environ.get("JARVIS_HOME", "").strip()
    if env_home:
        candidates.append(Path(env_home).expanduser())
    candidates.append(Path(__file__).resolve().parent.parent.parent)
    home = Path.home()
    candidates.extend(sorted(home.glob("Mark-*")))
    candidates.extend(sorted(home.glob("*/Mark-*")))
    for root in candidates:
        inbox = root.joinpath(*_INBOX_REL)
        if inbox.is_file():
            return inbox
    return None


# Where Jarvis's inbox script lives. None if no checkout is found — handle()
# then declines to forward rather than firing a broken command.
_INBOX = _find_inbox()

# ── Intent detection ──────────────────────────────────────────────────────────
#
# Every alternative requires a genuine mail/calendar noun, so ordinary chat
# ("delete that file", "play some music") never matches.
#
# Note on \b and Devanagari: \b is defined against \w, which is Unicode-aware,
# but the boundary behaviour around combining marks is not something to rely on.
# So Hindi alternatives are written WITHOUT \b around them — they never need a
# boundary to be safe, because they are long, specific words.

_MAIL_NOUN = (r"(gmail|gmail|gmial|e-?mails?|mails?|mailbox|inbox|unread)"
              r"|(?:मेल|ईमेल|इनबॉक्स)")

# Hindi/Hinglish question + time words. "kal kya schedule hai" is a calendar ask
# with NO English verb in it, so the calendar rule needs these to fire.
_HI = r"(?:क्या|कल|आज|परसों|है|हैं|दिन|बार|को)"

_MAIL_RE = re.compile(
    # verb + mail noun ("check my mail", "do I have any unread emails")
    rf"\b(check|read|open|show|list|search|find|any|have|get|see)\b"
    rf"[^.?!\n]{{0,40}}?\b{_MAIL_NOUN}\b"
    # or the noun carrying the ask on its own ("unread emails kya hain")
    rf"|\b{_MAIL_NOUN}\b[^.?!\n]{{0,25}}?\b(check|unread|new|latest|any)\b"
    rf"|\b{_MAIL_NOUN}\b[^.?!\n]{{0,25}}?{_HI}"
    # or an explicit send ("send an email to alice")
    rf"|\b(send|write|reply|forward|compose)\b[^.?!\n]{{0,40}}?"
    rf"\b(?:an?\s+|the\s+)?{_MAIL_NOUN}\b"
    # "mail X about Y" / "email X"
    rf"|\b(?:mail|email)\s+(?:me\s+|us\s+)?\w+",
    re.IGNORECASE)

_CAL_NOUN = (r"(calendar|calender|schedul\w*|appointments?|meetings?|"
             r"events?|agenda|bookings?|slots?|invites?|invitations?)"
             r"|(?:कैलेंडर|शेड्यूल|अपॉइंटमेंट|मीटिंग)")

_CAL_RE = re.compile(
    rf"\b(what'?s|whats|what|when|am i|do i|any|show|check|list|"
    rf"book|schedule|add|create|set|cancel|delete|remove|move|reschedule|"
    rf"invite)\b[^.?!\n]{{0,40}}?\b{_CAL_NOUN}\b"
    rf"|\b{_CAL_NOUN}\b[^.?!\n]{{0,30}}?"
    rf"\b(free|busy|on|for|today|tomorrow|next|this|at)\b"
    rf"|\b{_CAL_NOUN}\b[^.?!\n]{{0,30}}?{_HI}"
    # "am I free Friday?" / "am I busy tomorrow?" — the canonical way to ask
    # about a calendar, and it contains NO calendar noun at all. Requiring one
    # would miss the single most common phrasing there is.
    rf"|\b(am i|do i|are we)\s+(free|busy|available)\b",
    re.IGNORECASE)

# A turn where HERMES ITSELF already did the Google work (e.g. it has a Gmail
# tool wired up). Forwarding then would double-answer the user.
_HERMES_DID_IT_RE = re.compile(
    r"\b(gmail|google calendar)\s+(api|tool|skill)\b"
    r"|\bfetched\s+(your\s+)?(mail|email|calendar)\b"
    r"|\bi\s+(read|checked|found)\s+(your\s+)?(mail|emails|inbox|calendar)\b",
    re.IGNORECASE)

_JARVIS_ENV = {
    "PATH": f"{Path.home()}/.local/bin:/usr/local/bin:/usr/bin:/bin",
    "HOME": str(Path.home()),
}

# Per-chat cooldown, so one ask is one forward. 15 min: long enough that a
# follow-up ("and tomorrow?") is answered by Hermes alone, short enough that a
# genuine new ask an hour later still reaches Jarvis.
_COOLDOWN_S = 900
_STATE_FILE = Path.home() / ".hermes" / "hooks" / "jarvis-google-bridge" \
              / ".cooldown.json"


def _load_state() -> dict:
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    """Best effort — a failed write only costs us a duplicate suppression."""
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass


def _in_cooldown(state: dict, chat_key: str) -> bool:
    last = state.get(chat_key)
    return bool(last) and (time.time() - float(last)) < _COOLDOWN_S


def _mark_forwarded(state: dict, chat_key: str) -> None:
    # Keep the map small; drop entries older than the cooldown window.
    now = time.time()
    state = {k: v for k, v in state.items()
             if now - float(v) < _COOLDOWN_S * 4}
    state[chat_key] = now
    _save_state(state)


def _is_google_ask(message: str) -> bool:
    """True when the message is a mail/calendar ask worth routing to Jarvis."""
    if _HERMES_DID_IT_RE.search(message):
        return False      # Hermes already handled it — stay out of the way
    return bool(_MAIL_RE.search(message) or _CAL_RE.search(message))


def handle(event_type: str, context: dict) -> None:
    try:
        if event_type != "agent:end":
            return
        message = str(context.get("message") or "").strip()
        if not message:
            return

        # Cheap exit first: no mail/calendar noun → not ours.
        if not _is_google_ask(message):
            return

        platform = str(context.get("platform") or "").strip().lower()
        if not platform:
            return          # CLI/local turn — nothing to route back to
        chat_id = str(context.get("user_id") or
                      context.get("chat_id") or "").strip()

        chat_key = f"{platform}:{chat_id}" if chat_id else platform
        state = _load_state()
        if _in_cooldown(state, chat_key):
            print(f"[jarvis-google-bridge] {chat_key} within cooldown — "
                  f"not forwarding again", flush=True)
            return

        if _INBOX is None:
            print("[jarvis-google-bridge] no Jarvis checkout found "
                  "(set JARVIS_HOME) — cannot forward", flush=True)
            return

        cmd = [
            "python3", str(_INBOX),
            "--channel", platform,
            "--chat", chat_id,
            "--text", message[:500],
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=30, env=_JARVIS_ENV)
            ok = proc.returncode == 0
        except Exception as e:
            proc = None
            ok = False
            print(f"[jarvis-google-bridge] inbox error: {e}", flush=True)

        if ok:
            _mark_forwarded(state, chat_key)
            print(f"[jarvis-google-bridge] forwarded Google ask from "
                  f"{chat_key} to Jarvis: {message[:80]}", flush=True)
        elif proc is not None:
            # Do NOT start a cooldown on failure — the user should get another
            # chance rather than being silently blocked by a transient error.
            print(f"[jarvis-google-bridge] inbox failed: "
                  f"{(proc.stderr or proc.stdout or '').strip()[:200]}",
                  flush=True)
    except Exception as e:   # never break the gateway
        print(f"[jarvis-google-bridge] error: {e}", flush=True)
