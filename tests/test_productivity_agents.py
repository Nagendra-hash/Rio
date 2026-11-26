"""
tests/test_productivity_agents.py — offline acceptance tests for the Phase 12
Email + Calendar agents.

NO network, NO LLM, NO real Google tokens: JARVIS_HOME points at a fresh
temp dir, stores/checkpoints are built on explicit temp directories, and the
Google service is a fake. Token material is injected into tool payloads to
prove the redaction boundary holds.

What this locks in:
  1. SECURITY    — redact() kills tokens/bearer/JWT/api-keys in replies AND
                   store writes; BLOCKED ≠ approval; permission levels.
  2. EMAIL LOGIC — classification, newsletter detection, urgency,
                   extractive summaries, reply drafts.
  3. CALENDAR    — busy intervals, conflicts, timezone conversion, free
                   slots, proposals, invite drafts, reschedule plans.
  4. STORE       — 0600 files in a 0700 dir, redaction on write,
                   actions-only audit, path containment, caps, wipe.
  5. CHECKPOINT  — snapshots, pruning, perms, never raises.
  6. EMAIL AGENT — full flows with a fake Gmail service: triage, read,
                   summary, newsletter digest, drafts, approval park →
                   resume → send, trash approval, never raises on garbage.
  7. CALENDAR AGENT — availability, conflicts, propose, invite (external →
                   approval ALWAYS; internal → approval unless trusted),
                   reschedule + confirm, never raises on garbage.
  8. WIRING      — classifier lanes + explicit routes, other lanes
                   unchanged, registry stubs off / real agents on behind
                   JARVIS_EMAIL_AGENT / JARVIS_CALENDAR_AGENT.

Run:  .venv/bin/python -m pytest tests/test_productivity_agents.py -q
"""
import itertools
import json
import os
import stat
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# ── isolation: this module gets a fresh JARVIS_HOME ──────────────────────────
_TMP = tempfile.mkdtemp(prefix="jarvis-prod-")
os.environ["JARVIS_HOME"] = _TMP

from orchestrator.agents.productivity import calendar_logic, email_logic  # noqa: E402
from orchestrator.agents.productivity import security                     # noqa: E402
from orchestrator.agents.productivity.checkpoint import (                 # noqa: E402
    ProductivityCheckpoint)
from orchestrator.agents.productivity.email_agent import EmailAgent       # noqa: E402
from orchestrator.agents.productivity.calendar_agent import CalendarAgent  # noqa: E402
from orchestrator.agents.productivity.store import ProductivityStore      # noqa: E402
from orchestrator.classifier import IntentClassifier                      # noqa: E402
from orchestrator.contracts import AgentRequest, TaskStatus, TaskType     # noqa: E402
from orchestrator.agents import build_default_registry                    # noqa: E402

_SEQ = itertools.count(1)
REF = ZoneInfo("Asia/Kolkata")


def _req(text: str = "", **meta) -> AgentRequest:
    return AgentRequest(task_id=f"prod-test-{next(_SEQ)}",
                        task_type=TaskType.EMAIL, text=text, metadata=meta)


@pytest.fixture()
def tmp():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture()
def store(tmp):
    return ProductivityStore(tmp / "prod")


@pytest.fixture()
def ckpt(tmp):
    return ProductivityCheckpoint(tmp / "ckpt")


# ════════════════════════════════════════════════════════════ 1. security ═══

class TestSecurity:
    def test_redact_access_token_kv(self):
        blob = 'access_token: ya29.a0AfB_byAbcdef1234567890'
        out = security.redact(blob)
        assert "ya29." not in out
        assert "[REDACTED]" in out

    def test_redact_refresh_token_json(self):
        blob = '{"refresh_token": "1//0gAbcdefGHijklmnop", "scope": "x"}'
        out = security.redact(blob)
        assert "1//0g" not in out
        assert "scope" in out          # non-secret key survives

    def test_redact_bearer_and_jwt(self):
        out = security.redact(
            "Authorization: Bearer abc123def456\n"
            "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.SflKxwRJSMeKKF2QT4f")
        assert "abc123def456" not in out
        assert "eyJ" not in out

    def test_redact_client_secret_and_api_key(self):
        out = security.redact(
            'client_secret: GOCSPX-abcdef1234567890\n'
            'api_key = AIzaSyA1234567890abcdefghijklmnopqrstuvw')
        assert "GOCSPX" not in out
        assert "AIzaSyA" not in out

    def test_redact_never_touches_normal_prose(self):
        prose = ("Meeting on 2026-10-08 at 14:00 with the design team; "
                 "invoice #12345 is attached. Theme: productivity.")
        assert security.redact(prose) == prose

    def test_redact_obj_deep(self):
        obj = {"to": "a@b.com", "access_token": "ya29.supersecret",
               "nested": {"client_secret": "xyz123456789", "note": "keep"}}
        out = security.redact_obj(obj)
        assert out["to"] == "a@b.com"
        assert out["access_token"] == "[REDACTED]"
        assert out["nested"]["client_secret"] == "[REDACTED]"
        assert out["nested"]["note"] == "keep"

    def test_permission_levels(self):
        assert security.needs_approval(security.APPROVAL_REQUIRED)
        assert not security.needs_approval(security.READ_ONLY)
        assert not security.needs_approval(security.SAFE_ACTION)
        assert security.is_blocked(security.BLOCKED)
        assert not security.is_blocked(security.APPROVAL_REQUIRED)

    def test_approval_result_envelope(self, ):
        r = security.approval_result(_req("x"), "email_agent", "send_reply",
                                     "send email to a@b.com")
        assert r.status is TaskStatus.NEEDS_APPROVAL
        assert "approval_summary" in r.data
        assert "nothing has left your machine" in r.text


# ══════════════════════════════════════════════════ 2. email_logic ══════════

def _msg(**kw):
    base = {"id": "msg123456789", "from": "Alice <alice@example.com>",
            "subject": "Quick question about the project", "snippet": "",
            "body": "", "labels": [], "unsubscribe": ""}
    base.update(kw)
    return base


def _msg_from(sender=None, **kw):
    """_msg with a from_ kwarg ("from" is a Python keyword)."""
    if sender is not None:
        kw["from"] = sender
    return _msg(**kw)


class TestEmailLogic:
    def test_newsletter_by_header_and_sender(self):
        m = _msg_from("The Digest <news@bulk.example.com>",
                      subject="Weekly Digest #42",
                      unsubscribe="<https://x.example.com/unsub?id=1>",
                      body="You are receiving this email because you signed "
                           "up. Unsubscribe any time.")
        assert email_logic.is_newsletter(m)

    def test_plain_email_is_not_newsletter(self):
        assert not email_logic.is_newsletter(_msg())

    def test_transactional_receipt(self):
        m = _msg_from("Shop <noreply@shop.example.com>",
                      subject="Your order has shipped")
        assert email_logic.is_transactional(m)
        assert not email_logic.is_newsletter(m)

    def test_urgency_urgent_vocabulary(self):
        m = _msg(subject="URGENT: production outage",
                 body="We need this fixed immediately.")
        c = email_logic.classify_message(m)
        assert c["urgency"] == 3 and c["urgency_label"] == "urgent"

    def test_urgency_newsletter_is_routine(self):
        m = _msg_from("Digest <news@bulk.example.com>",
                      subject="Weekly Digest — 20% off inside!",
                      unsubscribe="<https://x/unsub>",
                      body="Unsubscribe. View in browser.")
        c = email_logic.classify_message(m)
        assert c["category"] == "newsletter"
        assert c["urgency"] == 0

    def test_urgency_priority_sender(self):
        m = _msg_from("Boss <boss@corp.example.com>",
                      subject="Meeting notes")
        c = email_logic.classify_message(m)
        assert c["urgency"] >= 2

    def test_summary_extracts_ask(self):
        m = _msg(body="Hi team. The draft looks good overall. "
                      "One thing: can you send the final numbers by "
                      "tomorrow? Thanks, Pat.")
        s = email_logic.summarize_message(m)
        assert "numbers" in s or "draft" in s

    def test_summary_empty_body_falls_back(self):
        m = _msg(body="", snippet="just a snippet here")
        assert email_logic.summarize_message(m) == "just a snippet here"

    def test_draft_reply_shape(self):
        d = email_logic.draft_reply(_msg(), user_name="J")
        assert d["to"] == "alice@example.com"
        assert d["subject"].startswith("Re:")
        assert "Hi Alice" in d["body"]
        assert "Best regards" in d["body"] and "J" in d["body"]

    def test_draft_reply_intents(self):
        for intent in ("acknowledge", "interested", "decline", "question",
                       "meeting", "thanks"):
            d = email_logic.draft_reply(_msg(), intent=intent)
            assert d["body"] and d["intent"] == intent

    def test_render_inbox_rows(self):
        rows = [
            {"idx": 1, "from": "a@x.com", "subject": "S1",
             "category": "personal", "urgency": 3, "urgency_label": "urgent",
             "reasons": ["urgent vocabulary"], "summary": "s"},
            {"idx": 2, "from": "b@x.com", "subject": "S2",
             "category": "newsletter", "urgency": 0,
             "urgency_label": "routine", "reasons": [], "summary": ""},
        ]
        out = email_logic.render_inbox(rows, "Inbox (inbox)")
        assert out.startswith("Inbox (inbox): 2 message(s)")
        assert "‼️" in out and "📰" in out


# ═══════════════════════════════════════════════════ 3. calendar_logic ══════

def _ev(summary, s, e, attendees=None, all_day=False, id_="ev1"):
    return {"id": id_, "summary": summary,
            "start": s, "end": e, "all_day": all_day,
            "attendees": attendees or []}


class TestCalendarLogic:
    def test_busy_intervals_sorted_and_aware(self):
        d = datetime(2026, 10, 8, 9, 0)
        events = [
            _ev("b", d.replace(hour=13), d.replace(hour=14)),
            _ev("a", d.replace(hour=9), d.replace(hour=10)),
        ]
        busy = calendar_logic.busy_intervals(events, REF)
        assert [b[2]["summary"] for b in busy] == ["a", "b"]

    def test_conflicts_detect_overlap(self):
        d = datetime(2026, 10, 8, 9, 0, tzinfo=REF)
        events = [
            _ev("one", d.replace(hour=10), d.replace(hour=11, minute=30),
                id_="e1"),
            _ev("two", d.replace(hour=11), d.replace(hour=12), id_="e2"),
        ]
        confs = calendar_logic.find_conflicts(events, REF)
        assert len(confs) == 1 and confs[0].overlap_minutes == 30

    def test_no_conflict_when_adjacent(self):
        d = datetime(2026, 10, 8, 9, 0, tzinfo=REF)
        events = [
            _ev("one", d.replace(hour=10), d.replace(hour=11), id_="e1"),
            _ev("two", d.replace(hour=11), d.replace(hour=12), id_="e2"),
        ]
        assert calendar_logic.find_conflicts(events, REF) == []

    def test_conflict_against_candidate_slot(self):
        d = datetime(2026, 10, 8, 9, 0, tzinfo=REF)
        events = [_ev("busy", d.replace(hour=10), d.replace(hour=11),
                      id_="e1")]
        cand = (d.replace(hour=10, minute=30), d.replace(hour=11, minute=30))
        confs = calendar_logic.find_conflicts(events, REF, candidate=cand)
        assert len(confs) == 1 and confs[0].overlap_minutes == 30

    def test_timezone_conversion_and_errors(self):
        d = datetime(2026, 10, 8, 9, 0, tzinfo=ZoneInfo("UTC"))
        ny, err = calendar_logic.to_zone(d, "America/New_York")
        assert err == "" and ny.hour == 5
        _, err = calendar_logic.to_zone(d, "Mars/Olympus")
        assert "Mars/Olympus" in err

    def test_cross_zone_conflict_detected(self):
        d = datetime(2026, 10, 8, 9, 0)
        one = _ev("ist", d.replace(hour=14, tzinfo=REF),
                  d.replace(hour=15, tzinfo=REF), id_="e1")
        # 09:00-10:00 UTC == 14:30-15:30 IST → 30 min overlap with e1
        two = _ev("utc", d.replace(hour=9, tzinfo=ZoneInfo("UTC")),
                  d.replace(hour=10, tzinfo=ZoneInfo("UTC")),
                  id_="e2")
        confs = calendar_logic.find_conflicts([one, two], REF)
        assert len(confs) == 1 and confs[0].overlap_minutes == 30

    def test_free_slots_respect_working_hours(self):
        d = datetime(2026, 10, 8, 9, 0, tzinfo=REF)      # a Thursday
        events = [_ev("lunch-ish", d.replace(hour=12), d.replace(hour=13),
                      id_="e1")]
        hours = calendar_logic.WorkHours()
        slots = calendar_logic.free_slots(d, events, hours, REF)
        assert slots[0][0].hour == 9 and slots[-1][1].hour == 17
        assert all(e <= d.replace(hour=12) or a >= d.replace(hour=13)
                   for a, e in slots)

    def test_free_slots_empty_on_weekend(self):
        d = datetime(2026, 10, 10, 9, 0, tzinfo=REF)     # a Saturday
        assert calendar_logic.free_slots(
            d, [], calendar_logic.WorkHours(), REF) == []

    def test_propose_slots_avoid_busy(self):
        d = datetime(2026, 10, 8, 9, 0, tzinfo=REF)
        events = [_ev("busy", d.replace(hour=10), d.replace(hour=11),
                      id_="e1")]
        slots = calendar_logic.propose_slots(d, 30, events,
                                             calendar_logic.WorkHours(), REF)
        starts = [datetime.fromisoformat(s["start"]).astimezone(REF)
                  for s in slots]
        assert starts[0].hour == 9
        assert all(not (10 <= s.hour < 11) for s in starts)
        assert all(s["conflict_free"] for s in slots)

    def test_propose_slots_caps_count(self):
        d = datetime(2026, 10, 8, 9, 0, tzinfo=REF)
        slots = calendar_logic.propose_slots(
            d, 30, [], calendar_logic.WorkHours(), REF, max_slots=3)
        assert len(slots) == 3

    def test_draft_invite_marks_external(self):
        d = datetime(2026, 10, 8, 14, 0, tzinfo=REF)
        draft = calendar_logic.draft_invite(
            "Design sync", d, d + timedelta(minutes=30),
            ["pat@example.com"], timezone_name="Asia/Kolkata",
            agenda="roadmap")
        assert draft["external"] is True
        assert draft["event"]["attendees"] == [{"email": "pat@example.com"}]
        assert "Design sync" in draft["text"]

    def test_draft_invite_internal_only(self):
        d = datetime(2026, 10, 8, 14, 0, tzinfo=REF)
        draft = calendar_logic.draft_invite("Focus block", d,
                                            d + timedelta(minutes=60), [])
        assert draft["external"] is False
        assert "attendees" not in draft["event"]

    def test_reschedule_plan_same_duration(self):
        d = datetime(2026, 10, 8, 9, 0, tzinfo=REF)
        ev = _ev("Sync", d.replace(hour=10), d.replace(hour=11, minute=15),
                 attendees=["a@x.com"], id_="e1")
        plan = calendar_logic.reschedule_plan(
            ev, [], datetime(2026, 10, 9, 9, 0, tzinfo=REF), REF)
        assert plan["duration_minutes"] == 75
        assert plan["external"] is True
        assert plan["options"] and plan["options"][0]["conflict_free"]


# ═══════════════════════════════════════════════════════════ 4. store ═══════

class TestStore:
    def test_permissions_owner_only(self, tmp):
        s = ProductivityStore(tmp / "p")
        s.save_draft("email_reply", {"subject": "x", "body": "y"})
        assert stat.S_IMODE(s.root.stat().st_mode) == 0o700
        for f in s.root.iterdir():
            assert stat.S_IMODE(f.stat().st_mode) == 0o600, f.name

    def test_drafts_roundtrip_and_filter(self, tmp):
        s = ProductivityStore(tmp / "p")
        s.save_draft("email_reply", {"to": "a@x.com"}, task_id="t1")
        s.save_draft("calendar_invite", {"summary": "x"}, task_id="t2")
        assert len(s.drafts()) == 2
        assert len(s.drafts(kind="email_reply")) == 1
        assert s.latest_draft("email_reply")["payload"]["to"] == "a@x.com"

    def test_draft_payloads_are_redacted_on_disk(self, tmp):
        s = ProductivityStore(tmp / "p")
        s.save_draft("email_reply", {"to": "a@x.com",
                                     "access_token": "ya29.tokengoeshere"})
        raw = (tmp / "p" / "drafts.jsonl").read_text()
        assert "ya29." not in raw and "[REDACTED]" in raw

    def test_audit_records_actions_only(self, tmp):
        s = ProductivityStore(tmp / "p")
        marker = "super-secret-subject-xyzzy"
        s.save_draft("email_reply", {"subject": marker, "body": marker})
        audit = (tmp / "p" / "audit.jsonl").read_text()
        assert "draft_email_reply" in audit
        assert marker not in audit

    def test_approval_and_sent_records(self, tmp):
        s = ProductivityStore(tmp / "p")
        s.record_approval("t1", "email_agent", "send_reply", True, "user")
        s.record_sent("t1", "email_agent", "send_reply", "user", ref="d1")
        assert s.approvals()[0]["approved"] is True
        assert s.sent_log()[0]["ref"] == "d1"

    def test_path_containment(self, tmp):
        s = ProductivityStore(tmp / "p")
        assert s._safe_name("../../etc/passwd") == "etcpasswd"
        assert s._safe_name("") == "unnamed"

    def test_wipe_keeps_audit(self, tmp):
        s = ProductivityStore(tmp / "p")
        s.save_draft("email_reply", {"x": 1})
        assert s.wipe()
        assert s.summary()["drafts"] == 0
        assert "wipe" in (tmp / "p" / "audit.jsonl").read_text()


# ════════════════════════════════════════════════════════ 5. checkpoint ═════

class TestCheckpoint:
    def test_save_load_prune_and_perms(self, tmp):
        cp = ProductivityCheckpoint(tmp / "ck")
        for i in range(25):
            assert cp.save({"i": i})
        assert len(cp.list_ids()) == 20
        assert cp.load_latest()["i"] == 24
        assert stat.S_IMODE(
            (tmp / "ck" / "latest.json").stat().st_mode) == 0o600
        assert stat.S_IMODE((tmp / "ck").stat().st_mode) == 0o700

    def test_checkpoint_redacts_token_payloads(self, tmp):
        cp = ProductivityCheckpoint(tmp / "ck")
        cp.save({"payload": {"access_token": "ya29.secrettoken123456"}})
        blob = (tmp / "ck" / "latest.json").read_text()
        assert "ya29." not in blob

    def test_never_raises_on_broken_dir(self, tmp):
        cp = ProductivityCheckpoint(tmp / "ck")
        blocked = tmp / "blocked"
        blocked.write_text("not a dir")
        cp.dir = blocked
        assert cp.save({"x": 1}) is None
        assert cp.load_latest() is None
        assert cp.list_ids() == []


# ═══════════════════════════════════════════════════ 6. fake gmail svc ══════

TOKEN_STASH = '{"access_token": "ya29.SHOULD_NEVER_APPEAR", "scope": "x"}'


class FakeGmail:
    """Minimal users().messages() surface used by the email agent."""

    SENT: list = []
    MODIFIED: list = []
    TRASHED: list = []

    def __init__(self, messages):
        self._messages = messages          # dict id → message payload

    def users(self):
        return _UsersApi(self)

    def list(self, **kw):
        ids = [{"id": mid} for mid in self._messages]
        return _Req({"messages": ids})

    def get(self, **kw):
        m = self._messages[kw["id"]]
        return _Req(m if kw.get("format") == "full" else _meta_view(m))

    def send(self, **kw):
        FakeGmail.SENT.append(kw)
        return _Req({"id": "sent-1"})

    def modify(self, **kw):
        FakeGmail.MODIFIED.append(kw)
        return _Req({"id": kw["id"]})

    def trash(self, **kw):
        FakeGmail.TRASHED.append(kw["id"])
        return _Req({"id": kw["id"]})


class _UsersApi:
    """users() → callable chain: users().messages().list()…"""

    def __init__(self, outer):
        self._outer = outer

    def messages(self):
        return self._outer


class _Req:
    def __init__(self, data):
        self._data = data

    def execute(self):
        return self._data


def _meta_view(full: dict) -> dict:
    return {"id": full["id"], "threadId": full.get("threadId", ""),
            "snippet": full.get("snippet", ""),
            "internalDate": full.get("internalDate", ""),
            "payload": {"headers": full["payload"]["headers"]}}


def _full_msg(mid, frm, subject, body, unsub="", when_ms=0):
    headers = [{"name": "From", "value": frm},
               {"name": "To", "value": "me@me.com"},
               {"name": "Subject", "value": subject},
               {"name": "Date", "value": "Thu, 1 Oct 2026 10:00:00 +0000"}]
    if unsub:
        headers.append({"name": "List-Unsubscribe", "value": unsub})
    import base64 as b64
    data = b64.urlsafe_b64encode(body.encode()).decode().rstrip("=")
    return {"id": mid, "threadId": mid, "snippet": body[:100],
            "internalDate": str(when_ms),
            "payload": {"headers": headers,
                        "mimeType": "text/plain",
                        "body": {"data": data}}}


MSGS = {
    "AAAA1111BBBB": _full_msg("AAAA1111BBBB", "Boss <boss@corp.example.com>",
                              "Budget review tomorrow", "Can you send the "
                              "final numbers by tomorrow? This is urgent."),
    "CCCC2222DDDD": _full_msg("CCCC2222DDDD", "Digest <news@bulk.example.com>",
                              "Weekly Digest #42",
                              "View in browser. Unsubscribe any time. "
                              "You are receiving this email because.",
                              unsub="<https://x.example.com/unsub>"),
    "EEEE3333FFFF": _full_msg("EEEE3333FFFF", "Shop <noreply@shop.example.com>",
                              "Your order has shipped", "Track it here."),
}


@pytest.fixture()
def email_agent(tmp, ckpt):
    FakeGmail.SENT, FakeGmail.MODIFIED, FakeGmail.TRASHED = [], [], []
    return EmailAgent(store=ProductivityStore(tmp / "ep"),
                      checkpoint=ckpt,
                      service_factory=lambda: FakeGmail(MSGS))


class TestEmailAgent:
    def test_triage_classifies_prioritizes_summarizes(self, email_agent):
        r = email_agent.handle(_req("triage my inbox"))
        assert r.status is TaskStatus.COMPLETED
        assert "urgent" in r.text.lower() or "❗" in r.text or "‼️" in r.text
        assert "Digest" in r.text or "📰" in r.text
        cats = {m["id"]: m["category"] for m in r.data["messages"]}
        assert cats["CCCC2222DDDD"] == "newsletter"
        assert cats["EEEE3333FFFF"] == "transactional"

    def test_triage_prioritizes_urgent_first(self, email_agent):
        r = email_agent.handle(_req("triage my inbox"))
        first = r.data["messages"][0]
        assert first["id"] == "AAAA1111BBBB"
        assert first["urgency"] == 3

    def test_read_full_message(self, email_agent):
        r = email_agent.handle(_req("read AAAA1111BBBB"))
        assert "Budget review" in r.text
        assert "personal" in r.text

    def test_summary(self, email_agent):
        r = email_agent.handle(_req("summarize AAAA1111BBBB"))
        assert "Summary of" in r.text

    def test_newsletter_digest(self, email_agent):
        r = email_agent.handle(_req("newsletter digest"))
        assert "1 newsletter(s)" in r.text
        assert "never" in r.text.lower()

    def test_draft_reply_saved_not_sent(self, email_agent):
        r = email_agent.handle(_req("draft reply to AAAA1111BBBB"))
        assert "NOT been sent" in r.text
        assert FakeGmail.SENT == []
        assert email_agent.store.drafts(kind="email_reply")

    def test_send_reply_parks_for_approval_nothing_sent(self, email_agent):
        email_agent.handle(_req("draft reply to AAAA1111BBBB"))
        r = email_agent.handle(_req("send the reply draft"))
        assert r.status is TaskStatus.NEEDS_APPROVAL
        assert "approval_summary" in r.data
        assert FakeGmail.SENT == []            # the hard rule

    def test_approval_resume_sends_exact_saved_draft(self, email_agent):
        email_agent.handle(_req("draft reply to AAAA1111BBBB"))
        park = email_agent.handle(_req("send the reply draft"))
        task_id = park.task_id
        # a SECOND draft with a different recipient — resume must send the
        # FIRST (the exact payload that was approved), never the latest.
        email_agent.handle(_req("draft reply to CCCC2222DDDD"))
        r = email_agent.handle(_req("x", approved=True, resume=task_id,
                                    by="user"))
        assert r.status is TaskStatus.COMPLETED
        assert len(FakeGmail.SENT) == 1
        sent_raw = FakeGmail.SENT[0]["body"]["raw"]
        import base64 as b64
        decoded = b64.urlsafe_b64decode(sent_raw + "=" * (-len(sent_raw) % 4))
        text = decoded.decode()
        assert "To: boss@corp.example.com" in text
        assert "Re: Budget review tomorrow" in text
        assert "Weekly Digest" not in text and "news@bulk" not in text

    def test_trash_needs_approval_then_executes(self, email_agent):
        r = email_agent.handle(_req("trash CCCC2222DDDD"))
        assert r.status is TaskStatus.NEEDS_APPROVAL
        assert FakeGmail.TRASHED == []
        r2 = email_agent.handle(_req("x", approved=True, resume=r.task_id))
        assert r2.status is TaskStatus.COMPLETED
        assert FakeGmail.TRASHED == ["CCCC2222DDDD"]

    def test_mark_read_is_read_only(self, email_agent):
        r = email_agent.handle(_req("mark AAAA1111BBBB read"))
        assert r.status is TaskStatus.COMPLETED
        assert FakeGmail.MODIFIED and FakeGmail.SENT == []

    def test_no_token_material_in_any_reply(self, email_agent):
        for text in ("triage my inbox", "read AAAA1111BBBB",
                     "newsletter digest"):
            r = email_agent.handle(_req(text))
            assert "ya29" not in r.text
            assert "SHOULD_NEVER_APPEAR" not in r.text

    def test_menu_on_unknown_and_never_raises(self, email_agent):
        r = email_agent.handle(_req("what's the weather"))
        assert r.status is TaskStatus.NEEDS_INPUT
        for text in ("", "   ", "!!!", "x" * 5000, "draft reply to"):
            assert email_agent.handle(_req(text)) is not None

    def test_send_without_draft_asks_first(self, email_agent):
        r = email_agent.handle(_req("send the reply draft"))
        assert r.status is TaskStatus.NEEDS_INPUT


# ═══════════════════════════════════════════════════ 7. fake calendar ══════

class FakeCalendar:
    """Minimal events() surface used by the calendar agent."""

    EVENTS: dict = {}                  # id → raw event (mutable class state)

    def __init__(self):
        pass

    class _Events:
        def list(self, **kw):
            return _Req({"items": list(FakeCalendar.EVENTS.values())})

        def get(self, **kw):
            return _Req(FakeCalendar.EVENTS[kw["eventId"]])

        def insert(self, **kw):
            FakeCalendar.INSERTED.append(kw["body"])
            return _Req({"id": "new-1", **kw["body"]})

        def update(self, **kw):
            FakeCalendar.UPDATED.append((kw["eventId"], kw["body"]))
            return _Req({"id": kw["eventId"], **kw["body"]})

    INSERTED: list = []
    UPDATED: list = []

    def events(self):
        return FakeCalendar._Events()


@pytest.fixture()
def cal_agent(tmp, ckpt):
    FakeCalendar.INSERTED, FakeCalendar.UPDATED = [], []
    FakeCalendar.EVENTS = {
        "ev1": {"id": "ev1", "summary": "Standup",
                "start": {"dateTime": "2026-10-08T10:00:00+05:30"},
                "end": {"dateTime": "2026-10-08T10:30:00+05:30"}},
        "ev2": {"id": "ev2", "summary": "Overlapping review",
                "start": {"dateTime": "2026-10-08T10:15:00+05:30"},
                "end": {"dateTime": "2026-10-08T11:15:00+05:30"}},
        "ev3": {"id": "ev3", "summary": "1:1 with Pat",
                "start": {"dateTime": "2026-10-08T14:00:00+05:30"},
                "end": {"dateTime": "2026-10-08T15:00:00+05:30"},
                "attendees": [{"email": "pat@example.com"}]},
    }
    return CalendarAgent(store=ProductivityStore(tmp / "cp"),
                         checkpoint=ckpt,
                         service_factory=lambda: FakeCalendar(),
                         trusted=False)


class TestCalendarAgent:
    def test_availability_renders_day_and_conflicts(self, cal_agent):
        r = cal_agent.handle(_req("my availability on 2026-10-08"))
        assert "Availability" in r.text
        assert "Standup" in r.text
        assert "1 conflict(s)" in r.text
        assert "open:" in r.text

    def test_conflicts_scan(self, cal_agent):
        r = cal_agent.handle(_req("check conflicts on 2026-10-08"))
        assert "1 conflict(s)" in r.text and "15 min" in r.text

    def test_conflicts_in_other_timezone(self, cal_agent):
        r = cal_agent.handle(_req("conflicts on 2026-10-08",
                                  timezone="America/New_York"))
        assert r.status is TaskStatus.COMPLETED
        # 10:00 IST == 00:30 EDT, 10:15 IST == 00:45 EDT — the renderer
        # converts both instants into the zone the user asked for.
        assert "00:30" in r.text and "00:45" in r.text
        assert "EDT" in r.text or "GMT-4" in r.text or "-04:00" in r.text

    def test_bad_timezone_is_honest(self, cal_agent):
        r = cal_agent.handle(_req("conflicts", timezone="Mars/Olympus"))
        assert "Mars/Olympus" in r.text

    def test_propose_slots_avoid_busy(self, cal_agent):
        r = cal_agent.handle(_req("propose slots on 2026-10-08",
                                  duration_minutes=30))
        assert "Proposed 30-minute slots" in r.text
        assert "09:00" in r.text or "11:15" in r.text

    def test_invite_with_attendees_always_needs_approval(self, cal_agent):
        r = cal_agent.handle(_req("set up a design sync", trusted=True,
                                  title="Design sync",
                                  start="2026-10-09 15:00",
                                  duration_minutes=30,
                                  attendees=["pat@example.com"]))
        assert r.status is TaskStatus.NEEDS_APPROVAL
        assert FakeCalendar.INSERTED == []      # nothing created, nothing sent

    def test_invite_without_attendees_needs_approval_standard_mode(
            self, cal_agent):
        r = cal_agent.handle(_req("block focus time", title="Focus",
                                  start="2026-10-09 15:00",
                                  duration_minutes=60))
        assert r.status is TaskStatus.NEEDS_APPROVAL
        assert FakeCalendar.INSERTED == []

    def test_trusted_mode_creates_internal_event_immediately(
            self, tmp, ckpt):
        agent = CalendarAgent(store=ProductivityStore(tmp / "ct"),
                              checkpoint=ckpt,
                              service_factory=lambda: FakeCalendar(),
                              trusted=True)
        r = agent.handle(_req("block focus time", title="Focus",
                              start="2026-10-09 15:00", duration_minutes=60))
        assert r.status is TaskStatus.COMPLETED
        assert len(FakeCalendar.INSERTED) == 1
        assert "attendees" not in FakeCalendar.INSERTED[0]
        # trusted mode STILL refuses external invites
        r2 = agent.handle(_req("invite pat", title="Sync",
                               start="2026-10-09 16:00",
                               attendees=["pat@example.com"]))
        assert r2.status is TaskStatus.NEEDS_APPROVAL
        assert len(FakeCalendar.INSERTED) == 1

    def test_invite_approval_resume_creates_event(self, cal_agent):
        park = cal_agent.handle(_req("set up sync", title="Sync",
                                     start="2026-10-09 15:00",
                                     attendees=["pat@example.com"]))
        r = cal_agent.handle(_req("x", approved=True, resume=park.task_id,
                                  by="user"))
        assert r.status is TaskStatus.COMPLETED
        assert len(FakeCalendar.INSERTED) == 1
        assert FakeCalendar.INSERTED[0]["attendees"] == \
            [{"email": "pat@example.com"}]

    def test_reschedule_flow_plan_then_confirm_then_approval(
            self, cal_agent):
        r = cal_agent.handle(_req("reschedule ev3 to 2026-10-09",
                                  event_id="ev3", day="2026-10-09"))
        assert "Reschedule plan" in r.text
        plan_id = r.data["plan_id"]
        r2 = cal_agent.handle(_req("confirm", plan_id=plan_id, option=1))
        assert r2.status is TaskStatus.NEEDS_APPROVAL   # external attendee
        assert FakeCalendar.UPDATED == []
        r3 = cal_agent.handle(_req("x", approved=True,
                                   resume=r2.task_id, by="user"))
        assert r3.status is TaskStatus.COMPLETED
        assert len(FakeCalendar.UPDATED) == 1
        moved = FakeCalendar.UPDATED[0][1]
        assert datetime.fromisoformat(
            moved["start"]["dateTime"]).date().isoformat() == "2026-10-09"

    def test_reschedule_internal_event_respects_approval_standard(
            self, cal_agent):
        r = cal_agent.handle(_req("reschedule ev1 to 2026-10-09",
                                  event_id="ev1", day="2026-10-09"))
        r2 = cal_agent.handle(_req("confirm", plan_id=r.data["plan_id"],
                                   option=1))
        # ev1 has no attendees — but standard mode still parks (approval
        # before sending unless trusted automation is explicitly enabled).
        assert r2.status is TaskStatus.NEEDS_APPROVAL

    def test_no_token_material_in_any_reply(self, cal_agent):
        for kwargs in ({"text": "my availability on 2026-10-08"},
                       {"text": "conflicts on 2026-10-08"},
                       {"text": "propose slots on 2026-10-08"}):
            r = cal_agent.handle(_req(**kwargs))
            assert "ya29" not in r.text
            assert "SHOULD_NEVER_APPEAR" not in r.text

    def test_menu_and_never_raises(self, cal_agent):
        r = cal_agent.handle(_req("tell me a joke"))
        assert r.status is TaskStatus.NEEDS_INPUT
        for text in ("", "   ", "!!!", "x" * 5000, "reschedule",
                     "propose slots on 2026-10-08"):
            assert cal_agent.handle(_req(text)) is not None


# ═══════════════════════════════════════════════════════════ 8. wiring ══════

class TestWiring:
    def test_classifier_email_lane(self):
        c = IntentClassifier()
        for text in ("check my email", "triage my inbox",
                     "summarize that email", "draft a reply to the mail",
                     "any newsletters?"):
            got = c.classify(text)
            assert got.task_type is TaskType.EMAIL, (text, got.reason)

    def test_classifier_calendar_lane(self):
        c = IntentClassifier()
        for text in ("what's my availability on friday",
                     "any conflicts in my calendar?",
                     "propose slots for a meeting",
                     "reschedule the 1:1 with pat",
                     "book a meeting with the team"):
            got = c.classify(text)
            assert got.task_type is TaskType.CALENDAR, (text, got.reason)

    def test_explicit_routes(self):
        c = IntentClassifier()
        assert c.classify("route this to email") \
            .task_type is TaskType.EMAIL
        assert c.classify("[calendar] propose slots") \
            .task_type is TaskType.CALENDAR
        assert c.classify("email agent please triage") \
            .task_type is TaskType.EMAIL
        assert c.classify("calendar agent, my availability") \
            .task_type is TaskType.CALENDAR

    def test_other_lanes_unchanged(self):
        c = IntentClassifier()
        assert c.classify("fix the bug in my python function") \
            .task_type is TaskType.CODING
        assert c.classify("analyze RELIANCE chart setup") \
            .task_type is TaskType.TRADING
        assert c.classify("what's the latest news about EVs") \
            .task_type is TaskType.RESEARCH
        assert c.classify("I have fever and headache") \
            .task_type is TaskType.HEALTH
        assert c.classify("hi").task_type is TaskType._FALLBACK

    def test_registry_off_by_default(self):
        os.environ.pop("JARVIS_EMAIL_AGENT", None)
        os.environ.pop("JARVIS_CALENDAR_AGENT", None)
        reg = build_default_registry()
        assert type(reg.get("email_agent")).__name__ == "StubAgent"
        assert type(reg.get("calendar_agent")).__name__ == "StubAgent"
        r = reg.get("email_agent").handle(_req("triage"))
        assert r.status is TaskStatus.NEEDS_INPUT

    def test_registry_on(self):
        os.environ["JARVIS_EMAIL_AGENT"] = "on"
        os.environ["JARVIS_CALENDAR_AGENT"] = "on"
        try:
            reg = build_default_registry()
            assert type(reg.get("email_agent")).__name__ == "EmailAgent"
            assert type(reg.get("calendar_agent")).__name__ == \
                "CalendarAgent"
        finally:
            os.environ.pop("JARVIS_EMAIL_AGENT", None)
            os.environ.pop("JARVIS_CALENDAR_AGENT", None)
