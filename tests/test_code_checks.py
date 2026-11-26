"""Tests for core/code_checks.py — what the coding agent knows before it writes.

These are the parts that must be right for the improve-a-folder flow to be safe:
which files are off limits, which stack a folder is, and whether an edit
silently threw away settings. Nothing here runs a project or calls a model.
"""

from __future__ import annotations

import json

from core import code_checks as cc


def _project(tmp_path):
    (tmp_path / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8")
    return tmp_path


def test_detects_python_project_and_its_test_command(tmp_path):
    stack = cc.detect_stack(_project(tmp_path))
    assert stack["languages"][0] == "Python"
    assert stack["test_cmd"] and "pytest" in stack["test_cmd"]
    assert "Python" in cc.describe_stack(stack)


def test_secrets_are_never_read_or_changed(tmp_path):
    protection = cc.Protection(tmp_path)
    for rel in ("config/api_keys.json", ".env", "secrets/token.txt"):
        assert protection.reason(rel), rel
    assert protection.reason("app.py") == ""


def test_gitignored_files_are_off_limits(tmp_path):
    (tmp_path / ".gitignore").write_text("data/\n*.log\n", encoding="utf-8")
    protection = cc.Protection(tmp_path)
    assert protection.reason("data/rows.csv")
    assert protection.reason("run.log")
    assert protection.reason(".gitignore") == ""
    assert protection.reason("app.py") == ""


def test_dropped_data_names_what_an_edit_removed():
    old = json.dumps({"voice": "Aoede", "theme": {"hue": 12, "bright": 1}})
    new = json.dumps({"voice": "Aoede", "theme": {"hue": 12}})
    assert "theme.bright" in cc.dropped_data("config.json", old, new)
    assert cc.dropped_data("config.json", old, old) == ""


def test_dropped_data_ignores_files_it_cannot_parse():
    assert cc.dropped_data("app.py", "a = 1", "a = 2") == ""


def test_fingerprint_changes_when_the_project_does(tmp_path):
    project = _project(tmp_path)
    before = cc.fingerprint(project)
    (project / "app.py").write_text("print('changed')\n", encoding="utf-8")
    assert cc.fingerprint(project) != before


def test_smoke_harness_sits_next_to_the_checks():
    """dev_agent's checks start it as a separate process; a missing file would
    only show up as a silent 'testing unavailable' at runtime."""
    assert cc._HARNESS.is_file()
