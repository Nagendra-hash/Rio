"""Tests for actions/background_tasks.py — the tool that fills the TASKS panel.

The panel (gui/window.py::_refresh_tasks) only ever shows what core/tasks has on
disk, and before this tool existed nothing in the conversation could put a task
there: only a scheduled routine could, so the panel read "No background tasks
yet." forever. These tests drive the tool against a throwaway engine
(autostart=False, so no scheduler thread and no model calls) and check that the
task really lands in the store, can be steered, and can be cancelled.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from actions import background_tasks as bt
from core.tasks import desktop
from core.tasks.engine import TaskEngine

AGENTS_DIR = Path(__file__).resolve().parent.parent / "agents"


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    """A real TaskEngine on a temporary directory, wired in as the desktop one."""
    eng = TaskEngine(
        tmp_path / "tasks",
        tmp_path / "reports",
        [AGENTS_DIR],
        autostart=False,
        log=lambda *_a: None,
    )
    monkeypatch.setattr(desktop, "_engine", eng)
    monkeypatch.setattr(desktop, "_player", None)
    yield eng
    eng.shutdown(wait=0)


def call(**params) -> dict:
    """Run the tool and parse its reply — every action answers in JSON except
    the two input-validation refusals, which are plain sentences."""
    raw = bt.background_tasks_action(params)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:  # the human-readable refusal path
        return {"message": raw}


def test_tool_declaration_is_registrable():
    tool = bt.TOOL
    assert tool["name"] == "background_tasks"
    assert callable(tool["handler"])
    props = tool["parameters"]["properties"]
    assert {"action", "task", "agent", "deliver"} <= set(props)
    # The model must be told not to poll: the engine announces the result.
    assert "NEVER poll" in tool["description"]


def test_start_creates_a_task_that_the_panel_can_list(engine):
    out = call(action="start", task="Draft a two-page project brief.", agent="general")
    started = out["started"]
    assert started["id"]
    assert started["status"] == "queued"
    assert started["active"] is True
    assert started["agent"] == "general"
    assert "TASKS panel" in out["note"]

    # The task is on disk in the engine's own store, which is what the GUI reads.
    assert (engine.store.root / f"{started['id']}.json").exists()
    listed = call(action="list")
    assert [t["id"] for t in listed["tasks"]] == [started["id"]]
    assert listed["active"] == 1


def test_start_is_the_default_action_when_a_task_is_given(engine):
    out = call(task="Prepare a checklist for the trip.")
    assert out["started"]["id"]


def test_start_trims_the_instruction_and_titles_the_task(engine):
    out = call(
        task="  Read three sources\n on saturn   mahadasha.  ", title="Saturn MD"
    )
    task = engine.get(out["started"]["id"])
    assert task["task"] == "Read three sources on saturn mahadasha."
    assert task["title"] == "Saturn MD"


def test_start_falls_back_to_a_known_agent_and_budget(engine):
    out = call(action="start", task="Summarise the notes.", agent="astrologer")
    assert out["started"]["agent"] == "general"
    assert set(out["agents"]) == {"general", "researcher"}
    budget = out["started"]["budget"]
    assert budget["minutes"] == engine.agents()["general"].minutes
    assert budget["rounds"] == engine.agents()["general"].rounds


def test_researcher_budget_comes_from_its_profile(engine):
    out = call(action="start", task="Research Lahiri ayanamsa.", agent="researcher")
    assert out["started"]["budget"]["rounds"] == 300


def test_explicit_budget_is_clamped(engine):
    out = call(action="start", task="Short job.", minutes=9999, rounds=-5)
    # 720 min / 1 round are the engine's own hard limits.
    assert out["started"]["budget"]["minutes"] == 720
    assert out["started"]["budget"]["rounds"] == 1


def test_status_reports_the_newest_running_task(engine):
    tid = call(action="start", task="Draft the letter.")["started"]["id"]
    out = call(action="status")
    assert out["id"] == tid
    assert "events" in out
    assert out["budget"]["minutes_used"] == 0.0


def test_status_by_id(engine):
    first = call(action="start", task="First job.")["started"]["id"]
    call(action="start", task="Second job.")
    out = call(action="status", task_id=first)
    assert out["id"] == first
    assert out["title"] != "Second job."


def test_steer_adds_the_note_to_the_task(engine):
    tid = call(action="start", task="Research the topic.")["started"]["id"]
    out = call(action="steer", task_id=tid, note="  Prefer primary sources.  ")
    assert out["steered"] == tid
    assert engine.get(tid)["notes_in"] == ["Prefer primary sources."]


def test_steer_without_a_note_is_refused(engine):
    call(action="start", task="Research the topic.")
    assert "Pass 'note'" in call(action="steer")["message"]


def test_cancel_stops_a_queued_task(engine):
    tid = call(action="start", task="Long job.")["started"]["id"]
    out = call(action="cancel", task_id=tid)
    assert out["cancelled"] == [tid]
    assert engine.get(tid)["status"] == "cancelled"
    assert call(action="list")["active"] == 0


def test_cancelling_nothing_is_not_an_error(engine):
    out = call(action="cancel")
    assert out["cancelled"] == []
    assert "No background task" in out["note"]


def test_empty_task_is_refused_with_the_schema_hint(engine):
    out = call(action="start", task="   ")
    assert "pass 'task'" in out["message"]
    assert engine.list(5) == []


def test_unknown_action_lists_the_real_ones(engine):
    message = call(action="pause")["message"]
    assert "pause" in message
    for name in ("start", "list", "status", "steer", "cancel"):
        assert name in message


def test_list_when_nothing_exists_points_at_start(engine):
    out = call(action="list")
    assert out["tasks"] == []
    assert out["active"] == 0
    assert "action='start'" in out["note"]


def test_deliver_is_validated(engine):
    out = call(action="start", task="Draft.", deliver="telepathy")
    assert out["started"]["deliver"] == "voice"
