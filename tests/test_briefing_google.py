"""
tests/test_briefing_google.py — offline tests for the morning-briefing
day-ahead snapshot (tomorrow's calendar events + unread mail count).

What this proves (no network, no real credentials):
  1. get_service_silent NEVER consents: no client secret / no token / a
     garbage token all read as "not set up" → None, never a browser tab.
  2. gmail_control.briefing_snapshot: one labels.get(UNREAD) call, the count
     formats naturally, zero unread says so, and any failure degrades to "".
  3. calendar_control.briefing_snapshot: the window is TOMORROW 00:00 → the
     day after, timed events render compactly ("Dentist 14:00–15:00"), all-day
     events stay compact too, an empty day says so, failures degrade to "".
  4. _google_auth.briefing_context: joins the two halves, one failing half
     never takes the other down, and both-empty means "nothing to say".

Isolation: every test that touches the filesystem points JARVIS_HOME at its
own fresh temp dir and restores the environment afterwards — so running this
file alongside the other google tests in one pytest process cannot leak
config state between them. Every service is a fake; nothing here can touch
Gmail or Calendar.

Run:  .venv/bin/python -m pytest tests/test_briefing_google.py -q
Also pytest-compatible: run_all().
"""
import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from actions import _google_auth as ga       # noqa: E402
from actions import gmail_control as g       # noqa: E402
from actions import calendar_control as c    # noqa: E402


# ── isolation + fake service (self-contained; no imports from other tests) ────

def _fresh_jh():
    """Point JARVIS_HOME at a new temp dir; returns a restore callable.

    The google actions read the env var per call, so tests that touch config
    paths set this first — and MUST restore, or the next test in the process
    (including the other google test file's) would see our leftovers.
    """
    tmp = tempfile.mkdtemp(prefix="jarvis-brief-e2e-")
    old = os.environ.get("JARVIS_HOME")
    os.environ["JARVIS_HOME"] = tmp

    def restore():
        if old is None:
            os.environ.pop("JARVIS_HOME", None)
        else:
            os.environ["JARVIS_HOME"] = old
    return restore


class _Pending:
    """One fluent node of the fake service chain."""

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
    """Records every (path, kwargs) chain; results keyed by path — plus the
    id kwarg when present, mirroring how per-resource gets differ."""

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


def _tomorrow() -> date:
    return date.today() + timedelta(days=1)


# ── 1. consent-free auth ──────────────────────────────────────────────────────

def test_silent_auth_never_consents():
    restore = _fresh_jh()
    try:
        # No client secret, no token → None, and no exception leaks a traceback.
        assert ga.get_service_silent("gmail", "v1", [ga.GMAIL_SCOPE],
                                     "gmail_token") is None
        assert ga.get_service_silent("calendar", "v3", [ga.CALENDAR_SCOPE],
                                     "calendar_token") is None
        assert g.briefing_snapshot() == ""
        assert c.briefing_snapshot() == ""
        assert ga.briefing_context() == ""

        # Client secret present but a garbage token file must still refuse
        # silently — a refresh failure must not escalate to a consent prompt.
        ga.config_dir().mkdir(parents=True, exist_ok=True)
        ga.client_secret_path().write_text(
            json.dumps({"installed": {"client_id": "x", "auth_uri": "u",
                                      "token_uri": "t"}}), encoding="utf-8")
        ga.token_path("gmail_token").write_text("{not json", encoding="utf-8")
        assert ga.get_service_silent("gmail", "v1", [ga.GMAIL_SCOPE],
                                     "gmail_token") is None
    finally:
        restore()


def test_snapshots_silence_setup_errors():
    orig = ga.get_service_silent
    try:
        ga.get_service_silent = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("network down"))
        assert g.briefing_snapshot() == ""
        assert c.briefing_snapshot() == ""
        assert ga.briefing_context() == ""
    finally:
        ga.get_service_silent = orig


# ── 2. unread-mail snapshot ───────────────────────────────────────────────────

def test_gmail_snapshot():
    orig = ga.get_service_silent
    try:
        # results key carries the id: labels.get(userId=me, id=UNREAD)
        svc = FakeService({("users", "labels", "get", "UNREAD"):
                           {"id": "UNREAD", "messagesUnread": 7}})
        ga.get_service_silent = lambda *a, **k: svc
        out = g.briefing_snapshot()
        assert "7 unread" in out, out
        hits = svc.called("users", "labels", "get")
        assert len(hits) == 1 and hits[0] == {"userId": "me", "id": "UNREAD"}

        svc = FakeService({("users", "labels", "get", "UNREAD"):
                           {"id": "UNREAD", "messagesUnread": 0}})
        ga.get_service_silent = lambda *a, **k: svc
        out = g.briefing_snapshot()
        assert "fully read" in out.lower() and "unread" not in out.lower(), out
    finally:
        ga.get_service_silent = orig


# ── 3. tomorrow-events snapshot ───────────────────────────────────────────────

def _tomorrows_events():
    day = _tomorrow()
    tz = datetime.now().astimezone().tzinfo
    base = datetime(day.year, day.month, day.day, tzinfo=tz)
    return [
        {"id": "e1", "summary": "Standup",
         "start": {"dateTime": base.replace(hour=9, minute=30).isoformat()},
         "end": {"dateTime": base.replace(hour=10, minute=0).isoformat()}},
        {"id": "e2", "summary": "Dentist",
         "start": {"dateTime": base.replace(hour=14, minute=0).isoformat()},
         "end": {"dateTime": base.replace(hour=15, minute=0).isoformat()}},
        {"id": "e3", "summary": "Town fair",
         "start": {"date": day.isoformat()},
         "end": {"date": (day + timedelta(days=1)).isoformat()}},
    ]


def test_calendar_snapshot():
    orig = ga.get_service_silent
    try:
        svc = FakeService({("events", "list"): {"items": _tomorrows_events()}})
        ga.get_service_silent = lambda *a, **k: svc
        out = c.briefing_snapshot()
        assert "3 event(s)" in out, out
        assert "Standup 09:30–10:00" in out, out
        assert "Dentist 14:00–15:00" in out, out
        assert "Town fair (all day)" in out, out
        assert "id=" not in out, "briefing lines must not carry API ids"

        # the window is tomorrow 00:00 → the day after
        hits = svc.called("events", "list")
        assert len(hits) == 1
        tmin = datetime.fromisoformat(hits[0]["timeMin"])
        tmax = datetime.fromisoformat(hits[0]["timeMax"])
        day = _tomorrow()
        assert tmin.date() == day and tmax.date() == day + timedelta(days=1)

        # an empty tomorrow still says something honest
        svc = FakeService({("events", "list"): {"items": []}})
        ga.get_service_silent = lambda *a, **k: svc
        out = c.briefing_snapshot()
        assert "nothing scheduled" in out.lower() and \
            day.strftime("%a %b %d") in out, out
    finally:
        ga.get_service_silent = orig


# ── 4. the combiner ───────────────────────────────────────────────────────────

def test_briefing_context_combines():
    orig_cal, orig_mail = c.briefing_snapshot, g.briefing_snapshot
    try:
        c.briefing_snapshot = lambda: "Calendar tomorrow: 1 event(s) — X."
        g.briefing_snapshot = lambda: "Email: 4 unread message(s)."
        out = ga.briefing_context()
        assert out == ("Calendar tomorrow: 1 event(s) — X.\n"
                       "Email: 4 unread message(s)."), out

        # a failing half must not take the other down
        def _boom():
            raise RuntimeError("x")
        c.briefing_snapshot = _boom
        assert ga.briefing_context() == "Email: 4 unread message(s)."

        # nothing configured → nothing to say
        c.briefing_snapshot = lambda: ""
        g.briefing_snapshot = lambda: ""
        assert ga.briefing_context() == ""
    finally:
        c.briefing_snapshot = orig_cal
        g.briefing_snapshot = orig_mail


def test_combiner_survives_missing_action_modules():
    """If an action module is broken/absent, the other half still reaches the
    briefing — the combiner imports lazily and tolerates ImportError."""
    orig_cal = c.briefing_snapshot
    real = sys.modules.get("actions.gmail_control")
    try:
        c.briefing_snapshot = lambda: "Calendar tomorrow: 1 event(s) — X."
        sys.modules["actions.gmail_control"] = None   # forces ImportError
        out = ga.briefing_context()
        assert "Calendar" in out and "Email" not in out, out
    finally:
        c.briefing_snapshot = orig_cal
        if real is not None:
            sys.modules["actions.gmail_control"] = real
        else:
            sys.modules.pop("actions.gmail_control", None)


# ── runner ────────────────────────────────────────────────────────────────────

def run_all() -> bool:
    test_silent_auth_never_consents()
    print("  ✓ silent auth: no secret/token/garbage token → None, never consent")
    test_snapshots_silence_setup_errors()
    print("  ✓ snapshots degrade to '' on setup errors")
    test_gmail_snapshot()
    print("  ✓ unread count: one labels.get, natural wording, zero case")
    test_calendar_snapshot()
    print("  ✓ tomorrow window, compact event lines, empty-day message")
    test_briefing_context_combines()
    print("  ✓ combiner joins halves, tolerates a failing half, empty → ''")
    test_combiner_survives_missing_action_modules()
    print("  ✓ combiner survives a missing action module")
    return True


if __name__ == "__main__":
    run_all()
    print("\nPASS: briefing day-ahead snapshot")
