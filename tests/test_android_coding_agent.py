"""
tests/test_android_coding_agent.py — offline tests for Android-app creation by
the coding agent (orchestrator/agents) and the DevAgent's Desktop export.

No gradle, no SDK, no network: the Gradle *build* is stubbed, while the
everything-else (intent detection, naming, the deterministic scaffold, the
permission allowlist, the Desktop export, rollback) runs for real on a temp
Desktop (`JARVIS_DESKTOP`).

Proves:
  1. android_support: create-intent detection (and the existing-app guard),
     app-name extraction, the scaffold writes a buildable Gradle skeleton and
     detects the package/activity from the sources, the APK export lands on the
     Desktop.
  2. Permissions: `gradle assembleDebug` is allowed, everything else refused;
     Kotlin/XML/gradle files are writable text formats.
  3. Error understanding: Android scaffold/resource/compile classes + .kt/.java
     traceback parsing.
  4. The full REQUEST→…→REPORT flow for "create an android app": the project is
     created on the Desktop, scaffolded, "built", and the APK saved there; a
     build failure triggers a fix round; rollback removes a fresh project's
     scaffold files.
  5. The app-spec stage ("first write the prompt, then build"): the design
     model (Gemini) is asked for an interactive spec BEFORE planning, the spec
     reaches the coder's plan prompt, and a missing/failed design model is not
     fatal. The voice DevAgent writes the same spec first.
  6. View-id drift: the Kotlin's `R.id.x` is realigned to the layout's
     `@+id/y` when the match is unambiguous, left alone when ambiguous, and the
     DevAgent classifies Android resource/compile failures instead of blaming
     the environment.

Run:  .venv/bin/python -m pytest tests/test_android_coding_agent.py -q
"""
import json
import sys
import zipfile
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from orchestrator.agents import android_support as android
from orchestrator.agents.coding_agent import CodingAgent, rollback_checkpoint, \
    load_checkpoint
from orchestrator.agents.coding_tools import (CodingToolbox, ToolPermissions,
                                              classify_error, parse_traceback)
from orchestrator.contracts import AgentRequest, TaskStatus, TaskType


# ── fixtures / helpers ───────────────────────────────────────────────────────

@pytest.fixture()
def desktop(tmp_path, monkeypatch):
    d = tmp_path / "Desktop"
    d.mkdir()
    monkeypatch.setenv("JARVIS_DESKTOP", str(d))
    return d


@pytest.fixture()
def cdir(tmp_path):
    return tmp_path / "checkpoints"


class ScriptedLLM:
    def __init__(self, answers: list[str]):
        self.answers = list(answers)
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, system: str = ""):
        self.prompts.append(prompt)
        return self.answers.pop(0) if self.answers else None


class SpecScriptedLLM(ScriptedLLM):
    """A scripted backend that ALSO writes the app spec (the design model) —
    the `spec` hook the real RouterLLM exposes. Records the order of calls so
    a test can prove the spec is written BEFORE the app is planned."""

    def __init__(self, answers: list[str], spec="INTERACTIVE-SPEC body"):
        super().__init__(answers)
        self.spec_answer = spec
        self.spec_prompts: list[str] = []
        self.events: list[str] = []

    def complete(self, prompt: str, *, system: str = ""):
        self.events.append("complete")
        return super().complete(prompt, system=system)

    def spec(self, prompt: str, *, system: str = ""):
        self.events.append("spec")
        self.spec_prompts.append(prompt)
        if isinstance(self.spec_answer, Exception):
            raise self.spec_answer
        return self.spec_answer


KOTLIN_MAIN = (
    "package com.example.app\n\n"
    "import android.os.Bundle\n"
    "import androidx.appcompat.app.AppCompatActivity\n\n"
    "class MainActivity : AppCompatActivity() {\n"
    "    override fun onCreate(savedInstanceState: Bundle?) {\n"
    "        super.onCreate(savedInstanceState)\n"
    "        setContentView(R.layout.activity_main)\n"
    "    }\n"
    "}\n"
)
LAYOUT_MAIN = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"\n'
    '    android:layout_width="match_parent"\n'
    '    android:layout_height="match_parent">\n'
    '    <TextView android:text="@string/hello" '
    'android:layout_width="wrap_content" '
    'android:layout_height="wrap_content"/>\n'
    "</LinearLayout>\n"
)


def android_plan(steps=None, deps=None) -> str:
    return json.dumps({
        "analysis": "a simple app",
        "package": "com.example.app",
        "entry_activity": "MainActivity",
        "dependencies": deps or [],
        "steps": steps if steps is not None else [
            {"action": "create",
             "path": "app/src/main/java/com/example/app/MainActivity.kt",
             "content": KOTLIN_MAIN, "why": "entry"},
            {"action": "create",
             "path": "app/src/main/res/layout/activity_main.xml",
             "content": LAYOUT_MAIN, "why": "ui"},
        ],
        "tests_to_run": [],
        "risks": [],
    })


def make_fake_build(fail_rounds: int = 0, fail_output: str = "compile error"):
    """A stubbed CodingToolbox.build_android: fails `fail_rounds` times, then
    produces a real-looking APK (a zip with a >4 KB classes.dex)."""
    calls = {"n": 0}

    def _build(self, timeout=900):
        calls["n"] += 1
        if calls["n"] <= fail_rounds:
            return {"ok": True, "returncode": 1, "stdout": "",
                    "stderr": fail_output, "command": ["gradle", "assembleDebug"],
                    "apk": None, "apk_real": False, "passed": False}
        apk_dir = self.root / "app/build/outputs/apk/debug"
        apk_dir.mkdir(parents=True, exist_ok=True)
        apk = apk_dir / "app-debug.apk"
        with zipfile.ZipFile(apk, "w") as z:
            z.writestr("classes.dex", b"\x00" * 10_000)
        return {"ok": True, "returncode": 0, "stdout": "BUILD SUCCESSFUL",
                "stderr": "", "command": ["gradle", "assembleDebug"],
                "apk": str(apk), "apk_real": True, "passed": True}

    return _build, calls


def android_request(text, **meta) -> AgentRequest:
    return AgentRequest(task_id="task-test-android0001", task_type=TaskType.CODING,
                        text=text, metadata=dict(meta))


# ── 1. android_support ───────────────────────────────────────────────────────

class TestAndroidIntent:
    def test_create_intents(self):
        for t in ("create an android app for notes",
                  "make me an android app to track habits",
                  "build a kotlin android app",
                  "ek android app banao"):
            assert android.is_android_request(t), t

    def test_existing_app_not_new(self):
        for t in ("fix the bug in my android app",
                  "debug the android app crash",
                  "refactor my kotlin app"):
            assert not android.is_android_request(t), t

    def test_non_android_not_new(self):
        assert not android.is_android_request("create a python web scraper")
        assert not android.is_android_request("")

    def test_name_extraction(self):
        assert android.detect_app_name("create an android app called TaskMaster") \
            == "TaskMaster"
        assert android.detect_app_name('make an android app named "Quiet Notes"') \
            == "QuietNotes"
        assert android.detect_app_name("build a todo app") == "TaskApp"
        assert android.detect_app_name("just build something") == "JarvisApp"

    def test_sanitize(self):
        assert android.sanitize_app_name("my_cool-app!") == "MyCoolApp"
        assert android.sanitize_app_name("123") == "App123"
        assert android.sanitize_app_name("") == "JarvisApp"

    def test_unique_workspace_never_clobbers(self, desktop):
        (desktop / "TaskApp").mkdir()
        (desktop / "TaskApp" / "x.txt").write_text("existing")
        second = android.unique_workspace("TaskApp")
        assert second.name == "TaskApp-2"
        assert (desktop / "TaskApp" / "x.txt").read_text() == "existing"


class TestScaffold:
    def test_scaffold_writes_buildable_skeleton(self, tmp_path):
        proj = tmp_path / "SampleApp"
        (proj / "app/src/main/java/com/jarvis/sample").mkdir(parents=True)
        (proj / "app/src/main/java/com/jarvis/sample/MainActivity.kt").write_text(
            "package com.jarvis.sample\n\nclass MainActivity\n")
        (proj / "app/src/main/res/layout").mkdir(parents=True)
        (proj / "app/src/main/res/layout/activity_main.xml").write_text(
            '<LinearLayout android:text="@string/greeting"\n'
            '    android:background="@color/brand"/>\n')
        note = android.scaffold_project(proj, None, app_name="SampleApp")
        assert (proj / "settings.gradle.kts").exists()
        assert (proj / "app/build.gradle.kts").exists()
        assert (proj / "app/src/main/AndroidManifest.xml").exists()
        # package detected from the source tree
        assert 'namespace = "com.jarvis.sample"' in \
            (proj / "app/build.gradle.kts").read_text()
        # missing @string/@color auto-defined
        assert "greeting" in (proj / "app/src/main/res/values/strings.xml").read_text()
        assert "brand" in (proj / "app/src/main/res/values/colors.xml").read_text()
        assert "package" in note.lower()

    def test_scaffold_normalizes_src_main(self, tmp_path):
        proj = tmp_path / "NormApp"
        (proj / "src/main/java/com/x").mkdir(parents=True)
        (proj / "src/main/java/com/x/MainActivity.java").write_text(
            "package com.x;\npublic class MainActivity {}\n")
        android.scaffold_project(proj, None, app_name="NormApp")
        assert (proj / "app/src/main/java/com/x/MainActivity.java").exists()
        assert 'namespace = "com.x"' in (proj / "app/build.gradle.kts").read_text()

    def test_scaffold_declares_extra_activities(self, tmp_path):
        proj = tmp_path / "MultiApp"
        pkgdir = proj / "app/src/main/java/com/jarvis/multi"
        (pkgdir / "ui").mkdir(parents=True)
        (pkgdir / "MainActivity.kt").write_text(
            "package com.jarvis.multi\nclass MainActivity\n")
        (pkgdir / "AddEditActivity.kt").write_text(
            "package com.jarvis.multi\nclass AddEditActivity\n")
        (pkgdir / "ui" / "DetailsActivity.kt").write_text(
            "package com.jarvis.multi.ui\nclass DetailsActivity\n")
        android.scaffold_project(proj, None, app_name="MultiApp")
        mf = (proj / "app/src/main/AndroidManifest.xml").read_text()
        # the launcher is MainActivity — NOT the alphabetically-first Activity
        assert 'android:name=".MainActivity" android:exported="true"' in mf
        # every other activity is declared (fully qualified), or it crashes on tap
        assert ('<activity android:name="com.jarvis.multi.AddEditActivity" '
                'android:exported="false"/>' in mf)
        assert ('<activity android:name="com.jarvis.multi.ui.DetailsActivity" '
                'android:exported="false"/>' in mf)

    def test_scaffold_honours_declared_entry_activity(self, tmp_path):
        proj = tmp_path / "EntryApp"
        pkgdir = proj / "app/src/main/java/com/x"
        pkgdir.mkdir(parents=True)
        (pkgdir / "HomeActivity.kt").write_text("package com.x\nclass HomeActivity\n")
        (pkgdir / "AddActivity.kt").write_text("package com.x\nclass AddActivity\n")
        android.scaffold_project(proj, None, app_name="EntryApp",
                                 entry_activity="HomeActivity")
        mf = (proj / "app/src/main/AndroidManifest.xml").read_text()
        assert 'android:name=".HomeActivity" android:exported="true"' in mf
        assert ('<activity android:name="com.x.AddActivity" '
                'android:exported="false"/>' in mf)

    def test_toolbox_scaffold_passes_entry_activity(self, tmp_path):
        proj = tmp_path / "TbApp"
        pkgdir = proj / "app/src/main/java/com/x"
        pkgdir.mkdir(parents=True)
        (pkgdir / "HomeActivity.kt").write_text("package com.x\nclass HomeActivity\n")
        (pkgdir / "AddActivity.kt").write_text("package com.x\nclass AddActivity\n")
        tb = CodingToolbox(proj)
        res = tb.android_scaffold(None, "TbApp", "HomeActivity")
        assert res["ok"]
        mf = (proj / "app/src/main/AndroidManifest.xml").read_text()
        assert 'android:name=".HomeActivity" android:exported="true"' in mf


class TestExport:
    def test_export_copies_apk_to_desktop(self, desktop, tmp_path):
        proj = desktop / "SampleApp"
        proj.mkdir()
        apk = proj / "app/build/outputs/apk/debug/app-debug.apk"
        apk.parent.mkdir(parents=True)
        apk.write_bytes(b"fake-apk-bytes")
        out = android.export_to_desktop(proj, apk, "SampleApp")
        assert out["copied"]
        assert Path(out["apk_in_project"]) == proj / "SampleApp.apk"
        assert Path(out["apk_on_desktop"]) == desktop / "SampleApp.apk"
        assert (desktop / "SampleApp.apk").read_bytes() == b"fake-apk-bytes"

    def test_export_reports_missing_apk(self, desktop):
        out = android.export_to_desktop(desktop / "Nope", None, "Nope")
        assert not out["copied"] and out["error"]


# ── 2. permissions ───────────────────────────────────────────────────────────

class TestAndroidPermissions:
    @pytest.mark.parametrize("argv", [
        ["gradle", "assembleDebug", "--no-daemon", "-q"],
        ["gradle", "assembleRelease"],
        ["gradle", "clean"],
        ["/home/u/.gradle-dist/gradle-8.7/bin/gradle", "assembleDebug"],
        ["/home/u/Desktop/App/gradlew", "assembleDebug"],
    ])
    def test_gradle_build_allowed(self, tmp_path, argv):
        assert ToolPermissions(tmp_path).check_command(argv)[0]

    @pytest.mark.parametrize("argv", [
        ["gradle", "publish"],
        ["gradle", "installDebug", "-Pevil=1"],
        ["gradle"],                       # no task
    ])
    def test_gradle_else_refused(self, tmp_path, argv):
        assert not ToolPermissions(tmp_path).check_command(argv)[0]

    def test_android_text_formats_writable(self, tmp_path):
        perms = ToolPermissions(tmp_path)
        for name in ("app/src/main/java/com/x/MainActivity.kt",
                     "app/src/main/res/layout/activity_main.xml",
                     "build.gradle.kts", "settings.gradle",
                     "gradle.properties"):
            ok, why = perms.can_write(name)
            assert ok, (name, why)

    def test_binary_still_unwritable(self, tmp_path):
        assert not ToolPermissions(tmp_path).can_write("app-release.apk")[0]


# ── 3. error understanding ───────────────────────────────────────────────────

class TestAndroidErrors:
    def test_scaffold_error(self):
        assert classify_error("error: package R does not exist") == "scaffold"

    def test_resource_error(self):
        assert classify_error("Android resource linking failed") == "resource"

    def test_compile_error(self):
        assert classify_error("e: file:///x/MainActivity.kt:19:5 "
                              "Unresolved reference: name") == "compile"
        assert classify_error("/x/MainActivity.java:30: error: cannot find "
                              "symbol") == "compile"

    def test_environment_error(self):
        assert classify_error("./gradlew: not found") == "environment"

    def test_python_classes_unchanged(self):
        assert classify_error("Traceback (most recent call last):\n"
                              "NameError: x") == "runtime"
        assert classify_error("SyntaxError: invalid syntax") == "syntax"

    def test_parse_traceback_kotlin(self):
        out = ("e: file:///home/u/Desktop/App/app/src/main/java/com/x/"
               "MainActivity.kt:19:56 Unresolved reference: foo")
        files = ["app/src/main/java/com/x/MainActivity.kt"]
        assert parse_traceback(out, files) == (files[0], 19)

    def test_parse_traceback_java(self):
        out = "/home/u/App/app/src/main/java/com/x/MainActivity.java:30: " \
              "error: cannot find symbol"
        files = ["app/src/main/java/com/x/MainActivity.java"]
        assert parse_traceback(out, files) == (files[0], 30)


# ── 4. full workflow ─────────────────────────────────────────────────────────

class TestAndroidWorkflow:
    def test_create_android_app_end_to_end(self, desktop, cdir, monkeypatch):
        build, calls = make_fake_build()
        monkeypatch.setattr(CodingToolbox, "build_android", build)
        ag = CodingAgent(llm=ScriptedLLM([android_plan()]), checkpoint_dir_=cdir)
        res = ag.handle(android_request(
            "create an android app called SampleApp for tracking tasks",
            new_android_app=True, app_name="SampleApp"))
        assert res.status is TaskStatus.COMPLETED, res.text
        proj = desktop / "SampleApp"
        # sources the model wrote
        assert (proj / "app/src/main/java/com/example/app/MainActivity.kt").exists()
        # deterministic scaffold
        assert (proj / "settings.gradle.kts").exists()
        assert (proj / "app/build.gradle.kts").exists()
        # APK saved on the Desktop (project copy + desktop root copy)
        assert (proj / "SampleApp.apk").exists()
        assert (desktop / "SampleApp.apk").exists()
        assert res.data["android"] is True
        assert res.data["app_name"] == "SampleApp"
        assert "Desktop" in res.text
        assert calls["n"] == 1
        cp = load_checkpoint("task-test-android0001", cdir)
        assert cp and cp["new_project"]["app_name"] == "SampleApp"

    def test_build_failure_then_fix(self, desktop, cdir, monkeypatch):
        build, calls = make_fake_build(
            fail_rounds=1,
            fail_output="e: file:///x/MainActivity.kt:3:1 Unresolved reference: x")
        monkeypatch.setattr(CodingToolbox, "build_android", build)
        fix = android_plan(steps=[{"action": "edit",
                                   "path": "app/src/main/java/com/example/app/MainActivity.kt",
                                   "old": "class MainActivity : AppCompatActivity()",
                                   "new": "class MainActivity : AppCompatActivity()  // fixed"}])
        ag = CodingAgent(llm=ScriptedLLM([android_plan(), fix]),
                         checkpoint_dir_=cdir)
        res = ag.handle(android_request(
            "create an android app called SampleApp", new_android_app=True,
            app_name="SampleApp"))
        assert res.status is TaskStatus.COMPLETED, res.text
        assert calls["n"] == 2                 # failed once, rebuilt
        assert (desktop / "SampleApp.apk").exists()

    def test_build_failure_is_failed_not_faked(self, desktop, cdir, monkeypatch):
        build, _ = make_fake_build(fail_rounds=99,
                                   fail_output="error: unresolved reference")
        monkeypatch.setattr(CodingToolbox, "build_android", build)
        ag = CodingAgent(llm=ScriptedLLM([android_plan()]), checkpoint_dir_=cdir)
        res = ag.handle(android_request("create an android app",
                                        new_android_app=True, app_name="BrokenApp"))
        assert res.status is TaskStatus.FAILED
        assert not (desktop / "BrokenApp.apk").exists()
        # the project is still on the Desktop for the user to inspect
        assert (desktop / "BrokenApp").is_dir()

    def test_scaffold_error_triggers_rescaffold(self, desktop, cdir, monkeypatch):
        build, calls = make_fake_build(
            fail_rounds=1, fail_output="error: package R does not exist")
        monkeypatch.setattr(CodingToolbox, "build_android", build)
        ag = CodingAgent(llm=ScriptedLLM([android_plan()]), checkpoint_dir_=cdir)
        res = ag.handle(android_request("create an android app for notes",
                                        new_android_app=True, app_name="NoteApp"))
        assert res.status is TaskStatus.COMPLETED, res.text
        assert calls["n"] == 2                 # re-scaffold + rebuild, no LLM fix

    def test_rollback_removes_fresh_project_files(self, desktop, cdir, monkeypatch):
        build, _ = make_fake_build()
        monkeypatch.setattr(CodingToolbox, "build_android", build)
        ag = CodingAgent(llm=ScriptedLLM([android_plan()]), checkpoint_dir_=cdir)
        res = ag.handle(android_request("create an android app",
                                        new_android_app=True, app_name="RollApp"))
        assert res.status is TaskStatus.COMPLETED
        proj = desktop / "RollApp"
        assert (proj / "settings.gradle.kts").exists()
        msg = rollback_checkpoint("task-test-android0001", cdir)
        assert "Rolled back" in msg
        # scaffold files created fresh are removed by rollback
        assert not (proj / "settings.gradle.kts").exists()
        assert not (proj / "app/src/main/java/com/example/app/MainActivity.kt").exists()

    def test_no_plan_fails_honestly(self, desktop, cdir):
        ag = CodingAgent(llm=ScriptedLLM([]), checkpoint_dir_=cdir)
        res = ag.handle(android_request("create an android app",
                                        new_android_app=True, app_name="X"))
        assert res.status is TaskStatus.FAILED
        assert "plan" in res.text.lower()

    def test_plain_request_still_needs_input(self, desktop, cdir):
        ag = CodingAgent(llm=ScriptedLLM([]), checkpoint_dir_=cdir)
        res = ag.handle(AgentRequest(task_id="task-test-plain000001",
                                     task_type=TaskType.CODING,
                                     text="fix my code please"))
        assert res.status is TaskStatus.NEEDS_INPUT


# ── 5. the app spec: "first write the prompt, then build" ────────────────────

class TestAndroidSpecStage:
    def test_spec_prompt_asks_for_interaction(self):
        p = android.spec_prompt("make a todo app", "TodoApp")
        assert "make a todo app" in p and "TodoApp" in p
        # the design brief itself demands an interactive app
        assert "INTERACTIVE" in p.upper()
        assert "static" in p.lower()          # explicitly forbids a dead screen

    def test_plan_prompt_demands_interactive_and_has_spec_slot(self):
        from orchestrator.agents.coding_agent import ANDROID_PLAN_PROMPT
        assert "{spec}" in ANDROID_PLAN_PROMPT
        assert "INTERACTIVE" in ANDROID_PLAN_PROMPT.upper()

    def test_spec_written_first_then_planned_from_it(self, desktop, cdir,
                                                     monkeypatch):
        build, _ = make_fake_build()
        monkeypatch.setattr(CodingToolbox, "build_android", build)
        llm = SpecScriptedLLM([android_plan()],
                              spec="INTERACTIVE-SPEC: two screens, add button")
        ag = CodingAgent(llm=llm, checkpoint_dir_=cdir)
        res = ag.handle(android_request(
            "create an android app called SpecApp",
            new_android_app=True, app_name="SpecApp"))
        assert res.status is TaskStatus.COMPLETED, res.text
        # the design model was asked first, before any planning/building
        assert llm.events[0] == "spec"
        assert "INTERACTIVE" in llm.spec_prompts[0].upper()
        # the spec text reached the coder's plan prompt
        assert any("INTERACTIVE-SPEC" in p for p in llm.prompts)
        assert res.data["spec"] is True
        assert (desktop / "SpecApp.apk").exists()

    def test_spec_model_unavailable_is_not_fatal(self, desktop, cdir,
                                                 monkeypatch):
        build, _ = make_fake_build()
        monkeypatch.setattr(CodingToolbox, "build_android", build)
        llm = SpecScriptedLLM([android_plan()],
                              spec=RuntimeError("gemini down"))
        ag = CodingAgent(llm=llm, checkpoint_dir_=cdir)
        res = ag.handle(android_request("create an android app",
                                        new_android_app=True,
                                        app_name="NoSpecApp"))
        assert res.status is TaskStatus.COMPLETED, res.text
        assert res.data["spec"] is False

    def test_backend_without_spec_hook_plans_as_before(self, desktop, cdir,
                                                       monkeypatch):
        build, _ = make_fake_build()
        monkeypatch.setattr(CodingToolbox, "build_android", build)
        ag = CodingAgent(llm=ScriptedLLM([android_plan()]), checkpoint_dir_=cdir)
        res = ag.handle(android_request("create an android app",
                                        new_android_app=True, app_name="PlainApp"))
        assert res.status is TaskStatus.COMPLETED, res.text
        assert res.data["spec"] is False


# ── 6. the voice DevAgent writes the same spec first ─────────────────────────

class TestDevAgentSpec:
    def _fake_model(self, text):
        class _Resp:
            def __init__(self, t):
                self.text = t

        class _Model:
            def __init__(self):
                self.prompts = []

            def generate_content(self, prompt):
                self.prompts.append(prompt)
                return _Resp(text)

        return _Model()

    def test_design_spec_uses_the_model(self, monkeypatch):
        import actions.dev_agent as dev
        m = self._fake_model("SPEC: interactive todo with an add button")
        monkeypatch.setattr(dev, "_get_model", lambda *a, **k: m)
        out = dev._design_android_spec("make a todo app")
        assert "interactive" in out.lower()
        assert "make a todo app" in m.prompts[0]
        assert "static" in m.prompts[0].lower()

    def test_design_spec_failure_is_empty_not_fatal(self, monkeypatch):
        import actions.dev_agent as dev

        class _Model:
            def generate_content(self, prompt):
                raise RuntimeError("llm down")

        monkeypatch.setattr(dev, "_get_model", lambda *a, **k: _Model())
        assert dev._design_android_spec("anything") == ""

    def test_plan_prompt_gains_android_rules(self, monkeypatch):
        import json as _json
        import actions.dev_agent as dev
        plan = {"project_name": "x", "entry_point": "MainActivity.kt",
                "files": [], "run_command": "gradlew assembleDebug",
                "dependencies": []}
        seen = {}
        m = self._fake_model(_json.dumps(plan))
        monkeypatch.setattr(dev, "_get_model", lambda *a, **k: m)

        dev._plan_project("desc", "android")
        assert "Android-specific rules" in m.prompts[0]
        assert "Do NOT plan Gradle files" in m.prompts[0]

        dev._plan_project("desc", "python")
        assert "Android-specific rules" not in m.prompts[1]

    def test_build_project_writes_spec_before_planning(self, monkeypatch,
                                                       tmp_path):
        import actions.dev_agent as dev
        seen = {}

        def fake_spec(desc):
            seen["spec_input"] = desc
            return "SPEC-BODY: an interactive app"

        def fake_plan(desc, language):
            seen["plan_desc"] = desc
            return {"project_name": "SpecApp", "entry_point": "MainActivity.kt",
                    "files": [{"path": "MainActivity.kt",
                               "description": "entry", "imports": []}],
                    "run_command": "gradlew assembleDebug", "dependencies": []}

        monkeypatch.setattr(dev, "_desktop_dir", lambda: tmp_path)
        monkeypatch.setattr(dev, "_design_android_spec", fake_spec)
        monkeypatch.setattr(dev, "_plan_project", fake_plan)
        monkeypatch.setattr(dev, "_write_file", lambda **k: "// code")
        monkeypatch.setattr(dev, "_scaffold_android_project",
                            lambda *a, **k: "scaffold note")
        monkeypatch.setattr(dev, "_android_run_command",
                            lambda *a, **k: "gradle assembleDebug")
        monkeypatch.setattr(dev, "_install_dependencies", lambda *a, **k: "ok")
        monkeypatch.setattr(dev, "_open_vscode", lambda *a, **k: True)
        monkeypatch.setattr(dev, "_run_project",
                            lambda *a, **k: "BUILD SUCCESSFUL")
        monkeypatch.setattr(dev, "_find_apk",
                            lambda *a, **k: str(tmp_path / "SpecApp" / "a.apk"))
        monkeypatch.setattr(dev, "_android_apk_real", lambda *a, **k: True)
        monkeypatch.setattr(dev, "_export_apk_to_desktop",
                            lambda *a, **k: [str(tmp_path / "SpecApp.apk")])

        out = dev._build_project("make a todo app", "android", "SpecApp", 30)
        assert seen["spec_input"] == "make a todo app"      # spec from raw ask
        assert seen["plan_desc"] == "SPEC-BODY: an interactive app"
        assert "APK" in out


class TestDevAgentScaffold:
    def test_manifest_declares_extra_activities(self, tmp_path, monkeypatch):
        import actions.dev_agent as dev
        proj = tmp_path / "VaApp"
        pkgdir = proj / "app/src/main/java/com/x"
        pkgdir.mkdir(parents=True)
        (pkgdir / "MainActivity.kt").write_text("package com.x\nclass MainActivity\n")
        (pkgdir / "AddEditActivity.kt").write_text(
            "package com.x\nclass AddEditActivity\n")
        monkeypatch.setattr(dev, "_android_sdk", lambda: None)
        dev._scaffold_android_project(proj, [], "MainActivity")
        mf = (proj / "app/src/main/AndroidManifest.xml").read_text()
        assert 'android:name=".MainActivity" android:exported="true"' in mf
        assert ('<activity android:name="com.x.AddEditActivity" '
                'android:exported="false"/>' in mf)

    def test_manifest_entry_activity_wins(self, tmp_path, monkeypatch):
        import actions.dev_agent as dev
        proj = tmp_path / "VaApp2"
        pkgdir = proj / "app/src/main/java/com/x"
        pkgdir.mkdir(parents=True)
        (pkgdir / "HomeActivity.kt").write_text("package com.x\nclass HomeActivity\n")
        (pkgdir / "AddActivity.kt").write_text("package com.x\nclass AddActivity\n")
        monkeypatch.setattr(dev, "_android_sdk", lambda: None)
        dev._scaffold_android_project(proj, [], "HomeActivity")
        mf = (proj / "app/src/main/AndroidManifest.xml").read_text()
        assert 'android:name=".HomeActivity" android:exported="true"' in mf
        assert ('<activity android:name="com.x.AddActivity" '
                'android:exported="false"/>' in mf)


# ── 7. the hollow-shell bug: an APK that installs but crashes on open ────────

class TestHollowShellIsCaught:
    def _apk(self, path, dex: bytes):
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("classes.dex", dex)
        return path

    def test_bytes_alone_is_fooled_but_class_check_is_not(self, tmp_path):
        apk = self._apk(tmp_path / "shell.apk", b"\x00" * 10_000)
        assert android.apk_looks_real(apk) is True            # size alone: fooled
        assert android.apk_looks_real(
            apk, "com.example.app.MainActivity") is False     # the real test
        assert android.apk_has_class(apk, "com.example.app.MainActivity") is False

    def test_apk_with_the_launcher_is_accepted(self, tmp_path):
        apk = self._apk(tmp_path / "real.apk",
                        b"\x00" * 10_000 + b"Lcom/example/app/MainActivity;")
        assert android.apk_looks_real(apk, "com.example.app.MainActivity") is True
        assert android.apk_has_class(apk, "com.example.app.MainActivity") is True
        assert android.apk_has_class(apk, "com.example.app.Other") is False

    def test_launcher_activity_reads_the_manifest(self, tmp_path):
        proj = tmp_path / "LApp"
        (proj / "app/src/main/java/com/example/app").mkdir(parents=True)
        (proj / "app/src/main/java/com/example/app/MainActivity.kt").write_text(
            "package com.example.app\nclass MainActivity\n")
        (proj / "app/src/main").mkdir(parents=True, exist_ok=True)
        (proj / "app/src/main/AndroidManifest.xml").write_text(
            '<manifest xmlns:android="http://schemas.android.com/apk/res/android">'
            '<application>'
            '<activity android:name=".MainActivity" android:exported="true">'
            '<intent-filter>'
            '<category android:name="android.intent.category.LAUNCHER"/>'
            '</intent-filter></activity>'
            '</application></manifest>')
        assert android.launcher_activity(proj) == "com.example.app.MainActivity"

    def test_build_android_flags_a_shell(self, tmp_path, monkeypatch):
        proj = tmp_path / "ShellApp"
        (proj / "app/src/main/java/com/example/app").mkdir(parents=True)
        (proj / "app/src/main/java/com/example/app/MainActivity.kt").write_text(
            "package com.example.app\nclass MainActivity\n")
        (proj / "app/src/main/AndroidManifest.xml").parent.mkdir(
            parents=True, exist_ok=True)
        (proj / "app/src/main/AndroidManifest.xml").write_text(
            '<manifest xmlns:android="http://schemas.android.com/apk/res/android">'
            '<application>'
            '<activity android:name=".MainActivity" android:exported="true">'
            '<intent-filter>'
            '<category android:name="android.intent.category.LAUNCHER"/>'
            '</intent-filter></activity>'
            '</application></manifest>')
        apk_dir = proj / "app/build/outputs/apk/debug"
        apk_dir.mkdir(parents=True)
        with zipfile.ZipFile(apk_dir / "app-debug.apk", "w") as z:
            z.writestr("classes.dex", b"\x00" * 10_000)        # libraries only
        monkeypatch.setattr(android, "build_argv",
                            lambda root: ["gradle", "assembleDebug"])
        tb = CodingToolbox(proj)
        monkeypatch.setattr(tb, "run", lambda argv, timeout: {
            "ok": True, "returncode": 0, "stdout": "BUILD SUCCESSFUL",
            "stderr": ""})
        res = tb.build_android()
        assert res["returncode"] == 0              # gradle said success...
        assert res["apk_real"] is False            # ...but it is a shell
        assert res["passed"] is False
        assert "launcher class" in res["stderr"]
        assert classify_error(res["stderr"]) == "scaffold"


class TestRelocateStraySources:
    def test_moves_and_unifies_packages(self, tmp_path):
        proj = tmp_path / "StrayApp"
        (proj / "ui").mkdir(parents=True)
        (proj / "data").mkdir(parents=True)
        (proj / "ui/MainActivity.kt").write_text(
            "package com.example.mine.ui\n\n"
            "import com.example.mine.R\n\n"
            "class MainActivity { val f = ActiveTasksFragment() }\n")
        (proj / "ui/ActiveTasksFragment.kt").write_text(
            "package com.example.other.ui\n\n"
            "import com.example.other.data.Task\n\n"
            "import androidx.fragment.app.Fragment\n\n"
            "class ActiveTasksFragment : Fragment() { val t: Task? = null }\n")
        (proj / "ui/TaskAdapter.kt").write_text(
            "package com.example.other.ui\n\n"
            "import com.example.third.databinding.ItemTaskBinding\n\n"
            "class TaskAdapter\n")
        (proj / "data/Task.kt").write_text("package data\n\nclass Task\n")

        note = android.relocate_stray_sources(proj)
        assert "relocated 4" in note
        pkg_dir = proj / "app/src/main/java/com/example/mine/ui"
        for name in ("MainActivity.kt", "ActiveTasksFragment.kt",
                     "TaskAdapter.kt", "Task.kt"):
            assert (pkg_dir / name).exists(), name
        # the stray tree is emptied (they were moved, not copied)
        assert not list((proj / "ui").glob("*.kt"))
        assert not list((proj / "data").glob("*.kt"))

        for f in pkg_dir.glob("*.kt"):
            txt = f.read_text()
            assert "package com.example.mine.ui" in txt        # ONE package
            assert "com.example.other" not in txt
            assert "package data" not in txt
        # the R import and the cross-file import both point at the new package
        main = (pkg_dir / "MainActivity.kt").read_text()
        assert "import com.example.mine.ui.R" in main
        frag = (pkg_dir / "ActiveTasksFragment.kt").read_text()
        assert "import com.example.mine.ui.Task" in frag
        assert "import androidx.fragment.app.Fragment" in frag   # library kept
        adapter = (pkg_dir / "TaskAdapter.kt").read_text()
        assert ("import com.example.mine.ui.databinding.ItemTaskBinding"
                in adapter)

    def test_noop_when_module_already_has_sources(self, tmp_path):
        proj = tmp_path / "OkApp"
        (proj / "app/src/main/java/com/x").mkdir(parents=True)
        (proj / "app/src/main/java/com/x/MainActivity.kt").write_text(
            "package com.x\nclass MainActivity\n")
        (proj / "stray.kt").write_text("package y\nclass Stray\n")
        assert android.relocate_stray_sources(proj) == ""
        assert (proj / "stray.kt").exists()      # untouched

    def test_scaffold_relocates_and_builds_manifest_for_it(self, tmp_path):
        proj = tmp_path / "ScaffoldStray"
        (proj / "ui").mkdir(parents=True)
        (proj / "ui/MainActivity.kt").write_text(
            "package com.example.mine.ui\nclass MainActivity\n")
        (proj / "ui/AddEditActivity.kt").write_text(
            "package com.example.other\nclass AddEditActivity\n")
        android.scaffold_project(proj, None, app_name="ScaffoldStray")
        gb = (proj / "app/build.gradle.kts").read_text()
        assert 'namespace = "com.example.mine.ui"' in gb
        mf = (proj / "app/src/main/AndroidManifest.xml").read_text()
        assert 'android:name=".MainActivity" android:exported="true"' in mf
        assert ("com.example.mine.ui.AddEditActivity" in mf)


# ── 8. view-id drift: Kotlin `R.id.x` vs the layout's `@+id/y` ───────────────

def _id_project(tmp_path):
    proj = tmp_path / "IdApp"
    (proj / "app/src/main/java/com/x").mkdir(parents=True)
    (proj / "app/src/main/res/layout").mkdir(parents=True)
    return proj


LAYOUT_ONE_BUTTON = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android">'
    '<ImageButton android:id="@+id/btnSettings" '
    'android:layout_width="48dp" android:layout_height="48dp"/>'
    "</LinearLayout>")


class TestReconcileViewIds:
    def test_unique_typed_match_is_repointed(self, tmp_path):
        proj = _id_project(tmp_path)
        (proj / "app/src/main/res/layout/activity_clock.xml").write_text(
            LAYOUT_ONE_BUTTON)
        kt = proj / "app/src/main/java/com/x/ClockActivity.kt"
        kt.write_text(
            "package com.x\n"
            "import android.widget.ImageButton\n"
            "class ClockActivity {\n"
            "    val b: ImageButton = findViewById(R.id.image_button_settings)\n"
            "}\n")
        note = android.reconcile_view_ids(proj)
        assert "image_button_settings→btnSettings" in note
        text = kt.read_text()
        assert "R.id.btnSettings" in text
        assert "image_button_settings" not in text

    def test_framework_id_is_left_alone(self, tmp_path):
        proj = _id_project(tmp_path)
        (proj / "app/src/main/res/layout/a.xml").write_text(
            '<LinearLayout xmlns:android='
            '"http://schemas.android.com/apk/res/android"/>')
        kt = proj / "app/src/main/java/com/x/A.kt"
        kt.write_text("package com.x\n"
                      "val x = view.findViewById(android.R.id.text1)\n")
        assert android.reconcile_view_ids(proj) == ""
        assert "android.R.id.text1" in kt.read_text()

    def test_ambiguous_same_type_is_not_guessed(self, tmp_path):
        proj = _id_project(tmp_path)
        (proj / "app/src/main/res/layout/a.xml").write_text(
            '<LinearLayout xmlns:android='
            '"http://schemas.android.com/apk/res/android">'
            '<TextView android:id="@+id/one"/>'
            '<TextView android:id="@+id/two"/>'
            "</LinearLayout>")
        kt = proj / "app/src/main/java/com/x/A.kt"
        src = ("package com.x\nimport android.widget.TextView\n"
               "val t: TextView = findViewById(R.id.optionTitle)\n")
        kt.write_text(src)
        assert android.reconcile_view_ids(proj) == ""
        assert kt.read_text() == src

    def test_already_consistent_is_a_noop(self, tmp_path):
        proj = _id_project(tmp_path)
        (proj / "app/src/main/res/layout/a.xml").write_text(
            LAYOUT_ONE_BUTTON)
        kt = proj / "app/src/main/java/com/x/A.kt"
        src = ("package com.x\nimport android.widget.ImageButton\n"
               "val b: ImageButton = findViewById(R.id.btnSettings)\n")
        kt.write_text(src)
        assert android.reconcile_view_ids(proj) == ""
        assert kt.read_text() == src

    def test_scaffold_reports_the_repair(self, tmp_path, monkeypatch):
        proj = _id_project(tmp_path)
        (proj / "app/src/main/res/layout/activity_clock.xml").write_text(
            LAYOUT_ONE_BUTTON)
        (proj / "app/src/main/java/com/x/ClockActivity.kt").write_text(
            "package com.x\nimport android.widget.ImageButton\n"
            "class ClockActivity {\n"
            "  val b: ImageButton = findViewById(R.id.image_button_settings)\n"
            "}\n")
        monkeypatch.setattr(android, "sdk_path", lambda: None)
        monkeypatch.setattr(android, "gradle_bin", lambda: None)
        note = android.scaffold_project(proj, None, app_name="IdApp")
        assert "view-id reference(s)" in note


# ── 9. the DevAgent stops blaming the environment for Android code ───────────

class TestDevAgentErrorClasses:
    def test_resource_error_is_not_environment(self):
        import actions.dev_agent as dev
        out = ("FAILURE: Build failed with an exception.\n"
               "* What went wrong:\n"
               "Execution failed for task ':app:processDebugResources'.\n"
               "> A failure occurred while executing "
               "LinkApplicationAndroidResourcesTask")
        assert dev._classify_error(out) == "resource_error"

    def test_aapt_layout_error_is_a_resource_error(self):
        import actions.dev_agent as dev
        assert dev._classify_error(
            "app/src/main/res/layout/a.xml:12: error: attribute not found") == \
            "resource_error"

    def test_unresolved_reference_is_compile(self):
        import actions.dev_agent as dev
        assert dev._classify_error(
            "e: file:///x/A.kt:18:61 Unresolved reference: image_but") == \
            "compile_error"

    def test_missing_gradlew_is_not_a_success(self):
        import actions.dev_agent as dev
        assert dev._classify_error("./gradlew: not found") == "environment_error"
        assert dev._has_error("./gradlew: not found", "gradlew assembleDebug")

    def test_error_signature_ignores_symbol_and_position(self):
        import actions.dev_agent as dev
        a = dev._error_signature(
            "e: file:///x/A.kt:18:61 Unresolved reference: foo")
        b = dev._error_signature(
            "e: file:///x/A.kt:12:5 Unresolved reference: bar")
        assert a == b

    def test_error_excerpt_keeps_the_failure_block(self):
        import actions.dev_agent as dev
        ex = dev._error_excerpt(
            "STDERR:\nFAILURE: Build failed\n* What went wrong:\nboom")
        assert ex.startswith("FAILURE:")
        assert "A failure occurred" in dev._error_excerpt(
            "FAILURE:\n> A failure occurred while executing X")

    def test_aapt_merge_path_targets_the_layout(self):
        """AAPT2's '...app-mergeDebugResources-31:/layout/x.xml:17: error' form
        must resolve to the layout, so the fixer edits the layout — not the
        activity, which is what it did for five rounds on 2026-10-17."""
        import actions.dev_agent as dev
        out = ("Execution failed for task ':app:processDebugResources'.\n"
               "> Android resource linking failed\n"
               "  com.example.watchface.app-mergeDebugResources-31:/layout/"
               "activity_customization.xml:17: error: attribute "
               "layout_top_toTopOf (aka com.example.watchface:"
               "layout_top_toTopOf) not found.")
        files = ["app/src/main/res/layout/activity_customization.xml",
                 "app/src/main/java/com/example/watchface/MainActivity.kt"]
        assert dev._parse_traceback(out, files) == (files[0], 17)

    def test_absolute_res_path_also_targets_the_layout(self):
        import actions.dev_agent as dev
        out = "app/src/main/res/layout/a.xml:12: error: attribute x not found"
        files = ["app/src/main/res/layout/a.xml"]
        assert dev._parse_traceback(out, files) == (files[0], 12)

    def test_unique_workspace_never_reuses_a_live_project(self, tmp_path,
                                                          monkeypatch):
        import actions.dev_agent as dev
        monkeypatch.setattr(dev, "_desktop_dir", lambda: tmp_path)
        first = dev._unique_workspace("AnalogWatchApp")
        assert first == tmp_path / "AnalogWatchApp"
        first.mkdir()
        (first / "settings.gradle.kts").write_text("// project")   # non-empty
        second = dev._unique_workspace("AnalogWatchApp")
        assert second == tmp_path / "AnalogWatchApp-2"
        assert not second.exists()


# ── 10. the app package is the source root, not the anchor activity ──────────

class TestAppPackageDetection:
    def _watch_project(self, tmp_path):
        proj = tmp_path / "WatchApp"
        root = proj / "app/src/main/java/com/example/watchface"
        (root / "ui").mkdir(parents=True)
        (root / "WatchFaceService.kt").write_text(
            "package com.example.watchface\nclass WatchFaceService\n")
        (root / "ui/SettingsActivity.kt").write_text(
            "package com.example.watchface.ui\n"
            "import com.example.watchface.R\nclass SettingsActivity\n")
        return proj

    def test_subpackage_activity_does_not_set_the_namespace(self, tmp_path):
        proj = self._watch_project(tmp_path)
        pkg, act = android.detect_package(proj)
        assert pkg == "com.example.watchface"
        assert act == "SettingsActivity"

    def test_launcher_in_a_subpackage_is_fully_qualified(self, tmp_path):
        proj = self._watch_project(tmp_path)
        assert android.launcher_name(proj, "com.example.watchface",
                                     "SettingsActivity") == \
            "com.example.watchface.ui.SettingsActivity"

    def test_launcher_in_the_app_package_keeps_the_dot(self, tmp_path):
        proj = tmp_path / "Plain"
        root = proj / "app/src/main/java/com/example/app"
        root.mkdir(parents=True)
        (root / "MainActivity.kt").write_text(
            "package com.example.app\nclass MainActivity\n")
        pkg, act = android.detect_package(proj)
        assert (pkg, act) == ("com.example.app", "MainActivity")
        assert android.launcher_name(proj, pkg, act) == ".MainActivity"


class TestDevAgentNamespace:
    def test_scaffold_uses_the_source_root_and_qualified_launcher(
            self, tmp_path, monkeypatch):
        import actions.dev_agent as dev
        proj = tmp_path / "SubApp"
        root = proj / "app/src/main/java/com/example/watchface"
        (root / "ui").mkdir(parents=True)
        (root / "WatchFaceService.kt").write_text(
            "package com.example.watchface\nclass WatchFaceService\n")
        (root / "ui/SettingsActivity.kt").write_text(
            "package com.example.watchface.ui\n"
            "import com.example.watchface.R\nclass SettingsActivity\n")
        monkeypatch.setattr(dev, "_android_sdk", lambda: None)
        dev._scaffold_android_project(proj, [], "WatchFaceService")
        gb = (proj / "app/build.gradle.kts").read_text()
        assert 'namespace = "com.example.watchface"\n' in gb
        mf = (proj / "app/src/main/AndroidManifest.xml").read_text()
        assert ('android:name="com.example.watchface.ui.SettingsActivity" '
                'android:exported="true"') in mf


# ── 11. the DevAgent model path (NVIDIA DeepSeek, Gemini fallback) ───────────

class TestDevAgentModelRouting:
    def test_router_skipped_without_a_cloud_coding_key(self, tmp_path, monkeypatch):
        import actions.dev_agent as dev
        cfg = tmp_path / "api_keys.json"
        cfg.write_text('{"gemini_api_key": "AIzaTest"}')
        monkeypatch.setattr(dev, "API_CONFIG_PATH", cfg)
        # No OpenRouter / NVIDIA key → no router probe latency, straight to Gemini.
        assert dev._nvidia_configured() is False
        assert dev._openrouter_configured() is False
        assert dev._router_coding_text("write something") == ""

    def test_get_model_falls_back_to_gemini(self, monkeypatch):
        import actions.dev_agent as dev
        monkeypatch.setattr(dev, "_nvidia_configured", lambda: False)
        monkeypatch.setattr(dev, "_openrouter_configured", lambda: False)

        class _Resp:
            text = "gemini answer"

        seen = {}

        def fake_call(contents, tier=None, timeout_ms=None):
            seen["hit"] = contents
            return _Resp()

        monkeypatch.setattr(dev.gemini, "call", fake_call)
        out = dev._get_model().generate_content("hi")
        assert out.text == "gemini answer" and seen["hit"] == "hi"

    def test_openrouter_configured_reads_config(self, tmp_path, monkeypatch):
        import actions.dev_agent as dev
        cfg = tmp_path / "api_keys.json"
        cfg.write_text('{"openrouter_keys": ["sk-or-x"]}')
        monkeypatch.setattr(dev, "API_CONFIG_PATH", cfg)
        assert dev._openrouter_configured() is True
        cfg.write_text('{"gemini_api_key": "AIzaTest"}')
        assert dev._openrouter_configured() is False

    def test_openrouter_key_alone_prefers_the_router(self, monkeypatch):
        """With OpenRouter keys but no NVIDIA key, the DeepSeek rung still
        serves coding — Gemini is not touched when the router answers."""
        import actions.dev_agent as dev
        monkeypatch.setattr(dev, "_openrouter_configured", lambda: True)
        monkeypatch.setattr(dev, "_nvidia_configured", lambda: False)
        monkeypatch.setattr(dev, "_router_coding_text",
                            lambda contents: "deepseek answer")
        monkeypatch.setattr(dev.gemini, "call",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("Gemini must not be called")))
        assert dev._get_model().generate_content("hi").text == "deepseek answer"

    def test_router_answer_is_preferred_when_available(self, monkeypatch):
        import actions.dev_agent as dev
        monkeypatch.setattr(dev, "_nvidia_configured", lambda: True)
        monkeypatch.setattr(dev, "_router_coding_text",
                            lambda contents: "deepseek/nemotron answer")
        monkeypatch.setattr(dev.gemini, "call",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("Gemini must not be called")))
        assert dev._get_model().generate_content("hi").text == \
            "deepseek/nemotron answer"
