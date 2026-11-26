# Background tasks (the TASKS panel)

Long work that outlives one turn of the conversation: deep research, a cited
report, a multi-section draft, working through a list, preparing a file. The
task runs next to the voice session while the user keeps talking, checkpoints
itself to disk after every model round, and ends in a spoken headline plus a
written report.

## Why the panel used to stay empty

`gui/window.py` refreshes the TASKS panel every two seconds from
`core/tasks/desktop.engine().list(12)`. The panel, the engine, the store and the
agents were all working — but nothing in the conversation could *create* a task.
Only a scheduled routine (`core/routines/desktop.py::start_task`) ever called
`engine.start(...)`, so unless a routine fired the list was empty and the panel
correctly said "No background tasks yet."

`core/tasks/desktop.py` had always referred to "the background_tasks tool"; that
tool did not exist. `actions/background_tasks.py` is it.

## Using it

One tool, `background_tasks`, with these actions:

| action   | what it does |
|----------|--------------|
| `start`  | create a task (the default when `task` is passed) |
| `list`   | running tasks first, then recent ones |
| `status` | one task's budget, progress line and last 20 events |
| `steer`  | add a note that the task reads at its next round |
| `cancel` | stop a task; by default it first writes up what it already found |

Parameters for `start`: `task` (the full instruction — what the finished result
must contain), `title`, `agent`, `minutes`, `rounds`, `deliver`.

- `agent` picks a profile from `agents/*.md`:
  - `general` — drafting, organising, lists and files. 15 min / 40 rounds.
  - `researcher` — plan, read sources in full, quote and check, cited report.
    10 min / 300 rounds.
  - An unknown name falls back to `general` rather than failing.
- `deliver`: `voice` (speak the headline when done), `panel` (show it only),
  `silent` (neither).

The engine announces completion itself. The model is told never to poll — it
says one sentence that the task is running and carries on.

## Where things live

- `memory/tasks/<id>.json` — the task document the panel reads (kept apart from
  the conversations so listing never parses a transcript full of web pages).
- `memory/tasks/<id>[.<key>].turns.json` — the model conversation checkpoints.
- `reports/` — the written report a finished task leaves behind.

## Tests

`tests/test_background_tasks.py` drives the tool against a throwaway
`TaskEngine(autostart=False)` — no scheduler thread, no model calls — and checks
that a started task really lands in the store, is listed, can be steered, and
can be cancelled.
