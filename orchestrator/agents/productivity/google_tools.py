"""
orchestrator/agents/productivity/google_tools.py — the shared tool layer.

The ONLY code in this package that talks to Google. Both the Inbox Agent and
the Calendar Agent go through these functions; tests inject a fake service,
production builds one via actions/_google_auth (the existing OAuth plumbing,
same tokens the gmail_control / calendar_control skills already use — no new
scopes are ever requested).

Design rules:
  * the service is INJECTABLE (fake it in tests, never hit the network);
  * every function returns plain dicts and never raises outward;
  * tokens/secrets are handled only inside actions._google_auth and are
    never included in any returned dict;
  * message/event content returned here is RAW — the agents pass it through
    security.redact() before it reaches the user or the store.
"""
from __future__ import annotations

import base64
import re
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Any, Optional

MAX_RESULTS_CAP = 25
BODY_CHARS = 4_000
EVENTS_WINDOW_CAP_DAYS = 60

try:                                     # google libs optional at import time
    from googleapiclient.errors import HttpError
    _HAS_GOOGLE = True
except ImportError:                      # pragma: no cover - env dependent
    HttpError = Exception
    _HAS_GOOGLE = False


def google_available() -> bool:
    return _HAS_GOOGLE


# ── errors ───────────────────────────────────────────────────────────────────

class ToolError(Exception):
    """A tool-level failure with a short, user-safe message."""


def _err(e: Exception) -> str:
    if _HAS_GOOGLE and isinstance(e, HttpError):
        try:
            return f"Google API error ({e.resp.status})."
        except Exception:
            return f"Google API error ({e})."
    return f"Google error: {type(e).__name__}"


# ── gmail ────────────────────────────────────────────────────────────────────

def _decode_b64(data: str) -> str:
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


def collect_body(payload: dict) -> str:
    """Walk a format=full payload; prefer text/plain, fall back to html."""
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


def _fmt_date(msg: dict) -> str:
    try:
        ms = int(msg.get("internalDate") or 0)
        if ms:
            return datetime.fromtimestamp(ms / 1000).astimezone() \
                .strftime("%b %d %H:%M")
    except (ValueError, OSError, OverflowError):
        pass
    return _header(msg, "Date")


_LABEL_MAP = {
    "unread": ["UNREAD"], "inbox": ["INBOX"], "starred": ["STARRED"],
    "important": ["IMPORTANT"], "sent": ["SENT"],
}


def list_message_ids(svc, query: str = "", label: str = "",
                     max_results: int = 10) -> list[str]:
    params: dict[str, Any] = {"userId": "me",
                              "maxResults": max(1, min(MAX_RESULTS_CAP,
                                                       int(max_results)))}
    label_ids = _LABEL_MAP.get((label or "").strip().lower())
    if label_ids:
        params["labelIds"] = label_ids
    if query and query.strip():
        params["q"] = query.strip()
    resp = svc.users().messages().list(**params).execute()
    return [m["id"] for m in (resp.get("messages") or [])]


def fetch_message(svc, message_id: str, full: bool = False) -> dict:
    """One message as a plain dict. Never includes token material."""
    fmt = "full" if full else "metadata"
    kwargs = {"userId": "me", "id": message_id, "format": fmt}
    if not full:
        kwargs["metadataHeaders"] = ["From", "To", "Subject", "Date",
                                     "List-Unsubscribe"]
    msg = svc.users().messages().get(**kwargs).execute()
    out = {
        "id": message_id,
        "thread_id": msg.get("threadId", ""),
        "from": _header(msg, "From"),
        "to": _header(msg, "To"),
        "subject": _header(msg, "Subject") or "(no subject)",
        "date": _fmt_date(msg),
        "snippet": (msg.get("snippet") or "").replace("\n", " ")[:200],
        "labels": list(msg.get("labelIds") or []),
        "unsubscribe": _header(msg, "List-Unsubscribe"),
    }
    if full:
        body = collect_body(msg.get("payload") or {})
        out["body"] = (body or msg.get("snippet") or "")[:BODY_CHARS]
    return out


def fetch_messages(svc, ids: list[str], full: bool = False) -> list[dict]:
    out = []
    for mid in ids[:MAX_RESULTS_CAP]:
        try:
            out.append(fetch_message(svc, mid, full=full))
        except Exception as e:
            out.append({"id": mid, "error": _err(e)})
    return out


def build_raw_email(to: str, subject: str, body: str,
                    cc: str = "") -> tuple[str, str]:
    """Returns (raw_mime, "") or ("", reason) — reason is user-actionable."""
    if "@" not in (to or ""):
        return "", "I need the recipient's email address."
    if not (body or "").strip():
        return "", "I need some text for the email body."
    msg = EmailMessage()
    msg["To"] = to.strip()
    msg["Subject"] = (subject or "").strip() or "(no subject)"
    if cc and cc.strip():
        msg["Cc"] = cc.strip()
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode(), ""


def send_raw(svc, raw: str) -> str:
    """Send. The caller MUST have parked this behind an approval first."""
    resp = svc.users().messages().send(
        userId="me", body={"raw": raw}).execute()
    return str(resp.get("id") or "")


def modify_labels(svc, message_id: str, remove: list[str],
                  add: list[str] | None = None) -> bool:
    body: dict[str, Any] = {}
    if remove:
        body["removeLabelIds"] = remove
    if add:
        body["addLabelIds"] = add
    svc.users().messages().modify(
        userId="me", id=message_id, body=body).execute()
    return True


def trash_message(svc, message_id: str) -> bool:
    """Gmail trash — recoverable for 30 days. Permanent delete is BLOCKED."""
    svc.users().messages().trash(userId="me", id=message_id).execute()
    return True


# ── calendar ─────────────────────────────────────────────────────────────────

def _rfc3339(dt: datetime) -> str:
    return dt.isoformat()


def parse_when(value: str, tz=None) -> Optional[datetime]:
    """Best-effort datetime parse for 'YYYY-MM-DD HH:MM' shapes (24h)."""
    v = (value or "").strip()
    if not v:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(v, fmt)
            return dt.replace(tzinfo=tz) if tz else dt
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(v)
        return dt
    except ValueError:
        return None


def list_events(svc, time_min: datetime, time_max: datetime,
                max_results: int = 50, calendar_id: str = "primary") -> list[dict]:
    resp = (svc.events()
                .list(calendarId=calendar_id,
                      timeMin=_rfc3339(time_min), timeMax=_rfc3339(time_max),
                      singleEvents=True, orderBy="startTime",
                      maxResults=max(1, min(250, int(max_results))))
                .execute())
    return resp.get("items") or []


def event_window(events: list[dict]) -> list[dict]:
    """Normalize events to [{start, end, summary, id, attendees}] with
    timezone-aware datetimes (naive stays naive = local)."""
    out = []
    for ev in events:
        start = (ev.get("start") or {})
        end = (ev.get("end") or {})
        if start.get("date"):                       # all-day
            s = datetime.fromisoformat(start["date"])
            e = datetime.fromisoformat(end.get("date") or start["date"])
            all_day = True
        else:
            try:
                s = datetime.fromisoformat(start["dateTime"])
                e = datetime.fromisoformat((end.get("dateTime")
                                            or start["dateTime"]))
            except (KeyError, ValueError, TypeError):
                continue
            all_day = False
        out.append({
            "id": ev.get("id", ""),
            "summary": ev.get("summary") or "(untitled)",
            "start": s, "end": e, "all_day": all_day,
            "attendees": [a.get("email", "") for a in
                          (ev.get("attendees") or []) if a.get("email")],
        })
    out.sort(key=lambda x: (x["start"], x["end"]))
    return out


def build_event_body(title: str, start: datetime, end: datetime,
                     timezone_name: str = "", description: str = "",
                     location: str = "", attendees: list[str] | None = None,
                     event_id_to_move: str = "") -> dict:
    """The event dict for insert/update. Attendees make it a REAL invite."""
    body: dict[str, Any] = {
        "summary": (title or "").strip() or "(untitled)",
        "start": {"dateTime": _rfc3339(start)},
        "end": {"dateTime": _rfc3339(end)},
    }
    if timezone_name:
        body["start"]["timeZone"] = timezone_name
        body["end"]["timeZone"] = timezone_name
    if description:
        body["description"] = description[:2000]
    if location:
        body["location"] = location[:300]
    clean = [a.strip() for a in (attendees or []) if "@" in (a or "")]
    if clean:
        body["attendees"] = [{"email": a} for a in clean]
    return body


def insert_event(svc, body: dict, calendar_id: str = "primary") -> dict:
    return svc.events().insert(calendarId=calendar_id, body=body).execute()


def update_event(svc, event_id: str, body: dict,
                 calendar_id: str = "primary") -> dict:
    return svc.events().update(calendarId=calendar_id, eventId=event_id,
                               body=body).execute()


def get_event(svc, event_id: str, calendar_id: str = "primary") -> dict:
    return svc.events().get(calendarId=calendar_id,
                            eventId=event_id).execute()
