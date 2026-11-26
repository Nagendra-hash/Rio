"""
orchestrator/agents/productivity/calendar_agent.py — the Calendar Agent
(Phase 12).

A BaseAgent on the standard contract. Reads availability, detects
conflicts, handles timezones, proposes meeting slots, drafts invites, and
runs reschedule workflows — all through the shared google_tools layer
(injectable service; production uses the EXISTING calendar.events token
from actions/_google_auth — no new scopes).

Approval policy (require-approval-before-send, in code):
    • creating an event WITH external attendees (a real invite email)
      and any reschedule that notifies others → NEEDS_APPROVAL, always.
    • trusted automation: JARVIS_CALENDAR_TRUSTED=on lets the agent create
      events on the user's OWN calendar WITHOUT attendees immediately —
      nothing is ever sent to another human without approval, trusted or
      not. Trusted mode must be enabled explicitly per deployment; the
      agent never flips it on itself.
    • deleting events is out of scope for this agent (permanent delete is
      BLOCKED territory); the calendar_control skill keeps its own HUD
      confirmation gate for that.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from orchestrator.agents.productivity import calendar_logic, security
from orchestrator.agents.productivity.checkpoint import \
    ProductivityCheckpoint
from orchestrator.agents.productivity.google_tools import (ToolError,
                                                           google_available)
from orchestrator.agents.productivity.store import ProductivityStore
from orchestrator.base import BaseAgent
from orchestrator.contracts import (AgentRequest, AgentResult, TaskStatus,
                                    TaskType)

log = logging.getLogger("jarvis.calendar")


class CalendarAgent(BaseAgent):
    name = "calendar_agent"
    description = ("Calendar specialist: availability, conflict detection, "
                   "timezone handling, meeting-slot proposals, invite "
                   "drafts, reschedule workflows. External invites and "
                   "reschedules require approval; trusted automation only "
                   "covers your own calendar.")
    task_types = (TaskType.CALENDAR,)

    def __init__(self, store: ProductivityStore | None = None,
                 checkpoint: ProductivityCheckpoint | None = None,
                 service_factory=None, trusted: bool | None = None) -> None:
        self.store = store or ProductivityStore()
        self.checkpoint = checkpoint or ProductivityCheckpoint()
        self._service_factory = service_factory or self._default_service
        if trusted is None:
            import os
            trusted = os.environ.get("JARVIS_CALENDAR_TRUSTED",
                                     "").strip().lower() == "on"
        self.trusted = bool(trusted)

    # ── service plumbing ──────────────────────────────────────────────────
    @staticmethod
    def _default_service():
        from actions import _google_auth as ga
        return ga.get_service("calendar", "v3", [ga.CALENDAR_SCOPE],
                              "calendar_token")

    def _svc(self):
        return self._service_factory()

    # ── entry point ───────────────────────────────────────────────────────
    def handle(self, request: AgentRequest) -> AgentResult:
        meta = request.metadata or {}
        try:
            if meta.get("approved") and meta.get("resume"):
                return self._resume_approved(request, str(meta["resume"]),
                                             str(meta.get("by") or "user"))
            action = str(meta.get("calendar_action") or "").strip().lower() \
                or self._intent(request.text or "")
            handler = self._HANDLERS.get(action)
            if handler is None:
                return self._menu(request)
            return handler(self, request)
        except ToolError as exc:
            return self._reply(request, f"Calendar tool error: {exc}",
                               action="tool_error")
        except Exception as exc:                  # never raise outward
            log.warning("calendar agent error: %s", type(exc).__name__)
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.FAILED,
                               error="calendar action failed", agent=self.name)

    # ── intent detection ──────────────────────────────────────────────────
    @staticmethod
    def _intent(text: str) -> str:
        t = (text or "").lower()
        if "reschedule" in t or ("move" in t and "meeting" in t):
            return "reschedule"
        if "confirm" in t and ("option" in t or "plan" in t or "slot" in t
                               or "reschedul" in t or t.strip()
                                       .startswith("confirm")):
            return "confirm_reschedule"
        if "conflict" in t or "double-book" in t or "double book" in t:
            return "conflicts"
        if ("propose" in t or "slots" in t or "suggest" in t
                or ("find" in t and "time" in t)):
            return "propose"
        if ("invite" in t or "draft" in t or "schedule" in t or "set up" in t
                or "create" in t or "book" in t or "block" in t
                or "meeting" in t):
            return "invite"
        if ("availability" in t or "available" in t or "free" in t
                or "busy" in t or "schedule" in t or "my day" in t
                or "calendar" in t):
            return "availability"
        return ""

    _HANDLERS: dict = {}

    # ── result helpers ────────────────────────────────────────────────────
    def _reply(self, request: AgentRequest, text: str, *, action: str,
               status: TaskStatus = TaskStatus.COMPLETED,
               extra: dict | None = None) -> AgentResult:
        return AgentResult(
            task_id=request.task_id, status=status,
            text=security.redact(text),
            data={"productivity": True, "domain": "calendar",
                  "action": action, **(extra or {})},
            agent=self.name)

    def _menu(self, request: AgentRequest) -> AgentResult:
        return self._reply(request, (
            "I can help with your calendar:\n"
            "  • availability — what my day/week looks like\n"
            "  • conflicts — overlapping meetings\n"
            "  • propose slots for <duration> on <date> [in <tz>]\n"
            "  • draft invite: title / when / attendees [agenda]\n"
            "  • reschedule <event_id> to <date>\n"
            "Invites that include other people always need your approval "
            "before anything is sent."
        ), action="menu", status=TaskStatus.NEEDS_INPUT, extra={"menu": True})

    # ── google-backed helpers ─────────────────────────────────────────────
    def _connected(self, request: AgentRequest) -> AgentResult | None:
        if not google_available():
            return self._reply(request, (
                "The Google API libraries aren't installed. Run: "
                "pip install google-api-python-client google-auth-oauthlib"),
                action="not_installed")
        try:
            self._svc()
        except Exception as exc:
            msg = str(exc)
            if "setup" in msg.lower() or "consent" in msg.lower():
                return self._reply(request,
                                   "Google Calendar needs one-time setup: "
                                   "save client_secret.json in the config "
                                   "folder, then ask me again to open "
                                   "consent.", action="setup_needed")
            return self._reply(request,
                               f"Could not connect to Google Calendar: "
                               f"{type(exc).__name__}.",
                               action="connect_failed")
        return None

    def _fetch_window(self, svc, start: datetime, days: int) -> list[dict]:
        from orchestrator.agents.productivity import google_tools as gt
        raw = gt.list_events(svc, start, start + timedelta(days=days))
        return gt.event_window(raw)

    @staticmethod
    def _ref_zone(meta: dict):
        from orchestrator.agents.productivity import calendar_logic as cl
        zone, err = cl.resolve_zone(str(meta.get("timezone") or ""))
        if zone is None:
            return None, err
        return zone, ""

    @staticmethod
    def _parse_day(text: str, meta: dict) -> tuple[datetime | None, str]:
        """metadata.day (ISO) wins; else scan the text for a date."""
        iso = str(meta.get("day") or "").strip()
        if iso:
            dt = gt_parse(iso)
            if dt:
                return dt.replace(hour=9, minute=0), ""
            return None, f"I couldn't read the date '{iso}'."
        import re
        m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text or "")
        if m:
            dt = gt_parse(m.group(1))
            if dt:
                return dt.replace(hour=9, minute=0), ""
        return None, ""

    # ── actions ───────────────────────────────────────────────────────────
    def _availability(self, request: AgentRequest) -> AgentResult:
        blocked = self._connected(request)
        if blocked:
            return blocked
        meta = request.metadata or {}
        ref, err = self._ref_zone(meta)
        if ref is None:
            return self._reply(request, err, action="availability")
        day, derr = self._parse_day(request.text or "", meta)
        if day is None and derr:
            return self._reply(request, derr, action="availability")
        if day is None:
            day = datetime.now(ref).replace(hour=9, minute=0)
        days = int(meta.get("days") or 1)
        svc = self._svc()
        events = self._fetch_window(svc, day.replace(hour=0, minute=0), days)
        self.store.audit("availability_read", days=days)
        if days > 1:
            chunks = []
            for i in range(days):
                d = day + timedelta(days=i)
                day_events = [e for e in events
                              if e["start"].astimezone(ref).date()
                              == d.astimezone(ref).date()]
                chunks.append(calendar_logic.render_day(d, day_events, ref))
            return self._reply(request, "\n\n".join(chunks),
                               action="availability", extra={"days": days})
        return self._reply(request,
                           calendar_logic.render_day(day, events, ref),
                           action="availability")

    def _conflicts(self, request: AgentRequest) -> AgentResult:
        blocked = self._connected(request)
        if blocked:
            return blocked
        meta = request.metadata or {}
        ref, err = self._ref_zone(meta)
        if ref is None:
            return self._reply(request, err, action="conflicts")
        day, _ = self._parse_day(request.text or "", meta)
        if day is None:
            day = datetime.now(ref).replace(hour=0, minute=0)
        days = int(meta.get("days") or 7)
        svc = self._svc()
        events = self._fetch_window(svc, day, days)
        confs = calendar_logic.find_conflicts(events, ref)
        self.store.audit("conflict_scan", window_days=days,
                         conflicts=len(confs))
        return self._reply(request,
                           calendar_logic.render_conflicts(confs, ref)
                           + f"\n(window: {days} day(s))",
                           action="conflicts",
                           extra={"conflicts": len(confs)})

    def _propose(self, request: AgentRequest) -> AgentResult:
        blocked = self._connected(request)
        if blocked:
            return blocked
        meta = request.metadata or {}
        ref, err = self._ref_zone(meta)
        if ref is None:
            return self._reply(request, err, action="propose")
        day, derr = self._parse_day(request.text or "", meta)
        if day is None:
            if derr:
                return self._reply(request, derr, action="propose")
            return self._reply(request,
                               "Which day should I find slots on? "
                               "(YYYY-MM-DD)", action="propose",
                               status=TaskStatus.NEEDS_INPUT)
        duration = int(meta.get("duration_minutes") or 30)
        from orchestrator.agents.productivity import calendar_logic as cl
        hours = cl.WorkHours(
            slot_minutes=duration,
            workdays=tuple(meta.get("workdays") or (0, 1, 2, 3, 4)))
        svc = self._svc()
        events = self._fetch_window(svc, day.replace(hour=0, minute=0), 1)
        slots = cl.propose_slots(day, duration, events, hours, ref)
        self.store.audit("slots_proposed", duration=duration,
                         count=len(slots))
        if not slots:
            return self._reply(request,
                               f"No free {duration}-minute slots inside "
                               f"working hours that day.",
                               action="propose")
        lines = [f"Proposed {duration}-minute slots on "
                 f"{day.astimezone(ref).strftime('%a %b %d')}:"]
        for i, s in enumerate(slots, 1):
            st = datetime.fromisoformat(s["start"]).astimezone(ref)
            en = datetime.fromisoformat(s["end"]).astimezone(ref)
            lines.append(f"  {i}. {st.strftime('%H:%M')}–{en.strftime('%H:%M')}"
                         + ("" if s["conflict_free"] else " ⚠️"))
        lines.append("\nSay 'draft invite' with one of these and the "
                     "attendees — I'll draft it and park it behind your "
                     "approval before anything is sent.")
        return self._reply(request, "\n".join(lines), action="propose",
                           extra={"slots": slots})

    def _invite(self, request: AgentRequest) -> AgentResult:
        blocked = self._connected(request)
        if blocked:
            return blocked
        meta = request.metadata or {}
        ref, err = self._ref_zone(meta)
        if ref is None:
            return self._reply(request, err, action="invite")
        title = str(meta.get("title") or "").strip()
        start_iso = str(meta.get("start") or "").strip()
        end_iso = str(meta.get("end") or "").strip()
        attendees = list(meta.get("attendees") or [])
        if not title or not start_iso:
            return self._reply(request, (
                "I need a title and a start time to draft an invite — e.g. "
                "metadata {title: 'Design sync', start: '2026-10-08 14:00', "
                "duration_minutes: 30, attendees: ['a@x.com']}"),
                action="invite", status=TaskStatus.NEEDS_INPUT)
        start = gt_parse(start_iso)
        if start is None:
            return self._reply(request,
                               f"I couldn't read the start time "
                               f"'{start_iso}' (use YYYY-MM-DD HH:MM).",
                               action="invite")
        if start.tzinfo is None:
            start = start.replace(tzinfo=ref)
        duration = int(meta.get("duration_minutes") or 30)
        end = gt_parse(end_iso) if end_iso else None
        if end is None:
            end = start + timedelta(minutes=duration)
        if end.tzinfo is None:
            end = end.replace(tzinfo=ref)
        if end <= start:
            return self._reply(request, "The end time has to be after the "
                                        "start time.", action="invite")
        draft = calendar_logic.draft_invite(
            title, start, end, attendees,
            timezone_name=str(meta.get("timezone") or ""),
            location=str(meta.get("location") or ""),
            agenda=str(meta.get("agenda") or ""),
            organizer_name=str(meta.get("user_name") or ""))
        saved = self.store.save_draft("calendar_invite", draft,
                                      task_id=request.task_id)
        self._save_checkpoint()

        # ── the approval policy ───────────────────────────────────────────
        if draft["external"]:
            # Inviting other people → ALWAYS parks, trusted or not.
            cp = {"task_id": request.task_id, "agent": self.name,
                  "action": "create_event", "payload": draft["event"],
                  "status": "awaiting_approval"}
            self.checkpoint.save_for(request.task_id, cp)
            detail = (f"create event “{title}” "
                      f"{start.strftime('%b %d %H:%M')} and email the "
                      f"invite to {', '.join(draft['attendees'])}")
            return security.approval_result(request, self.name,
                                            "create_event", detail)
        # Own-calendar only → immediate if trusted automation is enabled.
        if self.trusted:
            svc = self._svc()
            from orchestrator.agents.productivity import google_tools as gt
            created = gt.insert_event(svc, draft["event"])
            self.store.record_sent(request.task_id, self.name,
                                   "create_event", "trusted_automation",
                                   ref=saved["id"])
            return self._reply(request, (
                f"Event created on your calendar (trusted automation): "
                f"{created.get('summary', title)} — "
                f"{start.strftime('%b %d %H:%M')}. No attendees were "
                f"notified."),
                action="create_event", extra={"created": True})
        cp = {"task_id": request.task_id, "agent": self.name,
              "action": "create_event", "payload": draft["event"],
              "status": "awaiting_approval"}
        self.checkpoint.save_for(request.task_id, cp)
        detail = (f"create event “{title}” "
                  f"{start.strftime('%b %d %H:%M')} on your calendar "
                  f"(no attendees)")
        return security.approval_result(request, self.name, "create_event",
                                        detail)

    def _reschedule(self, request: AgentRequest) -> AgentResult:
        blocked = self._connected(request)
        if blocked:
            return blocked
        meta = request.metadata or {}
        ref, err = self._ref_zone(meta)
        if ref is None:
            return self._reply(request, err, action="reschedule")
        event_id = str(meta.get("event_id") or "").strip()
        new_day, derr = self._parse_day(request.text or "", meta)
        if not event_id or new_day is None:
            return self._reply(request, (
                "Which event should I move, and to which day? I need "
                "metadata event_id and day (YYYY-MM-DD)."),
                action="reschedule", status=TaskStatus.NEEDS_INPUT)
        svc = self._svc()
        from orchestrator.agents.productivity import google_tools as gt
        raw = gt.get_event(svc, event_id)
        events = gt.event_window([raw])
        if not events:
            return self._reply(request, "I couldn't read that event.",
                               action="reschedule")
        target_events = self._fetch_window(
            svc, new_day.replace(hour=0, minute=0), 1)
        plan = calendar_logic.reschedule_plan(
            events[0], target_events, new_day, ref)
        if not plan["options"]:
            return self._reply(request,
                               f"No free slots that day for a "
                               f"{plan['duration_minutes']}-minute meeting.",
                               action="reschedule")
        saved = self.store.save_draft("reschedule", plan,
                                      task_id=request.task_id)
        lines = [f"Reschedule plan for “{plan['event']['summary']}” "
                 f"(saved draft {saved['id']}) — options on "
                 f"{new_day.astimezone(ref).strftime('%a %b %d')}:",
                 ]
        for i, o in enumerate(plan["options"], 1):
            st = datetime.fromisoformat(o["start"]).astimezone(ref)
            en = datetime.fromisoformat(o["end"]).astimezone(ref)
            lines.append(f"  {i}. {st.strftime('%H:%M')}–{en.strftime('%H:%M')}"
                         + ("" if o["conflict_free"] else " ⚠️"))
        return self._reply(request, "\n".join(lines), action="reschedule",
                           extra={"plan_id": saved["id"],
                                  "options": plan["options"]})

    def _confirm_reschedule(self, request: AgentRequest) -> AgentResult:
        """Apply a reschedule option: external attendees → approval gate."""
        blocked = self._connected(request)
        if blocked:
            return blocked
        meta = request.metadata or {}
        plan_id = str(meta.get("plan_id") or "")
        option = int(meta.get("option") or 0)
        plans = [d for d in self.store.drafts(kind="reschedule", limit=10)
                 if d.get("id") == plan_id]
        if not plans:
            return self._reply(request, "I don't have that reschedule plan.",
                               action="confirm_reschedule",
                               status=TaskStatus.NEEDS_INPUT)
        plan = plans[-1]["payload"]
        if not 1 <= option <= len(plan["options"]):
            return self._reply(request, "Which option number?",
                               action="confirm_reschedule",
                               status=TaskStatus.NEEDS_INPUT)
        chosen = plan["options"][option - 1]
        start = datetime.fromisoformat(chosen["start"])
        end = datetime.fromisoformat(chosen["end"])
        event_id = plan["event"].get("id", "")
        attendees = list(plan["event"].get("attendees") or [])
        body = {
            "summary": plan["event"].get("summary", "(untitled)"),
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
        }
        payload = {"event_id": event_id, "body": body,
                   "external": bool(attendees), "attendees": attendees}
        cp = {"task_id": request.task_id, "agent": self.name,
              "action": "move_event", "payload": payload,
              "status": "awaiting_approval"}
        self.checkpoint.save_for(request.task_id, cp)
        detail = (f"move “{body['summary']}” to "
                  f"{start.strftime('%b %d %H:%M')}"
                  + (f" and re-invite {', '.join(attendees)}"
                     if attendees else ""))
        return security.approval_result(request, self.name, "move_event",
                                        detail)

    def _resume_approved(self, request: AgentRequest, resume_id: str,
                         by: str) -> AgentResult:
        """The approval path. Applies the EXACT saved payload."""
        cp = self.checkpoint.load_for(resume_id)
        if not cp or cp.get("task_id") != resume_id:
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.FAILED,
                               text=f"No pending approval checkpoint for "
                                    f"{resume_id!r} — nothing was changed.",
                               agent=self.name)
        if cp.get("status") != "awaiting_approval":
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.DECLINED,
                               text=f"Approval {resume_id!r} is "
                                    f"'{cp.get('status')}' — already "
                                    f"handled.",
                               agent=self.name)
        action = cp.get("action")
        payload = cp.get("payload") or {}
        svc = self._svc()
        from orchestrator.agents.productivity import google_tools as gt
        if action == "create_event":
            created = gt.insert_event(svc, payload)
            self.store.record_sent(resume_id, self.name, "create_event", by)
            cp["status"] = "created"
            self.checkpoint.save_for(resume_id, cp)
            return self._reply(request,
                               f"Event created (approved by {by}): "
                               f"{created.get('summary', '')} — invites "
                               f"were sent to the attendees by Google.",
                               action="create_event",
                               extra={"created": True,
                                      "event_id": created.get("id", "")})
        if action == "move_event":
            updated = gt.update_event(svc, payload.get("event_id", ""),
                                      payload.get("body") or {})
            self.store.record_sent(resume_id, self.name, "move_event", by,
                                   ref=payload.get("event_id", ""))
            cp["status"] = "moved"
            self.checkpoint.save_for(resume_id, cp)
            new_start = (payload.get("body") or {}).get("start", {}) or {}
            when = new_start.get("dateTime", "") if isinstance(new_start, dict) \
                else str(new_start)
            return self._reply(request,
                               f"Event moved (approved by {by}): "
                               f"{updated.get('summary', '')} → {when}.",
                               action="move_event",
                               extra={"moved": True})
        return AgentResult(task_id=request.task_id, status=TaskStatus.FAILED,
                           text=f"Unknown approved action {action!r}.",
                           agent=self.name)

    # ── helpers ───────────────────────────────────────────────────────────
    def _save_checkpoint(self) -> None:
        try:
            self.checkpoint.save({"saved_at": self.store.summary(),
                                  "drafts": self.store.drafts(limit=5)})
        except Exception:
            pass

    _HANDLERS = {
        "availability": _availability,
        "conflicts": _conflicts,
        "propose": _propose,
        "invite": _invite,
        "reschedule": _reschedule,
        "confirm_reschedule": _confirm_reschedule,
    }


def gt_parse(value: str) -> datetime | None:
    from orchestrator.agents.productivity import google_tools as gt
    return gt.parse_when(value)
