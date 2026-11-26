"""
actions/_google_auth.py — the shared Google OAuth plumbing.

gmail_control and calendar_control both need exactly the same three things:
a client secret, a stored token, and a googleapiclient service built on top of
them. This module is that shared half; the leading underscore keeps the action
loader from treating it as a skill (files starting with "_" are skipped, same
convention as plugins/).

SETUP (one time, per Google account)
    1. https://console.cloud.google.com → create (or pick) a project.
    2. APIs & Services → Library → enable **Gmail API**, **Google Calendar API**
       and **Google Drive API**.
    3. APIs & Services → OAuth consent screen → External → add yourself as a
       test user (scopes are only "modify"-grade, but Google still wants it).
    4. APIs & Services → Credentials → Create credentials → **OAuth client ID**
       → Application type: **Desktop app** → Download JSON.
    5. Save the download as ``config/client_secret.json`` next to api_keys.json
       (both are gitignored; JARVIS_HOME relocates the folder in pendrive mode).

The first voice command then opens a browser for the consent screen and stores
the token as ``config/<token_name>.json``. Tokens auto-refresh; a refresh that
Google rejects (revoked access, password change) raises SetupNeeded and the
skill tells the user to run the command again — which reopens consent.

SCOPES (least privilege, deliberately)
    Gmail    — gmail.modify: read, send, labels, trash. NO permanent delete.
    Calendar — calendar.events: create/edit/trash events on the user's own
               calendar. No free/busy of other people, no calendar admin.
    Drive    — drive: full file management (list/search/upload/download/
               move/copy/rename/permissions) across the user's own Drive.
               Kept in a SEPARATE token file (drive_token) so access to Drive
               can be revoked without touching Gmail or Calendar.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# ── Scopes ────────────────────────────────────────────────────────────────────
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"
# Drive needs full file management (rename/move/copy/permissions), not just
# drive.file. It lives in its own consent + token file (drive_token) so that
# revoking Drive never breaks Gmail, and vice versa.
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"

_SETUP_STEPS = (
    "Google account access needs a one-time setup:\n"
    "1. Open console.cloud.google.com, create or pick a project.\n"
    "2. Enable the Gmail API, the Google Calendar API and the Google Drive API "
    "(APIs & Services → Library).\n"
    "3. OAuth consent screen → External → add your own account as a test user.\n"
    "4. Credentials → Create credentials → OAuth client ID → Desktop app → Download JSON.\n"
    "5. Save that file as client_secret.json in the app's config folder "
    "(next to api_keys.json).\n"
    "Then ask me again and a browser window will open for the one-time consent."
)


class SetupNeeded(Exception):
    """Raised instead of a raw library error when the fix is a user action.

    The message is written to be SPOKEN back to the user by the model, so it
    must say what is missing and what to do — never a bare traceback.
    """

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def base_dir() -> Path:
    """Same JARVIS_HOME contract as memory_manager / config_manager: the env var
    relocates every writable path; frozen builds anchor on the executable."""
    import os

    home = os.environ.get("JARVIS_HOME", "").strip()
    if home:
        return Path(home).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def config_dir() -> Path:
    return base_dir() / "config"


def client_secret_path() -> Path:
    return config_dir() / "client_secret.json"


def token_path(token_name: str) -> Path:
    """config/<token_name>.json — covered by .gitignore's **/token*.json."""
    return config_dir() / f"{token_name}.json"


def _require_google_libs():
    """Import the google stack or raise SetupNeeded with the pip line."""
    try:
        from google.auth.transport.requests import Request  # noqa: F401
        from google.oauth2.credentials import Credentials  # noqa: F401
        from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: F401
        from googleapiclient.discovery import build  # noqa: F401
    except ImportError as e:
        raise SetupNeeded(
            "The Google API libraries are not installed. Run: "
            "pip install google-api-python-client google-auth-oauthlib"
        ) from e


def _load_or_consent(token_file: Path, scopes: list[str]):
    """Return live Credentials for `scopes`, refreshing or re-consenting as needed.

    Consent opens the machine's default browser and blocks this (worker) thread
    until the user finishes — tool handlers already run off the UI thread, so
    the HUD keeps breathing while the tab is up.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if token_file.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_file), scopes)
        except Exception as e:
            # Corrupt/partial token file — reconsent rather than die.
            print(f"[GoogleAuth] ⚠️ unreadable token {token_file.name}: {e}")
            creds = None

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:
            print(f"[GoogleAuth] ⚠️ refresh failed ({e}) — asking for consent.")
            creds = None
        else:
            _save_token(token_file, creds)

    if not creds or not creds.valid:
        secret = client_secret_path()
        flow = InstalledAppFlow.from_client_secrets_file(str(secret), scopes)
        try:
            creds = flow.run_local_server(
                port=0,
                timeout_seconds=300,
                prompt="consent",
                success_message="JARVIS is connected — you can close this tab.",
            )
        except TypeError:
            # Older google-auth-oauthlib without timeout_seconds/success_message.
            creds = flow.run_local_server(port=0)
        _save_token(token_file, creds)

    return creds


def _save_token(token_file: Path, creds) -> None:
    try:
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(creds.to_json(), encoding="utf-8")
        token_file.chmod(0o600)
    except Exception as e:
        print(f"[GoogleAuth] ⚠️ could not persist {token_file.name}: {e}")


def get_service(api_name: str, api_version: str, scopes, token_name: str):
    """Authenticated googleapiclient service, or SetupNeeded that explains itself.

    `token_name` keeps Gmail and Calendar tokens in separate files so revoking
    one connection never invalidates the other.
    """
    _require_google_libs()

    secret = client_secret_path()
    if not secret.exists():
        raise SetupNeeded(_SETUP_STEPS)

    token_file = token_path(token_name)
    scopes = list(scopes)
    try:
        creds = _load_or_consent(token_file, scopes)
    except FileNotFoundError as e:
        raise SetupNeeded(_SETUP_STEPS) from e

    from googleapiclient.discovery import build

    try:
        return build(api_name, api_version, credentials=creds, cache_discovery=False)
    except Exception as e:
        raise SetupNeeded(
            f"Could not connect to the Google {api_name.capitalize()} API: {e}"
        ) from e


def get_service_silent(api_name: str, api_version: str, scopes, token_name: str):
    """Like get_service, but NEVER asks for consent — None instead.

    Background surfaces (the morning briefing) call this: a browser tab
    popping up at 7am to ask for a Google login is not a briefing, it is an
    ambush. A stored token that is valid — or refreshes cleanly — is used;
    everything else reads as "not set up" and the caller stays quiet.
    """
    try:
        _require_google_libs()
    except SetupNeeded:
        return None
    if not client_secret_path().exists() or not token_path(token_name).exists():
        return None
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        creds = Credentials.from_authorized_user_file(
            str(token_path(token_name)), list(scopes)
        )
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _save_token(token_path(token_name), creds)  # keep it fresh
        if not creds.valid:
            return None
        from googleapiclient.discovery import build

        return build(api_name, api_version, credentials=creds, cache_discovery=False)
    except Exception as e:
        print(f"[GoogleAuth] silent connect ({token_name}) failed: {e}")
        return None


def briefing_context() -> str:
    """Combined day-ahead snapshot for the morning briefing: tomorrow's
    calendar events plus the unread-mail count, one line each.

    Consent-free (get_service_silent) and never raises — each half degrades to
    an empty string on its own, and an empty result reads as "nothing to say".
    This is the function main.py hands to run_in_executor during phase 1 of
    the briefing.
    """
    try:
        from actions.calendar_control import briefing_snapshot as _calendar
    except Exception:
        _calendar = None
    try:
        from actions.gmail_control import briefing_snapshot as _gmail
    except Exception:
        _gmail = None

    parts = []
    for snapshot in (_calendar, _gmail):
        if snapshot is None:
            continue
        try:
            text = (snapshot() or "").strip()
        except Exception as e:
            print(f"[GoogleAuth] briefing snapshot failed: {e}")
            text = ""
        if text:
            parts.append(text)
    return "\n".join(parts)


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
