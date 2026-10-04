"""
actions/gmail_control.py — voice control for Gmail.

OAuth setup lives in _google_auth.py's docstring (one-time, Desktop OAuth
client → config/client_secret.json). Scope is gmail.modify: read, send, label,
archive and TRASH — but never a permanent delete. Gmail keeps trashed mail for
30 days, so everything this skill does is recoverable, which is what lets it
answer without a confirmation banner on every step.

Operations (the `operation` parameter):
    list       latest mail (label: unread | inbox | starred | important | sent)
    search     Gmail search syntax in `query` ("from:alice has:attachment")
    read       full body of one message (message_id from a list/search result)
    send       `to` + `subject` + `body` (optional `cc`)
    mark_read  clear UNREAD on one message
    archive    remove INBOX (still in All Mail)
    trash      move to Trash (recoverable for 30 days)

The handler never raises at the model: setup problems come back as SetupNeeded
text (which the model speaks), API errors come back as short plain sentences.
"""

from __future__ import annotations

import base64
import re
from datetime import datetime
from email.message import EmailMessage

from actions import _google_auth as ga

try:
    from googleapiclient.errors import HttpError
    _HAS_GOOGLE = True
except ImportError:
    HttpError = Exception
    _HAS_GOOGLE = False

MAX_RESULTS_DEFAULT = 8
MAX_RESULTS_CAP = 25
BODY_CHARS = 4000

_LABEL_MAP = {
    "unread": ["UNREAD"],
    "inbox": ["INBOX"],
    "starred": ["STARRED"],
    "important": ["IMPORTANT"],
    "sent": ["SENT"],
}


# ── small helpers ─────────────────────────────────────────────────────────────

def _decode_b64(data: str) -> str:
    """Gmail's body data is raw urlsafe base64, unpadded."""
    if not data:
        return ""
    pad = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + pad).decode("utf-8", "replace")
    except Exception:
        return ""


def _strip_html(html: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"[ \t\r\f\v]*\n[ \t]*", "\n", text).strip()


def _collect_body(payload: dict) -> str:
    """Walk a format=full payload; prefer text/plain, fall back to text/html."""
    plains, htmls = [], []

    def walk(node: dict) -> None:
        mime = node.get("mimeType", "")
        data = (node.get("body") or {}).get("data", "")
        if mime == "text/plain" and data:
            plains.append(_decode_b64(data))
        elif mime == "text/html" and data:
            htmls.append(_decode_b64(data))
        for part in node.get("parts") or []:
            walk(part)

    walk(payload or {})
    if plains:
        return "\n".join(p for p in plains if p.strip())
    if htmls:
        return _strip_html("\n".join(htmls))
    return ""


def _header(msg: dict, name: str) -> str:
    for h in (msg.get("payload") or {}).get("headers") or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _short_from(value: str) -> str:
    """"Alice <alice@example.com>" → "Alice <alice@example.com>" but plain
    "alice@example.com" stays as-is — the model needs the address either way."""
    return (value or "").strip()


def _fmt_date(msg: dict) -> str:
    try:
        ms = int(msg.get("internalDate") or 0)
        if ms:
            return datetime.fromtimestamp(ms / 1000).astimezone().strftime("%b %d %H:%M")
    except (ValueError, OSError, OverflowError):
        pass
    return _header(msg, "Date")


def _err(e: Exception) -> str:
    if isinstance(e, HttpError):
        try:
            detail = e.resp.status
        except Exception:
            detail = ""
        return f"Gmail API error ({detail or e})."
    return f"Gmail error: {e}"


# ── API-backed pieces (service is injected so tests can fake it) ─────────────

def _list_message_ids(svc, query: str, label: str, max_results: int) -> list[str]:
    params = {"userId": "me", "maxResults": max_results}
    label_ids = _LABEL_MAP.get((label or "").strip().lower())
    if label_ids:
        params["labelIds"] = label_ids
    if query and query.strip():
        params["q"] = query.strip()
    resp = svc.users().messages().list(**params).execute()
    return [m["id"] for m in (resp.get("messages") or [])]


def _fetch_summaries(svc, ids: list[str]) -> list[dict]:
    out = []
    for mid in ids:
        try:
            msg = (svc.users().messages()
                        .get(userId="me", id=mid, format="metadata",
                             metadataHeaders=["From", "Subject", "Date"])
                        .execute())
        except Exception as e:
            out.append({"id": mid, "error": _err(e)})
            continue
        out.append({
            "id": mid,
            "from": _header(msg, "From"),
            "subject": _header(msg, "Subject") or "(no subject)",
            "date": _fmt_date(msg),
            "snippet": (msg.get("snippet") or "").replace("\n", " ")[:140],
        })
    return out


def _render_list(summaries: list[dict], header: str) -> str:
    if not summaries:
        return f"{header}: no messages."
    lines = [f"{header}: {len(summaries)} message(s)."]
    for i, s in enumerate(summaries, 1):
        if "error" in s:
            lines.append(f"{i}. {s['id']} — could not fetch ({s['error']})")
            continue
        lines.append(
            f"{i}. id={s['id']} | {s['date']} | From: {s['from']} | "
            f"Subject: {s['subject']} | {s['snippet']}"
        )
    lines.append("Use read/mark_read/archive/trash with one of these ids.")
    return "\n".join(lines)


def _send_raw(to: str, subject: str, body: str, cc: str):
    """Returns (raw_mime, "") on success or ("", reason) when the model left
    something out — the reason is a sentence the model should act on."""
    if "@" not in to:
        return "", ("I need the recipient's email address — a name alone is "
                    "not enough for Gmail. Ask me for the address if unsure.")
    if not body.strip():
        return "", "I need some text to put in the email body."
    msg = EmailMessage()
    msg["To"] = to.strip()
    msg["Subject"] = (subject or "").strip() or "(no subject)"
    if cc and cc.strip():
        msg["Cc"] = cc.strip()
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return raw, ""


def _read_message(svc, message_id: str) -> str:
    msg = (svc.users().messages()
                .get(userId="me", id=message_id, format="full").execute())
    body = _collect_body(msg.get("payload") or {})
    if not body:
        body = msg.get("snippet") or "(empty body)"
    body = body[:BODY_CHARS]
    return (
        f"From: {_header(msg, 'From')}\n"
        f"To: {_header(msg, 'To')}\n"
        f"Date: {_fmt_date(msg)}\n"
        f"Subject: {_header(msg, 'Subject') or '(no subject)'}\n"
        f"\n{body}"
    )


def briefing_snapshot() -> str:
    """Unread-mail count for the morning briefing, or "" when Google is not
    connected. Consent-free on purpose (get_service_silent) — the briefing
    must never open a login tab. One labels.get call, never raises."""
    try:
        svc = ga.get_service_silent("gmail", "v1", [ga.GMAIL_SCOPE],
                                    "gmail_token")
        if svc is None:
            return ""
        label = svc.users().labels().get(userId="me", id="UNREAD").execute()
        n = int(label.get("messagesUnread") or 0)
        if n <= 0:
            return "Email: inbox is fully read."
        return f"Email: {n} unread message(s)."
    except Exception as e:
        print(f"[Gmail] briefing snapshot failed: {e}")
        return ""


# ── the skill entry point ─────────────────────────────────────────────────────

def gmail_control(parameters: dict, player=None) -> str:
    params = parameters or {}
    op = (params.get("operation") or "").strip().lower()

    if not op:
        return ("Tell me which Gmail operation to run: list, search, read, "
                "send, mark_read, archive or trash.")

    if not _HAS_GOOGLE:
        return ("The Google API libraries are not installed. Run: "
                "pip install google-api-python-client google-auth-oauthlib")

    try:
        svc = ga.get_service("gmail", "v1", [ga.GMAIL_SCOPE], "gmail_token")
    except ga.SetupNeeded as e:
        return e.message
    except Exception as e:
        return f"Could not connect to Gmail: {e}"

    try:
        return _dispatch(svc, op, params, player)
    except HttpError as e:
        return _err(e)
    except Exception as e:
        return f"Gmail error: {e}"


def _dispatch(svc, op: str, params: dict, player=None) -> str:
    mid = (params.get("message_id") or "").strip()

    if op in ("list", "search"):
        n = _clamp_n(params.get("max_results"))
        ids = _list_message_ids(svc, params.get("query") or "",
                                params.get("label") or "", n)
        header = f"Gmail search “{params.get('query')}”" if op == "search" \
            else f"Gmail {params.get('label') or 'inbox'} — latest"
        out = _render_list(_fetch_summaries(svc, ids), header)
        _log(player, out.splitlines()[0])
        return out

    if op == "read":
        if not mid:
            return "Give me a message_id from a previous list or search."
        out = _read_message(svc, mid)
        _log(player, f"[Gmail] read {mid[:10]}…")
        return out

    if op == "send":
        raw, problem = _send_raw(params.get("to") or "",
                                 params.get("subject") or "",
                                 params.get("body") or "",
                                 params.get("cc") or "")
        if problem:
            return problem
        svc.users().messages().send(
            userId="me", body={"raw": raw}).execute()
        out = f"Email sent to {params.get('to')}."
        _log(player, f"[Gmail] sent → {params.get('to')}")
        return out

    if op == "mark_read":
        if not mid:
            return "Give me a message_id from a previous list or search."
        svc.users().messages().modify(
            userId="me", id=mid,
            body={"removeLabelIds": ["UNREAD"]}).execute()
        return "Marked as read."

    if op == "archive":
        if not mid:
            return "Give me a message_id from a previous list or search."
        svc.users().messages().modify(
            userId="me", id=mid,
            body={"removeLabelIds": ["INBOX"]}).execute()
        return "Archived — still in All Mail, out of the inbox."

    if op == "trash":
        if not mid:
            return "Give me a message_id from a previous list or search."
        svc.users().messages().trash(userId="me", id=mid).execute()
        out = ("Moved to Trash — Gmail keeps it for 30 days if you want it back.")
        _log(player, f"[Gmail] trash {mid[:10]}…")
        return out

    return ("I don't know that Gmail operation. Use list, search, read, send, "
            "mark_read, archive or trash.")


def _clamp_n(value) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return MAX_RESULTS_DEFAULT
    return max(1, min(MAX_RESULTS_CAP, n))


def _log(player, text: str) -> None:
    if player:
        try:
            player.write_log(text[:120])
        except Exception:
            pass


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "gmail_control",
    "description": (
        "Manages the user's Gmail: list recent mail, search (Gmail syntax, e.g. "
        "'from:alice is:unread'), read a full message, send an email, mark as "
        "read, archive, or move to trash. Requires a message_id from an earlier "
        "list/search for read/mark_read/archive/trash. For send, use the "
        "recipient's real email address and write the body in the user's own "
        "language. Never claim an email was sent unless the result says it was."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "operation": {
                "type": "STRING",
                "description": ("One of: list, search, read, send, "
                                "mark_read, archive, trash"),
            },
            "query": {
                "type": "STRING",
                "description": ("Gmail search syntax for operation=search "
                                "(e.g. 'from:boss@x.com has:attachment')"),
            },
            "label": {
                "type": "STRING",
                "description": ("For list: unread, inbox, starred, important "
                                "or sent (default inbox)"),
            },
            "max_results": {
                "type": "NUMBER",
                "description": "How many messages to list (1-25, default 8)",
            },
            "message_id": {
                "type": "STRING",
                "description": ("Message id from a previous list/search — "
                                "required by read/mark_read/archive/trash"),
            },
            "to": {
                "type": "STRING",
                "description": "Recipient email address (operation=send)",
            },
            "cc": {
                "type": "STRING",
                "description": "Optional CC address (operation=send)",
            },
            "subject": {
                "type": "STRING",
                "description": "Subject line (operation=send)",
            },
            "body": {
                "type": "STRING",
                "description": ("Email body text, in the user's own language "
                                "(operation=send)"),
            },
        },
        "required": ["operation"],
    },
    "handler": gmail_control,
}
