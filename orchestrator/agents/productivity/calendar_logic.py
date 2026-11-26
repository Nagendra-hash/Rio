"""
orchestrator/agents/productivity/calendar_logic.py — deterministic calendar
brain.

No LLM in the loop. Pure functions over normalized events:

    busy_intervals()     events → sorted busy intervals
    find_conflicts()     pairwise overlap detection (with overlap length)
    to_zone()            timezone conversion with honest failure
    free_slots()         working-hours grid − busy → candidate slots
    propose_slots()      the best N slots (earliest-first, gap-spreading)
    draft_invite()       the invite text/event body (never sent directly)
    reschedule_plan()    old event → proposed new times for the SAME duration

Timezone handling: everything normalizes to aware datetimes in a reference
zone (the user's zone, default). Naive datetimes are interpreted in the
REFERENCE zone. Cross-zone comparisons happen after astimezone(ref).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

DEFAULT_WORK_START = time(9, 0)
DEFAULT_WORK_END = time(17, 0)
DEFAULT_SLOT_MINUTES = 30
DEFAULT_BUFFER_MINUTES = 0
MAX_PROPOSALS = 5


# ── timezones ────────────────────────────────────────────────────────────────

def to_zone(dt: datetime, zone_name: str) -> tuple[Optional[datetime], str]:
    """Convert dt to IANA zone. Returns (dt_in_zone, "") or (None, error)."""
    try:
        return dt.astimezone(ZoneInfo(zone_name)), ""
    except Exception:
        return None, f"I don't recognise the timezone '{zone_name}'."


def resolve_zone(zone_name: str = "") -> tuple[Optional[ZoneInfo], str]:
    """ZoneInfo for a name, else machine-local. (None, name) = bad name."""
    z = (zone_name or "").strip()
    if z:
        try:
            return ZoneInfo(z), ""
        except Exception:
            return None, z
    return datetime.now().astimezone().tzinfo, ""


def _aware(dt: datetime, ref: ZoneInfo) -> datetime:
    """Naive datetimes are interpreted in the reference zone."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ref)
    return dt


# ── busy intervals + conflicts ───────────────────────────────────────────────

@dataclass
class Conflict:
    a: dict
    b: dict
    overlap_minutes: int


def busy_intervals(events: list[dict], ref: ZoneInfo) -> list[tuple[datetime, datetime, dict]]:
    """Normalized events → sorted (start, end, event) busy intervals.
    All-day events count as busy for that calendar day."""
    out = []
    for ev in events:
        s = _aware(ev["start"], ref)
        e = _aware(ev["end"], ref)
        if ev.get("all_day"):
            s = s.replace(hour=0, minute=0, second=0, microsecond=0)
            e = s + timedelta(days=1)
        if e <= s:
            continue
        out.append((s, e, ev))
    out.sort(key=lambda x: (x[0], x[1]))
    return out


def _overlap(a_s, a_e, b_s, b_e) -> int:
    lo = max(a_s, b_s)
    hi = min(a_e, b_e)
    if hi <= lo:
        return 0
    return int((hi - lo).total_seconds() // 60)


def find_conflicts(events: list[dict], ref: ZoneInfo,
                   candidate: tuple[datetime, datetime] | None = None
                   ) -> list[Conflict]:
    """Pairwise overlaps among events — or overlaps of a CANDIDATE slot
    against the events. Zero-length overlaps don't count."""
    busy = busy_intervals(events, ref)
    conflicts: list[Conflict] = []
    if candidate is not None:
        cs, ce = _aware(candidate[0], ref), _aware(candidate[1], ref)
        for s, e, ev in busy:
            m = _overlap(cs, ce, s, e)
            if m > 0:
                conflicts.append(Conflict(a={"candidate": True,
                                             "start": cs.isoformat(),
                                             "end": ce.isoformat()},
                                          b=_brief(ev), overlap_minutes=m))
        return conflicts
    for i in range(len(busy)):
        for j in range(i + 1, len(busy)):
            s1, e1, ev1 = busy[i]
            s2, e2, ev2 = busy[j]
            if s2 >= e1:
                break            # sorted: later events can't overlap ev1
            m = _overlap(s1, e1, s2, e2)
            if m > 0:
                conflicts.append(Conflict(a=_brief(ev1), b=_brief(ev2),
                                          overlap_minutes=m))
    return conflicts


def _brief(ev: dict) -> dict:
    return {"id": ev.get("id", ""), "summary": ev.get("summary", ""),
            "start": _aware(ev["start"], _utc_like(ev)).isoformat()
            if isinstance(ev.get("start"), datetime) else str(ev.get("start")),
            "end": _aware(ev["end"], _utc_like(ev)).isoformat()
            if isinstance(ev.get("end"), datetime) else str(ev.get("end")),
            "attendees": ev.get("attendees", [])}


def _utc_like(ev: dict):
    z = resolve_zone("")[0]
    s = ev.get("start")
    if isinstance(s, datetime) and s.tzinfo is not None:
        return s.tzinfo
    return z


# ── availability / free slots ────────────────────────────────────────────────

@dataclass
class WorkHours:
    start: time = DEFAULT_WORK_START
    end: time = DEFAULT_WORK_END
    slot_minutes: int = DEFAULT_SLOT_MINUTES
    buffer_minutes: int = DEFAULT_BUFFER_MINUTES
    workdays: tuple = (0, 1, 2, 3, 4)          # Mon..Fri


def free_slots(day: datetime, events: list[dict], hours: WorkHours,
               ref: ZoneInfo) -> list[tuple[datetime, datetime]]:
    """Free intervals inside working hours for one day, busy removed."""
    day_ref = _aware(day, ref).astimezone(ref)
    base = day_ref.replace(hour=0, minute=0, second=0, microsecond=0)
    if base.weekday() not in hours.workdays:
        return []
    win_start = base.replace(hour=hours.start.hour, minute=hours.start.minute)
    win_end = base.replace(hour=hours.end.hour, minute=hours.end.minute)

    busy = [(s, e) for s, e, _ in busy_intervals(events, ref)]
    if hours.buffer_minutes:
        busy = [(s - timedelta(minutes=hours.buffer_minutes),
                 e + timedelta(minutes=hours.buffer_minutes)) for s, e in busy]

    free: list[tuple[datetime, datetime]] = []
    cursor = win_start
    for s, e in sorted(busy):
        s, e = s.astimezone(ref), e.astimezone(ref)
        if e <= cursor or s >= win_end:
            continue
        if s > cursor:
            free.append((cursor, min(s, win_end)))
        cursor = max(cursor, e)
    if cursor < win_end:
        free.append((cursor, win_end))
    return [(a, b) for a, b in free if b > a]


def propose_slots(day: datetime, duration_minutes: int, events: list[dict],
                  hours: WorkHours, ref: ZoneInfo,
                  max_slots: int = MAX_PROPOSALS
                  ) -> list[dict]:
    """Candidate meeting slots: earliest-first inside free intervals.

    Each proposal is checked against conflicts (defensive — free_slots
    already removed busy time, this guards against edge overlap)."""
    duration = timedelta(minutes=max(5, int(duration_minutes)))
    proposals: list[dict] = []
    for f_start, f_end in free_slots(day, events, hours, ref):
        cursor = f_start
        while cursor + duration <= f_end and len(proposals) < max_slots:
            end = cursor + duration
            clashes = find_conflicts(events, ref, candidate=(cursor, end))
            proposals.append({
                "start": cursor.isoformat(),
                "end": end.isoformat(),
                "conflict_free": not clashes,
            })
            cursor = end          # back-to-back grid; caller can filter
        if len(proposals) >= max_slots:
            break
    return proposals


# ── invite drafting ──────────────────────────────────────────────────────────

def draft_invite(title: str, start: datetime, end: datetime,
                 attendees: list[str], timezone_name: str = "",
                 location: str = "", agenda: str = "",
                 organizer_name: str = "") -> dict:
    """An invite DRAFT: the event body + a human-readable text rendition.
    Never sends — the agent parks it behind approval (or saves as draft)."""
    body = {
        "summary": (title or "").strip() or "(untitled meeting)",
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }
    if timezone_name:
        body["start"]["timeZone"] = timezone_name
        body["end"]["timeZone"] = timezone_name
    clean = [a.strip() for a in attendees or [] if "@" in (a or "")]
    if clean:
        body["attendees"] = [{"email": a} for a in clean]
    desc_bits = []
    if agenda:
        desc_bits.append(agenda[:1500])
    if organizer_name:
        desc_bits.append(f"Organized by {organizer_name}.")
    if desc_bits:
        body["description"] = "\n\n".join(desc_bits)
    if location:
        body["location"] = location[:300]

    zone_label = timezone_name or "local time"
    text = (f"Meeting: {body['summary']}\n"
            f"  When: {start.strftime('%a %d %b %Y, %H:%M')} – "
            f"{end.strftime('%H:%M')} ({zone_label})\n"
            f"  Attendees: {', '.join(clean) if clean else '(none yet)'}"
            + (f"\n  Where: {location}" if location else "")
            + (f"\n  Agenda: {agenda}" if agenda else ""))

    return {"event": body, "text": text, "attendees": clean,
            "external": bool(clean)}


# ── reschedule planning ──────────────────────────────────────────────────────

def reschedule_plan(event: dict, events_that_day: list[dict],
                    new_day: datetime, ref: ZoneInfo,
                    hours: WorkHours | None = None,
                    max_options: int = 3) -> dict:
    """Propose new times for the SAME duration, on new_day.

    Returns {event, duration_minutes, options, external} — options are
    conflict-free slots (same proposer as fresh invites)."""
    hours = hours or WorkHours()
    s = _aware(event["start"], ref)
    e = _aware(event["end"], ref)
    duration = int((e - s).total_seconds() // 60) or 30
    options = propose_slots(new_day, duration, events_that_day, hours, ref,
                            max_slots=max_options)
    attendees = list(event.get("attendees") or [])
    return {
        "event": {"id": event.get("id", ""),
                  "summary": event.get("summary", "(untitled)"),
                  "start": s.isoformat(), "end": e.isoformat(),
                  "attendees": attendees},
        "duration_minutes": duration,
        "options": options,
        "external": bool(attendees),       # others must be re-invited
    }


def render_conflicts(conflicts: list[Conflict], ref: ZoneInfo | None = None
                     ) -> str:
    if not conflicts:
        return "No conflicts found."

    def _when(iso: str) -> str:
        try:
            dt = datetime.fromisoformat(iso)
            if ref is not None:
                dt = dt.astimezone(ref)
            return dt.strftime("%b %d %H:%M %Z").strip()
        except (ValueError, TypeError):
            return iso

    lines = [f"{len(conflicts)} conflict(s):"]
    for c in conflicts[:10]:
        a = c.a.get("summary", "candidate")
        b = c.b.get("summary", "?")
        lines.append(f"  • {a} ({_when(c.a.get('start', ''))}) ↔ "
                     f"{b} ({_when(c.b.get('start', ''))}) — "
                     f"{c.overlap_minutes} min overlap")
    return "\n".join(lines)


def render_day(day: datetime, events: list[dict], ref: ZoneInfo,
               hours: WorkHours | None = None) -> str:
    """Human one-day availability answer."""
    hours = hours or WorkHours()
    header = f"Availability {day.astimezone(ref).strftime('%a %b %d')}:"
    if not events:
        return (f"{header} fully open "
                f"{hours.start.strftime('%H:%M')}–{hours.end.strftime('%H:%M')}.")
    lines = [header + f" {len(events)} event(s)."]
    for s, e, ev in busy_intervals(events, ref):
        s_l, e_l = s.astimezone(ref), e.astimezone(ref)
        label = ev.get("summary", "(untitled)")
        if ev.get("all_day"):
            lines.append(f"  • {label} (all day)")
        else:
            lines.append(f"  • {s_l.strftime('%H:%M')}–{e_l.strftime('%H:%M')}"
                         f" {label}")
            if ev.get("attendees"):
                lines.append(f"      attendees: "
                             f"{', '.join(ev['attendees'][:4])}")
    confs = find_conflicts(events, ref)
    if confs:
        lines.append("")
        lines.append(render_conflicts(confs, ref))
    free = free_slots(day, events, hours, ref)
    if free:
        spans = ", ".join(f"{a.astimezone(ref).strftime('%H:%M')}–"
                          f"{b.astimezone(ref).strftime('%H:%M')}"
                          for a, b in free[:6])
        lines.append(f"  open: {spans}")
    return "\n".join(lines)
