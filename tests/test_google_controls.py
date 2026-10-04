"""
tests/test_google_controls.py — offline tests for the Gmail / Calendar skills.

What this proves (no network, no real credentials, no Google libraries needed):
  1. Both TOOL dicts pass the action loader's validation and register under
     the names the requirements.txt comment reserved for them.
  2. _google_auth: JARVIS_HOME-relocated paths, and a missing client secret
     becomes a spoken-setup SetupNeeded instead of a traceback.
  3. gmail_control against a fake service: search/list params (q, labelIds,
     max_results), full-message body extraction (text/plain, html fallback,
     multipart), send MIME round-trip, and the setup/missing-arg sentences.
  4. calendar_control against a fake service: list windows, create payloads
     (default duration, end_time, all-day, timezone), delete by id, by unique
     title, and the ambiguous-match ask-back.

Isolation: JARVIS_HOME points at a temp dir for the whole run (the actions read
it per call), and every service is a fake — nothing here can touch Gmail or
Calendar even if real credentials exist on this machine.

Run:  .venv/bin/python -m pytest tests/test_google_controls.py -q
Also pytest-compatible: run_all().
"""
import base64
import importlib.util
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from email import message_from_bytes
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# ── isolation: JARVIS_HOME → temp dir BEFORE importing the actions ────────────
_TMP = tempfile.mkdtemp(prefix="jarvis-google-e2e-")
_OLD_JH = os.environ.get("JARVIS_HOME")
os.environ["JARVIS_HOME"] = _TMP

from actions import _google_auth as ga           # noqa: E402
from actions import gmail_control as g           # noqa: E402
from actions import calendar_control as c        # noqa: E402
from core.action_loader import _validate         # noqa: E402


# ── fake googleapiclient service ──────────────────────────────────────────────
# Every chain (svc.users().messages().list(**kw).execute()) is recorded as
# (path, kwargs); results are keyed by path — plus the id kwarg when present,
# so per-message gets can return different bodies.

class _Pending:
    def __init__(self, svc, path):
        self._svc, self._path = svc, path

    def __getattr__(self, name):
        if name == "execute":
            return lambda: self._svc.results.get(self._svc.last_key, {})
        def step(*_a, **kw):
            new = self._path + (name,)
            self._svc.calls.append((new, dict(kw)))
            self._svc.last_key = self._svc._key(new, kw)
            return _Pending(self._svc, new)
        return step


class FakeService:
    def __init__(self, results=None):
        self.results = results or {}
        self.calls = []
        self.last_key = ()

    @staticmethod
    def _key(path, kw):
        if "id" in kw:
            return path + (str(kw["id"]),)
        return path

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        def step(*_a, **kw):
            self.calls.append(((name,), dict(kw)))
            self.last_key = self._key((name,), kw)
            return _Pending(self, (name,))
        return step

    def called(self, *path):
        return [kw for p, kw in self.calls if p == path]


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _msg(mid, from_h, subject, body=None, snippet="s", html=None):
    parts = []
    if body is not None:
        parts.append({"mimeType": "text/plain",
                      "body": {"data": _b64(body)}})
    if html is not None:
        parts.append({"mimeType": "text/html",
                      "body": {"data": _b64(html)}})
    payload = {"headers": [{"name": "From", "value": from_h},
                           {"name": "Subject", "value": subject}],
               "mimeType": "multipart/alternative"}
    if parts:
        payload["parts"] = parts
    else:
        payload["mimeType"] = "text/plain"
        payload["body"] = {}
    return {"id": mid, "snippet": snippet, "internalDate": "1727900000000",
            "payload": payload}


# ── 1. tool validation / discovery contract ───────────────────────────────────

def test_tool_dicts_validate():
    for mod, name in ((g, "gmail_control"), (c, "calendar_control")):
        rec = _validate(mod, mod.__file__)
        assert rec.valid, f"{name} rejected by action loader: {rec.error}"
        assert rec.name == name
        assert callable(rec.handler)
        assert rec.parameters.get("type") == "OBJECT"


def test_gmail_home_paths():
    assert ga.client_secret_path() == Path(_TMP) / "config" / "client_secret.json"
    assert ga.token_path("gmail_token") == Path(_TMP) / "config" / "gmail_token.json"
    assert ga.token_path("calendar_token") == Path(_TMP) / "config" / "calendar_token.json"


def test_missing_client_secret_is_setup_needed():
    try:
        ga.get_service("gmail", "v1", [ga.GMAIL_SCOPE], "gmail_token")
        raise AssertionError("expected SetupNeeded with no client secret")
    except ga.SetupNeeded as e:
        assert "client_secret" in e.message, e.message
        assert "console.cloud.google.com" in e.message, e.message


# ── 2. gmail pieces ───────────────────────────────────────────────────────────

def test_gmail_body_extraction():
    assert g._collect_body(_msg("m1", "a@x", "S", body="hello")["payload"]) == "hello"
    html = g._collect_body(_msg("m2", "a@x", "S",
                                html="<p>Hi <b>you</b></p>")["payload"])
    assert "Hi" in html and "you" in html and "<" not in html
    # plain wins when both exist
    both = {"mimeType": "multipart/alternative", "parts": [
        {"mimeType": "text/html", "body": {"data": _b64("<p>rich</p>")}},
        {"mimeType": "text/plain", "body": {"data": _b64("plain")}},
    ]}
    assert g._collect_body(both) == "plain"


def test_gmail_send_raw():
    raw, problem = g._send_raw("a@x.com", "Hi", "Body text", "")
    assert not problem
    msg = message_from_bytes(base64.urlsafe_b64decode(raw))
    assert msg["To"] == "a@x.com" and msg["Subject"] == "Hi"
    assert "Body text" in msg.get_payload()

    raw, problem = g._send_raw("not-an-address", "Hi", "Body", "")
    assert not raw and "address" in problem.lower()

    raw, problem = g._send_raw("a@x.com", "Hi", "   ", "")
    assert not raw and "body" in problem.lower()


def test_gmail_search_params():
    svc = FakeService({("users", "messages", "list"):
                       {"messages": [{"id": "a"}, {"id": "b"}]}})
    ids = g._list_message_ids(svc, "from:alice is:unread", "unread", 5)
    assert ids == ["a", "b"]
    hits = svc.called("users", "messages", "list")
    assert len(hits) == 1, hits
    kw = hits[0]
    assert kw["q"] == "from:alice is:unread"
    assert kw["labelIds"] == ["UNREAD"]
    assert kw["maxResults"] == 5
    assert kw["userId"] == "me"


def test_gmail_render_and_empty():
    out = g._render_list(
        [{"id": "x1", "from": "A <a@x>", "subject": "Hi", "date": "Oct 3",
          "snippet": "hello there"}],
        "Gmail inbox — latest")
    assert "id=x1" in out and "a@x" in out and "read/mark_read" in out
    assert "no messages" in g._render_list([], "Gmail inbox")


def test_gmail_read_message():
    svc = FakeService({("users", "messages", "get", "m9"):
                       _msg("m9", "Alice <a@x.com>", "Lunch?", body="12:30 ok?")})
    out = g._read_message(svc, "m9")
    assert "Alice <a@x.com>" in out and "Lunch?" in out and "12:30 ok?" in out


def test_gmail_handler_paths():
    orig = ga.get_service
    try:
        # setup failure is spoken, not raised
        ga.get_service = lambda *a, **k: (_ for _ in ()).throw(
            ga.SetupNeeded("Set up Google first."))
        out = g.gmail_control({"operation": "list"})
        assert out == "Set up Google first."

        # search flows through with the query intact
        svc = FakeService({("users", "messages", "list"):
                           {"messages": [{"id": "z1"}]},
                           ("users", "messages", "get", "z1"):
                           _msg("z1", "B <b@x>", "T", body="body")})
        ga.get_service = lambda *a, **k: svc
        out = g.gmail_control({"operation": "search", "query": "from:b"})
        assert "from:b" in svc.called("users", "messages", "list")[0]["q"]
        assert "id=z1" in out

        # send records the raw MIME on the API call
        svc2 = FakeService({("users", "messages", "send"): {"id": "sent"}})
        ga.get_service = lambda *a, **k: svc2
        out = g.gmail_control({"operation": "send", "to": "c@x.com",
                               "subject": "Hi", "body": "Hello"})
        assert "sent" in out.lower() or "c@x.com" in out
        send_hits = svc2.called("users", "messages", "send")
        assert len(send_hits) == 1, send_hits
        decoded = message_from_bytes(
            base64.urlsafe_b64decode(send_hits[0]["body"]["raw"]))
        assert decoded["To"] == "c@x.com" and "Hello" in decoded.get_payload()

        # trash / mark_read / archive hit the right endpoints with the id
        for op, path in (("trash", ("users", "messages", "trash")),
                         ("mark_read", ("users", "messages", "modify")),
                         ("archive", ("users", "messages", "modify"))):
            svc3 = FakeService()
            ga.get_service = lambda *a, **k: svc3
            g.gmail_control({"operation": op, "message_id": "m7"})
            assert svc3.called(*path), f"{op} hit {path}"
        assert svc3.called("users", "messages", "modify")[0]["id"] == "m7"
    finally:
        ga.get_service = orig


def test_gmail_arg_guards():
    orig = ga.get_service
    try:
        ga.get_service = lambda *a, **k: FakeService()
        assert "operation" in g.gmail_control({}).lower()
        assert "message_id" in g.gmail_control({"operation": "read"}).lower()
        assert "address" in g.gmail_control({"operation": "send",
                                             "body": "x",
                                             "to": "name-only"}).lower()
    finally:
        ga.get_service = orig
    assert g._clamp_n(None) == 8 and g._clamp_n("99") == 25 and g._clamp_n(0) == 1


# ── 3. calendar pieces ────────────────────────────────────────────────────────

def test_calendar_parsers():
    assert c._parse_date("2026-10-05") == date(2026, 10, 5)
    assert c._parse_date("05-10-2026") is None
    assert c._parse_hhmm("14:30") == (14, 30)
    assert c._parse_hhmm("2:30 pm") is None
    assert c._clamp_days(None) == 7 and c._clamp_days("400") == 60


def test_calendar_fmt_when():
    tz = datetime.now().astimezone().tzinfo
    ev = {"start": {"dateTime": datetime(2026, 10, 5, 14, 0, tzinfo=tz).isoformat()},
          "end": {"dateTime": datetime(2026, 10, 5, 15, 0, tzinfo=tz).isoformat()}}
    assert "Oct 05" in c._fmt_when(ev) and "14:00" in c._fmt_when(ev)
    assert "15:00" in c._fmt_when(ev)
    assert "all day" in c._fmt_when({"start": {"date": "2026-10-05"},
                                     "end": {"date": "2026-10-06"}})


def test_calendar_create_bodies():
    day = date(2026, 10, 5)
    tz = datetime.now().astimezone().tzinfo

    # default 60-minute duration
    ev, problem = c._create_body(
        {"title": "Dentist", "start_time": "14:00"}, day)
    assert not problem
    assert datetime.fromisoformat(ev["start"]["dateTime"]).hour == 14
    end = datetime.fromisoformat(ev["end"]["dateTime"])
    assert end - datetime.fromisoformat(ev["start"]["dateTime"]) == timedelta(hours=1)

    # explicit end time
    ev, problem = c._create_body(
        {"title": "Trip", "start_time": "09:00", "end_time": "11:30"}, day)
    assert not problem and ev["end"]["dateTime"] > ev["start"]["dateTime"]

    # end before start is refused, not silently rolled
    _, problem = c._create_body(
        {"title": "Bad", "start_time": "15:00", "end_time": "14:00"}, day)
    assert "after the start" in problem

    # all-day takes date only
    ev, problem = c._create_body({"title": "Holiday", "all_day": True}, day)
    assert not problem and ev["start"] == {"date": "2026-10-05"}
    assert ev["end"] == {"date": "2026-10-06"}

    # an explicit IANA timezone lands in the payload
    ev, problem = c._create_body(
        {"title": "Call", "start_time": "22:00", "timezone": "Asia/Kolkata"}, day)
    assert not problem
    assert ev["start"]["timeZone"] == "Asia/Kolkata"
    assert "+05:30" in ev["start"]["dateTime"]

    # missing title / bad timezone are spoken problems
    _, problem = c._create_body({"start_time": "10:00"}, day)
    assert "title" in problem.lower()
    _, problem = c._create_body(
        {"title": "X", "start_time": "10:00", "timezone": "Mars/Olympus"}, day)
    assert "Mars/Olympus" in problem


def test_calendar_handler_list_and_create():
    orig = ga.get_service
    tz = datetime.now().astimezone().tzinfo
    items = [
        {"id": "e1", "summary": "Standup",
         "start": {"dateTime": datetime(2026, 10, 5, 9, 30, tzinfo=tz).isoformat()},
         "end": {"dateTime": datetime(2026, 10, 5, 10, 0, tzinfo=tz).isoformat()}},
        {"id": "e2", "summary": "Holiday",
         "start": {"date": "2026-10-06"}, "end": {"date": "2026-10-07"}},
    ]
    try:
        svc = FakeService({("events", "list"): {"items": items},
                           ("events", "insert"):
                           dict(items[0], summary="Dentist")})
        ga.get_service = lambda *a, **k: svc

        out = c.calendar_control({"operation": "list", "date": "2026-10-05",
                                  "days": 3})
        assert "Standup" in out and "Holiday" in out
        list_hits = svc.called("events", "list")
        assert len(list_hits) == 1, list_hits
        list_kw = list_hits[0]
        assert list_kw["calendarId"] == "primary"
        assert list_kw["singleEvents"] is True and list_kw["orderBy"] == "startTime"
        # window is date 00:00 → +3 days
        assert datetime.fromisoformat(list_kw["timeMin"]).day == 5
        assert datetime.fromisoformat(list_kw["timeMax"]).day == 8

        out = c.calendar_control({"operation": "create", "date": "2026-10-05",
                                  "title": "Dentist", "start_time": "14:00",
                                  "duration_minutes": 45})
        assert "Dentist" in out and "created" in out.lower()
        ins_hits = svc.called("events", "insert")
        assert len(ins_hits) == 1, ins_hits
        body = ins_hits[0]["body"]
        assert body["summary"] == "Dentist"
        assert datetime.fromisoformat(body["end"]["dateTime"]) - \
            datetime.fromisoformat(body["start"]["dateTime"]) == timedelta(minutes=45)
    finally:
        ga.get_service = orig


def test_calendar_handler_delete():
    orig = ga.get_service
    items = [{"id": "e1", "summary": "Standup",
              "start": {"date": "2026-10-05"}, "end": {"date": "2026-10-06"}},
             {"id": "e2", "summary": "Standup — backlog",
              "start": {"date": "2026-10-06"}, "end": {"date": "2026-10-07"}}]
    try:
        # by event id: straight to trash
        svc = FakeService()
        ga.get_service = lambda *a, **k: svc
        out = c.calendar_control({"operation": "delete", "event_id": "e1"})
        assert "Trash" in out
        assert svc.called("events", "trash")[0]["eventId"] == "e1"

        # unique title match: found and trashed
        svc = FakeService({("events", "list"): {"items": [items[0]]}})
        ga.get_service = lambda *a, **k: svc
        out = c.calendar_control({"operation": "delete", "match_title": "standup",
                                  "date": "2026-10-05"})
        assert "Deleted" in out
        assert svc.called("events", "trash")[0]["eventId"] == "e1"

        # ambiguous: asks back instead of guessing
        svc = FakeService({("events", "list"): {"items": items}})
        ga.get_service = lambda *a, **k: svc
        out = c.calendar_control({"operation": "delete", "match_title": "standup",
                                  "date": "2026-10-05"})
        assert "found 2" in out and "id=" in out
        assert not svc.called("events", "trash")

        # no match: honest miss
        svc = FakeService({("events", "list"): {"items": []}})
        ga.get_service = lambda *a, **k: svc
        out = c.calendar_control({"operation": "delete", "match_title": "ghost",
                                  "date": "2026-10-05"})
        assert "don't see" in out.lower() or "no event" in out.lower()
    finally:
        ga.get_service = orig


def test_calendar_setup_and_guards():
    orig = ga.get_service
    try:
        ga.get_service = lambda *a, **k: (_ for _ in ()).throw(
            ga.SetupNeeded("Set up Google first."))
        assert c.calendar_control({"operation": "list"}) == "Set up Google first."
        ga.get_service = lambda *a, **k: FakeService()
        assert "operation" in c.calendar_control({}).lower()
        assert "YYYY-MM-DD" in c.calendar_control({"operation": "create",
                                                   "title": "X",
                                                   "start_time": "10:00"})
    finally:
        ga.get_service = orig


# ── runner ────────────────────────────────────────────────────────────────────

def run_all() -> bool:
    test_tool_dicts_validate()
    print("  ✓ both TOOL dicts validate under the action loader")
    test_gmail_home_paths()
    print("  ✓ JARVIS_HOME-relocated token/secret paths")
    test_missing_client_secret_is_setup_needed()
    print("  ✓ missing client secret → spoken setup steps")
    test_gmail_body_extraction()
    print("  ✓ gmail body: plain, html fallback, multipart")
    test_gmail_send_raw()
    print("  ✓ gmail send MIME round-trip + guards")
    test_gmail_search_params()
    test_gmail_render_and_empty()
    test_gmail_read_message()
    print("  ✓ gmail list/search params, rendering, full read")
    test_gmail_handler_paths()
    print("  ✓ gmail handler: setup speech, search, send, mark/archive/trash")
    test_gmail_arg_guards()
    print("  ✓ gmail arg guards and clamps")
    test_calendar_parsers()
    test_calendar_fmt_when()
    print("  ✓ calendar parsers and time formatting")
    test_calendar_create_bodies()
    print("  ✓ calendar create payloads (duration/end/all-day/timezone)")
    test_calendar_handler_list_and_create()
    print("  ✓ calendar handler: list window + insert body")
    test_calendar_handler_delete()
    print("  ✓ calendar delete: by id, unique title, ambiguous ask-back")
    test_calendar_setup_and_guards()
    print("  ✓ calendar setup speech and arg guards")
    return True


if __name__ == "__main__":
    run_all()
    print("\nPASS: google controls")
