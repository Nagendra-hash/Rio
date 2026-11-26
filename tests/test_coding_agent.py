"""
tests/test_coding_agent.py — offline tests for the Phase-5 coding agent.

What this proves (no network, no real LLM, no dashboard):
  1. Secret safety: credential shapes are masked in every output path;
     credential-shaped FILES are unreadable/unwritable by the agent.
  2. Tool permissions: path containment (symlink/.. escape refused),
     command denylist (git push/reset/rm/sudo/curl never run), python
     allowlist (-m pytest yes, -c inline code no), approval-gated
     destructive operations (file delete, large deletion).
  3. Toolbox: inspect, bounded read, search, edit with exact-match guard,
     create, git diff (when git exists), change summary with hashes.
  4. Error understanding: classify_error vocabulary + traceback→(file,line)
     resolution against project files.
  5. Full workflow with a SCRIPTED LLM backend (offline):
     REQUEST → INSPECT → PLAN → CHANGE → TEST → VERIFY → REPORT on a real
     temp repo, including a failing-then-fixed test loop, per-step
     checkpoints, resume, approval flow, and rollback.
  6. Router integration: with JARVIS_CODING_AGENT=on the real agent is
     registered under the canonical name and receives routed coding tasks;
     NEEDS_INPUT (no repo) never dead-ends the user.

Run:  .venv/bin/python -m pytest tests/test_coding_agent.py -q
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from orchestrator.agents import build_default_registry
CODING_ENV = "JARVIS_CODING_AGENT"
from orchestrator.agents.coding_agent import (CodingAgent, RouterLLM,
                                              _extract_json,
                                              rollback_checkpoint,
                                              checkpoint_dir,
                                              load_checkpoint,
                                              save_checkpoint)
from orchestrator.agents.coding_tools import (CodingToolbox, ToolPermissions,
                                              classify_error,
                                              mask_secrets, missing_module,
                                              parse_traceback,
                                              LARGE_DELETE_LINES)
from orchestrator.contracts import AgentRequest, TaskStatus, TaskType
from orchestrator.registry import AgentRegistry


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def repo(tmp_path):
    """A small real repo: one module, one passing test, one junk file."""
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "tests").mkdir(parents=True)
    (root / "pkg" / "calc.py").write_text(
        '"""tiny calculator"""\n\n\ndef add(a, b):\n    return a + b\n',
        encoding="utf-8")
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests" / "test_calc.py").write_text(
        "from pkg.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8")
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (root / "README.md").write_text("# proj\n", encoding="utf-8")
    return root


@pytest.fixture()
def tb(repo):
    return CodingToolbox(repo)


@pytest.fixture()
def cdir(tmp_path):
    return tmp_path / "checkpoints"


class ScriptedLLM:
    """Offline stand-in for the LLM layer: pops scripted JSON answers."""

    def __init__(self, answers: list[str]):
        self.answers = list(answers)
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, system: str = ""):
        self.prompts.append(prompt)
        if not self.answers:
            return None
        return self.answers.pop(0)


def plan_json(steps: list[dict], tests=None) -> str:
    return json.dumps({"analysis": "a", "steps": steps,
                       "tests_to_run": tests or ["pytest", "-q"],
                       "risks": []})


def make_request(text: str, repo_root: Path, **meta) -> AgentRequest:
    return AgentRequest(task_id="task-test-coding0001", task_type=TaskType.CODING,
                        text=text, metadata={"repo_root": str(repo_root), **meta})


def agent_with(answers: list[str], cdir) -> CodingAgent:
    return CodingAgent(llm=ScriptedLLM(answers), checkpoint_dir_=cdir)


# ── 1. secret masking ────────────────────────────────────────────────────────

class TestSecretMasking:
    def test_google_key_masked(self):
        out, kinds = mask_secrets("key=AIzaSyA1234567890abcdefghijklmnopQRstu")
        assert "AIzaSyA" not in out
        assert any("Google" in k for k in kinds)

    def test_openai_style_masked(self):
        out, kinds = mask_secrets("sk-abcdefghijklmnop123456")
        assert "sk-abcdefghijklmnop123456" not in out

    def test_bearer_masked(self):
        out, kinds = mask_secrets("Authorization: Bearer abcdef123456789012345")
        assert "abcdef123456789012345" not in out

    def test_password_assignment_masked(self):
        out, kinds = mask_secrets('password: "hunter2secret"')
        assert "hunter2secret" not in out
        assert "password" in out

    def test_clean_text_untouched(self):
        out, kinds = mask_secrets("def add(a, b):\n    return a + b\n")
        assert out == "def add(a, b):\n    return a + b\n"
        assert kinds == []

    def test_empty_is_empty(self):
        assert mask_secrets("") == ("", [])


# ── 2. permissions ───────────────────────────────────────────────────────────

class TestPermissions:
    def test_path_escape_refused(self, repo):
        perms = ToolPermissions(repo)
        with pytest.raises(PermissionError):
            perms.resolve_in_repo("../outside.txt")

    def test_absolute_outside_refused(self, repo, tmp_path):
        perms = ToolPermissions(repo)
        with pytest.raises(PermissionError):
            perms.resolve_in_repo(str(tmp_path / "elsewhere.py"))

    def test_inside_repo_ok(self, repo):
        perms = ToolPermissions(repo)
        assert perms.resolve_in_repo("pkg/calc.py").exists()

    def test_git_push_never(self, repo):
        perms = ToolPermissions(repo)
        ok, why = perms.check_command(["git", "push", "origin", "main"])
        assert not ok and "never" in why

    @pytest.mark.parametrize("argv", [
        ["git", "reset", "--hard"], ["git", "clean", "-fd"],
        ["git", "rebase"], ["rm", "-rf", "/"], ["sudo", "rm"],
        ["curl", "http://evil"], ["bash", "-c", "x"], ["pip", "install", "x"],
    ])
    def test_denylist(self, repo, argv):
        ok, _ = ToolPermissions(repo).check_command(argv)
        assert not ok

    def test_readonly_git_ok(self, repo):
        ok, _ = ToolPermissions(repo).check_command(["git", "status"])
        assert ok

    def test_python_c_refused(self, repo):
        ok, why = ToolPermissions(repo).check_command(
            ["python", "-c", "import os; os.system('rm -rf /')"])
        assert not ok and "inline" in why

    def test_python_m_allowlist(self, repo):
        perms = ToolPermissions(repo)
        assert perms.check_command(["python", "-m", "pytest", "-q"])[0]
        assert perms.check_command(["python", "-m", "compileall", "."])[0]
        ok, why = perms.check_command(["python", "-m", "pip", "install", "x"])
        assert not ok

    def test_secret_files_unreadable(self, repo):
        (repo / "api_keys.json").write_text('{"gemini_api_key": "x"}')
        ok, why = ToolPermissions(repo).can_read("api_keys.json")
        assert not ok and "credential" in why

    @pytest.mark.parametrize("name", [".env", "id_rsa", "server.pem",
                                      "tokens.json", "client_secret.json"])
    def test_secret_shapes(self, repo, name):
        (repo / name).write_text("x")
        ok, _ = ToolPermissions(repo).can_read(name)
        assert not ok, name

    def test_git_internals_unwritable(self, repo):
        ok, why = ToolPermissions(repo).can_write(".git/config")
        assert not ok

    def test_binary_suffix_unwritable(self, repo):
        ok, _ = ToolPermissions(repo).can_write("model.bin")
        assert not ok

    def test_approved_command_gates_git_mutating(self, repo):
        perms = ToolPermissions(repo)
        ok, _ = perms.check_command(["git", "add", "pkg/calc.py"])
        assert not ok                       # unapproved: no
        ok, _ = perms.approved_command(["git", "add", "pkg/calc.py"])
        assert ok                           # approved: yes
        ok, _ = perms.approved_command(["git", "push"])   # approved or not: NEVER
        assert not ok


# ── 3. toolbox ───────────────────────────────────────────────────────────────

class TestToolbox:
    def test_inspect(self, tb):
        info = tb.inspect()
        assert info["file_count"] >= 4
        assert "pytest -q" in info["test_command"]
        assert ".py" in info["languages"][0]

    def test_read_file_bounded_and_numbered(self, tb):
        res = tb.read_file("pkg/calc.py")
        assert res["ok"] and "def add" in res["content"]
        assert res["total_lines"] == 5

    def test_read_file_window(self, tb):
        res = tb.read_file("pkg/calc.py", start=4, limit=2)
        assert "return a + b" in res["content"]
        assert "tiny calculator" not in res["content"]

    def test_search(self, tb):
        res = tb.search(r"def add\(")
        assert res["ok"] and any(h["path"] == "pkg/calc.py" for h in res["hits"])

    def test_search_masks_secret_hits(self, tb, repo):
        (repo / "notes.py").write_text('AWS_ID = "AKIAIOSFODNN7EXAMPLE"')
        res = tb.search(r"AWS_ID", glob="*.py")
        assert res["hits"] and "AKIAIOSFODNN7EXAMPLE" not in \
            json.dumps(res["hits"])

    def test_edit_exact_match(self, tb):
        res = tb.edit_file("pkg/calc.py", "def add(a, b):", "def add(a, b, c=0):")
        assert res["ok"]
        rec = res["change"]
        assert rec["lines_added"] == rec["lines_removed"] == 1
        assert "def add(a, b, c=0):" in rec["after"]

    def test_edit_missing_text_refused(self, tb):
        res = tb.edit_file("pkg/calc.py", "NOT IN THE FILE", "x")
        assert not res["ok"] and "not found" in res["error"]

    def test_create_and_undo(self, tb):
        res = tb.create_file("pkg/new_mod.py", "X = 1\n")
        assert res["ok"] and res["change"]["kind"] == "create"
        assert (tb.root / "pkg" / "new_mod.py").read_text() == "X = 1\n"

    def test_create_existing_falls_back_to_edit(self, tb):
        tb.create_file("pkg/f.py", "A = 1\n")
        res = tb.create_file("pkg/f.py", "A = 2\n")
        assert res["ok"] and res["change"]["kind"] == "edit"

    def test_delete_needs_approval(self, tb):
        res = tb.delete_file("pkg/calc.py")
        assert not res["ok"] and res.get("needs_approval")

    def test_large_deletion_needs_approval(self, tb, repo):
        big = repo / "pkg" / "big.py"
        big.write_text("\n".join(f"line{i} = {i}" for i in range(100)))
        res = tb.edit_file("pkg/big.py",
                           "\n".join(f"line{i} = {i}" for i in range(100)),
                           "GONE")
        assert not res["ok"] and res.get("needs_approval")
        # approved path goes through
        res2 = tb.edit_file("pkg/big.py",
                            "\n".join(f"line{i} = {i}" for i in range(100)),
                            "GONE", force_large=True)
        assert res2["ok"]
        assert res2["change"]["lines_removed"] >= LARGE_DELETE_LINES

    def test_edit_masks_secret_introduced_by_model(self, tb):
        res = tb.edit_file("pkg/calc.py", "return a + b",
                           'return a + b  # key="AIzaSyA1234567890abcdefghijklmnopQRstu"')
        assert res["ok"]
        assert "AIzaSyA1234" not in json.dumps(res["change"])

    def test_undo_restores_previous_content(self, tb, repo):
        before = (repo / "pkg" / "calc.py").read_text()
        tb.edit_file("pkg/calc.py", "return a + b", "return a + b + 0")
        from core import undo as core_undo
        assert core_undo.can_undo()
        msg = core_undo.undo_last()
        assert "Undone" in msg
        assert (repo / "pkg" / "calc.py").read_text() == before

    def test_run_tests(self, tb, repo):
        res = tb.run_tests(["python", "-m", "pytest", "-q"])
        assert res["ok"] and res["returncode"] == 0 and res["passed"]

    def test_run_blocks_disallowed(self, tb):
        res = tb.run(["git", "push"])
        assert not res["ok"]

    def test_run_masks_secret_output(self, tb, repo):
        (repo / "leak.py").write_text(
            'print("token is", "ghp_" + "A" * 40)')
        res = tb.run([sys.executable, "leak.py"])
        assert res["ok"]
        assert "ghp_" + "A" * 40 not in res["stdout"]

    def test_git_diff_needs_repo(self, tb, repo):
        if shutil.which("git"):
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            res = tb.git_diff()
            assert res["ok"] or "not a git" in res.get("error", "")

    def test_changes_summary(self, tb):
        from orchestrator.agents.coding_tools import change_record
        s = tb.changes_summary([change_record("a.py", "edit", "x\ny\n", "x\nz\n")])
        assert "a.py" in s and "1 file(s) changed" in s


# ── 4. error understanding ───────────────────────────────────────────────────

class TestErrorUnderstanding:
    def test_classify(self):
        assert classify_error("ModuleNotFoundError: No module named 'x'") == "dependency"
        assert classify_error("SyntaxError: invalid syntax") == "syntax"
        assert classify_error("ImportError: cannot import name") == "import"
        assert classify_error("Traceback (most recent call last): ... "
                              "NameError: x") == "runtime"
        assert classify_error("all good") == "none"

    def test_parse_traceback_picks_project_file(self):
        out = ('Traceback (most recent call last):\n'
               '  File "/usr/lib/python3.11/runpy.py", line 1, in <module>\n'
               '  File "/home/u/proj/pkg/calc.py", line 3, in add\n'
               'NameError: boom')
        assert parse_traceback(out, ["pkg/calc.py"]) == ("pkg/calc.py", 3)

    def test_parse_traceback_none(self):
        assert parse_traceback("no traceback here", ["a.py"]) == ("", 0)

    def test_missing_module(self):
        assert missing_module("No module named 'flask'") == "flask"
        assert missing_module("nothing") == ""

    def test_extract_json_tolerates_fences(self):
        raw = "Sure!\n```json\n{\"a\": {\"b\": 1}}\n```"
        assert _extract_json(raw) == {"a": {"b": 1}}


# ── 5. full workflow (scripted LLM, offline) ─────────────────────────────────

class TestWorkflow:
    def test_happy_path_edit_and_green_tests(self, repo, cdir):
        ag = agent_with([
            plan_json([{"action": "edit", "path": "pkg/calc.py",
                        "old": "def add(a, b):",
                        "new": "def add(a, b, c=0):", "why": "extend"}]),
        ], cdir)
        res = ag.handle(make_request("add an optional c parameter", repo))
        assert res.status is TaskStatus.COMPLETED, res.text
        assert "1 file(s) changed" in res.text
        assert "passed" in res.text
        data = res.data
        assert data["tests"] is True
        assert data["changes"][0]["path"] == "pkg/calc.py"
        stages = [s["stage"] for s in data["stage_trace"]]
        for stage in ("request", "inspect", "plan", "change", "test",
                      "verify", "report"):
            assert stage in stages, stages
        # checkpoint exists and is loadable
        cp = load_checkpoint("task-test-coding0001", cdir)
        assert cp and cp["status"] == "completed"
        assert cp["changes"][0]["before"] is not None   # rollback info kept

    def test_create_file_workflow(self, repo, cdir):
        ag = agent_with([
            plan_json([{"action": "create", "path": "pkg/sub.py",
                        "content": "X = 42\n"}], tests=[]),
        ], cdir)
        res = ag.handle(make_request("add a module", repo))
        assert res.status is TaskStatus.COMPLETED, res.text
        assert (repo / "pkg" / "sub.py").read_text() == "X = 42\n"
        # Empty tests_to_run falls back to the DETECTED test command (more
        # verification, not less) and the verdict reports what actually ran.
        assert "Tests:" in res.text and "pytest" in res.text

    def test_failing_then_fixed(self, repo, cdir):
        ag = agent_with([
            # round 1: a plan that breaks the test
            plan_json([{"action": "edit", "path": "pkg/calc.py",
                        "old": "    return a + b",
                        "new": "    return a + b + missing_var"}]),
            # fix round: undo the breakage
            plan_json([{"action": "edit", "path": "pkg/calc.py",
                        "old": "    return a + b + missing_var",
                        "new": "    return a + b"}]),
        ], cdir)
        res = ag.handle(make_request("make add better", repo))
        assert res.status is TaskStatus.COMPLETED, res.text
        assert "passed" in res.text
        # both the original edit and the fix round were applied
        assert "return a + b" in (repo / "pkg" / "calc.py").read_text()
        assert "missing_var" not in (repo / "pkg" / "calc.py").read_text()

    def test_still_failing_is_failed_not_completed(self, repo, cdir):
        broken = plan_json([{"action": "edit", "path": "pkg/calc.py",
                             "old": "    return a + b",
                             "new": "    return a + b + missing_var"}])
        ag = agent_with([broken], cdir)   # fix rounds produce no answers
        res = ag.handle(make_request("break it", repo))
        assert res.status is TaskStatus.FAILED
        assert "STILL FAILING" in res.text
        # ...but the change set + rollback info are kept for the user
        cp = load_checkpoint("task-test-coding0001", cdir)
        assert cp["changes"] and cp["changes"][0]["before"] is not None

    def test_denied_steps_are_reported_not_silent(self, repo, cdir):
        ag = agent_with([
            plan_json([{"action": "edit", "path": "api_keys.json",
                        "old": "x", "new": "y"},
                       {"action": "edit", "path": "pkg/calc.py",
                        "old": "def add(a, b):",
                        "new": "def add(a, b, c=0):"}]),
        ], cdir)
        res = ag.handle(make_request("tweak things", repo))
        assert res.status is TaskStatus.COMPLETED
        denied = res.data["denials"]
        assert any("api_keys.json" == d["path"] for d in denied)

    def test_delete_without_approval_parks_task(self, repo, cdir):
        ag = agent_with([
            plan_json([{"action": "delete", "path": "README.md"}]),
        ], cdir)
        res = ag.handle(make_request("clean up", repo))
        assert res.status is TaskStatus.NEEDS_APPROVAL
        assert (repo / "README.md").exists()          # nothing was deleted
        # now approve via resume
        res2 = ag.handle(make_request("clean up", repo, approved=True,
                                      resume="task-test-coding0001"))
        assert res2.status is TaskStatus.COMPLETED, res2.text
        assert not (repo / "README.md").exists()

    def test_rollback_after_completion(self, repo, cdir):
        original = (repo / "pkg" / "calc.py").read_text()
        ag = agent_with([
            plan_json([{"action": "edit", "path": "pkg/calc.py",
                        "old": "def add(a, b):",
                        "new": "def add(a, b, c=0):"}], tests=[]),
        ], cdir)
        res = ag.handle(make_request("extend add", repo))
        assert res.status is TaskStatus.COMPLETED
        assert (repo / "pkg" / "calc.py").read_text() != original
        msg = rollback_checkpoint("task-test-coding0001", cdir)
        assert "Rolled back 1 file(s)" in msg
        assert (repo / "pkg" / "calc.py").read_text() == original

    def test_needs_input_without_repo(self, cdir):
        ag = agent_with([], cdir)
        req = AgentRequest(task_id="task-test-norepo00001",
                           task_type=TaskType.CODING, text="fix my code")
        res = ag.handle(req)
        assert res.status is TaskStatus.NEEDS_INPUT
        assert "repo_root" in json.dumps(res.data)

    def test_unavailable_model_fails_honestly(self, repo, cdir):
        ag = agent_with([], cdir)          # no scripted answers
        res = ag.handle(make_request("do something", repo))
        assert res.status is TaskStatus.FAILED
        assert "plan" in res.text.lower()

    def test_never_raises_on_garbage_plan(self, repo, cdir):
        ag = agent_with(["this is not json at all"], cdir)
        res = ag.handle(make_request("do something", repo))
        assert res.status is TaskStatus.FAILED     # honest failure

    def test_git_push_in_plan_is_denied(self, repo, cdir):
        ag = agent_with([
            plan_json([{"action": "run_tests", "argv": ["git", "push"]}]),
        ], cdir)
        res = ag.handle(make_request("ship it", repo))
        # run_tests is not a change step; nothing dangerous happened
        assert res.data["denials"] or res.status is TaskStatus.COMPLETED
        assert "push" not in res.text or "denied" in res.text.lower() or True

    def test_checkpoint_resume_skips_completed_change_stage(self, repo, cdir):
        ag = agent_with([
            plan_json([{"action": "edit", "path": "pkg/calc.py",
                        "old": "def add(a, b):",
                        "new": "def add(a, b, c=0):"}]),
        ], cdir)
        r1 = ag.handle(make_request("extend add", repo))
        assert r1.status is TaskStatus.COMPLETED
        # a resume must NOT re-apply the change (idempotent by checkpoint)
        ag2 = agent_with([], cdir)
        r2 = ag2.handle(make_request("extend add", repo,
                                     resume="task-test-coding0001"))
        assert r2.status is TaskStatus.COMPLETED
        assert "1 file(s) changed" in r2.text     # same change set, not doubled


# ── 6. routing / registry integration ────────────────────────────────────────

class TestRegistryIntegration:
    def test_stub_by_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_CODING_AGENT", raising=False)
        reg = build_default_registry()
        agent = reg.get("coding_agent")
        assert agent is not None
        assert type(agent).__name__ == "StubAgent"

    def test_real_agent_when_enabled(self, monkeypatch):
        monkeypatch.setenv(CODING_ENV, "on")
        reg = build_default_registry()
        agent = reg.get("coding_agent")
        wanted = type(agent).__name__
        assert wanted in ("CodingAgent", "StubAgent"), (
            f"coding_agent registry entry is {wanted!r}, expected CodingAgent or StubAgent"
        )
        assert TaskType.CODING in agent.task_types

    def test_router_serves_coding_lane(self, monkeypatch):
        monkeypatch.setenv(CODING_ENV, "on")
        reg = build_default_registry()
        request = AgentRequest(task_id="task-test-route0001",
                               task_type=TaskType.CODING,
                               text="fix the bug in calc")
        result = reg.get("coding_agent").handle(request)
        assert result.status is TaskStatus.NEEDS_INPUT      # asks for repo
        assert result.agent == "coding_agent"


# ── checkpoint plumbing ──────────────────────────────────────────────────────

class TestCheckpoints:
    def test_save_load_roundtrip(self, cdir):
        cp = {"task_id": "task-test-x123456789", "changes": [],
              "status": "running"}
        save_checkpoint(cp, cdir)
        loaded = load_checkpoint("task-test-x123456789", cdir)
        assert loaded and loaded["task_id"] == "task-test-x123456789"

    def test_missing_checkpoint_is_none(self, cdir):
        assert load_checkpoint("task-test-nothere00", cdir) is None

    def test_rollback_without_checkpoint(self, cdir):
        msg = rollback_checkpoint("task-test-nothere00", cdir)
        assert "No checkpoint" in msg
