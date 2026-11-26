"""
background_tasks.py — start, inspect, steer and cancel long work that runs while
the conversation carries on (core/tasks).

This is the "background_tasks tool" that core/tasks/desktop.py has always
referred to: without it nothing on the desktop could create a task, so the TASKS
panel in the app had nothing to show and only a scheduled *routine* could ever
populate it.

A task is handed to an agent profile (agents/*.md), works one round at a time
through a tool-calling model, is checkpointed to disk after every round, and
ends in a spoken headline plus a written report. The engine announces the finish
by itself — this tool must never be polled.
"""

import json

from core.tasks import desktop

_KNOWN_ACTIONS = {"start", "list", "status", "cancel", "steer"}
_ACTIVE = {"running", "queued", "scheduled", "waiting_quota"}


def _engine(player=None):
    """The desktop engine (main.py attaches it at startup; ensure() covers a
    harness or an early call)."""
    return desktop.ensure(player)


def _agents(eng) -> list[str]:
    try:
        return sorted(eng.agents())
    except Exception:  # noqa: BLE001 — a broken profile must not kill the tool
        return ["general"]


def _last_line(task: dict) -> str:
    """The most useful one-liner the panel would also be showing."""
    for event in reversed(task.get("events") or []):
        text = str(event.get("text") or "").strip()
        if text and event.get("kind") in (
            "progress",
            "finding",
            "note",
            "state",
            "tool",
        ):
            return text[:300]
    return ""


def _summary(task: dict) -> dict:
    budget = task.get("budget") or {}
    used = task.get("used") or {}
    status = task.get("status") or ""
    return {
        "id": task.get("id"),
        "title": task.get("title"),
        "status": status,
        "active": status in _ACTIVE,
        "agent": task.get("agent"),
        "deliver": task.get("deliver"),
        "budget": {
            "minutes": budget.get("minutes"),
            "rounds": budget.get("rounds"),
            "rounds_used": used.get("rounds") or 0,
            "minutes_used": round(float(used.get("seconds") or 0) / 60.0, 1),
        },
        "last_update": _last_line(task),
        "result": (task.get("result") or "")[:1500],
        "report": task.get("report") or "",
        "error": task.get("error") or "",
    }


def _pick(eng, task_id: str) -> dict | None:
    """A specific task, or the newest one still running, or the newest one."""
    task_id = str(task_id or "").strip()
    if task_id and task_id.lower() not in ("latest", "current", "last"):
        found = eng.get(task_id)
        if found:
            return found
    tasks = eng.list(50)
    for task in tasks:
        if task.get("status") in _ACTIVE:
            return task
    return tasks[0] if tasks else None


def background_tasks_action(parameters: dict, player=None, session_memory=None) -> str:
    p = parameters or {}
    action = (p.get("action") or "").strip().lower()
    if not action:
        # The declaration promises "list, or start when 'task' is given".
        action = "start" if (p.get("task") or p.get("goal")) else "list"
    if action not in _KNOWN_ACTIONS:
        return (
            f"Unknown action {action!r}. Use one of: "
            f"{', '.join(sorted(_KNOWN_ACTIONS))}."
        )

    try:
        eng = _engine(player)
    except Exception as exc:  # noqa: BLE001 — engine construction is the only fatal case
        msg = f"Background tasks are unavailable: {type(exc).__name__}: {exc}"
        _log(msg, player)
        return msg

    if action == "start":
        return _start(eng, p, player)
    if action == "list":
        return _list(eng, p)
    if action == "status":
        return _status(eng, p)
    if action == "cancel":
        return _cancel(eng, p, player)
    return _steer(eng, p, player)


def _start(eng, p: dict, player=None) -> str:
    task = " ".join(str(p.get("task") or p.get("goal") or "").split())
    if not task:
        msg = (
            "A background task needs the work to do — pass 'task' with the full "
            "instruction (what the finished result should be, not just the topic)."
        )
        _log(msg, player)
        return msg

    agent = str(p.get("agent") or "general").strip().lower()
    known = _agents(eng)
    if agent not in known:
        agent = "general" if "general" in known else known[0]

    deliver = str(p.get("deliver") or "voice").strip().lower()
    if deliver not in ("voice", "panel", "silent"):
        deliver = "voice"

    minutes = p.get("minutes")
    rounds = p.get("rounds")
    try:
        minutes = int(minutes) if minutes not in (None, "") else None
    except (TypeError, ValueError):
        minutes = None
    try:
        rounds = int(rounds) if rounds not in (None, "") else None
    except (TypeError, ValueError):
        rounds = None

    try:
        created = eng.start(
            task,
            agent=agent,
            minutes=minutes,
            rounds=rounds,
            deliver=deliver,
            origin="desk",
            title=str(p.get("title") or ""),
        )
    except Exception as exc:  # noqa: BLE001 — surface the engine's reason, do not crash
        msg = f"Could not start the background task: {type(exc).__name__}: {exc}"
        _log(msg, player)
        return msg

    _log(f"background task started — {created['id']} ({agent}, {deliver})", player)
    return json.dumps(
        {
            "started": _summary(created),
            "agents": known,
            "note": (
                "The task now runs in the background and appears in the TASKS "
                "panel. Do not poll it: the engine speaks the headline and shows "
                "the report when it finishes. Tell the user in one sentence that "
                "it is running, and carry on with the conversation."
            ),
        },
        ensure_ascii=False,
    )


def _list(eng, p: dict) -> str:
    try:
        limit = max(1, min(50, int(p.get("limit") or 10)))
    except (TypeError, ValueError):
        limit = 10
    tasks = eng.list(limit)
    active = [t for t in tasks if t.get("status") in _ACTIVE]
    return json.dumps(
        {
            "active": len(active),
            "tasks": [_summary(t) for t in tasks],
            "note": (
                "Running tasks first. The TASKS panel shows the same list, "
                "refreshed every two seconds."
                if tasks
                else "No background tasks yet — start one with action='start'."
            ),
        },
        ensure_ascii=False,
    )


def _status(eng, p: dict) -> str:
    task = _pick(eng, p.get("task_id"))
    if not task:
        return json.dumps(
            {"tasks": [], "note": "No background tasks yet."}, ensure_ascii=False
        )
    out = _summary(task)
    out["events"] = [
        {
            "at": e.get("ts"),
            "kind": e.get("kind"),
            "text": str(e.get("text") or "")[:400],
        }
        for e in (task.get("events") or [])[-20:]
    ]
    return json.dumps(out, ensure_ascii=False)


def _cancel(eng, p: dict, player=None) -> str:
    if p.get("all"):
        active = [t for t in eng.list(50) if t.get("status") in _ACTIVE]
        stopped = [t["id"] for t in active if eng.cancel(t["id"])]
        _log(f"cancelled {len(stopped)} background task(s)", player)
        return json.dumps(
            {
                "cancelled": stopped,
                "note": "Cancelled." if stopped else "Nothing was running.",
            },
            ensure_ascii=False,
        )

    task = _pick(eng, p.get("task_id"))
    if not task:
        return json.dumps(
            {"cancelled": [], "note": "No background task to cancel."},
            ensure_ascii=False,
        )
    # finish=True lets a task that already did work write its report first.
    finish = bool(p.get("finish_report", True))
    ok = eng.cancel(task["id"], finish=finish)
    _log(f"cancel {task['id']} → {ok}", player)
    return json.dumps(
        {
            "cancelled": [task["id"]] if ok else [],
            "finished_report_first": finish and ok,
            "note": (
                "Stopped — it writes up what it already found."
                if ok and finish
                else "Stopped."
                if ok
                else "That task is no longer running."
            ),
        },
        ensure_ascii=False,
    )


def _steer(eng, p: dict, player=None) -> str:
    note = " ".join(str(p.get("note") or "").split())
    if not note:
        return "Pass 'note' with the guidance to add to the running task."
    task = _pick(eng, p.get("task_id"))
    if not task:
        return "No running background task to steer."
    ok = eng.steer(task["id"], note)
    _log(f"steer {task['id']} → {ok}", player)
    return json.dumps(
        {
            "steered": task["id"] if ok else None,
            "note": (
                "The task will take this into account on its next round."
                if ok
                else "That task is not running, so the note was not applied."
            ),
        },
        ensure_ascii=False,
    )


def _log(message: str, player=None) -> None:
    print(f"[Tasks] {message}")


TOOL = {
    "name": "background_tasks",
    "description": (
        "Long work that outlives the conversation: run a task in the background "
        "while the user keeps talking. USE THIS whenever the job takes minutes "
        "to hours — deep research on a topic, reading several sources and "
        "writing a cited report, drafting a long document, working through a "
        "list, preparing a file. It appears live in the TASKS panel. "
        "action='start' (default when 'task' is given): pass the FULL "
        "instruction — what the finished result must contain, not just the "
        "topic. agent='researcher' plans, reads sources in full and writes a "
        "cited report; agent='general' drafts, organises and produces files. "
        "Optional minutes/rounds set the budget (1-720 min); deliver='voice' "
        "(default) speaks the headline when done, 'panel' only shows it, "
        "'silent' does neither. "
        "action='list' shows running and recent tasks, action='status' one "
        "task's progress and events, action='steer' adds guidance to a running "
        "task ('note'), action='cancel' stops one (patch 'task_id'; "
        "'finish_report=True' makes it write up what it already found). "
        "NEVER poll a task you started: the engine announces the result itself. "
        "Say in one sentence that it is running, then carry on."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "start | list | status | steer | cancel (default: list, or start when 'task' is given)",
            },
            "task": {
                "type": "STRING",
                "description": "start: the full instruction, including what a finished result looks like",
            },
            "title": {
                "type": "STRING",
                "description": "start: short label for the TASKS panel (defaults to the task's first words)",
            },
            "agent": {
                "type": "STRING",
                "description": "'researcher' (deep research + cited report) or 'general' (drafting, lists, files). Default: general",
            },
            "minutes": {
                "type": "NUMBER",
                "description": "start: time budget in minutes (1-720). Default: the agent's own budget",
            },
            "rounds": {
                "type": "NUMBER",
                "description": "start: model-round budget (1-400). Default: the agent's own budget",
            },
            "deliver": {
                "type": "STRING",
                "description": "start: voice (speak the headline when done, default) | panel (show only) | silent",
            },
            "task_id": {
                "type": "STRING",
                "description": "status/steer/cancel: the task id from action='list'. Omit for the newest running task",
            },
            "note": {
                "type": "STRING",
                "description": "steer: guidance added to a running task's next round",
            },
            "finish_report": {
                "type": "BOOLEAN",
                "description": "cancel: let the task write up what it already found (default true)",
            },
            "all": {
                "type": "BOOLEAN",
                "description": "cancel: stop every running task",
            },
            "limit": {
                "type": "NUMBER",
                "description": "list: how many tasks to return (default 10)",
            },
        },
        "required": [],
    },
    "handler": background_tasks_action,
}
