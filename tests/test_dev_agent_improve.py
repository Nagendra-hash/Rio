"""Tests for the coding agent's improve-an-existing-folder mode.

`dev_agent` could only build a new project. This is the other half: point it at
a folder that already exists and it reads the project, plans the change, backs
up what it touches, writes, checks that the files fit together and parse, and
registers one undo entry. These tests run the whole flow with a stand-in model,
so nothing reaches the network and nothing outside a temp folder is written.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from actions import dev_agent as da


class _Reply:
    def __init__(self, text: str):
        self.text = text


NEW_APP = 'def add(a, b):\n    return a + b\n\n\ndef greet(name):\n    return f"Hello, {name}"\n'


class _Model:
    """Answers each prompt kind the way the real model would, from a script."""

    def __init__(self, plan: dict, content: str = NEW_APP):
        self.plan = plan
        self.content = content
        self.prompts: list[str] = []

    def generate_content(self, prompt: str):
        self.prompts.append(prompt)
        if "Decide which files to change" in prompt:
            return _Reply(json.dumps(self.plan))
        if "reviewing a code change" in prompt:
            return _Reply('{"problems": []}')
        if "senior engineer joining this project" in prompt:
            return _Reply("## How it works\nuse functions")
        return _Reply(self.content)


@pytest.fixture()
def project(tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    (folder / "app.py").write_text("def add(a, b):\n    return a + b\n",
                                   encoding="utf-8")
    (folder / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    return folder


def _wire(monkeypatch, model):
    monkeypatch.setattr(da, "_get_model", lambda tier=None: model)
    monkeypatch.setattr(da, "_project_brief",
                        lambda folder, context, stack, player=None: "")
    monkeypatch.setattr(da, "_open_vscode", lambda folder: True)
    monkeypatch.setattr(da, "_coding_provider", lambda: "")


# ── the helpers the flow depends on ──────────────────────────────────────────
def test_safe_rel_keeps_paths_inside_the_folder(tmp_path):
    root = tmp_path.resolve()
    assert da._safe_rel(root, "core/tasks/engine.py") == "core/tasks/engine.py"
    assert da._safe_rel(root, "./app.py") == "app.py"
    for bad in ("../outside.py", "~/x.py", "C:/x.py",
                ".venv/lib/x.py", "a/../../b.py", ""):
        assert da._safe_rel(root, bad) is None, bad
    # An absolute path is read as relative to the folder, never used as one:
    # whatever comes back must still land inside the project.
    resolved = da._safe_rel(root, "/etc/passwd")
    assert resolved is None or (root / resolved).resolve().is_relative_to(root)


def test_refuses_whole_personal_folders():
    home = Path.home()
    assert da._refusal(home)
    assert da._refusal(home / "Desktop")
    assert da._refusal(home / "Documents")


def test_truncated_answers_are_never_saved():
    assert da._looks_truncated("x" * 5000, "# rest of the file unchanged")
    assert da._looks_truncated("x" * 5000, "")
    assert not da._looks_truncated("x" * 5000, "y" * 5000)


def test_reasoning_blocks_never_reach_the_file():
    text = "<thinking>hmm</thinking>```python\nprint(1)\n```"
    assert da._strip_fences(text) == "print(1)"


def test_status_report_before_any_job(monkeypatch):
    monkeypatch.setattr(da, "_JOB", {})
    assert "has not worked on a folder" in da._status_report()


def test_tool_declaration_offers_improve_and_status():
    props = da.TOOL["parameters"]["properties"]
    assert "mode" in props and "folder" in props
    assert "improve" in props["mode"]["description"]
    assert "status" in props["mode"]["description"]


# ── the whole flow ───────────────────────────────────────────────────────────
def test_improve_writes_the_change_and_can_be_undone(project, monkeypatch):
    model = _Model({"summary": "Adds a greet function.",
                    "changes": [{"path": "app.py", "action": "edit",
                                 "what": "add greet(name)"}]})
    _wire(monkeypatch, model)

    result = da._improve_folder(project, "add a greet function", None)

    assert "changed 1 file" in result
    assert "greet" in (project / "app.py").read_text()
    backups = list((project / da._BACKUP_DIR).iterdir())
    assert len(backups) == 1
    assert (backups[0] / "app.py").read_text() == "def add(a, b):\n    return a + b\n"

    from core.undo import undo_last
    undo_last()
    assert (project / "app.py").read_text() == "def add(a, b):\n    return a + b\n"


def test_improve_creates_a_new_file_and_undo_removes_it(project, monkeypatch):
    model = _Model({"summary": "Adds a helper module.",
                    "changes": [{"path": "helpers/greet.py", "action": "create",
                                 "what": "greet(name)"}]},
                   content="def greet(name):\n    return f'Hello, {name}'\n")
    _wire(monkeypatch, model)

    da._improve_folder(project, "add a helper", None)
    assert (project / "helpers" / "greet.py").is_file()

    from core.undo import undo_last
    undo_last()
    assert not (project / "helpers" / "greet.py").exists()


def test_improve_refuses_paths_outside_the_folder(project, monkeypatch):
    model = _Model({"summary": "Tries to escape.",
                    "changes": [{"path": "../evil.py", "action": "create",
                                 "what": "nope"}]})
    _wire(monkeypatch, model)

    result = da._improve_folder(project, "write outside", None)
    assert not (project.parent / "evil.py").exists()
    assert "Tries to escape." in result
    assert (project / "app.py").read_text() == "def add(a, b):\n    return a + b\n"


def test_improve_keeps_a_truncated_rewrite(project, monkeypatch):
    original = (project / "app.py").read_text()
    model = _Model({"summary": "Rewrites app.py.",
                    "changes": [{"path": "app.py", "action": "edit",
                                 "what": "add a lot"}]},
                   content="# rest of the file unchanged")
    _wire(monkeypatch, model)

    da._improve_folder(project, "add a lot", None)
    assert (project / "app.py").read_text() == original


def test_improve_reports_a_folder_with_no_sources(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    _wire(monkeypatch, _Model({"summary": "", "changes": []}))
    result = da._improve_folder(empty, "do something", None)
    assert "no readable source files" in result
