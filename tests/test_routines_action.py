"""Tests for actions/routines.py — routines managed by voice.

The engine (core/routines) already had its own tests; what was missing was the
tool that lets the model list, add, change and remove a routine, and set quiet
hours, without the user editing memory/routines.json by hand. These drive the
tool against a throwaway RoutineEngine (autostart=False, so no scheduler thread
and no model calls) and check that the request really lands in the engine.
"""

from __future__ import annotations

import pytest

from actions import routines as rt
from core.routines import desktop
from core.routines.desktop import CallbackHost
from core.routines.engine import RoutineEngine


class _Host(CallbackHost):
    def __init__(self):
        self.said: list[str] = []
        super().__init__(
            ready=lambda: True,
            idle_seconds=lambda: 10_000,
            say=self.said.append,
            prompt=lambda text: self.said.append(f"PROMPT: {text}"),
            builtin=lambda name, options: "builtin result",
            log=lambda msg: None,
        )


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    """A real RoutineEngine on a temporary file, wired in as the desktop one."""
    host = _Host()
    eng = RoutineEngine(tmp_path / "routines.json", host, autostart=False,
                        log=lambda *_a: None)
    monkeypatch.setattr(desktop, "_engine", eng)
    yield eng, host
    eng.shutdown()


def _find(eng, text: str) -> dict:
    return next(r for r in eng.list() if r.get("text") == text)


def test_declaration_is_valid():
    tool = rt.TOOL
    assert tool["name"] == "routines"
    assert callable(tool["handler"])
    props = tool["parameters"]["properties"]
    assert set(props["action"]["enum"]) == {
        "add", "list", "update", "remove", "run_now", "quiet_hours"}
    assert tool["parameters"]["required"] == ["action"]


def test_lists_nothing_when_empty(engine):
    eng, _host = engine
    assert eng.list() == []
    assert rt.routines({"action": "list"}) == "No routines."


def test_add_list_and_remove(engine):
    eng, _host = engine
    added = rt.routines({"action": "add", "kind": "say",
                         "text": "drink water", "time": "06:30"})
    assert "status=done" in added and "drink water" in added

    routine = _find(eng, "drink water")
    listed = rt.routines({"action": "list"})
    assert routine["id"] in listed
    assert "quiet hours: off" in listed

    removed = rt.routines({"action": "remove", "id": routine["id"]})
    assert "status=done" in removed
    assert eng.list() == []


def test_add_without_kind_asks_instead_of_guessing(engine):
    eng, _host = engine
    answer = rt.routines({"action": "add", "text": "stretch", "time": "09:00"})
    assert answer.startswith("status=ask")
    assert eng.list() == []


def test_add_without_when_fails_cleanly(engine):
    eng, _host = engine
    answer = rt.routines({"action": "add", "kind": "say", "text": "stretch"})
    assert answer.startswith("status=failed")
    assert eng.list() == []


def test_update_moves_a_routine(engine):
    eng, _host = engine
    rt.routines({"action": "add", "kind": "say", "text": "walk", "time": "08:00"})
    rid = _find(eng, "walk")["id"]

    changed = rt.routines({"action": "update", "id": rid, "time": "09:30"})
    assert "status=done" in changed
    assert "09:30" in eng.describe(_find(eng, "walk"))


def test_update_with_nothing_to_change_fails(engine):
    eng, _host = engine
    rt.routines({"action": "add", "kind": "say", "text": "walk", "time": "08:00"})
    rid = _find(eng, "walk")["id"]
    assert rt.routines({"action": "update", "id": rid}).startswith("status=failed")


def test_run_now_says_the_reminder(engine):
    eng, host = engine
    rt.routines({"action": "add", "kind": "say", "text": "take a break",
                 "time": "10:00"})
    rid = _find(eng, "take a break")["id"]

    result = rt.routines({"action": "run_now", "id": rid})
    assert "status=done" in result
    assert "take a break" in host.said


def test_quiet_hours_set_and_cleared(engine):
    eng, _host = engine
    assert "23:00-07:00" in rt.routines({"action": "quiet_hours",
                                         "between": "23:00-07:00"})
    assert eng.quiet_hours() == "23:00-07:00"
    assert "off" in rt.routines({"action": "quiet_hours", "between": ""})
    assert eng.quiet_hours() == ""


def test_missing_routine_reports_failure(engine):
    _eng, _host = engine
    assert rt.routines({"action": "remove", "id": "nope"}).startswith("status=failed")
    assert rt.routines({"action": "bogus"}).startswith("status=failed")


def test_without_engine_reports_failure(monkeypatch):
    monkeypatch.setattr(desktop, "_engine", None)
    answer = rt.routines({"action": "list"})
    assert answer.startswith("status=failed")
    assert "not running" in answer
