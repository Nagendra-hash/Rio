"""Tests for core/media_watch.py — sound the assistant did not make.

The HUD closes the microphone for its own video and music. These check the
other half: noticing that Spotify, a browser tab or the dashboard is playing,
so the assistant stops answering somebody else's loudspeaker — and, just as
importantly, that "nobody could tell us" is never read as silence.
"""

from __future__ import annotations

import time

import pytest

from core import media_watch as mw


class _Watch(mw.MediaWatch):
    """A watcher whose readings come from a script, not from D-Bus."""

    def __init__(self, readings):
        self.readings = list(readings)
        self.calls = 0
        super().__init__(self._record, query=self._query, interval=0.01)

    def _record(self, playing, who):
        self.seen = getattr(self, "seen", []) + [(playing, who)]

    def _query(self):
        self.calls += 1
        return self.readings.pop(0) if self.readings else None


def test_reports_a_transition_once():
    seen = []
    watch = mw.MediaWatch(lambda playing, who: seen.append((playing, who)),
                          query=lambda: (True, "Spotify"), interval=0.01)
    assert watch.poll_once() is True
    assert seen == [(True, "Spotify")]
    assert watch.poll_once() is False          # same state, no repeat
    assert seen == [(True, "Spotify")]


def test_reports_when_the_sound_stops():
    seen = []
    readings = iter([(True, "VLC"), (False, "")])
    watch = mw.MediaWatch(lambda playing, who: seen.append((playing, who)),
                          query=lambda: next(readings), interval=0.01)
    watch.poll_once()
    watch.poll_once()
    assert seen == [(True, "VLC"), (False, "")]


def test_cannot_tell_is_not_silence():
    """A machine with no media session must not unlock the microphone as if the
    sound had stopped — the last state stands until somebody answers."""
    seen = []
    readings = iter([(True, "Spotify"), None, None, (False, "")])
    watch = mw.MediaWatch(lambda playing, who: seen.append((playing, who)),
                          query=lambda: next(readings), interval=0.01)
    for _ in range(4):
        watch.poll_once()
    assert seen == [(True, "Spotify"), (False, "")]


def test_a_failing_query_never_raises():
    def boom():
        raise RuntimeError("no bus")
    seen = []
    watch = mw.MediaWatch(lambda playing, who: seen.append((playing, who)),
                          query=boom, interval=0.01)
    assert watch.poll_once() is False
    assert seen == []


def test_start_polls_immediately_and_stop_ends_the_thread():
    watch = _Watch([(True, "Spotify")] * 500)
    watch.start()
    deadline = time.monotonic() + 2.0
    while not getattr(watch, "seen", []) and time.monotonic() < deadline:
        time.sleep(0.01)
    watch.stop()
    assert watch.seen[0] == (True, "Spotify")
    calls = watch.calls
    time.sleep(0.1)
    assert watch.calls - calls <= 1           # the loop is not running any more


# ── the platform query ───────────────────────────────────────────────────────
@pytest.fixture()
def sessions(monkeypatch):
    """Stand in for actions/music_player's OS probes."""
    state = {"sessions": [], "raise": None}

    def fake():
        if state["raise"] is not None:
            raise state["raise"]
        return state["sessions"]

    monkeypatch.setitem(__import__("sys").modules, "actions.music_player",
                        type("M", (), {"_query_media_sessions": staticmethod(fake)}))
    return state


def test_playing_session_names_the_app(sessions):
    sessions["sessions"] = [{"player": "spotify", "title": "Kesariya",
                             "status": "Playing"}]
    assert mw.query_media() == (True, "Spotify — Kesariya")


def test_paused_and_stopped_sessions_do_not_count(sessions):
    sessions["sessions"] = [{"player": "spotify", "title": "x", "status": "Paused"},
                            {"player": "vlc", "title": "y", "status": "Stopped"}]
    assert mw.query_media() == (False, "")


def test_one_playing_session_among_paused_ones_counts(sessions):
    sessions["sessions"] = [{"player": "vlc", "title": "y", "status": "Paused"},
                            {"player": "chromium", "title": "Mix", "status": "Playing"}]
    assert mw.query_media() == (True, "Chromium — Mix")


def test_no_answer_at_all_is_none(sessions):
    sessions["raise"] = RuntimeError("no D-Bus")
    assert mw.query_media() is None


def test_an_empty_session_list_is_silence(sessions):
    sessions["sessions"] = []
    assert mw.query_media() == (False, "")


def test_dashboard_music_is_not_treated_as_room_sound(sessions, monkeypatch):
    """The dashboard card is for the phone; its sound is not in this room, so
    muting the laptop microphone for it would lock the user out. A browser on
    this machine playing the same card registers a session and is caught above."""
    sessions["sessions"] = []
    monkeypatch.setattr(mw, "_dashboard_playing", lambda: (True, "the RIO dashboard"))
    assert mw.query_media() == (False, "")
