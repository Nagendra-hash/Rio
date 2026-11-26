"""
actions/calendar_control.py — voice control for Google Calendar.

Same OAuth setup as gmail_control (shared client secret in
config/client_secret.json; its own token in config/calendar_token.json so
revoking one never invalidates the other). Scope is calendar.events: events on
the user's primary calendar only — no other people's free/busy, no calendar
admin, no settings.

Operations (the `operation` parameter):
    list    events in a window (default: today, next 7 days)
    create  title + date (+ start_time or all_day, end_time/duration_minutes,
            location, description, timezone)
    delete  by event_id, or by match_title within the window. Calendar v3 has
            no trash endpoint — delete is PERMANENT — so it goes behind the
            HUD confirmation gate and only runs once the user presses CONFIRM.

Times are spoken-language friendly: the model supplies YYYY-MM-DD and HH:MM
(24h), this file does the RFC3339 conversion with the machine's local offset —
or an explicit IANA timezone when one is given.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from actions import _google_auth as ga

try:
    from googleapiclient.errors import HttpError

    _HAS_GOOGLE = True
except ImportError:
    HttpError = Exception
    _HAS_GOOGLE = False

MAX_RESULTS_CAP = 50


# ── parsing / formatting (unit-tested directly, no API needed) ───────────────


def _parse_date(value: str):
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _parse_hhmm(value: str):
    try:
        h, m = (value or "").strip().split(":")
        return int(h), int(m)
    except (ValueError, TypeError):
        return None


def _resolve_tz(timezone: str):
    """ZoneInfo for an explicit name, else the machine's local zone. (None, name)
    only if the caller gave a bad name — handled as a user-facing error. The
    second element is the IANA name to echo into the payload; it stays empty
    for the local zone, whose ISO offset already says everything."""
    tz = (timezone or "").strip()
    if tz:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(tz), tz
        except Exception:
            return None, tz
    return datetime.now().astimezone().tzinfo, ""


def _when(date_part: date, hhmm, tzinfo, minutes_from_start: int = 0):
    """Local datetime → RFC3339 isoformat (offset included, which is what the
    Events API wants)."""
    base = datetime(
        date_part.year, date_part.month, date_part.day, hhmm[0], hhmm[1], tzinfo=tzinfo
    )
    return (base + timedelta(minutes=minutes_from_start)).isoformat()


def _fmt_when(ev: dict) -> str:
    """Human one-liner for an event's start/end, e.g. 'Oct 5 14:00–15:00'."""
    start = ev.get("start") or {}
    end = ev.get("end") or {}
    if start.get("date"):  # all-day
        return f"{start['date']} (all day)"
    try:
        s = datetime.fromisoformat(start["dateTime"]).astimezone()
        e = datetime.fromisoformat(end["dateTime"]).astimezone()
    except (KeyError, ValueError, TypeError):
        return start.get("dateTime", "?")
    same_day = s.date() == e.date()
    if same_day:
        return f"{s.strftime('%b %d %H:%M')}–{e.strftime('%H:%M')}"
    return f"{s.strftime('%b %d %H:%M')} → {e.strftime('%b %d %H:%M')}"


def _fmt_event(ev: dict) -> str:
    title = ev.get("summary") or "(untitled)"
    loc = f" @ {ev['location']}" if ev.get("location") else ""
    return f"id={ev.get('id', '')} | {_fmt_when(ev)} | {title}{loc}"


def _fmt_event_brief(ev: dict) -> str:
    """Compact form for the morning briefing — no ids, and within a single
    day the date is dropped ("Dentist 14:00–15:00", "Holiday (all day)")."""
    title = ev.get("summary") or "untitled"
    start, end = ev.get("start") or {}, ev.get("end") or {}
    if start.get("date"):
        return f"{title} (all day)"
    try:
        s = datetime.fromisoformat(start["dateTime"]).astimezone()
        e = datetime.fromisoformat(end["dateTime"]).astimezone()
    except (KeyError, ValueError, TypeError):
        return title
    if s.date() == e.date():
        return f"{title} {s.strftime('%H:%M')}–{e.strftime('%H:%M')}"
    return f"{title} {s.strftime('%b %d %H:%M')} → {e.strftime('%b %d %H:%M')}"


def briefing_snapshot() -> str:
    """Tomorrow's events as one line for the morning briefing, or "" when
    Google is not connected. Consent-free on purpose (get_service_silent) —
    the briefing must never open a login tab. Never raises."""
    try:
        svc = ga.get_service_silent(
            "calendar", "v3", [ga.CALENDAR_SCOPE], "calendar_token"
        )
        if svc is None:
            return ""
        day = date.today() + timedelta(days=1)
        tmin, tmax = _window(day, 1)
        events = _list_events(svc, tmin, tmax, 10)
        if not events:
            return (
                f"Calendar: nothing scheduled tomorrow "
                f"({day.strftime('%a %b %d')})."
            )
        items = "; ".join(_fmt_event_brief(ev) for ev in events)
        return (
            f"Calendar tomorrow ({day.strftime('%a %b %d')}): "
            f"{len(events)} event(s) — {items}."
        )
    except Exception as e:
        print(f"[Calendar] briefing snapshot failed: {e}")
        return ""


# ── API-backed pieces (service injected so tests can fake it) ────────────────


def _list_events(
    svc, time_min_iso: str, time_max_iso: str, max_results: int
) -> list[dict]:
    resp = (
        svc.events()
        .list(
            calendarId="primary",
            timeMin=time_min_iso,
            timeMax=time_max_iso,
            singleEvents=True,
            orderBy="startTime",
            maxResults=max_results,
        )
        .execute()
    )
    return resp.get("items") or []


def _window(date_part: date, days: int):
    start_dt = datetime(
        date_part.year,
        date_part.month,
        date_part.day,
        tzinfo=datetime.now().astimezone().tzinfo,
    )
    return start_dt.isoformat(), (start_dt + timedelta(days=days)).isoformat()


def _render_window(day: date, days: int, events: list[dict]) -> str:
    end_day = day + timedelta(days=days - 1)
    header = (
        f"Calendar {day.strftime('%b %d')} – {end_day.strftime('%b %d')}"
        if days > 1
        else f"Calendar {day.strftime('%b %d')}"
    )
    if not events:
        return f"{header}: nothing scheduled."
    lines = [f"{header}: {len(events)} event(s)."]
    for ev in events:
        lines.append(_fmt_event(ev))
    return "\n".join(lines)


def _create_body(params: dict, day: date):
    """Build the event body. Returns (event_dict, "") or ("", problem)."""
    title = (params.get("title") or "").strip()
    if not title:
        return "", "I need a title for the event."

    all_day = bool(params.get("all_day"))
    tzinfo, tz_name = _resolve_tz(params.get("timezone") or "")
    if tzinfo is None:
        return "", f"I don't recognise the timezone '{tz_name}'."

    event: dict = {"summary": title}
    if params.get("location"):
        event["location"] = params["location"].strip()
    if params.get("description"):
        event["description"] = params["description"].strip()

    if all_day:
        event["start"] = {"date": day.isoformat()}
        event["end"] = {"date": (day + timedelta(days=1)).isoformat()}
        return event, ""

    start = _parse_hhmm(params.get("start_time") or "")
    if start is None:
        return "", (
            "I need a start time in HH:MM (24-hour) — or say all_day "
            "for an all-day event."
        )
    end_spec = (params.get("end_time") or "").strip()
    if end_spec:
        end = _parse_hhmm(end_spec)
        if end is None:
            return "", "I couldn't read that end time — use HH:MM (24-hour)."
        start_min = start[0] * 60 + start[1]
        end_min = end[0] * 60 + end[1]
        if end_min <= start_min:
            return "", "The end time has to be after the start time."
        duration = end_min - start_min
    else:
        try:
            duration = int(params.get("duration_minutes") or 60)
        except (TypeError, ValueError):
            duration = 60
        duration = max(5, min(24 * 60, duration))

    event["start"] = {"dateTime": _when(day, start, tzinfo)}
    event["end"] = {"dateTime": _when(day, start, tzinfo, duration)}
    if (params.get("timezone") or "").strip():
        event["start"]["timeZone"] = tz_name
        event["end"]["timeZone"] = tz_name
    return event, ""


# ── the skill entry point ─────────────────────────────────────────────────────


def calendar_control(parameters: dict, player=None) -> str:
    params = parameters or {}
    op = (params.get("operation") or "").strip().lower()

    if not op:
        return "Tell me which calendar operation to run: list, create or " "delete."

    if not _HAS_GOOGLE:
        return (
            "The Google API libraries are not installed. Run: "
            "pip install google-api-python-client google-auth-oauthlib"
        )

    try:
        svc = ga.get_service("calendar", "v3", [ga.CALENDAR_SCOPE], "calendar_token")
    except ga.SetupNeeded as e:
        return e.message
    except Exception as e:
        return f"Could not connect to Google Calendar: {e}"

    try:
        return _dispatch(svc, op, params, player)
    except HttpError as e:
        try:
            return f"Calendar API error ({e.resp.status})."
        except Exception:
            return f"Calendar API error: {e}"
    except Exception as e:
        return f"Calendar error: {e}"


def _dispatch(svc, op: str, params: dict, player=None) -> str:
    day = _parse_date(params.get("date") or "")
    days = _clamp_days(params.get("days"))

    if op == "list":
        if day is None:
            day = date.today()
        tmin, tmax = _window(day, days)
        events = _list_events(svc, tmin, tmax, _clamp_n(params.get("max_results"), 25))
        out = _render_window(day, days, events)
        _log(player, f"[Calendar] {out.splitlines()[0]}")
        return out

    if op == "create":
        if day is None:
            return "I need the date for the event, as YYYY-MM-DD."
        event, problem = _create_body(params, day)
        if problem:
            return problem
        created = svc.events().insert(calendarId="primary", body=event).execute()
        out = f"Event created: {created.get('summary')} — " f"{_fmt_when(created)}."
        if created.get("htmlLink"):
            out += f" Link: {created['htmlLink']}"
        _log(player, f"[Calendar] created: {created.get('summary')}")
        return out

    if op == "delete":
        # Calendar v3 has NO trash endpoint — events().delete() is permanent,
        # so this is the one operation in this skill that cannot be undone and
        # MUST go through the HUD confirmation gate (core/confirm.py). It parks
        # behind a banner and returns immediately; the delete runs only if the
        # human presses CONFIRM.
        event_id = (params.get("event_id") or "").strip()
        if event_id:
            return _confirm_delete(svc, event_id, "that event", player)

        match = (params.get("match_title") or "").strip()
        if not match:
            return (
                "Tell me which event to delete — its id from a list, or a "
                "title to match."
            )
        if day is None:
            day = date.today()
        tmin, tmax = _window(day, days)
        events = _list_events(svc, tmin, tmax, MAX_RESULTS_CAP)
        needle = match.lower()
        hits = [ev for ev in events if needle in (ev.get("summary") or "").lower()]

        if not hits:
            return f"I don't see any event matching “{match}” in that window."
        if len(hits) > 1:
            lines = [
                f"I found {len(hits)} events matching “{match}” — "
                "which one? Tell me the id or a more exact title:"
            ]
            lines += [f"- {_fmt_event(ev)}" for ev in hits]
            return "\n".join(lines)

        return _confirm_delete(
            svc,
            hits[0]["id"],
            f"“{hits[0].get('summary')}” " f"({_fmt_when(hits[0])})",
            player,
        )

    return "I don't know that calendar operation. Use list, create or " "delete."


def _confirm_delete(svc, event_id: str, label: str, player=None) -> str:
    """Park a permanent event delete behind the HUD confirmation gate.

    core/confirm.py hands the UI a CONFIRM / CANCEL banner and returns at once,
    so the model keeps talking while it waits and the interface never blocks.
    Nothing here deletes until the user presses CONFIRM — and the calendar v3
    delete really is permanent, which is why it needs the gate that reading,
    creating and trash-equivalent operations do not.
    """
    try:
        from core import confirm
    except Exception as e:
        # No gate available (headless, or a broken core import). Refuse rather
        # than delete something that cannot be undone.
        return (
            f"I cannot delete {label} because the confirmation interface "
            f"is unavailable, so I have not done it ({e})."
        )

    if confirm.pending_title():
        return (
            "There is already a confirmation waiting on screen. Ask the "
            "user to answer that one first."
        )

    def _run() -> str:
        svc.events().delete(calendarId="primary", eventId=event_id).execute()
        out = f"Deleted {label}."
        _log(player, f"[Calendar] deleted {event_id[:10]}…")
        return out

    return confirm.request(
        key="calendar_delete_event",
        title=f"Delete calendar event: {label}",
        detail=(
            "This permanently deletes the event from your Google Calendar "
            "— it cannot be undone."
        ),
        run=_run,
    )


def _clamp_days(value) -> int:
    try:
        d = int(value)
    except (TypeError, ValueError):
        return 7
    return max(1, min(60, d))


def _clamp_n(value, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(MAX_RESULTS_CAP, n))


def _log(player, text: str) -> None:
    if player:
        try:
            player.write_log(text[:120])
        except Exception:
            pass


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "calendar_control",
    "description": (
        "Manages the user's Google Calendar (primary): list upcoming events, "
        "create events, and delete events. Dates are YYYY-MM-DD, times HH:MM "
        "24-hour in the user's local timezone unless a timezone is given. "
        "Deleting is PERMANENT and puts a confirmation on the user's screen — "
        "it does NOT happen until they press CONFIRM, so never tell the user "
        "an event was deleted unless the result says so. Deleting by title "
        "asks back when the match is ambiguous."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "operation": {
                "type": "STRING",
                "description": "One of: list, create, delete",
            },
            "date": {
                "type": "STRING",
                "description": (
                    "YYYY-MM-DD — window start for list/delete, "
                    "event date for create (required for create)"
                ),
            },
            "days": {
                "type": "NUMBER",
                "description": "Window size in days for list/delete (1-60, default 7)",
            },
            "max_results": {
                "type": "NUMBER",
                "description": "Max events to list (default 25)",
            },
            "title": {
                "type": "STRING",
                "description": "Event title (operation=create)",
            },
            "start_time": {
                "type": "STRING",
                "description": "HH:MM 24-hour start (operation=create unless all_day)",
            },
            "end_time": {
                "type": "STRING",
                "description": "HH:MM 24-hour end — or give duration_minutes instead",
            },
            "duration_minutes": {
                "type": "NUMBER",
                "description": "Event length in minutes when no end_time (default 60)",
            },
            "all_day": {
                "type": "BOOLEAN",
                "description": "true for an all-day event (no start_time needed)",
            },
            "location": {
                "type": "STRING",
                "description": "Optional location (operation=create)",
            },
            "description": {
                "type": "STRING",
                "description": "Optional notes (operation=create)",
            },
            "timezone": {
                "type": "STRING",
                "description": (
                    "Optional IANA timezone like Asia/Kolkata "
                    "(default: this machine's timezone)"
                ),
            },
            "event_id": {
                "type": "STRING",
                "description": "Event id from a list (operation=delete)",
            },
            "match_title": {
                "type": "STRING",
                "description": (
                    "Title substring to find the event when no "
                    "event_id is given (operation=delete)"
                ),
            },
        },
        "required": ["operation"],
    },
    "handler": calendar_control,
}
